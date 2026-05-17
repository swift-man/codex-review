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
        {
          "path": "src/a.py",
          "line": 12,
          "severity": "critical",
          "body": "None 체크가 필요합니다."
        },
        {
          "path": "src/a.py",
          "line": 30,
          "severity": "suggestion",
          "body": "pathlib.Path 사용을 고려하세요."
        }
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


def test_parse_repairs_unescaped_quotes_inside_json_strings() -> None:
    """회귀(PR #15): 모델이 Swift range 표현을 JSON 문자열 안에 그대로 넣으면
    `"1.4.0"..<"1.12.0"` 의 따옴표 때문에 JSON 파싱이 깨진다. 이때 raw JSON 을
    댓글 본문으로 흘리지 말고 구조화 리뷰로 복구해야 한다.
    """
    positive = (
        "라이브러리 소비 프로젝트에서도 적용되는 Package.swift 의존성 선언에 직접 "
        "상한을 둔 점이 좋습니다."
    )
    improvement = (
        "향후 swift-dependencies resolver 문제가 해소되면 상한 <1.12.0을 "
        "재검토해도 좋습니다."
    )
    raw = f"""
    {{
      "summary": "Package.swift의 "1.4.0"..<"1.12.0" 범위 지정으로 처리된 것으로 보입니다.",
      "event": "APPROVE",
      "positives": [
        "{positive}"
      ],
      "must_fix": [],
      "improvements": [
        "{improvement}"
      ],
      "comments": [],
      "meta_replies": []
    }}
    """
    result = parse_review(raw)

    assert result.event == ReviewEvent.APPROVE
    assert result.summary == (
        'Package.swift의 "1.4.0"..<"1.12.0" 범위 지정으로 처리된 것으로 보입니다.'
    )
    assert result.positives == (positive,)
    assert result.must_fix == ()
    assert result.improvements == (improvement,)


def test_parse_repairs_unescaped_quotes_when_reasoning_precedes_json() -> None:
    """회귀(PR #27 리뷰): reasoning prefix가 있으면 먼저 JSON 후보를 잘라야 한다.
    이 단계도 문자열 내부 따옴표가 깨져 있으면 실패할 수 있으므로 suffix 후보를
    복구 파서에 직접 넣어 구조화 리뷰를 보존한다.
    """
    raw = (
        "사고 과정: 먼저 변경 파일을 확인했습니다.\n"
        '{"note": "intermediate"}\n'
        "Final:\n"
        "{\n"
        '  "summary": "prefix가 있는 출력도 "{" marker를 본문으로 보존합니다.",\n'
        '  "event": "APPROVE",\n'
        '  "comments": []\n'
        "}\n"
    )
    result = parse_review(raw)

    assert result.event == ReviewEvent.APPROVE
    assert result.summary == 'prefix가 있는 출력도 "{" marker를 본문으로 보존합니다.'


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
    message = (
        "거래어 감지 정규식에서 경계를 완전히 제거하면서 감사요/회사요 같은 일반 "
        "문장도 거래 안내를 발사할 수 있습니다."
    )
    raw = f"""
    {{
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {{"path": "src/a.py", "line": 7, "severity": "major",
         "body": "{{'severity': 'major', 'message': '{message}'}}"}}
      ]
    }}
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


# ---------------------------------------------------------------------------
# 트리거 키 누락 회귀 (codex PR #20 리뷰)
# ---------------------------------------------------------------------------


def test_parse_unwraps_text_key_dict_repr() -> None:
    """추출 루프는 `text` 도 지원하는데 트리거 정규식이 빠뜨려 아예 정화 시도가
    안 되던 회귀. text 키 단독 dict 도 잡혀야 한다.
    """
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "minor",
         "body": "{\\"text\\": \\"이 변수는 사용되지 않습니다.\\"}"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.findings[0].body == "이 변수는 사용되지 않습니다."


def test_parse_unwraps_detail_key_dict_repr() -> None:
    """`detail` 키도 동일."""
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "minor",
         "body": "{'detail': '경계 조건 누락'}"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.findings[0].body == "경계 조건 누락"


def test_parse_unwraps_outer_comment_dict_with_path_first() -> None:
    """모델이 outer comment 스키마 전체를 body 안에 박은 케이스.
    (`{'path': '...', 'line': 1, 'body': '실제 본문'}`)
    트리거가 `path` 로 시작해도 발동해 inner `body` 가 추출돼야 한다.
    """
    raw_body = (
        "{'path': 'x.py', 'line': 1, 'severity': 'major', "
        "'body': '경계 조건이 무시됩니다.'}"
    )
    raw = f"""
    {{
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {{"path": "x.py", "line": 1, "severity": "major",
         "body": "{raw_body}"}}
      ]
    }}
    """
    result = parse_review(raw)
    body = result.findings[0].body
    assert body == "경계 조건이 무시됩니다."
    # raw outer dict 형태가 본문에 새지 않는다.
    assert "'path'" not in body
    assert "'line'" not in body


def test_parse_unwraps_outer_dict_starting_with_line_key() -> None:
    """outer dict 의 키 순서가 다를 때도 (`line` 먼저) 트리거가 발동해야 한다."""
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "major",
         "body": "{'line': 1, 'path': 'x.py', 'body': '리뷰 본문 텍스트'}"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.findings[0].body == "리뷰 본문 텍스트"


def test_parse_unwraps_pretty_printed_json_with_space_after_brace() -> None:
    """회귀 (codex / gemini / coderabbit PR #20 합의): pretty-printed JSON 은
    `{ "message": ... }` 처럼 여는 중괄호와 첫 키 사이에 공백이 들어간다. 이전
    트리거 정규식 `^\\s*\\{['"]` 은 이 케이스를 놓쳐 정화가 작동하지 않았다.
    """
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "major",
         "body": "{ \\"message\\": \\"공백이 끼워진 pretty JSON\\" }"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.findings[0].body == "공백이 끼워진 pretty JSON"


def test_parse_unwraps_pretty_printed_json_with_newline_indent() -> None:
    """`{\\n  "severity": ... }` 처럼 줄바꿈 + 들여쓰기가 끼워진 형태도 잡아야 한다."""
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "major",
         "body": "{\\n  \\"severity\\": \\"major\\",\\n  \\"message\\": \\"여러 줄 dict\\"\\n}"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.findings[0].body == "여러 줄 dict"


def test_parse_unwraps_double_encoded_dict_repr_recursively() -> None:
    """회귀 (coderabbit PR #20 Major): outer dict 의 `body` 값이 또 다른 dict repr
    문자열인 이중 직렬화 케이스. 한 번만 unwrap 하면 inner dict repr 가 그대로 PR
    인라인 코멘트에 노출된다. 두 단계까지는 자동으로 정화한다.
    """
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "major",
         "body": "{'body': \\"{'message': '진짜 본문 텍스트'}\\"}"}
      ]
    }
    """
    result = parse_review(raw)
    body = result.findings[0].body
    # 두 번 벗긴 결과가 노출돼야 한다.
    assert body == "진짜 본문 텍스트"
    assert "'message'" not in body
    assert "'body'" not in body


def test_parse_double_encoded_with_outer_path_wrapper() -> None:
    """`{'path':..., 'body':"{'message': '...'}"}` 처럼 outer 가 path 시작이고
    inner 가 message 인 케이스도 두 단계 정화로 깨끗한 본문이 노출돼야 한다.
    """
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "major",
         "body": "{'path': 'x.py', 'line': 1, 'body': \\"{'message': '실제 리뷰 메시지'}\\"}"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.findings[0].body == "실제 리뷰 메시지"


def test_parse_recursive_sanitize_respects_depth_limit() -> None:
    """비정상적으로 깊게 중첩된 dict repr 은 무한 재귀 대신 안내 문구로 감싸 보호."""
    # 4단계 초과 깊이로 손상된 모델 출력 시뮬레이션.
    deep = "'마지막'"
    for _ in range(6):
        deep = "{'message': " + repr(deep) + "}"
    raw_payload = {
        "summary": "ok",
        "event": "COMMENT",
        "comments": [
            {"path": "x.py", "line": 1, "severity": "minor", "body": deep},
        ],
    }
    import json as _json
    result = parse_review(_json.dumps(raw_payload))
    body = result.findings[0].body
    # 정상 파싱 끝까지 가지는 못해도 raw dict 가 평문에 그대로 새지는 않아야 한다 —
    # 안내 문구로 감싼 fallback (`추출에 실패` 또는 `깊게 중첩`) 이 등장.
    assert ("실패" in body) or ("중첩" in body)


def test_parse_drops_finding_when_body_is_json_null() -> None:
    """회귀 (gemini PR #20 Minor): 모델이 `"body": null` 을 보낼 때 `str(None)` 으로
    평가돼 "None" 문자열이 그대로 인라인 코멘트가 되는 누출. `or ""` 로 흡수해
    빈 본문은 falsy 체크에서 그대로 drop 되도록.
    """
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "minor", "body": null},
        {"path": "y.py", "line": 2, "severity": "major", "body": "정상 본문"}
      ]
    }
    """
    result = parse_review(raw)
    # null body 는 drop, 정상 본문만 통과.
    assert len(result.findings) == 1
    assert result.findings[0].path == "y.py"
    assert result.findings[0].body == "정상 본문"


def test_parse_does_not_crash_on_recursion_error_during_sanitize(monkeypatch) -> None:
    """회귀 (codex / gemini / coderabbit PR #20 Major): json.loads / ast.literal_eval
    이 비정상적으로 깊게 중첩된 입력에서 RecursionError 를 던질 수 있다. suppress
    에 잡혀 있지 않으면 parse_review 전체가 크래시해 리뷰 게시가 중단된다.

    `ast.literal_eval` 을 강제로 RecursionError 를 던지게 만들어 _sanitize_body 가
    예외를 흘리지 않고 원본 유지 fallback 으로 수렴하는지 검증. (`json.loads` 도 동일
    suppress 가 추가됐지만, 그 경로는 outer `_extract_json` 까지 같이 잡혀 시나리오
    구분이 어렵다 — 여기서는 ast.literal_eval 경로만 정밀 검증한다.)
    """
    from codex_review.infrastructure import codex_parser as _parser

    def boom(_s):
        raise RecursionError("simulated stack overflow during literal_eval")

    monkeypatch.setattr(_parser.ast, "literal_eval", boom)

    # body 값이 JSON 으로는 파싱되지 않고 (싱글 쿼터 → JSONDecodeError) ast.literal_eval
    # 로만 시도되는 형태. 본문은 dict repr 모양이라 trigger regex 가 발동.
    raw = """
    {
      "summary": "ok",
      "event": "COMMENT",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "minor",
         "body": "{'severity': 'minor', 'message': '실제 본문'}"}
      ]
    }
    """
    # parse_review 자체가 예외 없이 끝나야 한다.
    result = parse_review(raw)

    # 정화는 실패했지만 (RecursionError suppressed → parsed 가 dict 가 아님 → 원본 반환)
    # 예외가 새지 않고 finding 이 정상적으로 생성된다.
    assert len(result.findings) == 1


# ---------------------------------------------------------------------------
# Section 단위 dict-repr 누출 회귀 — Today-s-Fortune PR #3 실 사고
# ---------------------------------------------------------------------------


def test_parse_unwraps_dict_repr_inside_improvements_array() -> None:
    """회귀 (Today-s-Fortune#3 실 사고): 모델이 `improvements` 배열 한 항목으로
    `{'severity': 'major', 'message': '...'}` 를 박아 PR 본문 권장 개선 섹션에 raw
    dict 가 그대로 노출된 사례. comments[].body 정화만으로는 못 막음 — 본문 섹션
    리스트도 모두 정화 대상으로 포함.
    """
    message = "저장 동작이 비동기 begin 호출에서 동기 runModal 호출로 바뀌었다."
    raw = f"""
    {{
      "summary": "ok",
      "event": "COMMENT",
      "improvements": [
        "{{'severity': 'major', 'message': '{message}'}}"
      ]
    }}
    """
    result = parse_review(raw)
    assert len(result.improvements) == 1
    item = result.improvements[0]
    assert item.startswith("저장 동작이")
    assert "'severity'" not in item
    assert "'message'" not in item


def test_parse_unwraps_dict_repr_inside_must_fix_and_positives() -> None:
    """`must_fix` 와 `positives` 도 동일하게 정화 대상."""
    raw = """
    {
      "summary": "ok",
      "event": "REQUEST_CHANGES",
      "positives": [
        "{'message': '도메인 분리 깔끔'}"
      ],
      "must_fix": [
        "{'severity': 'critical', 'message': '인증 누락'}"
      ]
    }
    """
    result = parse_review(raw)
    assert result.positives == ("도메인 분리 깔끔",)
    assert result.must_fix == ("인증 누락",)


def test_parse_unwraps_dict_repr_inside_summary() -> None:
    """summary 가 통째로 dict repr 인 극단 케이스도 메시지만 추출."""
    raw = """
    {
      "summary": "{'severity': 'major', 'message': '전반적으로 견고하지만 테스트가 부족.'}",
      "event": "COMMENT"
    }
    """
    result = parse_review(raw)
    assert result.summary == "전반적으로 견고하지만 테스트가 부족."


def test_parse_preserves_normal_prose_in_sections() -> None:
    """평문 섹션 항목은 절대 건드리지 않는다 (false-positive 방지)."""
    raw = """
    {
      "summary": "전반적으로 깔끔합니다.",
      "event": "COMMENT",
      "positives": ["Protocol 패턴 적용", "테스트 커버리지 좋음"],
      "improvements": ["문서화 보강 권장"]
    }
    """
    result = parse_review(raw)
    assert result.summary == "전반적으로 깔끔합니다."
    assert result.positives == ("Protocol 패턴 적용", "테스트 커버리지 좋음")
    assert result.improvements == ("문서화 보강 권장",)


# ---------------------------------------------------------------------------
# LGTM with nits 정책 — minor/suggestion 만 있을 때 모델 APPROVE 보존
# ---------------------------------------------------------------------------


def test_parse_preserves_approve_when_only_minor_findings_present() -> None:
    """회귀 (PR #23 정책 변경, LGTM with nits): 모델이 critical/major/must_fix
    없이 minor/suggestion 만 남기고 명시적 APPROVE 를 반환하면 parser 가 그대로
    보존한다. is_blocking 신호가 없으므로 강제 승격 로직이 발동하지 않는지 확인.
    """
    raw = """
    {
      "summary": "전반적으로 깔끔. 사소한 nits 만 남음.",
      "event": "APPROVE",
      "improvements": ["네이밍 일관성 보강"],
      "comments": [
        {"path": "x.py", "line": 1, "severity": "minor", "body": "변수명 모호"},
        {"path": "x.py", "line": 5, "severity": "suggestion", "body": "리팩터 아이디어"}
      ]
    }
    """
    result = parse_review(raw)
    # 핵심 계약: minor/suggestion 만 있으면 모델의 APPROVE 의사를 존중.
    assert result.event == ReviewEvent.APPROVE
    assert len(result.findings) == 2
    # 본문 섹션에 nits 가 명시적으로 남아 있어야 — APPROVE 라고 nits 가 사라지면 안 됨.
    assert result.improvements == ("네이밍 일관성 보강",)


def test_parse_preserves_approve_with_only_suggestion_findings() -> None:
    """suggestion 만 있는 케이스도 동일하게 APPROVE 보존."""
    raw = """
    {
      "summary": "ok",
      "event": "APPROVE",
      "comments": [
        {"path": "x.py", "line": 1, "severity": "suggestion", "body": "대안 제안"}
      ]
    }
    """
    assert parse_review(raw).event == ReviewEvent.APPROVE


def test_parse_preserves_approve_with_zero_findings() -> None:
    """기존 동작 회귀 — 지적이 0건이고 APPROVE 면 그대로 통과."""
    raw = """
    {
      "summary": "이슈 없음",
      "event": "APPROVE"
    }
    """
    assert parse_review(raw).event == ReviewEvent.APPROVE


# ---------------------------------------------------------------------------
# meta_replies 파싱 — 다른 봇 inline comment 에 대한 대댓글
# ---------------------------------------------------------------------------


def test_parse_meta_replies_extracts_single_item() -> None:
    """모델이 산출한 메타리플라이 1건이 도메인 객체로 추출되는지."""
    raw = """
    {
      "summary": "ok",
      "event": "APPROVE",
      "meta_replies": [
        {"reply_to_comment_id": 12345, "body": "다른 봇 phantom 지적 — 실제 코드에 그 패턴 없음."}
      ]
    }
    """
    result = parse_review(raw)
    assert len(result.meta_replies) == 1
    m = result.meta_replies[0]
    assert m.reply_to_comment_id == 12345
    assert "phantom" in m.body


def test_parse_meta_replies_caps_at_one_when_model_oversharing() -> None:
    """노이즈 차단을 위해 모델이 여러 건 보내도 첫 항목 1건만 채택."""
    raw = """
    {
      "summary": "ok",
      "event": "APPROVE",
      "meta_replies": [
        {"reply_to_comment_id": 1, "body": "동의"},
        {"reply_to_comment_id": 2, "body": "또 다른 동의"},
        {"reply_to_comment_id": 3, "body": "세 번째"}
      ]
    }
    """
    result = parse_review(raw)
    assert len(result.meta_replies) == 1
    assert result.meta_replies[0].reply_to_comment_id == 1


def test_parse_meta_replies_skips_invalid_items() -> None:
    """comment_id 누락 / 음수 / body 빈 문자열은 스킵."""
    raw = """
    {
      "summary": "ok",
      "event": "APPROVE",
      "meta_replies": [
        {"reply_to_comment_id": "abc", "body": "string id 무효"},
        {"reply_to_comment_id": -1, "body": "음수 무효"},
        {"reply_to_comment_id": 99, "body": ""},
        {"reply_to_comment_id": 100, "body": "정상"}
      ]
    }
    """
    result = parse_review(raw)
    assert len(result.meta_replies) == 1
    assert result.meta_replies[0].reply_to_comment_id == 100


def test_parse_meta_replies_empty_when_field_missing() -> None:
    """meta_replies 필드가 없으면 빈 튜플 — 첫 리뷰 호환성."""
    raw = """
    {
      "summary": "ok",
      "event": "APPROVE"
    }
    """
    assert parse_review(raw).meta_replies == ()


def test_parse_meta_replies_sanitizes_dict_repr_in_body() -> None:
    """body 가 dict repr 이면 _sanitize_body 가 통과 — 누출 차단 일관성 유지."""
    raw = """
    {
      "summary": "ok",
      "event": "APPROVE",
      "meta_replies": [
        {"reply_to_comment_id": 99, "body": "{'message': '실제 응답 텍스트'}"}
      ]
    }
    """
    result = parse_review(raw)
    assert len(result.meta_replies) == 1
    assert result.meta_replies[0].body == "실제 응답 텍스트"


def test_parse_meta_replies_coerces_string_id_to_int() -> None:
    """회귀 (gemini PR #24 후속 라운드 Major): LLM 이 JSON 숫자를 문자열로 환각해도
    (예: "12345") `_coerce_line` 패턴으로 string→int coerce 해 valid 응답 유실 방지.
    """
    raw = """
    {
      "summary": "ok",
      "event": "APPROVE",
      "meta_replies": [
        {"reply_to_comment_id": "12345", "body": "string-as-int 환각도 통과"}
      ]
    }
    """
    result = parse_review(raw)
    assert len(result.meta_replies) == 1
    assert result.meta_replies[0].reply_to_comment_id == 12345


def test_parse_meta_replies_rejects_non_digit_string_id() -> None:
    """isdigit() 통과 못 하는 string 은 여전히 drop — 환각 / 변조 방어."""
    raw = """
    {
      "summary": "ok",
      "event": "APPROVE",
      "meta_replies": [
        {"reply_to_comment_id": "abc12345", "body": "비숫자 string drop"},
        {"reply_to_comment_id": "0", "body": "0 / 음수 drop (양수만 허용)"}
      ]
    }
    """
    result = parse_review(raw)
    assert result.meta_replies == ()


def test_extract_json_handles_deeply_nested_braces_in_string() -> None:
    """회귀 (gemini PR #24 후속 라운드 Major): 모델 본문 코드 스니펫이 다단 중첩
    중괄호를 포함해도 brace-counting 파서가 균형 매칭으로 outer JSON 을 정확히 추출.
    이전 정규식 (`\\{(?:[^{}]|\\{[^{}]*\\})*\\}`) 은 1단계 중첩까지만 허용해 다단 중첩
    시 매칭 실패로 응답 전체가 plain text fallback 으로 강등됐다.
    """
    raw = (
        "사고 과정:\n"
        '아래 코드를 보세요: function() { return { foo: { bar: 1 } }; }\n'
        '\nFinal:\n'
        '{"summary": "최종", "event": "COMMENT", '
        '"comments": [{"path": "x.py", "line": 1, "severity": "minor", '
        '"body": "코드 예시: function() { return { foo: { bar: 1 } }; }"}]}'
    )
    result = parse_review(raw)
    # outer JSON 이 정확히 추출 + 파싱돼 finding 1건이 살아남아야 한다.
    assert result.summary == "최종"
    assert len(result.findings) == 1
    assert result.findings[0].path == "x.py"
    # body 안의 nested 중괄호도 보존.
    assert "function()" in result.findings[0].body


def test_extract_json_ignores_braces_inside_json_strings() -> None:
    """JSON string literal 안의 `{` `}` 는 균형 매칭에서 무시돼야 한다 — 그래야 string
    안에 brace 가 있어도 outer 경계가 정확히 잡힌다.
    """
    raw = (
        '추론...\n'
        '{"summary": "여는 중괄호 } 닫는 중괄호 만 있는 string", '
        '"event": "APPROVE"}'
    )
    result = parse_review(raw)
    assert result.event == ReviewEvent.APPROVE
    assert "string" in result.summary


def test_extract_json_handles_escaped_quotes_in_strings() -> None:
    """JSON string 안의 escape 처리된 quote (`\\"`) 는 string 종료가 아니므로 brace
    스캐너가 string 모드에서 빠져 나오면 안 된다.
    """
    raw = '{"summary": "escape \\" 다음 { 무시 }", "event": "COMMENT"}'
    result = parse_review(raw)
    assert result.event == ReviewEvent.COMMENT
    assert "escape" in result.summary
