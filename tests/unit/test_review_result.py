from codex_review.domain import Finding, ReviewEvent, ReviewResult


def test_render_body_includes_three_sections() -> None:
    result = ReviewResult(
        summary="요약입니다.",
        event=ReviewEvent.COMMENT,
        positives=("Protocol 기반 DIP",),
        improvements=("계층 경계 강화",),
        findings=(Finding(path="a.py", line=1, body="functools.cache를 고려하세요."),),
    )
    body = result.render_body()
    assert body.startswith("요약입니다.")
    assert "**좋은 점**" in body
    assert "- Protocol 기반 DIP" in body
    assert "**개선할 점**" in body
    assert "- 계층 경계 강화" in body
    assert "기술 단위 코멘트 1건" in body


def test_render_body_omits_empty_sections() -> None:
    result = ReviewResult(summary="요약", event=ReviewEvent.COMMENT)
    body = result.render_body()
    assert body == "요약"


def test_render_body_without_findings_does_not_mention_inline_comments() -> None:
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        positives=("좋음",),
    )
    body = result.render_body()
    assert "기술 단위 코멘트" not in body


def test_render_body_does_not_include_model_footer() -> None:
    result = ReviewResult(summary="요약", event=ReviewEvent.COMMENT)
    body = result.render_body()
    assert "리뷰 모델" not in body
    assert "<sub>" not in body
