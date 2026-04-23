from codex_review.domain import Finding, ReviewEvent, ReviewResult
from codex_review.domain.finding import (
    SEVERITY_CRITICAL,
    SEVERITY_MAJOR,
    SEVERITY_MINOR,
    SEVERITY_SUGGESTION,
)


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


def test_render_body_without_dropped_findings_omits_collapsible_section() -> None:
    """기본 경로 회귀: dropped_findings 가 비어 있으면 본문에 접이식 섹션이 없어야 한다."""
    result = ReviewResult(summary="요약", event=ReviewEvent.COMMENT)
    body = result.render_body()
    assert "<details>" not in body
    assert "인라인 게시에서 제외" not in body


def test_render_body_with_dropped_findings_emits_collapsible_details() -> None:
    """dropped_findings 가 있으면 접이식 `<details>` 섹션으로 라인·등급·원문을 보존."""
    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        dropped_findings=(
            Finding(path="a.py", line=42, body="보존돼야 할 지적"),
        ),
    )
    body = result.render_body()
    assert "<details>" in body
    assert "</details>" in body
    assert "인라인 게시에서 제외된 지적 1건" in body
    assert "`a.py:42`" in body
    assert "보존돼야 할 지적" in body
    assert "[Suggestion]" in body  # 기본 severity 라벨


def test_render_body_does_not_include_model_footer() -> None:
    """Footer 는 인프라 계층(GitHubAppClient) 에서만 붙인다."""
    result = ReviewResult(summary="요약", event=ReviewEvent.COMMENT)
    body = result.render_body()
    assert "리뷰 모델" not in body
    assert "<sub>" not in body


def test_finding_default_severity_is_suggestion() -> None:
    f = Finding(path="a.py", line=1, body="x")
    assert f.severity == SEVERITY_SUGGESTION
    assert f.is_blocking is False
    assert f.label == "Suggestion"


def test_finding_unknown_severity_falls_back_to_suggestion() -> None:
    f = Finding(path="a.py", line=1, body="x", severity="apocalyptic")
    assert f.severity == SEVERITY_SUGGESTION
    assert f.label == "Suggestion"


def test_finding_blocking_severities_are_critical_and_major() -> None:
    crit = Finding(path="a.py", line=1, body="x", severity=SEVERITY_CRITICAL)
    major = Finding(path="a.py", line=1, body="x", severity=SEVERITY_MAJOR)
    minor = Finding(path="a.py", line=1, body="x", severity=SEVERITY_MINOR)
    suggestion = Finding(path="a.py", line=1, body="x", severity=SEVERITY_SUGGESTION)
    assert crit.is_blocking is True
    assert major.is_blocking is True
    assert minor.is_blocking is False
    assert suggestion.is_blocking is False


def test_finding_label_is_human_readable_for_each_severity() -> None:
    """`[Label]` 접두로 직접 쓰이므로 대소문자·공백이 계약이다."""
    assert Finding(path="a", line=1, body="x", severity=SEVERITY_CRITICAL).label == "Critical"
    assert Finding(path="a", line=1, body="x", severity=SEVERITY_MAJOR).label == "Major"
    assert Finding(path="a", line=1, body="x", severity=SEVERITY_MINOR).label == "Minor"
    assert Finding(path="a", line=1, body="x", severity=SEVERITY_SUGGESTION).label == "Suggestion"
