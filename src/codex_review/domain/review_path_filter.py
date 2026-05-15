from dataclasses import dataclass, field
from fnmatch import fnmatchcase


@dataclass(frozen=True)
class ReviewPathFilter:
    """Path policy loaded from `.reviewbot.yml`.

    Semantics:
      - `always_review` wins over both include and exclude.
      - if `include` is empty, every path is included unless excluded.
      - if `include` is set, a path must match at least one include pattern.
    """

    include: tuple[str, ...] = field(default_factory=tuple)
    exclude: tuple[str, ...] = field(default_factory=tuple)
    always_review: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def allow_all(cls) -> "ReviewPathFilter":
        return cls()

    def allows(self, path: str) -> bool:
        normalized = _normalize_path(path)
        if self.always_allows(normalized):
            return True
        if self.include and not _matches_any(normalized, self.include):
            return False
        return not _matches_any(normalized, self.exclude)

    def always_allows(self, path: str) -> bool:
        return _matches_any(_normalize_path(path), self.always_review)


def _matches_any(path: str, patterns: tuple[str, ...]) -> bool:
    return any(_matches(path, pattern) for pattern in patterns)


def _matches(path: str, pattern: str) -> bool:
    normalized_pattern = _normalize_pattern(pattern)
    if not normalized_pattern:
        return False
    return _match_segments(
        tuple(normalized_pattern.split("/")),
        tuple(_normalize_path(path).split("/")),
    )


def _match_segments(pattern_parts: tuple[str, ...], path_parts: tuple[str, ...]) -> bool:
    if not pattern_parts:
        return not path_parts

    head = pattern_parts[0]
    rest = pattern_parts[1:]
    if head == "**":
        return _match_segments(rest, path_parts) or (
            bool(path_parts) and _match_segments(pattern_parts, path_parts[1:])
        )

    if not path_parts:
        return False
    return fnmatchcase(path_parts[0], head) and _match_segments(rest, path_parts[1:])


def _normalize_path(path: str) -> str:
    return path.replace("\\", "/").removeprefix("./").strip("/")


def _normalize_pattern(pattern: str) -> str:
    normalized = _normalize_path(pattern.strip())
    if normalized.endswith("/"):
        return f"{normalized}**"
    return normalized
