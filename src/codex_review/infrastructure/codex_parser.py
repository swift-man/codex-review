import json
import logging
import re

from codex_review.domain import Finding, ReviewEvent, ReviewResult

logger = logging.getLogger(__name__)

_JSON_BLOCK = re.compile(r"\{(?:[^{}]|\{[^{}]*\})*\}", re.DOTALL)


def parse_review(raw: str) -> ReviewResult:
    payload = _extract_json(raw)
    if payload is None:
        logger.warning("codex output did not contain JSON; falling back to plain text")
        return ReviewResult(
            summary=raw.strip()[:4000] or "Codex 응답을 파싱하지 못했습니다.",
            event=ReviewEvent.COMMENT,
        )

    event = _parse_event(payload.get("event"))
    findings = tuple(_parse_findings(payload.get("comments")))

    return ReviewResult(
        summary=str(payload.get("summary", "")).strip() or "요약 없음",
        event=event,
        positives=tuple(_as_str_list(payload.get("positives"))),
        improvements=tuple(_as_str_list(payload.get("improvements"))),
        findings=findings,
    )


def _extract_json(text: str) -> dict[str, object] | None:
    stripped = text.strip()
    if stripped.startswith("{"):
        try:
            return json.loads(stripped)
        except json.JSONDecodeError:
            pass

    # Prefer the LAST complete JSON object (Codex may emit reasoning first).
    candidates = _JSON_BLOCK.findall(text)
    for candidate in reversed(candidates):
        try:
            data = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(data, dict) and "summary" in data:
            return data
    return None


def _parse_event(value: object) -> ReviewEvent:
    if isinstance(value, str):
        upper = value.strip().upper()
        if upper in ReviewEvent.__members__:
            return ReviewEvent[upper]
    return ReviewEvent.COMMENT


def _parse_findings(raw: object) -> list[Finding]:
    if not isinstance(raw, list):
        return []
    out: list[Finding] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        path = str(item.get("path", "")).strip()
        body = str(item.get("body", "")).strip()
        line = _coerce_line(item.get("line"))
        if not path or not body or line is None:
            # A finding without a concrete line number cannot be attached
            # inline, so we drop it per product spec.
            continue
        out.append(Finding(path=path, line=line, body=body))
    return out


def _coerce_line(value: object) -> int | None:
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.isdigit():
        n = int(value)
        return n if n > 0 else None
    return None


def _as_str_list(value: object) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(v).strip() for v in value if str(v).strip()]
