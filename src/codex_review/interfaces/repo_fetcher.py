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

    async def head_sha(self, repo_path: Path) -> str:
        """현재 작업 트리의 HEAD SHA 반환 (`git rev-parse HEAD`).

        `session()` 컨텍스트 안에서만 호출해야 의미 있다 — checkout 직후 SHA 검증용.
        구현은 git subprocess 호출이라 동기적으로 완료될 수도 있지만, 호출자 입장에서
        다른 git 작업과 시그니처가 일치하도록 코루틴으로 통일한다.
        """
        ...
