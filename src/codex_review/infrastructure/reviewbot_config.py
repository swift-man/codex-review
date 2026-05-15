import logging
from pathlib import Path
from typing import cast

import yaml
from yaml.error import YAMLError

from codex_review.domain import ReviewPathFilter

logger = logging.getLogger(__name__)

_CONFIG_NAME = ".reviewbot.yml"
_SUPPORTED_VERSION = 1
_BUILTIN_ALWAYS_REVIEW = (_CONFIG_NAME,)


def load_review_path_filter(root: Path) -> ReviewPathFilter:
    config_path = root / _CONFIG_NAME
    if not config_path.is_file():
        return ReviewPathFilter.allow_all()

    try:
        raw = yaml.safe_load(config_path.read_text(encoding="utf-8"))
    except (OSError, YAMLError) as exc:
        logger.warning("failed to read %s: %s; ignoring review path filter", config_path, exc)
        return ReviewPathFilter.allow_all()

    if not isinstance(raw, dict):
        logger.warning("%s must be a YAML mapping; ignoring review path filter", config_path)
        return ReviewPathFilter.allow_all()

    data = cast("dict[object, object]", raw)
    version = data.get("version")
    if version not in (_SUPPORTED_VERSION, str(_SUPPORTED_VERSION)):
        logger.warning(
            "%s has unsupported version %r; ignoring review path filter",
            config_path,
            version,
        )
        return ReviewPathFilter.allow_all()

    review = data.get("review", {})
    if not isinstance(review, dict):
        logger.warning("%s `review` must be a mapping; ignoring review path filter", config_path)
        return ReviewPathFilter.allow_all()

    review_data = cast("dict[object, object]", review)
    include = _read_pattern_list(review_data, "include", config_path)
    exclude = _read_pattern_list(review_data, "exclude", config_path)
    always_review = _read_pattern_list(review_data, "always_review", config_path)
    if include is None or exclude is None or always_review is None:
        return ReviewPathFilter.allow_all()

    return ReviewPathFilter(
        include=include,
        exclude=exclude,
        always_review=_dedupe(_BUILTIN_ALWAYS_REVIEW + always_review),
    )


def _read_pattern_list(
    data: dict[object, object],
    key: str,
    config_path: Path,
) -> tuple[str, ...] | None:
    if key not in data:
        return ()
    raw = data[key]
    if raw is None:
        return ()
    if not isinstance(raw, list):
        logger.warning("%s `review.%s` must be a list of strings", config_path, key)
        return None

    patterns: list[str] = []
    for value in raw:
        if not isinstance(value, str):
            logger.warning("%s `review.%s` must contain only strings", config_path, key)
            return None
        stripped = value.strip()
        if stripped:
            patterns.append(stripped)
    return tuple(patterns)


def _dedupe(patterns: tuple[str, ...]) -> tuple[str, ...]:
    seen: set[str] = set()
    out: list[str] = []
    for pattern in patterns:
        if pattern in seen:
            continue
        seen.add(pattern)
        out.append(pattern)
    return tuple(out)
