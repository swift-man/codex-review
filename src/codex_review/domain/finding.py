from dataclasses import dataclass
from enum import Enum


class ReviewEvent(str, Enum):
    COMMENT = "COMMENT"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    APPROVE = "APPROVE"


@dataclass(frozen=True)
class Finding:
    """A line-anchored technical comment in Korean.

    `line` is required — the bot only emits comments attached to a specific
    RIGHT-side line number so reviewers can see them inline in the PR.
    """

    path: str
    line: int
    body: str
