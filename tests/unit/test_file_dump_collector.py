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
