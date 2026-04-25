from typing import Protocol

from codex_review.domain import PullRequest, RepoRef, ReviewResult, ReviewThread


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

    # --- Follow-up support (Phase 1) -----------------------------------------
    # 새 push 가 들어왔을 때 봇이 단 옛 라인 코멘트의 해소 여부를 보고하기 위한 3개의
    # 메서드. PR 별 review thread 목록을 GraphQL 로 조회 → 분류 → 답글(REST) 또는
    # resolve(GraphQL) 로 마무리한다.

    async def list_review_threads(
        self, pr: PullRequest, installation_id: int
    ) -> tuple[ReviewThread, ...]:
        """PR 의 review thread 전부 반환. 봇 식별·이미 resolve 됐는지·사람 답글 여부를
        포함해 follow-up 판정에 필요한 모든 도메인 정보를 한 번에 제공한다.
        """
        ...

    async def reply_to_review_comment(
        self,
        pr: PullRequest,
        comment_id: int,
        body: str,
    ) -> None:
        """기존 review comment 에 대댓글로 답한다 (스레드 안으로 묶임)."""
        ...

    async def resolve_review_thread(
        self, thread_id: str, installation_id: int
    ) -> None:
        """GraphQL `resolveReviewThread` mutation 으로 스레드를 닫는다.

        `thread_id` 는 GraphQL 노드 ID (`PullRequestReviewThread.id`).
        """
        ...
