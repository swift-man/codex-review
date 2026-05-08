from typing import Protocol

from codex_review.domain import (
    PullRequest,
    RepoRef,
    ReviewHistory,
    ReviewResult,
    ReviewThread,
)


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

    async def fetch_review_history(
        self, pr: PullRequest, installation_id: int
    ) -> ReviewHistory:
        """PR 의 이전 코멘트·리뷰 기록을 시간순으로 반환.

        구성:
          - issue comments  (PR 본문 아래 일반 코멘트)
          - inline review comments (라인에 붙은 review comment, 메타리플라이 대상)
          - review summaries (다른 봇/사람의 리뷰 본문)

        용도: 새 라운드 리뷰 시 "이전 라운드의 deferred 신호 / 다른 봇 의견" 을
        prompt 컨텍스트로 노출해 동일 항목 반복 지적·환각을 줄인다.

        구현은 우리 follow-up marker 박힌 자동 답글은 제외 (메타 신호 노이즈).
        history 가 비어 있으면 빈 `ReviewHistory(comments=())` 반환 — 첫 리뷰
        호환성 유지.
        """
        ...

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
