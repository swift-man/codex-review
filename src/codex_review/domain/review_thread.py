from dataclasses import dataclass


@dataclass(frozen=True)
class ReviewThread:
    """A PR review comment thread observed via GitHub API.

    이 도메인 객체는 follow-up 판정에 필요한 최소 정보만 담는다:
      - `id`: GraphQL 노드 ID (resolveReviewThread mutation 의 인자)
      - `is_resolved`: 이미 closed 된 스레드인지 (skip 대상)
      - `root_comment_id`: REST POST `/comments/{id}/replies` 에 쓰는 root 댓글의
        databaseId. 답글은 항상 root 에 단다.
      - `root_author_login`: 우리 봇이 단 스레드인지 식별. 다른 봇·사람 스레드는 skip.
      - `path` / `line`: follow-up 분류 시 "이 코드 아직 PR 에 있나?" 판정에 사용.
        `line` 은 GitHub 가 outdated 처리한 스레드에서 None 일 수 있다.
      - `commit_id`: 코멘트가 달린 시점의 SHA — Phase 2 의 LLM 판정에서 옛/새 hunk
        대비에 쓸 수 있도록 함께 보관.
      - `has_non_root_author_reply`: 사람 또는 다른 봇의 답글이 이미 있는지. True 면
        대화가 진행 중이라 자동 follow-up 으로 끼어들지 않는다.
      - `has_followup_marker`: 우리가 이미 답글을 단 스레드인지 (멱등성). 본문에 박힌
        `<!-- codex-review-followup:v1 -->` 마커로 식별.
    """

    id: str
    is_resolved: bool
    root_comment_id: int
    root_author_login: str
    path: str
    line: int | None
    commit_id: str
    body: str
    has_non_root_author_reply: bool
    has_followup_marker: bool
