import pytest

from codex_review.domain import ReviewEvent
from codex_review.domain.finding import (
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    SEVERITY_SUGGESTION,
)
from codex_review.infrastructure.codex_parser import parse_review


def test_parse_strict_json_with_all_sections() -> None:
    raw = """
    {
      "summary": "전반적으로 구조가 깔끔합니다.",
      "event": "REQUEST_CHANGES",
      "positives": ["Protocol을 통한 DIP 적용"],
      "must_fix": ["인증 토큰 캐시 경쟁 조건"],
      "improvements": ["도메인 계층과 인프라 계층의 경계를 더 명확히"],
      "comments": [
        {"path": "src/a.py", "line": 12, "severity": "critical", "body": "None 체크가 필요합니다."},
        {"path": "src/a.py", "line": 30, "severity": "suggestion", "body": "pathlib.Path 사용을 고려하세요."}
      ]
    }
    """
    result = parse_review(raw)
    assert result.summary.startswith("전반적으로")
    assert result.event == ReviewEvent.REQUEST_CHANGES
    assert result.positives == ("Protocol을 통한 DIP 적용",)
    assert result.must_fix == ("인증 토큰 캐시 경쟁 조건",)
    assert result.improvements == ("도메인 계층과 인프라 계층의 경계를 더 명확히",)
    assert len(result.findings) == 2
    assert result.findings[0].severity == SEVERITY_CRITICAL
    assert result.findings[0].is_blocking is True
    assert result.findings[1].severity == SEVERITY_SUGGESTION
    assert result.findings[1].is_blocking is False


def test_parse_accepts_all_four_severities() -> None:
    raw = """
    {
      "summary": "ok",
      "event": "REQUEST_CHANGES",
      "comments": [
        {"path": "a.py", "line": 1, "severity": "critical", "body": "c"},
        {"path": "a.py", "line": 2, "severity": "major", "body": "m"},
        {"path": "a.py", "line": 3, "severity": "minor", "body": "n"},
        {"path": "a.py", "line": 4, "severity": "suggestion", "body": "s"}
      ]
    }
    """
    result = parse_review(raw)
    severities = [f.severity for f in result.findings]
    assert severities == [
        SEVERITY_CRITICAL, SEVERITY_MAJOR, SEVERITY_MINOR, SEVERITY_SUGGESTION
    ]
    # is_blocking 은 Critical/Major 둘 뿐.
    assert [f.is_blocking for f in result.findings] == [True, True, False, False]


def test_parse_missing_severity_defaults_to_suggestion() -> None:
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "src/a.py", "line": 5, "body": "no severity field"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.findings[0].severity == SEVERITY_SUGGESTION


def test_parse_unknown_severity_falls_back_to_suggestion() -> None:
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "src/a.py", "line": 5, "severity": "apocalyptic", "body": "x"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.findings[0].severity == SEVERITY_SUGGESTION


def test_parse_upgrades_comment_to_request_changes_when_blocking_finding_present() -> None:
    """회귀(codex PR #15 피드백): 모델이 `event:"COMMENT"` 로 내려보내도 Critical 또는
    Major 라인 지적이 있으면 자동으로 REQUEST_CHANGES 로 승격. 그러지 않으면 심각한
    지적이 묻혀 "승인 리뷰처럼" 게시되는 사고가 난다.
    """
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "a.py", "line": 1, "severity": "critical", "body": "None 체크 누락"}
      ]
    }
    """
    assert parse_review(raw).event == ReviewEvent.REQUEST_CHANGES

    raw_major = raw.replace("critical", "major")
    assert parse_review(raw_major).event == ReviewEvent.REQUEST_CHANGES


def test_parse_upgrades_comment_to_request_changes_when_must_fix_section_present() -> None:
    """회귀 (codex PR #17 Major): 모델이 `must_fix` 섹션에 "반드시 수정" 항목을
    넣고도 인라인 `comments` 에는 해당 라인을 특정 못해 안 넣는 경우가 있다. 이때도
    이벤트는 REQUEST_CHANGES 로 승격돼야 병합 차단 신호가 살아난다.

    이전 구현은 `findings.is_blocking` 만 봐서, `must_fix` 섹션만 있는 경우 COMMENT
    로 새어 나갔음 — 리뷰어가 본문 안 보고 merge 해버릴 위험.
    """
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "must_fix": ["핵심 보안 결함 — 토큰 로깅 경로 확인 필요"],
      "comments": []
    }
    """
    assert parse_review(raw).event == ReviewEvent.REQUEST_CHANGES


def test_parse_keeps_comment_event_when_only_minor_or_suggestion() -> None:
    """비차단 등급만 있을 때는 event 를 건드리지 않는다."""
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "a.py", "line": 1, "severity": "minor", "body": "네이밍"},
        {"path": "a.py", "line": 2, "severity": "suggestion", "body": "대안"}
      ]
    }
    """
    assert parse_review(raw).event == ReviewEvent.COMMENT


def test_parse_overrides_approve_when_blocking_finding_present(
    caplog: pytest.LogCaptureFixture,
) -> None:
    """회귀 (codex PR #17 Major): 모델이 APPROVE + critical finding 을 함께 반환하면
    내부 모순이지만, safety first — event 를 REQUEST_CHANGES 로 덮어써 병합 차단 신호
    를 살린다. GitHub 가 봇 APPROVE 를 승인 카운트로 집계해 심각한 지적이 있음에도
    병합이 뚫리는 위험을 방지. 모순은 로그로만 노출.
    """
    raw = """
    {
      "summary": "ok",
      "event": "APPROVE",
      "comments": [
        {"path": "a.py", "line": 1, "severity": "critical", "body": "x"}
      ]
    }
    """
    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger="codex_review.infrastructure.codex_parser"):
        result = parse_review(raw)
    assert result.event == ReviewEvent.REQUEST_CHANGES
    # 모순 로그가 남아야 운영자가 "모델이 이상한 응답을 냈다" 는 걸 추후 추적 가능.
    assert any("APPROVE" in rec.getMessage() for rec in caplog.records)


def test_parse_overrides_approve_when_must_fix_section_present() -> None:
    """APPROVE + must_fix 섹션 조합도 동일하게 승격."""
    raw = """
    {
      "summary": "ok",
      "event": "APPROVE",
      "must_fix": ["심각한 보안 이슈"]
    }
    """
    assert parse_review(raw).event == ReviewEvent.REQUEST_CHANGES


def test_parse_keeps_approve_when_no_blocking_signal() -> None:
    """차단 신호가 없으면 APPROVE 는 그대로 유지 (회귀 방지)."""
    raw = """
    {
      "summary": "clean",
      "event": "APPROVE",
      "comments": [
        {"path": "a.py", "line": 1, "severity": "suggestion", "body": "nit"}
      ]
    }
    """
    assert parse_review(raw).event == ReviewEvent.APPROVE


def test_parse_legacy_must_fix_alias_normalizes_to_critical() -> None:
    """전환기 호환: 이전 프롬프트의 `must_fix`/`suggest`/`nit` 도 새 등급으로 흡수한다."""
    raw = """
    {
      "summary": "ok",
      "event": "REQUEST_CHANGES",
      "comments": [
        {"path": "a.py", "line": 1, "severity": "Must-Fix", "body": "x"},
        {"path": "a.py", "line": 2, "severity": "suggest", "body": "y"},
        {"path": "a.py", "line": 3, "severity": "nit", "body": "z"}
      ]
    }
    """
    result = parse_review(raw)
    assert [f.severity for f in result.findings] == [
        SEVERITY_CRITICAL, SEVERITY_SUGGESTION, SEVERITY_MINOR
    ]


def test_parse_missing_must_fix_field_defaults_to_empty() -> None:
    raw = """
    {"summary": "ok", "event": "COMMENT"}
    """
    result = parse_review(raw)
    assert result.must_fix == ()


def test_parse_picks_last_valid_json_when_reasoning_precedes() -> None:
    raw = (
        "사고 과정: 먼저 파일을 확인...\n"
        '{"note": "intermediate"}\n'
        'Final:\n'
        '{"summary": "최종 리뷰", "event": "REQUEST_CHANGES", "comments": []}'
    )
    result = parse_review(raw)
    assert result.summary == "최종 리뷰"
    assert result.event == ReviewEvent.REQUEST_CHANGES


def test_parse_fallbacks_to_plain_text_when_no_json() -> None:
    result = parse_review("그냥 평문 응답입니다.")
    assert "평문" in result.summary
    assert result.event == ReviewEvent.COMMENT


def test_parse_drops_findings_without_valid_line() -> None:
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "", "line": 1, "body": "empty path"},
        {"path": "src/a.py", "line": "bad", "body": "invalid line"},
        {"path": "src/b.py", "body": "no line — dropped"},
        {"path": "src/c.py", "line": 0, "body": "zero line — dropped"},
        {"path": "src/d.py", "line": 5, "body": "valid"}
      ]
    }
    """
    result = parse_review(raw)
    paths = [f.path for f in result.findings]
    assert paths == ["src/d.py"]
    assert result.findings[0].line == 5


# ---------------------------------------------------------------------------
# body 정화 — 모델이 dict repr 을 박는 실 운영 사고 회귀
# ---------------------------------------------------------------------------


def test_parse_unwraps_python_dict_repr_in_body_into_message() -> None:
    """회귀(실 운영 사고): 모델이 가끔 `body` 안에 또 한 번 Python dict repr 을 박아
    `{'severity': 'major', 'message': '...'}` 라는 raw 문자열이 PR 인라인 코멘트에
    그대로 노출되는 사례. 본문에서 message 만 추출해 자연어로 보이게 정화한다.
    """
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "src/a.py", "line": 7, "severity": "major",
         "body": "{'severity': 'major', 'message': '거래어 감지 정규식에서 경계를 완전히 제거하면서 감사요/회사요 같은 일반 문장도 거래 안내를 발사할 수 있습니다.'}"}
      ]
    }
    """
    result = parse_review(raw)
    assert len(result.findings) == 1
    f = result.findings[0]
    assert f.body.startswith("거래어 감지 정규식")
    # raw dict 형태가 본문에 새지 않는다.
    assert "'severity'" not in f.body
    assert "'message'" not in f.body


def test_parse_unwraps_json_dict_with_message_key() -> None:
    """JSON 형식(쌍따옴표) 으로 박힌 dict 도 동일하게 unwrap."""
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "minor",
         "body": "{\\"severity\\": \\"minor\\", \\"message\\": \\"네이밍이 일관되지 않습니다.\\"}"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.findings[0].body == "네이밍이 일관되지 않습니다."


def test_parse_preserves_inline_dict_quote_in_plain_prose() -> None:
    """평문 본문 안에 짧은 dict 가 인용된 케이스는 unwrap 시도하지 않는다 (false
    positive 방지). 정화는 body 전체가 dict literal 모양일 때만 적용.
    """
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "suggestion",
         "body": "이 옵션은 `{'a': 1}` 처럼 dict 형태로 넘기는 게 직관적입니다."}
      ]
    }
    """
    result = parse_review(raw)
    body = result.findings[0].body
    # 원문이 그대로 보존돼야 한다.
    assert "{'a': 1}" in body
    assert "직관적입니다" in body


def test_parse_falls_back_when_dict_lacks_message_key() -> None:
    """dict 인데 message 류 키가 없으면 raw 를 코드펜스로 감싸 noise 최소화 + 경고 안내."""
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "minor",
         "body": "{'severity': 'minor', 'foo': 'bar'}"}
      ]
    }
    """
    result = parse_review(raw)
    body = result.findings[0].body
    assert "추출에 실패" in body
    assert "```" in body  # 코드펜스로 감싸 시각 노이즈 최소화


def test_parse_keeps_body_unchanged_when_not_dict_shape() -> None:
    """일반 평문 + 코드펜스 포함 본문은 절대 건드리지 않는다 — happy path 보존."""
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "major",
         "body": "문제: ... 영향: ... 제안: ```python\\nuse Path\\n```"}
      ]
    }
    """
    result = parse_review(raw)
    body = result.findings[0].body
    assert body.startswith("문제:")
    assert "```python" in body
