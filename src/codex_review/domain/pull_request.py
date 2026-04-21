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
    # unified diff 의 context( ) 와 add(+) 라인이 포함되며, 모델이 이 범위 밖에 코멘트를
    # 제안하면 GitHub 가 422 로 거부하므로 `post_review` 직전에 필터링 기준으로 쓴다.
    # 타입을 `Mapping` 으로 한정해 도메인 계층에서 우연한 변경을 차단한다 —
    # 어댑터 쪽에서 `dict` 를 만들어 넘겨도 읽기 전용 계약으로 취급된다.
    diff_right_lines: Mapping[str, frozenset[int]] = field(
        default_factory=lambda: _EMPTY_DIFF_RIGHT_LINES
    )
