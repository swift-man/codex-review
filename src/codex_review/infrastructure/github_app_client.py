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

from codex_review.domain import Finding, PullRequest, RepoRef, ReviewEvent, ReviewResult

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


__all__ = ["GitHubAppClient", "ReviewEvent", "_default_tls_context"]
