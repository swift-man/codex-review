from dataclasses import dataclass, field

from .finding import Finding, ReviewEvent


@dataclass(frozen=True)
class ReviewResult:
    """Structured review output.

    Three sections are rendered in the top-level review body:
    - 좋은 점 (positives)
    - 개선할 점 (improvements)
    - 기술 단위 코멘트 (findings) — posted as inline, line-anchored comments
    """

    summary: str
    event: ReviewEvent
    positives: tuple[str, ...] = field(default_factory=tuple)
    improvements: tuple[str, ...] = field(default_factory=tuple)
    findings: tuple[Finding, ...] = field(default_factory=tuple)

    def render_body(self) -> str:
        parts: list[str] = [self.summary.strip()]
        if self.positives:
            parts.append("\n**좋은 점**")
            parts.extend(f"- {p}" for p in self.positives)
        if self.improvements:
            parts.append("\n**개선할 점**")
            parts.extend(f"- {i}" for i in self.improvements)
        if self.findings:
            parts.append(f"\n_기술 단위 코멘트 {len(self.findings)}건은 각 라인에 별도 표시됩니다._")
        return "\n".join(parts).strip()
