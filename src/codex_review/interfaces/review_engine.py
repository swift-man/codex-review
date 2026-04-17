from typing import Protocol

from codex_review.domain import FileDump, PullRequest, ReviewResult


class ReviewEngine(Protocol):
    def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult: ...
