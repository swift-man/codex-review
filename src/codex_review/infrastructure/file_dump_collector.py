import logging
import subprocess
from pathlib import Path

from codex_review.domain import FileDump, FileEntry, TokenBudget

logger = logging.getLogger(__name__)

_ALWAYS_SKIP_DIRS = {
    ".git",
    "node_modules",
    "dist",
    "build",
    "out",
    "target",
    "vendor",
    "__pycache__",
    ".venv",
    "venv",
    ".next",
    ".nuxt",
    ".turbo",
    ".cache",
    ".idea",
    ".vscode",
    "coverage",
    ".mypy_cache",
    ".pytest_cache",
    ".ruff_cache",
}

_SKIP_SUFFIXES = {
    ".lock",
    ".min.js",
    ".min.css",
    ".map",
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".svg",
    ".pdf",
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".7z",
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    ".dll",
    ".so",
    ".dylib",
    ".exe",
    ".bin",
    ".dat",
    ".db",
    ".sqlite",
    ".pyc",
    ".pyo",
    ".class",
    ".jar",
    ".wasm",
}

_LOCK_FILENAMES = {
    "package-lock.json",
    "yarn.lock",
    "pnpm-lock.yaml",
    "poetry.lock",
    "uv.lock",
    "Cargo.lock",
    "Gemfile.lock",
    "composer.lock",
    "go.sum",
}

_PRIORITY_DIRS = ("src", "app", "lib", "pkg", "internal", "packages", "apps")


class FileDumpCollector:
    """Collects repository files into a prioritized dump honoring a token budget."""

    def __init__(self, file_max_bytes: int) -> None:
        self._file_max_bytes = file_max_bytes

    def collect(
        self,
        root: Path,
        changed_files: tuple[str, ...],
        budget: TokenBudget,
    ) -> FileDump:
        # git ls-files 로 .gitignore 를 존중하는 "진짜 소스" 파일만 뽑는다.
        # 레포 루트 파일을 os.walk 로 순회하면 로컬 빌드 산출물까지 섞여 들어온다.
        tracked = _git_ls_files(root)
        changed_set = set(changed_files)

        # 변경 파일 → 핵심 소스 디렉터리 → 기타 순으로 정렬하는 이유:
        # 예산이 부족할 때 하위 우선순위 파일부터 잘라내야 PR 컨텍스트가 살아남는다.
        ordered = _sort_by_priority(tracked, changed_set)

        entries: list[FileEntry] = []
        excluded: list[str] = []
        total_chars = 0
        max_chars = budget.max_chars()

        for rel_path in ordered:
            abs_path = root / rel_path
            if not abs_path.is_file():
                continue
            if _should_skip(rel_path, abs_path, self._file_max_bytes):
                excluded.append(rel_path)
                continue
            try:
                content = abs_path.read_text(encoding="utf-8")
            except (UnicodeDecodeError, OSError):
                excluded.append(rel_path)
                continue

            # 32자 여유는 프롬프트에 붙는 "--- FILE: path ---" / 라인 번호 접두사 등
            # 프레이밍 오버헤드 근사치. 정확한 토큰 산정은 아니지만 보수적으로 잡아 예산 초과를 막는다.
            entry_chars = len(content) + len(rel_path) + 32
            if total_chars + entry_chars > max_chars:
                excluded.append(rel_path)
                continue

            entries.append(
                FileEntry(
                    path=rel_path,
                    content=content,
                    size_bytes=len(content.encode("utf-8")),
                    is_changed=rel_path in changed_set,
                )
            )
            total_chars += entry_chars

        # exceeded 판정 기준:
        # (1) 변경 파일 중 하나라도 예산 때문에 제외됐다면 → 리뷰 품질이 크게 떨어지므로 exceeded
        # (2) 전체 예산을 꽉 채웠다면(>=)  → 프롬프트 뒤쪽이 잘렸을 가능성 높음
        # use case 레이어에서 (1)에 해당하는 경우에만 리뷰 대신 "예산 초과" 코멘트를 게시.
        exceeded = any(p for p in excluded if p in changed_set) or total_chars >= max_chars
        return FileDump(
            entries=tuple(entries),
            total_chars=total_chars,
            excluded=tuple(excluded),
            exceeded_budget=exceeded,
            budget=budget,
        )


def _git_ls_files(root: Path) -> list[str]:
    result = subprocess.run(  # noqa: S603
        ["git", "-C", str(root), "ls-files"],
        capture_output=True,
        text=True,
        check=True,
    )
    return [line for line in result.stdout.splitlines() if line]


def _sort_by_priority(paths: list[str], changed: set[str]) -> list[str]:
    def rank(path: str) -> tuple[int, str]:
        if path in changed:
            return (0, path)
        top = path.split("/", 1)[0]
        if top in _PRIORITY_DIRS:
            return (1, path)
        return (2, path)

    return sorted(paths, key=rank)


def _should_skip(rel_path: str, abs_path: Path, file_max_bytes: int) -> bool:
    parts = rel_path.split("/")
    if any(p in _ALWAYS_SKIP_DIRS for p in parts):
        return True
    name = parts[-1]
    if name in _LOCK_FILENAMES:
        return True
    suffix = abs_path.suffix.lower()
    if suffix in _SKIP_SUFFIXES:
        return True
    if _is_double_suffix_skip(name):
        return True
    try:
        size = abs_path.stat().st_size
    except OSError:
        return True
    if size > file_max_bytes:
        return True
    return False


def _is_double_suffix_skip(name: str) -> bool:
    lowered = name.lower()
    return any(lowered.endswith(s) for s in (".min.js", ".min.css", ".d.ts.map"))
