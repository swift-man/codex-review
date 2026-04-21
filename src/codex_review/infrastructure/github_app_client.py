import asyncio
import logging
import ssl
import time
from collections import OrderedDict
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


# installation_id 별 락을 무한히 쌓지 않도록 상한. 1024 는 현실적 규모(보통 수~수백 개
# installation)의 10~100배 여유. 넘치면 LRU 로 가장 오래 쓰이지 않은 락을 폐기한다.
_MAX_TRACKED_INSTALLATIONS = 1024


class _LockRegistry:
    """installation_id → `asyncio.Lock` 매핑을 LRU 상한으로 관리.

    Gemini 의 "장기 실행 시 defaultdict 에 락이 무한히 쌓여 메모리 누수 가능" 지적에 대응.
    상한을 넘으면 가장 오래 사용되지 않은 항목을 제거한다. 제거 시점에 해당 락을 잡고 있는
    `async with` 블록이 있다면 그 블록은 자기 지역 변수로 락을 유지하므로 안전하게 완료되고,
    새로 들어오는 같은 iid 요청은 새 락을 쓰게 되지만 본 애플리케이션의 동시성(최대 N=수 개)
    규모에서는 경쟁이 사실상 일어나지 않는다.
    """

    def __init__(self, maxsize: int = _MAX_TRACKED_INSTALLATIONS) -> None:
        self._maxsize = maxsize
        self._locks: OrderedDict[int, asyncio.Lock] = OrderedDict()

    def get(self, installation_id: int) -> asyncio.Lock:
        lock = self._locks.get(installation_id)
        if lock is not None:
            self._locks.move_to_end(installation_id)
            return lock
        while len(self._locks) >= self._maxsize:
            self._locks.popitem(last=False)  # evict oldest
        lock = asyncio.Lock()
        self._locks[installation_id] = lock
        return lock

    def __len__(self) -> int:  # for tests / observability
        return len(self._locks)


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
                # Python 3.11+ 의 fromisoformat 은 "Z" 접미사를 UTC 로 네이티브 지원.
                try:
                    expires_at = datetime.fromisoformat(expires).timestamp()
                except ValueError:
                    pass
            self._token_cache[installation_id] = _CachedToken(token, expires_at)
            return token

    # --- Public API ---------------------------------------------------------

    async def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest:
        token = await self.get_installation_token(installation_id)
        pr_path = f"/repos/{repo.full_name}/pulls/{number}"

        # PR 메타와 첫 페이지 files 는 상호 독립적이라 병렬로 조회해 네트워크 대기 단축.
        # TaskGroup 은 형제 태스크 중 하나가 실패하면 나머지를 자동 취소해 주므로
        # gather + return_exceptions 보다 에러 처리가 깔끔하다. (페이지 2부터는 이전 페이지
        # 크기를 봐야 하므로 순차 유지.)
        async with asyncio.TaskGroup() as tg:
            meta_task = tg.create_task(
                self._request("GET", pr_path, auth=f"token {token}")
            )
            first_files_task = tg.create_task(
                self._request(
                    "GET",
                    f"{pr_path}/files?per_page=100&page=1",
                    auth=f"token {token}",
                )
            )
        pr_data = meta_task.result()
        first_page = first_files_task.result()
        assert isinstance(pr_data, dict)

        # 변경 파일 전체를 가져와야 우선순위 정렬(변경 파일 먼저)이 정확해진다.
        # per_page=100 은 GitHub 허용 최대치라 PR 이 큰 경우의 라운드트립 수를 최소화.
        changed: list[str] = []
        diff_right_lines: dict[str, frozenset[int]] = {}
        page = 1
        files = first_page
        while True:
            if not isinstance(files, list) or not files:
                break
            for f in files:
                path = str(f["filename"])
                changed.append(path)
                # GitHub 는 큰 diff / rename / delete / binary 상태에서 `patch` 키를 생략한다.
                # 그 파일에 대한 인라인 코멘트는 use-case 필터에서 전부 사라지므로 운영자가
                # 알아볼 수 있도록 경고 로그로 남긴다.
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
                diff_right_lines[path] = parse_right_lines(patch)
            # 100개 미만이면 마지막 페이지 — Link 헤더 대신 길이로 단순 판정.
            if len(files) < 100:
                break
            page += 1
            files = await self._request(
                "GET",
                f"{pr_path}/files?per_page=100&page={page}",
                auth=f"token {token}",
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
            # 본문도 findings 제거된 상태로 재렌더해야 "기술 단위 코멘트 N건" 안내가 남는
            # 거짓 상태를 피할 수 있다.
            if exc.response.status_code == 422 and payload["comments"]:
                logger.warning(
                    "422 on review POST for %s#%d; retrying without inline comments",
                    pr.repo.full_name,
                    pr.number,
                )
                retry_result = replace(result, findings=())
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

    async def _request(
        self,
        method: str,
        path: str,
        *,
        auth: str,
        body: object | None = None,
    ) -> Any:
        """Issue a single JSON REST call. `path` 는 base_url 에 붙는 상대 경로."""
        headers = {
            "Authorization": auth,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-review-bot",
        }
        resp = await self._http.request(method, path, headers=headers, json=body)
        # httpx 는 4xx/5xx 를 예외로 승격시키지 않으므로 명시적으로 raise — `post_review` 가
        # 422 를 구분해서 잡아야 하기 때문에 HTTPStatusError 가 필요.
        if resp.status_code >= 400:
            logger.error(
                "GitHub %s %s failed: %s %s",
                method, path, resp.status_code, resp.text[:500],
            )
            raise httpx.HTTPStatusError(
                f"{resp.status_code} {resp.reason_phrase}",
                request=resp.request,
                response=resp,
            )
        if not resp.content:
            return {}
        return resp.json()


def _finding_to_comment(f: Finding) -> dict[str, object]:
    return {"path": f.path, "line": f.line, "side": "RIGHT", "body": f.body}


__all__ = ["GitHubAppClient", "ReviewEvent", "_default_tls_context"]
