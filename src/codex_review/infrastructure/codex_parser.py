import contextlib
import json
import logging
import re

from codex_review.domain import Finding, ReviewEvent, ReviewResult
from codex_review.domain.finding import (
    SEVERITY_CRITICAL,
    SEVERITY_MINOR,
    SEVERITY_SUGGESTION,
    VALID_SEVERITIES,
)

# 레거시 값 → 새 4단계 매핑. 이전 프롬프트가 "must_fix"/"suggest" 만 쓰던 시기의
# 응답도 무해하게 받아들이기 위함 — 신규 프롬프트 배포 직후 쌓여 있던 작업 큐 방어.
_LEGACY_SEVERITY_ALIASES: dict[str, str] = {
    "must_fix": SEVERITY_CRITICAL,
    "mustfix": SEVERITY_CRITICAL,
    "must-fix": SEVERITY_CRITICAL,
    "blocker": SEVERITY_CRITICAL,
    "suggest": SEVERITY_SUGGESTION,
    "nit": SEVERITY_MINOR,
    "nitpick": SEVERITY_MINOR,
    "info": SEVERITY_SUGGESTION,
}

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
    must_fix = tuple(_as_str_list(payload.get("must_fix")))

    # 모델 프롬프트에 `critical/major → REQUEST_CHANGES` 규칙을 명시했지만 LLM 이 가끔
    # COMMENT 로 내려보낸다 (자기 확신 부족·프롬프트 경합). 차단 신호가 **어떤 형태로든**
    # 있으면 이벤트를 승격해 병합 차단 효과를 살려야 한다.
    #   1) 인라인 findings 에 critical/major 가 하나라도 있음 (is_blocking).
    #   2) 파일/모듈 단위 `must_fix` 섹션에 항목이 있음 — 라인 고정이 애매해 인라인으로
    #      못 달았을 뿐 "반드시 수정" 의도임. 프롬프트 규칙상 이 자체로 REQUEST_CHANGES.
    # APPROVE 는 모순 상태를 그대로 노출하도록 두 경우 모두 덮어쓰지 않는다.
    has_blocking_signal = any(f.is_blocking for f in findings) or bool(must_fix)
    if event == ReviewEvent.COMMENT and has_blocking_signal:
        event = ReviewEvent.REQUEST_CHANGES

    return ReviewResult(
        summary=str(payload.get("summary", "")).strip() or "요약 없음",
        event=event,
        positives=tuple(_as_str_list(payload.get("positives"))),
        must_fix=must_fix,
        improvements=tuple(_as_str_list(payload.get("improvements"))),
        findings=findings,
    )


def _extract_json(text: str) -> dict[str, object] | None:
    stripped = text.strip()
    if stripped.startswith("{"):
        # 통째로 JSON 이면 그대로 사용. 파싱 실패는 "JSON 아닐 수 있다" 는 정상 신호이므로 의도적으로 무시.
        with contextlib.suppress(json.JSONDecodeError):
            return json.loads(stripped)

    # Codex agentic 실행은 "추론 → 최종 답" 순서로 여러 JSON 조각을 내뱉을 수 있다.
    # 예: 중간에 `{"note": "..."}` 같은 로그 성격의 JSON 이 섞여도 최종 리뷰 JSON 은 맨 뒤.
    # 따라서 뒤에서부터 훑으며 "summary" 키를 가진 첫 후보를 리뷰 결과로 채택한다.
    candidates = _JSON_BLOCK.findall(text)
    for candidate in reversed(candidates):
        # 후보 하나가 JSON 이 아니면 다음 후보로 넘어간다 — JSONDecodeError 는 의도적으로 삼킨다.
        with contextlib.suppress(json.JSONDecodeError):
            data = json.loads(candidate)
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
        # 라인 번호가 없는 지적은 PR 인라인 코멘트로 붙을 수 없다. 제품 스펙상 "라인 고정 기술 단위
        # 코멘트"만 인라인 대상이며, 나머지 거시적 지적은 improvements/must_fix 섹션으로 모델이 분류해야 한다.
        if not path or not body or line is None:
            continue
        severity = _coerce_severity(item.get("severity"))
        out.append(Finding(path=path, line=line, body=body, severity=severity))
    return out


def _coerce_severity(value: object) -> str:
    """4단계 등급 중 하나로 변환. 모르는 값/레거시 값/비문자열은 안전한 기본값으로 강등.

    - 공백·대소문자·하이픈/언더스코어 차이는 흡수한다.
    - 이전 스키마의 "must_fix"/"suggest" 등은 `_LEGACY_SEVERITY_ALIASES` 로 승격/매핑.
    - 그 외는 `suggestion` — Finding 생성자에서도 같은 강등을 수행하므로 이중 안전망.
    """
    if not isinstance(value, str):
        return SEVERITY_SUGGESTION
    normalized = value.strip().lower().replace("-", "_")
    if normalized in VALID_SEVERITIES:
        return normalized
    if normalized in _LEGACY_SEVERITY_ALIASES:
        return _LEGACY_SEVERITY_ALIASES[normalized]
    return SEVERITY_SUGGESTION


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
