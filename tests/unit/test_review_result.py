from codex_review.domain import Finding, ReviewEvent, ReviewResult
from codex_review.domain.finding import SEVERITY_MUST_FIX, SEVERITY_SUGGEST


def test_render_body_includes_four_sections() -> None:
    result = ReviewResult(
        summary="요약입니다.",
        event=ReviewEvent.REQUEST_CHANGES,
        positives=("Protocol 기반 DIP",),
        must_fix=("경쟁 조건이 있는 캐시 접근",),
        improvements=("계층 경계 강화",),
        findings=(Finding(path="a.py", line=1, body="functools.cache를 고려하세요."),),
    )
    body = result.render_body()
    assert body.startswith("요약입니다.")
    assert "**좋은 점**" in body
    assert "- Protocol 기반 DIP" in body
    assert "**🔴 반드시 수정할 사항**" in body
    assert "- 경쟁 조건이 있는 캐시 접근" in body
    assert "**💡 권장 개선 사항**" in body
    assert "- 계층 경계 강화" in body
    assert "기술 단위 코멘트 1건" in body


def test_render_body_section_order_must_fix_before_improvements() -> None:
    result = ReviewResult(
        summary="s",
        event=ReviewEvent.REQUEST_CHANGES,
        must_fix=("blocker",),
        improvements=("nit",),
    )
    body = result.render_body()
    assert body.index("반드시 수정") < body.index("권장 개선")


def test_render_body_omits_empty_sections() -> None:
    result = ReviewResult(summary="요약", event=ReviewEvent.COMMENT)
    assert result.render_body() == "요약"


def test_render_body_without_findings_does_not_mention_inline_comments() -> None:
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        positives=("좋음",),
    )
    assert "기술 단위 코멘트" not in result.render_body()


def test_render_body_does_not_include_model_footer() -> None:
    """Footer 는 인프라 계층(GitHubAppClient) 에서만 붙인다."""
    result = ReviewResult(summary="요약", event=ReviewEvent.COMMENT)
    body = result.render_body()
    assert "리뷰 모델" not in body
    assert "<sub>" not in body


def test_finding_default_severity_is_suggest() -> None:
    f = Finding(path="a.py", line=1, body="x")
    assert f.severity == SEVERITY_SUGGEST
    assert f.is_must_fix is False


def test_finding_unknown_severity_falls_back_to_suggest() -> None:
    f = Finding(path="a.py", line=1, body="x", severity="critical")
    assert f.severity == SEVERITY_SUGGEST


def test_finding_must_fix_severity_marks_is_must_fix_true() -> None:
    f = Finding(path="a.py", line=1, body="x", severity=SEVERITY_MUST_FIX)
    assert f.is_must_fix is True
