"""이전 라운드의 PR 코멘트·리뷰 기록 — 봇이 같은 항목을 반복 지적하지 않고 deferred
신호 / 다른 봇의 의견을 인지한 뒤 리뷰하도록 제공하는 컨텍스트.

`ReviewHistory` 는 다음 3종을 시간순으로 묶은 도메인 컬렉션:
  - issue comments  (PR 본문 아래의 일반 코멘트, 작성자/봇 응답 등)
  - inline review comments (라인에 붙은 review comment, 메타리플라이 대상)
  - review summaries (다른 봇/사람의 리뷰 본문)

`MetaReply` 는 우리 봇이 다른 봇의 inline review comment 에 다는 대댓글 — 동의 / 반박 /
defer 권장 등 메타 의견. summary 코멘트에는 thread 가 없어 reply 불가하므로 inline
한정.
"""

from dataclasses import dataclass
from datetime import datetime
from typing import Literal

ReviewCommentKind = Literal["issue", "inline", "review-summary"]


@dataclass(frozen=True)
class ReviewComment:
    """한 건의 PR 컨텍스트 코멘트. 봇 / 사람 / 작성자 어느 쪽이든 동일 도메인 형태.

    - `kind` 로 issue / inline / review-summary 구분.
    - `comment_id` 는 inline 일 때만 의미 있음 (메타리플라이 타깃). 다른 종류는 None.
    - `path` / `line` 도 inline 한정.

    "우리 봇 vs 다른 봇" 식별이 필요해지면 모델은 `review_model_label` footer 패턴
    (`<sub>리뷰 모델: <code>...`) 으로 자체 추정 가능 — 데드 필드를 두지 않는다.
    """

    author_login: str
    kind: ReviewCommentKind
    body: str
    created_at: datetime
    comment_id: int | None = None
    path: str | None = None
    line: int | None = None


@dataclass(frozen=True)
class ReviewHistory:
    """`ReviewComment` 를 시간순으로 묶은 컬렉션. 비어 있으면 빈 튜플.

    use case 는 빈 history (첫 리뷰) 를 받으면 prompt 의 history 섹션 자체를 생략해
    기존 동작을 그대로 보존한다 — 회귀 보호.
    """

    comments: tuple[ReviewComment, ...] = ()

    @property
    def is_empty(self) -> bool:
        return len(self.comments) == 0


@dataclass(frozen=True)
class MetaReply:
    """다른 봇의 inline review comment 에 다는 대댓글. 모델이 출력 JSON 의
    `meta_replies` 배열로 산출하면 use case 가 review post 후 `reply_to_review_comment`
    로 게시한다.

    `reply_to_comment_id` 는 GitHub REST `pulls/{n}/comments` 응답의 `id` (int).
    GraphQL `databaseId` 와 동일 — 봇이 모델에게 history 컨텍스트로 넘긴 값을 그대로
    회수해야 정합성이 유지된다.
    """

    reply_to_comment_id: int
    body: str
