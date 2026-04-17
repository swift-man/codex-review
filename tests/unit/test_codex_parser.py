from codex_review.domain import ReviewEvent
from codex_review.infrastructure.codex_parser import parse_review


def test_parse_strict_json() -> None:
    raw = """
    {
      "summary": "전반적으로 구조가 깔끔합니다.",
      "event": "COMMENT",
      "positives": ["Protocol을 통한 DIP 적용"],
      "improvements": ["도메인 계층과 인프라 계층의 경계를 더 명확히"],
      "comments": [
        {"path": "src/a.py", "line": 12, "body": "pathlib.Path를 사용하면 경로 조합이 안전해집니다."}
      ]
    }
    """
    result = parse_review(raw)
    assert result.summary.startswith("전반적으로")
    assert result.event == ReviewEvent.COMMENT
    assert result.positives == ("Protocol을 통한 DIP 적용",)
    assert result.improvements == ("도메인 계층과 인프라 계층의 경계를 더 명확히",)
    assert len(result.findings) == 1
    assert result.findings[0].path == "src/a.py"
    assert result.findings[0].line == 12


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
