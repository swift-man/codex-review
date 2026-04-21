import json
import logging
import ssl
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, replace

import certifi
import jwt

from codex_review.domain import Finding, PullRequest, RepoRef, ReviewEvent, ReviewResult

from .diff_parser import parse_right_lines

logger = logging.getLogger(__name__)

# macOS · python.org 빌드 Python 은 시스템 CA 번들을 자동으로 신뢰하지 않아
# urllib 로 https://api.github.com 호출 시 CERTIFICATE_VERIFY_FAILED 가 뜬다.
# certifi 가 배포하는 루트 번들을 기본값으로 잡아 이 문제를 회피한다. 필요 시
# GitHubAppClient 생성자에 `tls_context=` 를 주입해 교체 가능하다.
def _default_tls_context() -> ssl.SSLContext:
    return ssl.create_default_context(cafile=certifi.where())


# 리뷰 본문 footer 포맷. 모델명은 `CODEX_MODEL` 값을 그대로 표시한다.
_MODEL_FOOTER_TEMPLATE = "\n\n---\n<sub>리뷰 모델: <code>{label}</code></sub>"


def _with_model_footer(body: str, model_label: str | None) -> str:
    if not model_label:
        return body
    return body + _MODEL_FOOTER_TEMPLATE.format(label=model_label)


@dataclass(frozen=True)
class _CachedToken:
    token: str
    expires_at: float

    def is_valid(self) -> bool:
        return time.time() < self.expires_at - 60


class GitHubAppClient:
    """GitHub REST client authenticating as a GitHub App installation."""

    def __init__(
        self,
        app_id: int,
        private_key_pem: str,
        api_base: str = "https://api.github.com",
        dry_run: bool = False,
        tls_context: ssl.SSLContext | None = None,
        review_model_label: str | None = None,
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key_pem
        self._api_base = api_base.rstrip("/")
        self._dry_run = dry_run
        # DIP: 기본값은 certifi 기반 컨텍스트지만 테스트/다른 CA 번들 주입이 필요할 때
        # 생성자에서 SSLContext 를 교체할 수 있도록 열어 둔다.
        self._tls_context = tls_context or _default_tls_context()
        # 본문 footer 에 표시할 모델 라벨. None 이면 footer 생략.
        self._review_model_label = review_model_label
        self._token_cache: dict[int, _CachedToken] = {}

    # --- Auth ---------------------------------------------------------------

    def _app_jwt(self) -> str:
        # iat 를 30초 과거로 당기고 exp 를 10분 한도(GitHub 제한)에 못 미치는 9분으로 잡는 건
        # 로컬-GitHub 간 시계 오차로 인한 "JWT not yet valid / expired" 실패를 피하기 위함.
        now = int(time.time())
        payload = {"iat": now - 30, "exp": now + 9 * 60, "iss": str(self._app_id)}
        return jwt.encode(payload, self._private_key, algorithm="RS256")

    def get_installation_token(self, installation_id: int) -> str:
        cached = self._token_cache.get(installation_id)
        if cached and cached.is_valid():
            return cached.token

        url = f"{self._api_base}/app/installations/{installation_id}/access_tokens"
        data = self._request("POST", url, auth=f"Bearer {self._app_jwt()}")
        token = str(data["token"])
        expires = data.get("expires_at", "")
        # GitHub installation token 은 1시간 유효. 만료 직전 요청이 실패하지 않도록 5분 여유.
        expires_at = time.time() + 55 * 60
        if expires:
            try:
                expires_at = time.mktime(time.strptime(expires, "%Y-%m-%dT%H:%M:%SZ"))
            except ValueError:
                pass
        self._token_cache[installation_id] = _CachedToken(token, expires_at)
        return token

    # --- Public API ---------------------------------------------------------

    def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest:
        token = self.get_installation_token(installation_id)
        pr_url = f"{self._api_base}/repos/{repo.full_name}/pulls/{number}"
        pr_data = self._request("GET", pr_url, auth=f"token {token}")

        # 변경 파일 전체를 가져와야 우선순위 정렬(변경 파일 먼저)이 정확해진다.
        # per_page=100 은 GitHub 허용 최대치라 PR 이 큰 경우의 라운드트립 수를 최소화.
        files_url = f"{pr_url}/files?per_page=100"
        changed: list[str] = []
        diff_right_lines: dict[str, frozenset[int]] = {}
        page = 1
        while True:
            files = self._request("GET", f"{files_url}&page={page}", auth=f"token {token}")
            if not isinstance(files, list) or not files:
                break
            for f in files:
                path = str(f["filename"])
                changed.append(path)
                # patch 는 큰 바이너리/renamed/deleted 변경에서 빠질 수 있음 → 그땐 빈 집합.
                diff_right_lines[path] = parse_right_lines(f.get("patch"))
            # 100개 미만이면 마지막 페이지 — Link 헤더 대신 길이로 단순 판정.
            if len(files) < 100:
                break
            page += 1

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

    def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        if self._dry_run:
            logger.info("DRY_RUN — review not posted: %s#%d", pr.repo.full_name, pr.number)
            return

        token = self.get_installation_token(pr.installation_id)
        url = f"{self._api_base}/repos/{pr.repo.full_name}/pulls/{pr.number}/reviews"

        # commit_id 를 명시해야 리뷰가 "이 head SHA 시점"에 고정된다. 생략하면 최신 SHA 기준으로
        # 붙어 라인 번호 오정렬이 발생할 수 있다.
        payload: dict[str, object] = {
            "commit_id": pr.head_sha,
            "body": _with_model_footer(result.render_body(), self._review_model_label),
            "event": result.event.value,
            "comments": [_finding_to_comment(f) for f in result.findings],
        }
        try:
            self._request("POST", url, auth=f"token {token}", body=payload)
        except urllib.error.HTTPError as exc:
            # 방어선: use-case 단계의 diff 필터가 있음에도 422 가 나면(예: diff cache 간 race,
            # GitHub 내부 정책 변화 등) 인라인 코멘트를 포기하고 본문만 재게시한다.
            # 리뷰 전체를 포기하는 것보다 낫다.
            #
            # 본문도 findings 제거된 상태를 기준으로 재렌더링해야 한다 — 그러지 않으면
            # "기술 단위 코멘트 N건은 각 라인에 별도 표시됩니다" 같은 안내가 남아
            # 실제로는 인라인이 없는데도 있는 것처럼 보이는 거짓 상태가 된다.
            if exc.code == 422 and payload["comments"]:
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
                self._request("POST", url, auth=f"token {token}", body=payload)
            else:
                raise

    def post_comment(self, pr: PullRequest, body: str) -> None:
        if self._dry_run:
            logger.info("DRY_RUN — comment not posted: %s#%d", pr.repo.full_name, pr.number)
            return

        token = self.get_installation_token(pr.installation_id)
        url = f"{self._api_base}/repos/{pr.repo.full_name}/issues/{pr.number}/comments"
        self._request("POST", url, auth=f"token {token}", body={"body": body})

    # --- HTTP ---------------------------------------------------------------

    def _request(
        self,
        method: str,
        url: str,
        *,
        auth: str,
        body: object | None = None,
    ) -> dict[str, object] | list[object]:
        headers = {
            "Authorization": auth,
            "Accept": "application/vnd.github+json",
            "X-GitHub-Api-Version": "2022-11-28",
            "User-Agent": "codex-review-bot",
        }
        data: bytes | None = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"

        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30, context=self._tls_context) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.error("GitHub %s %s failed: %s %s", method, url, exc.code, detail[:500])
            raise


def _finding_to_comment(f: Finding) -> dict[str, object]:
    return {"path": f.path, "line": f.line, "side": "RIGHT", "body": f.body}


__all__ = ["GitHubAppClient", "ReviewEvent"]
