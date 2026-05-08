from typing import Protocol

from codex_review.domain import FileDump, PullRequest, ReviewHistory, ReviewResult


class ReviewEngine(Protocol):
    async def review(
        self,
        pr: PullRequest,
        dump: FileDump,
        *,
        history: ReviewHistory | None = None,
    ) -> ReviewResult:
        """리뷰를 수행해 도메인 `ReviewResult` 반환.

        `history` 가 None 또는 빈 컬렉션이면 prompt 의 history 섹션을 생략 — 첫 리뷰
        호환성. 이전 라운드의 코멘트 / 다른 봇 의견이 있으면 prompt 에 직렬화해 모델
        이 동일 항목 반복 지적 / deferred 무시를 피하도록.
        """
        ...


class ReviewEngineError(RuntimeError):
    """리뷰 엔진(Codex CLI 등) 이 **입력을 받아 결과를 만드는 데 실패** 했음을 알리는
    도메인 예외.

    이 예외 타입을 분리한 이유: use case 의 자동 diff fallback 은 "엔진이 입력을
    소화 못 함" 케이스에만 의미가 있다. 만약 일반 `Exception` 으로 잡으면 `KeyError`,
    `TypeError`, 도메인 모델 버그 같은 **무관한 런타임 버그까지 삼키고** 잘못된
    fallback 으로 빠져 진짜 원인이 가려진다 (gemini PR #18 Major).

    구체 예시:
      - codex CLI returncode != 0 (모델이 입력 거부 / 모델명 무효 등)
      - codex 호출 타임아웃 (입력이 너무 커서 시간 안에 응답 못함)
      - 엔진 출력 파싱 단계의 의도된 실패 (혹시 추후 추가 시)

    `RuntimeError` 를 상속해 BC 유지: 외부 호출자가 단순히 RuntimeError 로 잡던 코드
    는 그대로 동작.
    """

    def __init__(self, message: str, *, returncode: int | None = None) -> None:
        super().__init__(message)
        self.returncode = returncode
