import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass

import jwt

from codex_review.domain import Finding, PullRequest, RepoRef, ReviewEvent, ReviewResult

logger = logging.getLogger(__name__)


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
    ) -> None:
        self._app_id = app_id
        self._private_key = private_key_pem
        self._api_base = api_base.rstrip("/")
        self._dry_run = dry_run
        self._token_cache: dict[int, _CachedToken] = {}

    # --- Auth ---------------------------------------------------------------

    def _app_jwt(self) -> str:
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
        expires_at = time.time() + 55 * 60  # GitHub tokens live ~1h, keep margin.
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

        files_url = f"{pr_url}/files?per_page=100"
        changed: list[str] = []
        page = 1
        while True:
            files = self._request("GET", f"{files_url}&page={page}", auth=f"token {token}")
            if not isinstance(files, list) or not files:
                break
            changed.extend(str(f["filename"]) for f in files)
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
        )

    def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        if self._dry_run:
            logger.info("DRY_RUN — review not posted: %s#%d", pr.repo.full_name, pr.number)
            return

        token = self.get_installation_token(pr.installation_id)
        url = f"{self._api_base}/repos/{pr.repo.full_name}/pulls/{pr.number}/reviews"
        payload: dict[str, object] = {
            "commit_id": pr.head_sha,
            "body": result.render_body(),
            "event": result.event.value,
            "comments": [_finding_to_comment(f) for f in result.findings],
        }
        self._request("POST", url, auth=f"token {token}", body=payload)

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
            with urllib.request.urlopen(req, timeout=30) as resp:  # noqa: S310
                return json.loads(resp.read().decode("utf-8") or "{}")
        except urllib.error.HTTPError as exc:
            detail = exc.read().decode("utf-8", errors="replace")
            logger.error("GitHub %s %s failed: %s %s", method, url, exc.code, detail[:500])
            raise


def _finding_to_comment(f: Finding) -> dict[str, object]:
    return {"path": f.path, "line": f.line, "side": "RIGHT", "body": f.body}


__all__ = ["GitHubAppClient", "ReviewEvent"]
