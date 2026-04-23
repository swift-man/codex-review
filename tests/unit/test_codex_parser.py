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


def test_parse_preserves_explicit_approve_even_with_blocking_finding() -> None:
    """모델이 APPROVE 를 단언한 상태에서 Critical 지적이 있으면 내부 모순 —
    서버가 덮어쓰지 않고 그대로 둬서 운영자/리뷰어가 비일관을 감지할 수 있게 한다.
    (COMMENT 만 자동 승격 대상)"""
    raw = """
    {
      "summary": "ok",
      "event": "APPROVE",
      "comments": [
        {"path": "a.py", "line": 1, "severity": "critical", "body": "x"}
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
