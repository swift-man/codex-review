import asyncio
import contextlib
import logging
import ssl
import time
import weakref
from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any

import certifi
import httpx
import jwt

from codex_review.domain import (
    FOLLOWUP_MARKER,
    Finding,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
    ReviewThread,
)

from .diff_parser import parse_right_lines

logger = logging.getLogger(__name__)


def _default_tls_context() -> ssl.SSLContext:
    """macOS · python.org 빌드 Python 은 시스템 CA 번들을 자동으로 신뢰하지 않아
    https 호출 시 CERTIFICATE_VERIFY_FAILED 가 뜬다. certifi 번들을 명시한다.
    """
    return ssl.create_default_context(cafile=certifi.where())


# 리뷰 본문 footer 포맷. 모델명은 `CODEX_MODEL` 값을 그대로 표시한다.
_MODEL_FOOTER_TEMPLATE = "\n\n---\n<sub>리뷰 모델: <code>{label}</code></sub>"


def _with_model_footer(body: str, model_label: str | None) -> str:
    if not model_label:
        return body
    return body + _MODEL_FOOTER_TEMPLATE.format(label=model_label)


class _LockRegistry:
    """installation_id → `asyncio.Lock` — WeakValueDictionary 기반.

    이전 LRU 방식은 잠긴 락까지 `popitem` 으로 evict 할 수 있어 같은 iid 의 다음 요청이
    새 락을 발급받으면 상호 배제가 깨지는 문제가 있었다. WeakValueDictionary 는 누군가
    강한 참조(예: `async with lock`)를 쥔 동안은 GC 되지 않고, 사용자가 없어지면 자동
    수거된다 — 활성 락의 배타성 + 메모리 누수 방지 둘 다 달성.
    """

    def __init__(self) -> None:
        self._locks: "weakref.WeakValueDictionary[int, asyncio.Lock]" = (
            weakref.WeakValueDictionary()
        )

    def get(self, installation_id: int) -> asyncio.Lock:
        # asyncio 는 싱글스레드라 get ↔ assignment 사이 선점이 없어 atomic.
        lock = self._locks.get(installation_id)
        if lock is None:
            lock = asyncio.Lock()
            self._locks[installation_id] = lock
        return lock


@dataclass(frozen=True)
class _CachedToken:
    token: str
    expires_at: float

    def is_valid(self) -> bool:
        return time.time() < self.expires_at - 60


class GitHubAppClient:
    """Async GitHub REST client authenticating as a GitHub App installation.

    httpx.AsyncClient 를 공유하며 수명 주기는 외부에서 관리한다(lifespan). 테스트에서는
    `transport=httpx.MockTransport(...)` 를 주입해 네트워크 없이 검증.
    """

    def __init__(
        self,
        app_id: int,
        private_key_pem: str,
        http_client: httpx.AsyncClient,
        dry_run: bool = False,
        review_model_label: str | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key_pem
        self._http = http_client
        self._dry_run = dry_run
        # 본문 footer 에 표시할 모델 라벨. None 이면 footer 생략.
        self._review_model_label = review_model_label
        self._token_cache: dict[int, _CachedToken] = {}
        # installation_id 별 개별 락. 단일 전역 락은 서로 다른 installation 의 동시 재발급까지
        # 직렬화해 병목을 만든다. LRU 상한이 있는 레지스트리를 써 무한히 쌓이지 않게 한다.
        self._token_locks = _LockRegistry()

    # --- Auth ---------------------------------------------------------------

    def _app_jwt(self) -> str:
        # iat 를 30초 과거로 당기고 exp 를 10분 한도(GitHub 제한)에 못 미치는 9분으로 잡는 건
        # 로컬-GitHub 간 시계 오차로 인한 "JWT not yet valid / expired" 실패를 피하기 위함.
        now = int(time.time())
        payload = {"iat": now - 30, "exp": now + 9 * 60, "iss": str(self._app_id)}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    async def get_installation_token(self, installation_id: int) -> str:
        cached = self._token_cache.get(installation_id)
        if cached and cached.is_valid():
            return cached.token

        async with self._token_locks.get(installation_id):
            # lock 진입 후 재확인: 대기 중 같은 installation 의 다른 워커가 이미 갱신했을 수 있다.
            cached = self._token_cache.get(installation_id)
            if cached and cached.is_valid():
                return cached.token

            data = await self._request(
                "POST",
                f"/app/installations/{installation_id}/access_tokens",
                auth=f"Bearer {self._app_jwt()}",
            )
            token = str(data["token"])
            expires = str(data.get("expires_at", ""))
            # GitHub installation token 은 1시간 유효. 만료 직전 요청이 실패하지 않도록 5분 여유.
            expires_at = time.time() + 55 * 60
            if expires:
                # Python 3.11+ 의 `datetime.fromisoformat` 은 "Z" 접미사를 UTC 로 네이티브 지원.
                # `time.strptime/mktime` 은 naive 로 파싱해 로컬 TZ 로 해석하므로 오프셋만큼
                # 어긋난다(예: KST 에서 9시간 일찍 만료 판정). 포맷 불일치는 의도적 무시 —
                # 5분 여유가 있는 기본값을 그대로 사용.
                with contextlib.suppress(ValueError):
                    expires_at = datetime.fromisoformat(expires).timestamp()
            self._token_cache[installation_id] = _CachedToken(token, expires_at)
            return token

    # --- Public API ---------------------------------------------------------

    async def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest:
        token = await self.get_installation_token(installation_id)
        pr_path = f"/repos/{repo.full_name}/pulls/{number}"

        # PR 메타와 첫 페이지 files 는 상호 독립적이라 병렬로 조회해 네트워크 대기 단축.
        # TaskGroup 은 형제 태스크 중 하나가 실패하면 나머지를 자동 취소한다.
        # 이후 페이지는 Link 헤더(`rel=next`) 로 순차 순회.
        async with asyncio.TaskGroup() as tg:
            meta_task = tg.create_task(
                self._request("GET", pr_path, auth=f"token {token}")
            )
            first_files_task = tg.create_task(
                self._get_page_with_next(
                    f"{pr_path}/files?per_page=100", auth=f"token {token}"
                )
            )
        pr_data = meta_task.result()
        first_page, next_url = first_files_task.result()
        assert isinstance(pr_data, dict)

        # per_page=100 은 GitHub 허용 최대치라 PR 이 큰 경우의 라운드트립 수를 최소화.
        changed: list[str] = []
        diff_right_lines: dict[str, frozenset[int]] = {}
        diff_patches: dict[str, str] = {}
        files: Any = first_page
        while True:
            if not isinstance(files, list) or not files:
                break
            for f in files:
                path = str(f["filename"])
                changed.append(path)
                # GitHub 는 큰 diff / rename / delete / binary 상태에서 `patch` 키를 생략한다.
                # 그 파일에 대한 인라인 코멘트는 use-case 필터에서 전부 사라지므로 운영자가
                # 알아볼 수 있도록 경고 로그로 남긴다. diff-only fallback 모드에서는
                # 이 파일을 통째로 리뷰에서 제외하고 본문 배지에 명시한다.
                patch = f.get("patch")
                if patch is None:
                    logger.warning(
                        "GitHub omitted patch for %s#%d file %r (status=%s); "
                        "inline comments on this file will be suppressed",
                        repo.full_name,
                        number,
                        path,
                        f.get("status"),
                    )
                else:
                    diff_patches[path] = str(patch)
                diff_right_lines[path] = parse_right_lines(patch)
            if not next_url:
                break
            files, next_url = await self._get_page_with_next(
                next_url, auth=f"token {token}"
            )

        head = pr_data["head"]
        base = pr_data["base"]
        return PullRequest(
            repo=repo,
            number=number,
            title=str(pr_data.get("title", "")),
            body=str(pr_data.get("body") or ""),
            head_sha=str(head["sha"]),
            head_ref=str(head["ref"]),
            base_sha=str(base["sha"]),
            base_ref=str(base["ref"]),
            clone_url=str(head["repo"]["clone_url"]),
            changed_files=tuple(changed),
            installation_id=installation_id,
            is_draft=bool(pr_data.get("draft", False)),
            diff_right_lines=diff_right_lines,
            diff_patches=diff_patches,
        )

    async def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        if self._dry_run:
            logger.info("DRY_RUN — review not posted: %s#%d", pr.repo.full_name, pr.number)
            return

        token = await self.get_installation_token(pr.installation_id)
        path = f"/repos/{pr.repo.full_name}/pulls/{pr.number}/reviews"

        # commit_id 를 명시해야 리뷰가 "이 head SHA 시점"에 고정된다. 생략하면 최신 SHA 기준으로
        # 붙어 라인 번호 오정렬이 발생할 수 있다.
        payload: dict[str, object] = {
            "commit_id": pr.head_sha,
            "body": _with_model_footer(result.render_body(), self._review_model_label),
            "event": result.event.value,
            "comments": [_finding_to_comment(f) for f in result.findings],
        }
        try:
            await self._request("POST", path, auth=f"token {token}", body=payload)
        except httpx.HTTPStatusError as exc:
            # 방어선: use-case 단계의 diff 필터가 있음에도 422 가 나면 인라인 코멘트를 포기하고
            # 본문만 재게시한다. 리뷰 전체를 포기하는 것보다 낫다.
            # 제거한 findings 는 **조용히 삭제하지 않고** `dropped_findings` 로 옮겨
            # 본문 접이식 섹션으로 보존. 그래야 리뷰어가 "모델이 뭘 지적했었는지" 를
            # 나중에라도 확인할 수 있다 (codex/gemini PR #17 지적 반영).
            if exc.response.status_code == 422 and payload["comments"]:
                logger.warning(
                    "422 on review POST for %s#%d; retrying without inline comments "
                    "(%d finding(s) preserved in body)",
                    pr.repo.full_name, pr.number, len(result.findings),
                )
                retry_result = replace(
                    result,
                    findings=(),
                    dropped_findings=result.dropped_findings + result.findings,
                )
                payload["body"] = _with_model_footer(
                    retry_result.render_body(), self._review_model_label
                )
                payload["comments"] = []
                await self._request("POST", path, auth=f"token {token}", body=payload)
            else:
                raise

    async def post_comment(self, pr: PullRequest, body: str) -> None:
        if self._dry_run:
            logger.info("DRY_RUN — comment not posted: %s#%d", pr.repo.full_name, pr.number)
            return

        token = await self.get_installation_token(pr.installation_id)
        path = f"/repos/{pr.repo.full_name}/issues/{pr.number}/comments"
        await self._request("POST", path, auth=f"token {token}", body={"body": body})

    # --- Follow-up support (Phase 1) -----------------------------------------

    async def list_review_threads(
        self, pr: PullRequest, installation_id: int
    ) -> tuple[ReviewThread, ...]:
        """GraphQL 로 PR review thread 와 각 root comment 를 조회 — **페이지네이션 지원**.

        thread 외부 페이지네이션은 `cursor` 로 끝까지 순회 (gemini PR #19 Major). thread
        내부 comments 도 50건 초과 시 `hasNextPage=True` 면 follow-up 후보에서 안전하게
        **제외** — 50건만 보고 has_followup_marker / has_non_root_author_reply 를
        판정하면 그 너머의 사람 답글이나 우리 마커를 놓쳐 잘못 자동 응답할 수 있다
        (coderabbitai PR #19 Major).
        """
        token = await self.get_installation_token(installation_id)
        query = """
        query($owner:String!, $name:String!, $number:Int!, $after:String) {
          repository(owner:$owner, name:$name) {
            pullRequest(number:$number) {
              reviewThreads(first:100, after:$after) {
                pageInfo { hasNextPage endCursor }
                nodes {
                  id
                  isResolved
                  comments(first:50) {
                    pageInfo { hasNextPage }
                    nodes {
                      databaseId
                      author { login }
                      path
                      line
                      body
                      commit { oid }
                    }
                  }
                }
              }
            }
          }
        }
        """
        out: list[ReviewThread] = []
        cursor: str | None = None
        # 안전 상한 — 100 page × 100 thread = 10K thread 까지. 정상 PR 은 절대 도달 X.
        # 무한 루프 방어 (잘못된 endCursor 반환 등 GitHub 측 이상 동작 대비).
        for _ in range(100):
            variables: dict[str, Any] = {
                "owner": pr.repo.owner,
                "name": pr.repo.name,
                "number": pr.number,
                "after": cursor,
            }
            data = await self._graphql(query, variables, auth=f"token {token}")
            repo = (data.get("data") or {}).get("repository") or {}
            prn = repo.get("pullRequest") or {}
            threads_node = prn.get("reviewThreads") or {}
            for raw in threads_node.get("nodes") or []:
                thread = _parse_review_thread(raw)
                if thread is not None:
                    out.append(thread)
            page_info = threads_node.get("pageInfo") or {}
            if not page_info.get("hasNextPage"):
                break
            cursor = page_info.get("endCursor")
            if not cursor:
                # GitHub 가 hasNextPage=True 인데 endCursor 누락하면 더 이상 진행 불가.
                logger.warning(
                    "%s#%d threads pagination missing endCursor — stopping",
                    pr.repo.full_name, pr.number,
                )
                break
        else:
            logger.warning(
                "%s#%d review threads pagination exceeded safety cap (100 pages)",
                pr.repo.full_name, pr.number,
            )
        return tuple(out)

    async def reply_to_review_comment(
        self, pr: PullRequest, comment_id: int, body: str
    ) -> None:
        """REST `POST /pulls/{n}/comments/{id}/replies` — 같은 스레드 내 답글로 묶인다."""
        if self._dry_run:
            logger.info(
                "DRY_RUN — follow-up reply not posted: %s#%d comment=%d",
                pr.repo.full_name, pr.number, comment_id,
            )
            return
        token = await self.get_installation_token(pr.installation_id)
        path = (
            f"/repos/{pr.repo.full_name}/pulls/{pr.number}/comments/"
            f"{comment_id}/replies"
        )
        await self._request("POST", path, auth=f"token {token}", body={"body": body})

    async def resolve_review_thread(
        self, thread_id: str, installation_id: int
    ) -> None:
        """GraphQL `resolveReviewThread` mutation — 스레드 closed 처리."""
        if self._dry_run:
            logger.info("DRY_RUN — thread not resolved: %s", thread_id)
            return
        token = await self.get_installation_token(installation_id)
        mutation = """
        mutation($threadId:ID!) {
          resolveReviewThread(input:{threadId:$threadId}) {
            thread { id isResolved }
          }
        }
        """
        await self._graphql(mutation, {"threadId": thread_id}, auth=f"token {token}")

    async def _graphql(
        self, query: str, variables: dict[str, Any], *, auth: str
    ) -> dict[str, Any]:
        """GitHub GraphQL v4 endpoint 호출. REST 와 다른 path (`/graphql`) 사용.

        GraphQL 은 HTTP 200 + `errors` 배열로 부분/전체 실패를 알린다. 이전 구현은
        warning 만 남기고 그대로 반환해, mutation (resolveReviewThread 등) 의 실패
        가 호출자에서 success 처럼 처리됐다 (gemini + coderabbitai PR #19 Major).

        해소: errors 가 있으면 `GraphQLError` 로 raise. 호출자 (e.g. follow-up use
        case) 가 try/except 로 부분 실패를 분리 처리할 수 있도록 한다. 단 query 만
        실패한 케이스에선 `data` 가 부분적으로 채워질 수 있어 호출자가 정상 처리
        가능하지만, 안전을 위해 default 동작은 raise.
        """
        body = {"query": query, "variables": variables}
        resp = await self._send("POST", "/graphql", auth=auth, body=body)
        data: Any = resp.json() if resp.content else {}
        if not isinstance(data, dict):
            return {}
        errors = data.get("errors")
        if errors:
            raise _GraphQLError(errors)
        return data

    # --- HTTP ---------------------------------------------------------------

    async def _send(
        self,
        method: str,
        url_or_path: str,
        *,
        auth: str,
        body: object | None = None,
    ) -> httpx.Response:
        """공통 헤더를 붙여 요청을 보내고 4xx/5xx 를 HTTPStatusError 로 승격한다."""
        headers = {
            "Authorization": auth,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-review-bot",
        }
        resp = await self._http.request(method, url_or_path, headers=headers, json=body)
        if resp.status_code >= 400:
            logger.error(
                "GitHub %s %s failed: %s %s",
                method, url_or_path, resp.status_code, resp.text[:500],
            )
            raise httpx.HTTPStatusError(
                f"{resp.status_code} {resp.reason_phrase}",
                request=resp.request,
                response=resp,
            )
        return resp

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: str,
        body: object | None = None,
    ) -> Any:
        """Issue a single JSON REST call. `path` 는 base_url 에 붙는 상대 경로."""
        resp = await self._send(method, path, auth=auth, body=body)
        if not resp.content:
            return {}
        return resp.json()

    async def _get_page_with_next(
        self, url_or_path: str, *, auth: str
    ) -> tuple[Any, str | None]:
        """Paginated GET — 본문 JSON 과 `Link: rel=next` URL 을 함께 돌려준다.

        `len(body) < per_page` 로 마지막 페이지를 추정하는 건 per_page 나 API 스펙 변경에
        취약하다. GitHub 공식 가이드는 `Link` 헤더의 `rel="next"` 존재 여부를 기준 삼는 것.
        """
        resp = await self._send("GET", url_or_path, auth=auth)
        body: Any = resp.json() if resp.content else {}
        next_url = resp.links.get("next", {}).get("url")
        return body, next_url


def _finding_to_comment(f: Finding) -> dict[str, object]:
    # PR 화면에서 수십 개 라인 코멘트가 쌓일 때 등급별로 한눈에 훑기 위해 본문 최상단에
    # `[Critical]` / `[Major]` / `[Minor]` / `[Suggestion]` 접두를 **일관되게** 붙인다.
    # 접두는 등급에 따라 없거나 있거나 달라지지 않음 — 리뷰어가 "이 코멘트는 어떤 등급인가"
    # 를 추론할 필요 없이 즉시 읽을 수 있도록 한다.
    body = f"[{f.label}] {f.body}"
    return {"path": f.path, "line": f.line, "side": "RIGHT", "body": body}


# `FOLLOWUP_MARKER` 는 도메인 모듈로 이동했고 위쪽 import 로 재사용한다 (coderabbitai
# Nitpick — 이전엔 application 계층이 infrastructure 의 상수를 직접 import 해 의존
# 방향이 역전돼 있었음). 외부 호환을 위해 모듈에서 계속 노출.


class _GraphQLError(RuntimeError):
    """GraphQL endpoint 가 HTTP 200 + `errors` 배열로 실패를 알릴 때 raise.

    호출자 (use case) 가 mutation 실패를 success 로 오인해 후속 동작 (예: 답글 게시)
    을 진행하지 않도록 명시적 예외로 승격 (gemini + coderabbitai PR #19 Major).
    """

    def __init__(self, errors: Any) -> None:
        super().__init__(f"GraphQL errors: {errors}")
        self.errors = errors


def _parse_review_thread(raw: dict[str, Any]) -> ReviewThread | None:
    """GraphQL reviewThread 노드 → 도메인 `ReviewThread`. 필수 필드 누락 시 None.

    **Truncated comments 안전장치** (coderabbitai PR #19 Major): comments 가 50건을
    넘어 `hasNextPage=True` 면, root 이후 어떤 comment 가 있는지 모두 보지 못한 상태
    이므로 `has_followup_marker` / `has_non_root_author_reply` 판정이 부정확해진다.
    이때는 thread 자체를 follow-up 후보에서 안전하게 제외하기 위해 `has_non_root_author_reply
    = True` 로 강제 — use case 의 candidate filter 가 이 thread 를 자동 skip 한다.
    """
    comments_node = raw.get("comments") or {}
    comments = comments_node.get("nodes") or []
    if not comments:
        return None
    comments_truncated = bool(
        (comments_node.get("pageInfo") or {}).get("hasNextPage")
    )

    root = comments[0]
    db_id = root.get("databaseId")
    if not isinstance(db_id, int):
        return None
    root_author = ((root.get("author") or {}).get("login")) or ""
    root_body = root.get("body") or ""
    commit_id = ((root.get("commit") or {}).get("oid")) or ""
    path = root.get("path") or ""
    raw_line = root.get("line")
    line = raw_line if isinstance(raw_line, int) else None

    # 같은 스레드에 우리가 이미 follow-up 답글을 단 적이 있는지(멱등성) +
    # 사람/다른 봇이 답글을 단 적이 있는지(인간 신호 존중) 모두 root 이후 comments 에서 본다.
    #
    # 작성자 식별 정책 (codex PR #19 Major):
    #   - GitHub 은 삭제된 사용자나 식별 불가 actor 의 댓글에서 author=null 을 반환한다.
    #     이 경우 login 을 빈 문자열로 흡수하는데, 단순히 "비어 있으면 무시" 하면 사람
    #     답글을 '대화 없음' 으로 오판해 자동 resolve 가 실행될 수 있다.
    #   - 우리 봇이 단 답글은 본문에 follow-up marker 가 박혀 있어 author 가 비어도 식별
    #     가능 (marker 는 다른 누구도 쓰지 않는 우리 시그니처). marker 가 있으면 우리 것
    #     으로 보고 has_other_author 로 카운트하지 않는다.
    #   - marker 가 없고 author 가 root 와 다르거나 비어 있으면 보수적으로 대화 진행 신호
    #     로 간주해 has_other_author=True. 즉 식별 불가 답글은 무조건 "타인 답글" 쪽으로
    #     기울인다 — false positive (무해한 차단) 보다 false negative (인간 답글 무시하고
    #     resolve) 가 훨씬 위험하기 때문.
    # marker 신원 보증 확장 (coderabbit PR #19 Major): marker 자체가 우리 시그니처
    # 이므로 author 가 비어 있어도 우리 follow-up 답글로 인식해야 멱등성이 유지된다.
    # 이전 구현은 `author == root_author` 일 때만 has_followup_marker 를 세워서, GitHub
    # 가 author 메타를 잃어버린 기존 follow-up 댓글이 다음 사이클에 다시 후보로 통과하고
    # 중복 답글을 게시하는 경로가 있었다. marker 만 있으면 무조건 멱등성 플래그를 켠다.
    has_followup_marker = False
    has_other_author = False
    for c in comments[1:]:
        body = c.get("body") or ""
        author = ((c.get("author") or {}).get("login")) or ""
        is_our_followup = FOLLOWUP_MARKER in body
        if is_our_followup:
            # marker 가 있으면 우리 답글 — author 메타와 무관하게 멱등성 플래그 ON.
            has_followup_marker = True
            continue
        # marker 가 없으면 우리 답글이 아님. author 가 root 와 다르거나 비어 있으면
        # 보수적으로 타인 답글로 카운트.
        if author != root_author:
            has_other_author = True

    if comments_truncated:
        # 50건 초과 thread — 보이지 않는 comment 에 사람 답글·우리 마커가 있을 수
        # 있으므로 보수적으로 follow-up 에서 제외. has_non_root_author_reply=True 면
        # use case 의 candidate filter 가 자동 skip.
        has_other_author = True

    return ReviewThread(
        id=str(raw.get("id") or ""),
        is_resolved=bool(raw.get("isResolved")),
        root_comment_id=db_id,
        root_author_login=root_author,
        path=path,
        line=line,
        commit_id=commit_id,
        body=root_body,
        has_non_root_author_reply=has_other_author,
        has_followup_marker=has_followup_marker,
    )


__all__ = ["FOLLOWUP_MARKER", "GitHubAppClient", "ReviewEvent", "_default_tls_context"]
