from typing import Protocol

from codex_review.domain import PullRequest, RepoRef, ReviewResult


class GitHubClient(Protocol):
    async def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest: ...

    async def post_review(
        self,
        pr: PullRequest,
        result: ReviewResult,
    ) -> None: ...

    async def post_comment(
        self,
        pr: PullRequest,
        body: str,
    ) -> None: ...

    async def get_installation_token(self, installation_id: int) -> str: ...
