from dataclasses import dataclass, field


@dataclass(frozen=True)
class TokenBudget:
    max_tokens: int

    @staticmethod
    def chars_per_token() -> int:
        return 4

    def fits(self, char_count: int) -> bool:
        return char_count <= self.max_tokens * self.chars_per_token()

    def max_chars(self) -> int:
        return self.max_tokens * self.chars_per_token()


@dataclass(frozen=True)
class FileEntry:
    path: str
    content: str
    size_bytes: int
    is_changed: bool


@dataclass(frozen=True)
class FileDump:
    entries: tuple[FileEntry, ...]
    total_chars: int
    excluded: tuple[str, ...] = field(default_factory=tuple)
    exceeded_budget: bool = False
    budget: TokenBudget | None = None
