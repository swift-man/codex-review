from pathlib import Path

from codex_review.domain import ReviewPathFilter
from codex_review.infrastructure.reviewbot_config import load_review_path_filter


def test_review_path_filter_supports_globstar_and_always_review() -> None:
    path_filter = ReviewPathFilter(
        include=("**/*.swift", "Package.swift", "**/Info.plist"),
        exclude=("**/Generated/**", "**/*.md"),
        always_review=("AGENTS.md",),
    )

    assert path_filter.allows("App.swift")
    assert path_filter.allows("Sources/App/View.swift")
    assert path_filter.allows("Package.swift")
    assert path_filter.allows("App/Info.plist")
    assert path_filter.allows("AGENTS.md")

    assert not path_filter.allows("Sources/Generated/API.swift")
    assert not path_filter.allows("README.md")
    assert not path_filter.allows("scripts/build.sh")


def test_load_reviewbot_config_adds_builtin_config_file_override(tmp_path: Path) -> None:
    (tmp_path / ".reviewbot.yml").write_text(
        """
version: 1
review:
  include:
    - "**/*.swift"
  exclude:
    - "**/*.md"
  always_review:
    - "AGENTS.md"
""",
        encoding="utf-8",
    )

    path_filter = load_review_path_filter(tmp_path)

    assert path_filter.allows(".reviewbot.yml")
    assert path_filter.allows("AGENTS.md")
    assert path_filter.allows("Sources/App.swift")
    assert not path_filter.allows("README.md")


def test_load_reviewbot_config_fails_open_on_invalid_version(tmp_path: Path) -> None:
    (tmp_path / ".reviewbot.yml").write_text(
        """
version: 2
review:
  include:
    - "**/*.swift"
  exclude:
    - "**/*.md"
""",
        encoding="utf-8",
    )

    path_filter = load_review_path_filter(tmp_path)

    assert path_filter.allows("README.md")
    assert path_filter.allows("scripts/build.sh")


def test_load_reviewbot_config_allows_exclude_only_config(tmp_path: Path) -> None:
    (tmp_path / ".reviewbot.yml").write_text(
        """
version: 1
review:
  exclude:
    - "**/*.md"
""",
        encoding="utf-8",
    )

    path_filter = load_review_path_filter(tmp_path)

    assert not path_filter.allows("README.md")
    assert path_filter.allows("Sources/App.swift")
