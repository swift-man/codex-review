from dataclasses import dataclass
from enum import Enum


class ReviewEvent(str, Enum):
    COMMENT = "COMMENT"
    REQUEST_CHANGES = "REQUEST_CHANGES"
    APPROVE = "APPROVE"


# 인라인 코멘트의 심각도. "must_fix" 는 반드시 수정해야 할 사항, "suggest" 는 권장 수준.
# Literal 대신 평범한 문자열 상수로 두어 JSON 스키마와 1:1 매핑이 쉽다.
SEVERITY_MUST_FIX = "must_fix"
SEVERITY_SUGGEST = "suggest"
_VALID_SEVERITIES = {SEVERITY_MUST_FIX, SEVERITY_SUGGEST}


@dataclass(frozen=True)
class Finding:
    """A line-anchored technical comment in Korean.

    `line` 은 필수 — RIGHT-side 에 실제 존재해야 GitHub 이 인라인으로 수락한다.
    `severity` 는 "must_fix" 이면 반드시 수정해야 할 사안으로 표시하고,
    그 외(기본 "suggest")는 권장 수준으로 표시한다.
    """

    path: str
    line: int
    body: str
    severity: str = SEVERITY_SUGGEST

    def __post_init__(self) -> None:
        # 알 수 없는 값이 들어오면 권장 수준으로 강등 — 파서가 잘못된 값을 넘겨도
        # 파이프라인이 깨지지 않도록 안전한 기본값으로 수렴시킨다.
        if self.severity not in _VALID_SEVERITIES:
            object.__setattr__(self, "severity", SEVERITY_SUGGEST)

    @property
    def is_must_fix(self) -> bool:
        return self.severity == SEVERITY_MUST_FIX
