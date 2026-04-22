from collections.abc import AsyncIterator
from contextlib import AbstractAsyncContextManager
from pathlib import Path
from typing import Protocol

from codex_review.domain import PullRequest


class RepoFetcher(Protocol):
    def session(
        self, pr: PullRequest, installation_token: str
    ) -> AbstractAsyncContextManager[Path]:
        """Async context manager — checkout 부터 collect 완료까지 저장소 락을 유지한다.

        블록 내부에서만 작업 트리가 기대 SHA 로 고정된다. 같은 저장소에 대한 다른 세션은
        이 블록이 끝날 때까지 대기한다.
        """
        ...
