from collections.abc import Mapping
from dataclasses import dataclass, field
from types import MappingProxyType


@dataclass(frozen=True)
class RepoRef:
    owner: str
    name: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"


# 빈 매핑 프록시 싱글톤: 기본값에 쓰일 읽기전용 매핑.
_EMPTY_DIFF_RIGHT_LINES: Mapping[str, frozenset[int]] = MappingProxyType({})


@dataclass(frozen=True)
class PullRequest:
    repo: RepoRef
    number: int
    title: str
    body: str
    head_sha: str
    head_ref: str
    base_sha: str
    base_ref: str
    clone_url: str
    changed_files: tuple[str, ...]
    installation_id: int
    is_draft: bool
    # path → 해당 파일에서 인라인 코멘트를 달 수 있는 RIGHT-side 라인 번호 집합.
    # 타입은 `Mapping` 이지만 어댑터가 가변 dict 를 그대로 넘길 수 있으므로
    # `__post_init__` 에서 `MappingProxyType` 으로 감싸 런타임 불변성까지 보장한다.
    diff_right_lines: Mapping[str, frozenset[int]] = field(
        default_factory=lambda: _EMPTY_DIFF_RIGHT_LINES
    )

    def __post_init__(self) -> None:
        # frozen=True 이므로 object.__setattr__ 우회가 필요. 이미 MappingProxyType 이면
        # 재래핑하지 않아 불필요한 복사를 피한다.
        if not isinstance(self.diff_right_lines, MappingProxyType):
            object.__setattr__(
                self,
                "diff_right_lines",
                MappingProxyType(dict(self.diff_right_lines)),
            )
