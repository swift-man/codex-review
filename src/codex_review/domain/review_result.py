from dataclasses import dataclass, field

from .finding import Finding, ReviewEvent


@dataclass(frozen=True)
class ReviewResult:
    """Structured review output.

    Three sections are rendered in the top-level review body:
    - 좋은 점 (positives)
    - 개선할 점 (improvements)
    - 기술 단위 코멘트 (findings) — posted as inline, line-anchored comments

    `model` 은 이 리뷰를 실제로 생성한 LLM 식별자(예: "gpt-5.4"). 운영자가 PR 화면에서
    어느 모델이 찍었는지 바로 구분할 수 있도록 본문 맨 아래 footer 로 렌더된다.
    도메인 정보는 아니지만 렌더링 위치가 본문과 밀접해 ReviewResult 에 얹는 편이 단순하다.
    """

    summary: str
    event: ReviewEvent
    positives: tuple[str, ...] = field(default_factory=tuple)
    improvements: tuple[str, ...] = field(default_factory=tuple)
    findings: tuple[Finding, ...] = field(default_factory=tuple)
    model: str | None = None

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
        if self.model:
            # 구분선 + 회색 톤 작은 글씨로 붙여 본문 가독성을 해치지 않게 함.
            parts.append(f"\n---\n<sub>리뷰 모델: <code>{self.model}</code></sub>")
        return "\n".join(parts).strip()
