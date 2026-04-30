"""Follow-up reply 본문에 박는 멱등성 마커.

application 계층(`FollowUpReviewUseCase`) 이 답글에 박고, infrastructure 계층
(`GitHubAppClient` 의 thread 파싱) 이 검출한다. 두 계층이 같은 상수를 참조하므로
**두 계층 모두 의존 가능한 도메인 위치** 에 둬 의존 방향을 깨끗이 유지
(coderabbitai PR #19 Nitpick 반영 — 이전엔 application → infrastructure 로
의존이 역전돼 있었음).

HTML 주석이라 GitHub UI 에선 보이지 않지만 본문 텍스트엔 그대로 남아 정확한
substring 검색이 가능하다. 버전 접미사(`v1`) 는 향후 follow-up 형식이 바뀔 때
구버전 마커와 구분하기 위해 보존.
"""

FOLLOWUP_MARKER = "<!-- codex-review-followup:v1 -->"
