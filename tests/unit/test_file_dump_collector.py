import subprocess
from pathlib import Path

import pytest

from codex_review.domain import TokenBudget
from codex_review.infrastructure.file_dump_collector import FileDumpCollector


def _git(cwd: Path, *args: str) -> None:
    subprocess.run(["git", "-C", str(cwd), *args], check=True, capture_output=True)


@pytest.fixture()
def repo(tmp_path: Path) -> Path:
    _git(tmp_path := tmp_path / "repo", "init", "-q") if False else None
    tmp_path.mkdir(parents=True, exist_ok=True)
    subprocess.run(["git", "init", "-q", str(tmp_path)], check=True)
    _git(tmp_path, "config", "user.email", "test@example.com")
    _git(tmp_path, "config", "user.name", "test")

    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "main.py").write_text("print('hi')\n", encoding="utf-8")
    (tmp_path / "README.md").write_text("# hi\n", encoding="utf-8")
    (tmp_path / "node_modules").mkdir()
    (tmp_path / "node_modules" / "ignored.js").write_text("x=1", encoding="utf-8")
    (tmp_path / "package-lock.json").write_text("{}", encoding="utf-8")
    (tmp_path / "logo.png").write_bytes(b"\x89PNGfake")

    _git(tmp_path, "add", "-A")
    _git(tmp_path, "commit", "-q", "-m", "init")
    return tmp_path


def test_collect_filters_skip_dirs_and_binaries(repo: Path) -> None:
    collector = FileDumpCollector(file_max_bytes=1024)
    dump = collector.collect(repo, changed_files=("src/main.py",), budget=TokenBudget(10_000))

    paths = [e.path for e in dump.entries]
    assert "src/main.py" in paths
    assert "README.md" in paths
    assert not any("node_modules" in p for p in paths)
    assert "package-lock.json" not in paths
    assert "logo.png" not in paths


def test_collect_prioritizes_changed_files(repo: Path) -> None:
    collector = FileDumpCollector(file_max_bytes=1024)
    dump = collector.collect(repo, changed_files=("README.md",), budget=TokenBudget(10_000))
    assert dump.entries[0].path == "README.md"
    assert dump.entries[0].is_changed is True


def test_collect_marks_exceeded_when_changed_file_excluded(repo: Path) -> None:
    (repo / "big.py").write_text("x\n" * 5000, encoding="utf-8")
    subprocess.run(["git", "-C", str(repo), "add", "-A"], check=True)
    subprocess.run(
        ["git", "-C", str(repo), "-c", "user.email=t@t", "-c", "user.name=t",
         "commit", "-q", "-m", "big"],
        check=True,
    )

    # Budget is tiny; even prioritized changed file won't fit.
    collector = FileDumpCollector(file_max_bytes=1024 * 1024)
    dump = collector.collect(
        repo, changed_files=("big.py",), budget=TokenBudget(max_tokens=1)
    )
    assert dump.exceeded_budget is True
    assert "big.py" in dump.excluded


def _commit_all(repo: Path) -> None:
    _git(repo, "add", "-A")
    _git(repo, "commit", "-q", "-m", "x")


def test_collect_skips_ios_project_meta(repo: Path) -> None:
    # iOS 프로젝트 메타 파일들이 확장자 기반으로 제외되는지
    (repo / "App.xcodeproj").mkdir()
    (repo / "App.xcodeproj" / "project.pbxproj").write_text("// big", encoding="utf-8")
    (repo / "View.storyboard").write_text("<xml/>", encoding="utf-8")
    (repo / "View.xib").write_text("<xml/>", encoding="utf-8")
    (repo / "ko.lproj").mkdir()
    (repo / "ko.lproj" / "Localizable.strings").write_text('"k" = "v";', encoding="utf-8")
    _commit_all(repo)

    collector = FileDumpCollector(file_max_bytes=1024 * 1024)
    dump = collector.collect(repo, changed_files=(), budget=TokenBudget(10_000))
    paths = [e.path for e in dump.entries]

    assert not any(p.endswith(".pbxproj") for p in paths)
    assert not any(p.endswith(".storyboard") for p in paths)
    assert not any(p.endswith(".xib") for p in paths)
    assert not any(p.endswith(".strings") for p in paths)


def test_collect_skips_ios_build_dirs(repo: Path) -> None:
    for d in ("Pods", "Carthage", ".build", "DerivedData"):
        (repo / d).mkdir()
        (repo / d / "x.swift").write_text("// noise", encoding="utf-8")
    _commit_all(repo)

    collector = FileDumpCollector(file_max_bytes=1024 * 1024)
    dump = collector.collect(repo, changed_files=(), budget=TokenBudget(10_000))
    paths = [e.path for e in dump.entries]

    for d in ("Pods", "Carthage", ".build", "DerivedData"):
        assert not any(p.startswith(f"{d}/") for p in paths)


def test_collect_skips_xcassets_bundle(repo: Path) -> None:
    (repo / "Assets.xcassets").mkdir()
    (repo / "Assets.xcassets" / "Contents.json").write_text("{}", encoding="utf-8")
    (repo / "Assets.xcassets" / "nested").mkdir()
    (repo / "Assets.xcassets" / "nested" / "info.json").write_text("{}", encoding="utf-8")
    _commit_all(repo)

    collector = FileDumpCollector(file_max_bytes=1024 * 1024)
    dump = collector.collect(repo, changed_files=(), budget=TokenBudget(10_000))
    paths = [e.path for e in dump.entries]
    assert not any("Assets.xcassets" in p for p in paths)


def test_collect_skips_snapshots_and_fixtures(repo: Path) -> None:
    (repo / "__snapshots__").mkdir()
    (repo / "__snapshots__" / "App.test.ts.snap").write_text("snap", encoding="utf-8")
    (repo / "tests").mkdir(exist_ok=True)
    (repo / "tests" / "fixtures").mkdir()
    (repo / "tests" / "fixtures" / "data.json").write_text("{}", encoding="utf-8")
    _commit_all(repo)

    collector = FileDumpCollector(file_max_bytes=1024 * 1024)
    dump = collector.collect(repo, changed_files=(), budget=TokenBudget(10_000))
    paths = [e.path for e in dump.entries]
    assert not any("__snapshots__" in p for p in paths)
    assert not any("fixtures" in p for p in paths)


def test_smart_filter_keeps_small_json_and_known_config(repo: Path) -> None:
    # 작은 JSON, 알려진 설정 이름(package.json) — 둘 다 포함돼야 함.
    # 25KB 로 데이터 상한(20KB)은 넘지만 화이트리스트라 통과해야 한다.
    (repo / "small.json").write_text('{"a": 1}', encoding="utf-8")
    (repo / "package.json").write_text('{"name": "x"}' + " " * 25_000, encoding="utf-8")
    # 같은 크기의 일반 JSON — 데이터 상한 초과 → 제외
    (repo / "locales.json").write_text('{"k": "v"}' + " " * 25_000, encoding="utf-8")
    _commit_all(repo)

    # budget 을 넉넉히 잡아 예산 초과가 아닌 data-limit 만 단독 검증
    collector = FileDumpCollector(file_max_bytes=1024 * 1024, data_file_max_bytes=20_000)
    dump = collector.collect(repo, changed_files=(), budget=TokenBudget(1_000_000))
    paths = [e.path for e in dump.entries]

    assert "small.json" in paths
    assert "package.json" in paths  # 화이트리스트 이름은 data_file 크기 제한 무시
    assert "locales.json" not in paths  # 이름 없고 크면 제외


def test_smart_filter_applies_to_yaml_and_xml(repo: Path) -> None:
    (repo / "app.yaml").write_text("k: v\n" + ("x" * 30_000), encoding="utf-8")
    (repo / "config.xml").write_text("<xml/>" + ("x" * 30_000), encoding="utf-8")
    _commit_all(repo)

    collector = FileDumpCollector(file_max_bytes=1024 * 1024, data_file_max_bytes=20_000)
    dump = collector.collect(repo, changed_files=(), budget=TokenBudget(100_000))
    paths = [e.path for e in dump.entries]

    # 모호한 확장자라서 데이터 상한에 걸림
    assert "app.yaml" not in paths
    assert "config.xml" not in paths


def test_min_json_is_skipped(repo: Path) -> None:
    (repo / "dict.min.json").write_text('{"a":1}', encoding="utf-8")
    _commit_all(repo)

    collector = FileDumpCollector(file_max_bytes=1024 * 1024)
    dump = collector.collect(repo, changed_files=(), budget=TokenBudget(10_000))
    assert "dict.min.json" not in [e.path for e in dump.entries]


def test_package_resolved_is_skipped(repo: Path) -> None:
    (repo / "Package.resolved").write_text("{}", encoding="utf-8")
    _commit_all(repo)

    collector = FileDumpCollector(file_max_bytes=1024 * 1024)
    dump = collector.collect(repo, changed_files=(), budget=TokenBudget(10_000))
    assert "Package.resolved" not in [e.path for e in dump.entries]
