import asyncio
import logging
from pathlib import Path

from codex_review.domain import FileDump, FileEntry, TokenBudget

logger = logging.getLogger(__name__)

_ALWAYS_SKIP_DIRS = {
    # VCS / Python / JS 공통
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
    # iOS / Swift 의존성·빌드 산출물 (코드 리뷰와 무관, 용량 큼)
    "Pods",
    "Carthage",
    ".build",
    "DerivedData",
    # 테스트 스냅샷/픽스처 — 자동 생성되거나 덤프성 데이터라 리뷰 가치 낮음
    "__snapshots__",
    "snapshots",
    "__fixtures__",
    "fixtures",
    # Storybook 빌드 산출물
    ".storybook",
    "storybook-static",
}

_SKIP_SUFFIXES = {
    # 번들/생성물
    ".lock",
    ".min.js",
    ".min.css",
    ".map",
    # 이미지/미디어
    ".png",
    ".jpg",
    ".jpeg",
    ".gif",
    ".ico",
    ".webp",
    ".svg",
    ".pdf",
    # 압축
    ".zip",
    ".tar",
    ".gz",
    ".bz2",
    ".7z",
    # 폰트
    ".woff",
    ".woff2",
    ".ttf",
    ".otf",
    ".eot",
    # 미디어
    ".mp3",
    ".mp4",
    ".mov",
    ".avi",
    # 바이너리
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
    # 데이터 테이블 (리뷰 대상 아님)
    ".csv",
    ".tsv",
    ".parquet",
    ".xlsx",
    ".xls",
    # 번역 리소스
    ".po",
    ".mo",
    ".xliff",
    ".strings",
    ".stringsdict",
    # iOS 프로젝트/UI 메타 (거의 생성 파일)
    ".pbxproj",
    ".xcworkspacedata",
    ".xcscheme",
    ".entitlements",
    ".storyboard",
    ".xib",
    # 스냅샷 개별 파일
    ".snap",
    ".snapshot",
    # 증분 빌드 메타
    ".tsbuildinfo",
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
    # Swift Package Manager
    "Package.resolved",
}

_PRIORITY_DIRS = ("src", "app", "lib", "pkg", "internal", "packages", "apps")

# 확장자만으로는 리뷰 가치를 단정할 수 없는(= 소스일 수도 데이터일 수도 있는) 형식.
# 이 집합에 포함된 파일은 "크면 제외, 작으면 포함" 규칙(_data_file_max_bytes)을 따른다.
_AMBIGUOUS_DATA_SUFFIXES = {
    ".json",
    ".yaml",
    ".yml",
    ".xml",
    ".plist",
    ".ndjson",
    ".jsonl",
}

# 크기와 무관하게 항상 포함해야 할 대표적 설정·매니페스트 파일명.
# 이름에 확신이 있을 때만 `_AMBIGUOUS_DATA_SUFFIXES` 의 크기 제한을 건너뛴다.
_IMPORTANT_CONFIG_NAMES = {
    # 프로젝트 매니페스트
    "package.json",
    "package.json5",
    "deno.json",
    "bun.json",
    "composer.json",
    "tsconfig.json",
    "tsconfig.base.json",
    "jsconfig.json",
    # 린트/포매터/빌더
    "eslint.config.json",
    ".eslintrc.json",
    ".prettierrc.json",
    "biome.json",
    "babel.config.json",
    "jest.config.json",
    # CI / 컨테이너
    "docker-compose.yml",
    "docker-compose.yaml",
    # Python / Rust / Go (*.toml/yaml 도 섞이지만 여기선 대표만)
    "pyproject.toml",
    "Cargo.toml",
    "Package.swift",
}


class FileDumpCollector:
    """Collects repository files into a prioritized dump honoring a token budget."""

    def __init__(
        self,
        file_max_bytes: int,
        data_file_max_bytes: int = 20_000,
    ) -> None:
        self._file_max_bytes = file_max_bytes
        # JSON/YAML/XML 처럼 "소스일 수도, 데이터일 수도" 있는 확장자에 대해
        # 적용할 더 엄격한 상한. 설정/매니페스트는 작아서 이 한도에 항상 통과한다.
        self._data_file_max_bytes = data_file_max_bytes

    async def collect(
        self,
        root: Path,
        changed_files: tuple[str, ...],
        budget: TokenBudget,
    ) -> FileDump:
        # git ls-files 는 async subprocess 로 먼저 실행 — 진짜 소스 파일만 뽑아낸다.
        tracked = await _git_ls_files(root)
        changed_set = set(changed_files)
        # 예산이 부족할 때 하위 우선순위 파일부터 잘려 나가야 PR 컨텍스트가 살아남는다.
        ordered = _sort_by_priority(tracked, changed_set)

        # 파일 stat/read 는 블로킹 I/O 라 이벤트 루프에서 직접 돌리면 동일 루프의 다른 웹훅·
        # 리뷰 처리까지 지연된다. 큰 저장소(수백 파일)에선 수백 ms 이상. 순수 계산·블로킹 I/O
        # 구간을 별도 스레드로 오프로드해 이벤트 루프를 열어 둔다.
        return await asyncio.to_thread(
            _build_dump_sync,
            root,
            ordered,
            changed_set,
            budget,
            self._file_max_bytes,
            self._data_file_max_bytes,
        )


def _build_dump_sync(
    root: Path,
    ordered: list[str],
    changed_set: set[str],
    budget: TokenBudget,
    file_max_bytes: int,
    data_file_max_bytes: int,
) -> FileDump:
    """순수 동기 파일 수집. `collect()` 가 스레드로 오프로드해 호출한다.

    두 종류의 제외를 구분한다:
      - filter_excluded : 바이너리/미디어/크기 한도 등 **정책상 제외**. PR 에 이미지만
                         변경돼도 무조건 들어간다 — "예산 초과" 신호로 쓰면 안 된다.
      - budget_trimmed  : 토큰 예산이 부족해 **컨텍스트에서 잘려 나간** 파일.
                          이것만이 `exceeded_budget` 판정의 근거.
    이전에는 둘을 합쳐 `excluded` 에 넣어 바이너리 파일 포함 PR 이 "예산 초과" 로 오진됐다.
    """
    entries: list[FileEntry] = []
    filter_excluded: list[str] = []
    budget_trimmed: list[str] = []
    total_chars = 0
    max_chars = budget.max_chars()

    for rel_path in ordered:
        abs_path = root / rel_path
        if not abs_path.is_file():
            continue
        if _should_skip(rel_path, abs_path, file_max_bytes, data_file_max_bytes):
            filter_excluded.append(rel_path)
            continue
        try:
            content = abs_path.read_text(encoding="utf-8")
        except (UnicodeDecodeError, OSError):
            filter_excluded.append(rel_path)
            continue

        # 32자 여유는 프롬프트에 붙는 "--- FILE: path ---" / 라인 번호 접두사 등 프레이밍
        # 오버헤드 근사치. 정확한 토큰 산정은 아니지만 보수적으로 잡아 예산 초과를 막는다.
        entry_chars = len(content) + len(rel_path) + 32
        if total_chars + entry_chars > max_chars:
            budget_trimmed.append(rel_path)
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

    # exceeded_budget: 오직 **예산 때문에 잘린** 경우만 True 로 잡는다.
    #   (1) 변경 파일 중 하나라도 budget_trimmed 에 있으면 리뷰 품질 급락
    #   (2) 전체 예산을 꽉 채웠으면 프롬프트 뒤쪽이 잘렸을 가능성
    exceeded = any(p in changed_set for p in budget_trimmed) or total_chars >= max_chars

    # 운영 관측용 `excluded` 는 두 카테고리 합집합(순서: budget → filter) 으로 유지.
    # 프롬프트 budget notice 에서 "왜 이 파일이 빠졌는가" 를 운영자가 훑을 수 있게.
    excluded = budget_trimmed + filter_excluded

    logger.info(
        "file dump: included=%d filter_excluded=%d budget_trimmed=%d "
        "chars=%d/%d (%.1f%%) exceeded=%s",
        len(entries),
        len(filter_excluded),
        len(budget_trimmed),
        total_chars,
        max_chars,
        100.0 * total_chars / max_chars if max_chars else 0.0,
        exceeded,
    )

    return FileDump(
        entries=tuple(entries),
        total_chars=total_chars,
        excluded=tuple(excluded),
        exceeded_budget=exceeded,
        budget=budget,
    )


async def _git_ls_files(root: Path) -> list[str]:
    # `-z` : NUL 구분자로 출력 → 한글/공백/따옴표 등 특수문자 파일명도 C-style escape 없이
    # 원형 그대로 전달된다. 기본 splitlines 경로는 이런 이름을 `"..."` 로 감싸 `Path.is_file()`
    # 검사가 통째로 실패한다.
    proc = await asyncio.create_subprocess_exec(
        "git", "-C", str(root), "ls-files", "-z",
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    if proc.returncode != 0:
        raise RuntimeError(
            f"git ls-files failed: {stderr.decode(errors='replace').strip()}"
        )
    return [name for name in stdout.decode("utf-8", errors="replace").split("\0") if name]


def _sort_by_priority(paths: list[str], changed: set[str]) -> list[str]:
    def rank(path: str) -> tuple[int, str]:
        if path in changed:
            return (0, path)
        top = path.split("/", 1)[0]
        if top in _PRIORITY_DIRS:
            return (1, path)
        return (2, path)

    return sorted(paths, key=rank)


def _should_skip(
    rel_path: str,
    abs_path: Path,
    file_max_bytes: int,
    data_file_max_bytes: int,
) -> bool:
    """Decide whether to exclude a tracked file from the dump.

    필터는 세 단계로 나뉜다 — 아이덴티티(경로/이름) → 확장자 → 크기.
    각 단계가 실패 이유(왜 제외되는지)를 한 군데 모아 두므로 Tier 추가/삭제 시 영향 범위가
    작아진다.
    """
    parts = rel_path.split("/")
    if _is_in_always_skip_dir(parts):
        return True
    name = parts[-1]
    suffix = abs_path.suffix.lower()
    if _is_hard_excluded_name_or_suffix(name, suffix):
        return True
    return _exceeds_size_limit(
        abs_path, name, suffix, file_max_bytes, data_file_max_bytes
    )


def _is_in_always_skip_dir(parts: list[str]) -> bool:
    if any(p in _ALWAYS_SKIP_DIRS for p in parts):
        return True
    # Xcode asset catalog 은 번들 디렉터리명이 `.xcassets` 로 끝난다. 하위 전체 제외.
    if any(p.endswith(".xcassets") for p in parts):
        return True
    return False


def _is_hard_excluded_name_or_suffix(name: str, suffix: str) -> bool:
    if name in _LOCK_FILENAMES:
        return True
    if suffix in _SKIP_SUFFIXES:
        return True
    if _is_double_suffix_skip(name):
        return True
    return False


def _is_important_config(name: str) -> bool:
    """Known project manifests that must reach the reviewer regardless of size.

    대형 모노레포의 루트 `package.json` 처럼 수백 KB 에 이르는 매니페스트도 리뷰 컨텍스트에
    반드시 포함돼야 한다. 이름 기반 화이트리스트라 실수로 데이터 덤프를 끌어올 위험은 낮다.
    """
    return name in _IMPORTANT_CONFIG_NAMES


def _exceeds_size_limit(
    abs_path: Path,
    name: str,
    suffix: str,
    file_max_bytes: int,
    data_file_max_bytes: int,
) -> bool:
    try:
        size = abs_path.stat().st_size
    except OSError:
        return True

    # 화이트리스트 매니페스트(package.json, tsconfig.json 등)는 두 한도 모두 면제.
    # PR #5 리뷰에서 지적됐듯 `data_file_max_bytes` 만 우회하고 `file_max_bytes` 가 걸리면
    # 큰 모노레포 매니페스트가 여전히 빠져 리뷰 컨텍스트 품질이 떨어진다.
    if _is_important_config(name):
        return False

    # 모호한 데이터형 확장자(JSON/YAML/XML/...)는 더 낮은 상한을 먼저 적용한다.
    if suffix in _AMBIGUOUS_DATA_SUFFIXES and size > data_file_max_bytes:
        return True
    return size > file_max_bytes


def _is_double_suffix_skip(name: str) -> bool:
    lowered = name.lower()
    return any(
        lowered.endswith(s)
        for s in (".min.js", ".min.css", ".d.ts.map", ".min.json")
    )
