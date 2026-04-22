from dataclasses import dataclass, field

from .finding import Finding, ReviewEvent


@dataclass(frozen=True)
class ReviewResult:
    """Structured review output rendered as four sections:
    좋은 점 (positives) / 🔴 반드시 수정할 사항 (must_fix) /
    💡 권장 개선 사항 (improvements) / 기술 단위 코멘트 (findings).
    """

    summary: str
    event: ReviewEvent
    positives: tuple[str, ...] = field(default_factory=tuple)
    must_fix: tuple[str, ...] = field(default_factory=tuple)
    improvements: tuple[str, ...] = field(default_factory=tuple)
    findings: tuple[Finding, ...] = field(default_factory=tuple)

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
        return "\n".join(parts).strip()
