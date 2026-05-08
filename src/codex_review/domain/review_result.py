from dataclasses import dataclass, field

from .finding import Finding, ReviewEvent
from .review_history import MetaReply


@dataclass(frozen=True)
class ReviewResult:
    """Structured review output rendered as four sections:
    좋은 점 (positives) / 🔴 반드시 수정할 사항 (must_fix) /
    💡 권장 개선 사항 (improvements) / 기술 단위 코멘트 (findings).

    `dropped_findings` 는 "모델이 산출했지만 PR RIGHT-side 라인 집합을 벗어나
    GitHub 이 인라인으로 수락하지 않을 것"들 — use case 의 diff 필터나 422 재시도
    경로에서 제거된 항목. 조용히 유실시키면 리뷰어가 fallback 리뷰 품질을 과대평가
    할 수 있으므로, 본문 `<details>` 접이식 섹션으로 이름·라인과 함께 남긴다.
    """

    summary: str
    event: ReviewEvent
    positives: tuple[str, ...] = field(default_factory=tuple)
    must_fix: tuple[str, ...] = field(default_factory=tuple)
    improvements: tuple[str, ...] = field(default_factory=tuple)
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    dropped_findings: tuple[Finding, ...] = field(default_factory=tuple)
    # 다른 봇의 inline review comment 에 다는 대댓글 (≤1건 권장). use case 가 review
    # post 후 `reply_to_review_comment` 로 게시. 빈 튜플이면 메타리플라이 게시 단계
    # 자체를 건너뛴다 — 기존 동작 회귀 보호.
    meta_replies: tuple[MetaReply, ...] = field(default_factory=tuple)

    def render_body(self) -> str:
        parts: list[str] = [self.summary.strip()]
        if self.positives:
            parts.append("\n**좋은 점**")
            parts.extend(f"- {p}" for p in self.positives)
        if self.must_fix:
            parts.append("\n**🔴 반드시 수정할 사항**")
            parts.extend(f"- {m}" for m in self.must_fix)
        if self.improvements:
            parts.append("\n**💡 권장 개선 사항**")
            parts.extend(f"- {i}" for i in self.improvements)
        if self.findings:
            parts.append(f"\n_기술 단위 코멘트 {len(self.findings)}건은 각 라인에 별도 표시됩니다._")
        if self.dropped_findings:
            parts.append(_render_dropped_findings(self.dropped_findings))
        return "\n".join(parts).strip()


def _render_dropped_findings(dropped: tuple[Finding, ...]) -> str:
    """인라인 게시가 거부된 finding 을 접이식 섹션으로 보존.

    왜 접이식(`<details>`) 인가: 라인 번호가 어긋난 채로 본문에 노출되면 리뷰어가
    혼동할 수 있다. 기본 접힘 상태로 두고, 투명성을 위해 "여기 N건 있었다" 는 사실만
    제목에서 즉시 보이게 한다.
    """
    lines = [
        "",
        "<details>",
        f"<summary>⚠️ 인라인 게시에서 제외된 지적 {len(dropped)}건 "
        "(RIGHT-side diff 밖 · GitHub 422 방어) — 펼쳐 보기</summary>",
        "",
    ]
    for f in dropped:
        # path:line 과 등급 라벨을 함께 남겨 리뷰어가 어느 위치를 가리키는지 쉽게 찾도록.
        lines.append(f"- `{f.path}:{f.line}` — [{f.label}] {f.body}")
    lines.extend(["", "</details>"])
    return "\n".join(lines)
