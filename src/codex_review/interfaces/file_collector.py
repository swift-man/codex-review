from pathlib import Path
from typing import Protocol

from codex_review.domain import FileDump, TokenBudget


class FileCollector(Protocol):
    async def collect(
        self,
        root: Path,
        changed_files: tuple[str, ...],
        budget: TokenBudget,
    ) -> FileDump: ...
