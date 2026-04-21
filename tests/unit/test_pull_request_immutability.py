"""Regression coverage for PullRequest.diff_right_lines runtime immutability."""

from types import MappingProxyType

import pytest

from codex_review.domain import PullRequest, RepoRef


def _pr(diff: dict[str, frozenset[int]] | None = None) -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=1,
        title="t",
        body="",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/x.git",
        changed_files=(),
        installation_id=1,
        is_draft=False,
        diff_right_lines=diff if diff is not None else {},
    )


def test_mutable_dict_is_wrapped_in_mapping_proxy() -> None:
    """어댑터가 가변 dict 를 넘겨도 도메인 객체는 읽기 전용 뷰로 유지돼야 한다."""
    mutable = {"a.py": frozenset({1, 2})}
    pr = _pr(mutable)

    assert isinstance(pr.diff_right_lines, MappingProxyType)
    # 쓰기 시도는 TypeError 로 거부돼야 한다.
    with pytest.raises(TypeError):
        pr.diff_right_lines["b.py"] = frozenset({3})  # type: ignore[index]


def test_external_mutation_does_not_leak_into_pull_request() -> None:
    """생성 이후 원본 dict 를 변경해도 PullRequest 의 뷰가 오염되지 않는다."""
    mutable = {"a.py": frozenset({1, 2})}
    pr = _pr(mutable)
    mutable["added-later.py"] = frozenset({9})  # 원본 변경

    assert "added-later.py" not in pr.diff_right_lines


def test_already_proxied_mapping_is_not_rewrapped() -> None:
    """이미 MappingProxyType 이면 추가 복사 없이 그대로 재사용 (불필요한 할당 방지)."""
    proxy = MappingProxyType({"a.py": frozenset({1})})
    pr = _pr()
    # 직접 필드를 수동 세팅한 시뮬레이션은 __post_init__ 경로를 우회하므로
    # 여기서는 "입력이 MappingProxy 일 때도 동일 객체가 유지되는가" 를 간접 검증한다.
    # 단 default 래핑 이후 재할당은 frozen 특성상 불가하므로, 생성 시 주입 경로로 확인.
    pr2 = PullRequest(
        repo=RepoRef("o", "r"),
        number=1,
        title="t",
        body="",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/x.git",
        changed_files=(),
        installation_id=1,
        is_draft=False,
        diff_right_lines=proxy,
    )
    assert pr2.diff_right_lines is proxy
