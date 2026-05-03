import ast
import contextlib
import json
import logging
import re

from codex_review.domain import Finding, MetaReply, ReviewEvent, ReviewResult
from codex_review.domain.finding import (
    SEVERITY_CRITICAL,
    SEVERITY_MINOR,
    SEVERITY_SUGGESTION,
    VALID_SEVERITIES,
)

# 메타리플라이는 노이즈 차단을 위해 한 라운드에 1건만 허용. 모델이 더 많이 산출해도
# 첫 항목만 채택하고 로그만 남긴다 (작성자 정책: "대댓글은 1개면 될 것 같다").
_META_REPLY_MAX = 1

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
    # `_sanitize_body` 는 `comments[].body` 외에도 본문 섹션 (`summary` /
    # `positives` / `must_fix` / `improvements`) 어디서든 dict repr 누출 가능 —
    # 실제 운영에서 모델이 `improvements` 배열 한 항목으로 `{'severity':'major',
    # 'message':'...'}` 를 박아 PR 본문에 raw dict 가 노출된 사고 발생.
    # 모든 사람이 읽는 텍스트 필드에 동일하게 적용해 단일 차단선으로 통일.
    must_fix = tuple(_sanitize_body(v) for v in _as_str_list(payload.get("must_fix")))

    # 차단 신호가 **어떤 형태로든** 있으면 이벤트를 `REQUEST_CHANGES` 로 강제 승격해
    # 병합 차단 효과를 살려야 한다.
    #   1) 인라인 findings 에 critical/major 가 하나라도 있음 (is_blocking).
    #   2) 파일/모듈 단위 `must_fix` 섹션에 항목이 있음 — 라인 고정이 애매해 인라인으로
    #      못 달았을 뿐 "반드시 수정" 의도임. 프롬프트 규칙상 이 자체로 REQUEST_CHANGES.
    #
    # 이전 구현은 `COMMENT` 만 승격 대상으로 보고 `APPROVE` 는 "모순 상태 가시화" 목적
    # 으로 그대로 두었으나, 실무적으로 GitHub 가 APPROVE 를 승인 카운트로 집계해 병합
    # 이 뚫리는 위험이 더 크다 (codex PR #17 Major 지적). safety first — event 는 항상
    # 안전한 쪽으로 덮어쓰고, APPROVE 가 덮이는 드문 경우엔 로그로 모순을 노출한다.
    has_blocking_signal = any(f.is_blocking for f in findings) or bool(must_fix)
    if has_blocking_signal and event != ReviewEvent.REQUEST_CHANGES:
        if event == ReviewEvent.APPROVE:
            # 운영자가 "모델이 모순된 응답을 냈다" 는 사실을 로그로라도 인지하도록.
            logger.warning(
                "model returned APPROVE with blocking signal "
                "(findings_blocking=%d, must_fix=%d) — forcing REQUEST_CHANGES",
                sum(1 for f in findings if f.is_blocking), len(must_fix),
            )
        event = ReviewEvent.REQUEST_CHANGES

    return ReviewResult(
        summary=_sanitize_body(str(payload.get("summary") or "").strip()) or "요약 없음",
        event=event,
        positives=tuple(_sanitize_body(v) for v in _as_str_list(payload.get("positives"))),
        must_fix=must_fix,
        improvements=tuple(
            _sanitize_body(v) for v in _as_str_list(payload.get("improvements"))
        ),
        findings=findings,
        meta_replies=tuple(_parse_meta_replies(payload.get("meta_replies"))),
    )


def _parse_meta_replies(raw: object) -> list[MetaReply]:
    """모델이 산출한 `meta_replies` 배열 → `MetaReply` 시퀀스.

    최대 `_META_REPLY_MAX` (=1) 건만 채택. 그 이상은 노이즈 방지를 위해 drop 하고
    로그로만 남긴다. body 가 비어 있거나 comment_id 가 정수가 아닌 항목은 스킵.

    `body` 도 `_sanitize_body` 로 통과시켜 dict-repr 누출을 메타리플라이 경로에서도
    동일하게 차단.
    """
    if not isinstance(raw, list):
        return []
    out: list[MetaReply] = []
    for item in raw:
        if not isinstance(item, dict):
            continue
        comment_id = item.get("reply_to_comment_id")
        if not isinstance(comment_id, int) or comment_id <= 0:
            continue
        body = _sanitize_body(str(item.get("body") or "").strip())
        if not body:
            continue
        out.append(MetaReply(reply_to_comment_id=comment_id, body=body))
    if len(out) > _META_REPLY_MAX:
        logger.info(
            "model returned %d meta_replies — capping to %d (using model's own ordering: first item kept)",
            len(out), _META_REPLY_MAX,
        )
        out = out[:_META_REPLY_MAX]
    return out


def _extract_json(text: str) -> dict[str, object] | None:
    stripped = text.strip()
    if stripped.startswith("{"):
        # 통째로 JSON 이면 그대로 사용. 파싱 실패는 "JSON 아닐 수 있다" 는 정상 신호이므로 의도적으로 무시.
        # `RecursionError` 는 모델이 비정상적으로 깊게 중첩된 출력을 냈을 때 던질 수 있어
        # 함께 잡는다 — 정화 시도가 실패하면 다음 후보로 넘어가고, 마지막엔 None 으로 수렴해
        # parse_review 의 plain-text fallback 경로가 동작하게 한다 (codex / gemini /
        # coderabbit PR #20 Major).
        with contextlib.suppress(json.JSONDecodeError, RecursionError):
            return json.loads(stripped)

    # Codex agentic 실행은 "추론 → 최종 답" 순서로 여러 JSON 조각을 내뱉을 수 있다.
    # 예: 중간에 `{"note": "..."}` 같은 로그 성격의 JSON 이 섞여도 최종 리뷰 JSON 은 맨 뒤.
    # 따라서 뒤에서부터 훑으며 "summary" 키를 가진 첫 후보를 리뷰 결과로 채택한다.
    candidates = _JSON_BLOCK.findall(text)
    for candidate in reversed(candidates):
        # 후보 하나가 JSON 이 아니면 다음 후보로 넘어간다 — JSONDecodeError 는 의도적으로 삼킨다.
        with contextlib.suppress(json.JSONDecodeError, RecursionError):
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
        path = str(item.get("path") or "").strip()
        # `or ""` 로 None 흡수 (gemini PR #20 Minor): 모델이 `"body": null` 을 보내면
        # `item.get("body", "")` 는 키가 존재하므로 default 가 무시되고 `None` 이 반환,
        # `str(None)` = "None" 이 그대로 본문이 돼 PR 코멘트에 "None" 문자열이 노출된다.
        # `or ""` 는 None / 빈 문자열 둘 다 안전한 기본값으로 수렴.
        # 추출 후 한 번 더 정화 (coderabbit PR #20 Major): 모델이 이중 직렬화한
        # `{"body": "{'message': '...'}"}` 케이스를 한 번에 한 단계만 벗기면 inner
        # dict repr 가 그대로 남는다. 깊이 상한으로 무한 재귀 방지.
        body = _sanitize_body(str(item.get("body") or "").strip())
        line = _coerce_line(item.get("line"))
        # 라인 번호가 없는 지적은 PR 인라인 코멘트로 붙을 수 없다. 제품 스펙상 "라인 고정 기술 단위
        # 코멘트"만 인라인 대상이며, 나머지 거시적 지적은 improvements/must_fix 섹션으로 모델이 분류해야 한다.
        if not path or not body or line is None:
            continue
        severity = _coerce_severity(item.get("severity"))
        out.append(Finding(path=path, line=line, body=body, severity=severity))
    return out


# 모델이 `body` 필드 안에 또 한 번 dict / JSON 오브젝트를 박는 패턴을 잡는다.
# 실제 운영에서 본 사례:
#   body = "{'severity': 'major', 'message': '거래어 감지 정규식에서 ...'}"
# 이 raw 가 그대로 PR 인라인 코멘트에 노출돼 리뷰어가 "코드가 깨졌다" 고 오해.
# 프롬프트로도 차단하지만, 모델이 어겼을 때의 defense-in-depth 가 필요하다.
#
# 패턴 매칭 정책:
#   - 평문 안의 짧은 dict 인용 (예: "이 파라미터는 {'a': 1} 처럼 ...") 는 보존해야 한다.
#   - body **전체**가 대략 dict literal 모양일 때만 unwrap 시도 → 평문 사용 시 false
#     positive 가 나오지 않는다.
#
# 트리거 키 집합 (codex 리뷰 PR #20 반영):
#   - 추출 루프가 인정하는 키: message / body / text / detail
#   - 프롬프트가 금지한 outer 스키마 키: severity / path / line / finding
# 두 집합의 합집합을 트리거에 둔다 — 모델이 outer comment dict 전체를 박은
# (`{'path': 'x.py', 'line': 12, 'body': '실제 본문'}`) 경우도 잡아 본문에서 `body` 를
# 추출하도록 보장.
#
# `\{` 와 첫 키 사이의 `\s*` (codex / gemini / coderabbit PR #20 후속): 모델이 pretty-
# print 한 JSON 은 `{ "message": ... }` 또는 `{\n  "severity": ... }` 처럼 여는 중괄호와
# 따옴표 사이에 공백·줄바꿈을 끼워 넣는다. 이 형태도 정화 트리거가 발동하도록 허용.
_DICT_REPR_RE = re.compile(
    r"^\s*\{\s*['\"](?:severity|path|line|message|body|text|detail|finding)['\"]"
)


# 이중 직렬화 보호 상한 (coderabbit PR #20 Major):
#   `{"body": "{'message': '실제 본문'}"}` 처럼 한 번 더 직렬화된 payload 는 outer 만
#   벗기면 inner dict repr 가 그대로 남는다. 추출 후 한 번 더 정화를 적용해 누출을
#   막되, 무한 재귀를 막기 위해 깊이 상한을 둔다. 실 운영에서 2 단계 초과는 본 적
#   없고, 깊이가 그 이상이면 모델 출력이 비정상이라 fallback 분기로 떨어지는 게 안전.
_SANITIZE_MAX_DEPTH = 4


def _sanitize_body(body: str, depth: int = 0) -> str:
    """모델이 `body` 안에 dict repr 을 박았을 때 message 만 추출. 실패 시 원본 유지.

    추출 로직:
      1) body 가 dict literal 시작 패턴(`{'severity': ...`, `{"message": ...` 등) 으로
         시작하는지 확인 — 평문 본문에 dict 가 인용된 경우는 건드리지 않는다.
      2) `ast.literal_eval` 로 안전 파싱 (eval 아님 — 임의 코드 실행 위험 없음).
         JSON 도 시도.
      3) dict 안에 `message` / `body` / `text` 같은 흔한 키가 있으면 그 값을 새 body 로.
      4) 추출된 값이 또 dict repr 모양이면 같은 절차를 한 번 더 적용 (이중 직렬화 보호).
      5) 어느 단계든 실패하면 원본 그대로 — false negative 가 false positive 보다 안전.
    """
    if not _DICT_REPR_RE.match(body):
        return body
    if depth >= _SANITIZE_MAX_DEPTH:
        # 비정상적으로 깊은 중첩 — 모델이 손상된 출력을 낸 신호. 더 벗기지 않고 안전한
        # 안내 문구로 감싼다. 무한 재귀 방어선.
        logger.warning(
            "_sanitize_body hit depth limit %d — wrapping raw (len=%d)",
            _SANITIZE_MAX_DEPTH, len(body),
        )
        return (
            "⚠️ 모델 응답이 비정상적으로 깊게 중첩된 dict 형식으로 도착해 본문 추출에 "
            "실패했습니다. 원본:\n```\n" + body + "\n```"
        )

    parsed: object | None = None
    # JSON 먼저 — 더 엄격하므로 평문 파싱 오류로 떨어질 가능성이 낮다.
    # `RecursionError` 는 비정상적으로 깊게 중첩된 입력에서 두 파서 모두 던질 수 있음
    # (codex / gemini / coderabbit PR #20 Major). 잡지 않으면 리뷰 워커 파이프라인 전체가
    # 크래시해 PR 게시가 중단된다 — 정화 실패는 원본 유지로 수렴해야지 예외로 번지면 안 됨.
    with contextlib.suppress(json.JSONDecodeError, RecursionError):
        parsed = json.loads(body)
    if not isinstance(parsed, dict):
        # Python dict literal (싱글 쿼터, True/False/None) 도 `ast.literal_eval` 로 시도.
        # 임의 코드 실행이 아니라 리터럴만 평가하므로 모델 출력에 부작용 없음.
        with contextlib.suppress(
            ValueError, SyntaxError, MemoryError, TypeError, RecursionError,
        ):
            parsed = ast.literal_eval(body)
    if not isinstance(parsed, dict):
        return body

    for key in ("message", "body", "text", "detail"):
        value = parsed.get(key)
        if isinstance(value, str) and value.strip():
            extracted = value.strip()
            logger.warning(
                "model body contained a dict repr — extracted '%s' field "
                "(len=%d → %d)",
                key, len(body), len(extracted),
            )
            # 추출한 문자열이 또 dict repr 이면 한 번 더 정화 (이중 직렬화 보호).
            return _sanitize_body(extracted, depth + 1)

    # dict 였지만 message-like 키가 없는 경우. raw dict 를 그대로 노출하면 리뷰어가
    # 혼란 → 한국어 안내 문구로 감싸서 fallback.
    logger.warning(
        "model body was a dict without message-like key — wrapping raw repr (len=%d)",
        len(body),
    )
    return (
        "⚠️ 모델 응답이 dict 형식으로 도착해 본문 추출에 실패했습니다. "
        "원본:\n```\n" + body + "\n```"
    )


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
