"""Regression coverage for the diff-only fallback path.

시나리오: 전체 코드베이스가 `CODEX_MAX_INPUT_TOKENS` 를 초과해 변경 파일이 누락됐을 때,
서버가 자동으로 unified patch 만 가지고 리뷰를 돌리는 경로가 end-to-end 로 맞는지 확인.

검증 layer:
  1) DiffContextCollector — PR.diff_patches → FileDump(mode="diff") 변환 정확성
  2) codex_prompt.build_prompt — mode 에 따라 시스템 규칙·본문 포맷 분기
  3) ReviewPullRequestUseCase — 전체 수집이 예산 넘으면 diff fallback 으로 전환,
     diff 까지 넘으면 기존 안내 코멘트 게시 유지
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

import pytest

from codex_review.application.review_pr_use_case import ReviewPullRequestUseCase
from codex_review.domain import (
    DUMP_MODE_DIFF,
    DUMP_MODE_FULL,
    FileDump,
    FileEntry,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
    TokenBudget,
)
from codex_review.infrastructure.codex_prompt import build_prompt
from codex_review.infrastructure.diff_context_collector import DiffContextCollector


# ---------------------------------------------------------------------------
# Fixtures / fakes
# ---------------------------------------------------------------------------


def _pr(
    changed: tuple[str, ...] = ("a.py", "b.py"),
    patches: dict[str, str] | None = None,
    diff_right: dict[str, frozenset[int]] | None = None,
) -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=1,
        title="t",
        body="pr body",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/x.git",
        changed_files=changed,
        installation_id=7,
        is_draft=False,
        diff_right_lines=diff_right or {},
        diff_patches=patches or {},
    )


@dataclass
class _CapturingGitHub:
    posted_reviews: list[tuple[PullRequest, ReviewResult]] = field(default_factory=list)
    posted_comments: list[tuple[PullRequest, str]] = field(default_factory=list)

    async def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest:
        raise AssertionError("not used in these tests")

    async def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        self.posted_reviews.append((pr, result))

    async def post_comment(self, pr: PullRequest, body: str) -> None:
        self.posted_comments.append((pr, body))

    async def get_installation_token(self, installation_id: int) -> str:
        return "tkn"


class _NoopFetcher:
    @asynccontextmanager
    async def session(
        self, pr: PullRequest, installation_token: str
    ) -> AsyncIterator[Path]:
        yield Path(".")


@dataclass
class _StaticFullCollector:
    """예산 초과 시나리오를 재현하기 위한 full collector 더블."""

    dump: FileDump

    async def collect(
        self, root: Path, changed_files: tuple[str, ...], budget: TokenBudget
    ) -> FileDump:
        return self.dump


@dataclass
class _CapturingEngine:
    result: ReviewResult
    seen_dumps: list[FileDump] = field(default_factory=list)

    async def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        self.seen_dumps.append(dump)
        return self.result


# ---------------------------------------------------------------------------
# DiffContextCollector
# ---------------------------------------------------------------------------


def _zero_overhead(pr: PullRequest, empty_dump: FileDump) -> int:
    """테스트용 stub — 오버헤드 0. 순수 truncation 동작만 검증하고 싶을 때 주입.

    실 운영 기본값은 `build_prompt()` 결과 길이(수 KB) 를 쓰므로 작은 예산의 단위
    테스트에서는 패치가 전부 오버헤드에 잡아 먹혀 결정성이 떨어진다. 오버헤드 자체
    계약은 전용 테스트(`test_diff_collector_reserves_prompt_overhead_from_budget`) 로 따로.
    """
    return 0


async def test_diff_collector_builds_dump_from_patches() -> None:
    collector = DiffContextCollector(overhead_estimator=_zero_overhead)
    patches = {
        "a.py": "@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n",
        "b.py": "@@ -0,0 +1,1 @@\n+print('hi')\n",
    }
    pr = _pr(changed=("a.py", "b.py"), patches=patches)

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=10_000))

    assert dump.mode == DUMP_MODE_DIFF
    assert len(dump.entries) == 2
    assert dump.entries[0].path == "a.py"
    # entry.content 에 원문 patch 가 들어가고 파일 헤더가 앞에 붙는다.
    assert "=== PATCH: a.py ===" in dump.entries[0].content
    assert "+y = 2" in dump.entries[0].content
    assert dump.entries[1].path == "b.py"
    assert "+print('hi')" in dump.entries[1].content
    assert dump.exceeded_budget is False
    assert dump.patch_missing == ()
    # `budget` 필드가 전달돼야 이후 본문 배지 등에서 한도를 노출할 수 있다.
    assert dump.budget is not None and dump.budget.max_tokens == 10_000


async def test_diff_collector_marks_patch_missing_files() -> None:
    """GitHub 가 patch 를 안 준 파일(rename/delete/binary/거대 diff) 은 patch_missing 에 적재."""
    collector = DiffContextCollector(overhead_estimator=_zero_overhead)
    pr = _pr(
        changed=("a.py", "removed.bin", "renamed.jpg"),
        patches={"a.py": "@@ -1,1 +1,1 @@\n-x\n+y\n"},
    )

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=10_000))

    assert [e.path for e in dump.entries] == ["a.py"]
    assert dump.patch_missing == ("removed.bin", "renamed.jpg")
    # excluded 에는 patch_missing 이 그대로 섞여 들어가야 운영자 노출용으로 통합됨.
    assert "removed.bin" in dump.excluded and "renamed.jpg" in dump.excluded
    assert dump.exceeded_budget is False  # patch 누락은 예산 이슈와 구분


async def test_diff_collector_truncates_when_budget_exceeded() -> None:
    """predicted size 가 예산을 넘는 순간 이후 파일은 drop — 부분 patch 로 자르지 않는다."""
    collector = DiffContextCollector(overhead_estimator=_zero_overhead)
    # patch 하나당 body ≈ 186 chars (header 20 + patch 내용 166).
    big_patch = "@@ -1,1 +1,1 @@\n" + "+x\n" * 50
    patches = {"a.py": big_patch, "b.py": big_patch, "c.py": big_patch}
    pr = _pr(changed=("a.py", "b.py", "c.py"), patches=patches)

    # chars_per_token=4 기본, max_tokens=100 → 400 chars 한도.
    # a.py(186) + b.py(186) = 372 → 들어가고, c.py(+186) 를 넣으면 558 → 초과.
    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=100))

    assert dump.exceeded_budget is True
    assert [e.path for e in dump.entries] == ["a.py", "b.py"]
    # c.py 는 예산 초과로 제외 (patch_missing 과 구분된 budget_trimmed).
    assert "c.py" in dump.excluded
    assert dump.patch_missing == ()


async def test_diff_collector_truncates_first_oversize_file() -> None:
    """첫 파일이 예산을 단독으로 넘기면 entries 가 비고 exceeded_budget=True."""
    collector = DiffContextCollector(overhead_estimator=_zero_overhead)
    huge = "@@ -1,1 +1,1 @@\n" + "+x\n" * 500  # ~1500 chars
    pr = _pr(changed=("big.py",), patches={"big.py": huge})

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=10))  # 40 chars

    assert dump.entries == ()
    assert dump.exceeded_budget is True
    assert "big.py" in dump.excluded


def test_file_dump_budget_trimmed_excludes_both_filter_and_patch_missing() -> None:
    """회귀 (gemini PR #17 Major): `budget_trimmed` 는 **예산 때문에만** 잘린 파일이어야 한다.
    `filter_excluded` (바이너리/정책 배제) 와 `patch_missing` (GitHub 가 patch 안 줌) 은
    모두 빼야 리뷰 use case 가 fallback 결정을 정확히 내린다.
    """
    from codex_review.domain import FileDump, TokenBudget

    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=(
            "big.py",           # 예산 컷
            "image.png",        # 정책 배제 (filter)
            "huge.py",          # 예산 컷
            "binary.bin",       # patch 누락 (diff 모드에서 발생하는 카테고리)
        ),
        filter_excluded=("image.png",),
        patch_missing=("binary.bin",),
        exceeded_budget=True,
        budget=TokenBudget(max_tokens=100),
    )

    # 순수 예산 컷만 남아야 한다.
    assert dump.budget_trimmed == ("big.py", "huge.py")


def test_file_dump_budget_trimmed_returns_empty_when_all_in_policy_categories() -> None:
    """full 모드에서 변경 파일이 전부 정책 배제(바이너리) 라면 budget_trimmed 는 비어야 한다.
    이 불변식이 `_changed_trimmed_by_budget` 의 올바른 판단을 보장한다.
    """
    from codex_review.domain import FileDump

    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.png", "b.jpg"),
        filter_excluded=("a.png", "b.jpg"),
        exceeded_budget=False,  # 예산 이슈 아님
    )
    assert dump.budget_trimmed == ()


def test_file_dump_budget_trimmed_property_returns_empty_when_no_excluded() -> None:
    from codex_review.domain import FileDump

    dump = FileDump(entries=(), total_chars=0)
    assert dump.budget_trimmed == ()


async def test_diff_collector_empty_when_no_patches() -> None:
    collector = DiffContextCollector(overhead_estimator=_zero_overhead)
    pr = _pr(changed=("a.py",), patches={})

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=10_000))

    assert dump.entries == ()
    assert dump.patch_missing == ("a.py",)


async def test_diff_collector_records_all_remaining_files_after_budget_hit() -> None:
    """회귀 (codex PR #17 Major): 예산 초과 지점에서 break 해버려 뒤 파일들이 `excluded`
    에도 SCOPE 안내에도 남지 않던 버그. 이제는 초과 이후 모든 변경 파일이 정확히
    budget_trimmed 로 기록되고, 중간에 섞인 patch_missing 파일도 계속 정확히 분류된다.
    """
    collector = DiffContextCollector(overhead_estimator=_zero_overhead)
    # patch 크기 ≈ 186 chars — a.py (186) + b.py (186) = 372 < 400.
    # c.py 부터 예산 초과. d.py 는 patch 가 아예 없는 파일 (rename 등). e.py 는 또 정상.
    big_patch = "@@ -1,1 +1,1 @@\n" + "+x\n" * 50
    patches = {
        "a.py": big_patch,
        "b.py": big_patch,
        "c.py": big_patch,
        "e.py": big_patch,
    }
    pr = _pr(changed=("a.py", "b.py", "c.py", "d.py", "e.py"), patches=patches)

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=100))

    assert [e.path for e in dump.entries] == ["a.py", "b.py"]
    assert dump.exceeded_budget is True
    # c.py 와 e.py 모두 예산으로 잘린 것으로 기록돼야 한다 (이전 구현은 c.py 만 남김).
    budget_trimmed = tuple(p for p in dump.excluded if p not in dump.patch_missing)
    assert budget_trimmed == ("c.py", "e.py")
    # d.py 는 patch 가 없었으니 patch_missing 쪽에 정확히 분류.
    assert dump.patch_missing == ("d.py",)
    # excluded 에는 둘 다 포함돼 운영자에게 전부 노출된다.
    assert set(dump.excluded) == {"c.py", "e.py", "d.py"}


async def test_diff_collector_reserves_prompt_overhead_from_budget() -> None:
    """회귀 (codex PR #17 Major): collector 의 예산 판정이 patch 본문만 보던 동작을
    고쳐, 최종 `build_prompt()` 가 포함하는 system rules + metadata + SCOPE 섹션까지
    포함해 판정해야 한다. overhead_estimator 가 max_chars 의 절반을 잡아먹는다고
    보고 하면, patch 는 남은 절반 안에서만 담겨야 한다.
    """
    # max_tokens=1000 → 4000 chars 예산. overhead 가 3000 chars 라고 하면 patch 에
    # 쓸 수 있는 공간은 1000 chars.
    def half_overhead(pr: PullRequest, empty_dump: FileDump) -> int:
        return 3000

    collector = DiffContextCollector(overhead_estimator=half_overhead)
    # patch 하나당 ≈ 370 chars. 1000 chars 안에 2개만 들어가야 한다.
    big_patch = "@@ -1,1 +1,1 @@\n" + "+x\n" * 100  # ~316 chars + 헤더 = ~336
    patches = {f"f{i}.py": big_patch for i in range(5)}
    pr = _pr(changed=tuple(f"f{i}.py" for i in range(5)), patches=patches)

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=1000))

    # 오버헤드를 감안한 실질 예산 내에서만 담겨야 한다 (2~3개 파일).
    assert len(dump.entries) <= 3
    assert dump.exceeded_budget is True
    assert dump.total_chars <= 1000  # patch_budget = 4000 - 3000 = 1000
    # 담긴 것 + 잘린 것의 합이 원래 변경 파일 수와 일치 (patch_missing 은 0건).
    assert len(dump.entries) + len(dump.budget_trimmed) == 5


async def test_diff_collector_early_returns_without_iterating_when_overhead_exceeds_budget() -> None:
    """회귀 (gemini PR #17 Minor): 오버헤드가 예산을 이미 넘으면 파일 순회 루프 자체를
    생략하는 early-return 경로를 타야 한다. estimator 가 초기 1회만 호출되고 이후
    호출(오버헤드 재측정·최종 verify) 이 **일어나지 않음** 을 관찰해 단락 여부 확인.
    """
    call_count = 0

    def tracking_overhead(pr: PullRequest, empty_dump: FileDump) -> int:
        nonlocal call_count
        call_count += 1
        return 10_000_000  # 예산보다 훨씬 큼

    collector = DiffContextCollector(overhead_estimator=tracking_overhead)
    # 파일 많이 넣어도 루프에 진입조차 하면 안 된다.
    patches = {f"f{i}.py": "@@ -1 +1 @@\n+x\n" for i in range(20)}
    pr = _pr(changed=tuple(f"f{i}.py" for i in range(20)), patches=patches)

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=100))

    # estimator 는 초기 overhead 산정 1회만 호출돼야 한다 — 루프나 최종 verify 에서 재호출 X.
    assert call_count == 1

    # 모든 patched 변경 파일이 budget_trimmed 로 정확히 기록.
    assert dump.entries == ()
    assert dump.exceeded_budget is True
    assert len(dump.budget_trimmed) == 20
    assert dump.budget_trimmed[0] == "f0.py"      # 원본 순서 유지
    assert dump.budget_trimmed[-1] == "f19.py"


async def test_diff_collector_returns_empty_when_overhead_exceeds_budget() -> None:
    """오버헤드만으로 예산을 넘으면 정직하게 `exceeded_budget=True` + 빈 entries 반환.

    인위적 최소 보장(floor) 없이 진실을 전달해야 use case 의 "빈 덤프 → fallback 불가"
    경로가 타진다. 이전엔 `_MIN_PATCH_BUDGET_CHARS` 같은 floor 가 실패 상태를 숨겼음.
    """
    def oversize_overhead(pr: PullRequest, empty_dump: FileDump) -> int:
        return 1_000_000  # max_chars 보다 훨씬 큼

    collector = DiffContextCollector(overhead_estimator=oversize_overhead)
    pr = _pr(changed=("a.py",), patches={"a.py": "@@ -1 +1 @@\n-x\n+y\n"})

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=1000))

    assert dump.entries == ()
    assert dump.exceeded_budget is True
    assert dump.budget_trimmed == ("a.py",)


async def test_diff_collector_final_verify_trims_when_scope_inflates_prompt() -> None:
    """회귀 (codex PR #17 Major): 초기 오버헤드 산정 시엔 budget_trimmed 목록이 없어서
    그 섹션이 차지할 추가 크기(파일당 ~40자)를 포함하지 못한다. 최종 verify 패스가
    실제 프롬프트 길이를 재측정해 초과 시 뒤 entries 를 떨어뜨려야 한다.

    estimator 는 "entries 당 110자 + budget_trimmed 당 20자" 로 설정해,
    초기엔 550 > 400 이지만 entry 를 trimmed 로 옮기면 점진적으로 줄어 수렴함을 확인.
    """
    def fake_length(pr: PullRequest, dump: FileDump) -> int:
        # entry 는 trimmed 보다 훨씬 크므로 (patch 원문) entry → trimmed 전환이 단조 감소.
        return 110 * len(dump.entries) + 20 * len(dump.budget_trimmed)

    collector = DiffContextCollector(overhead_estimator=fake_length)
    # max_tokens=100 → 400 chars 예산.
    # 5 entries: 550 > 400
    # 4 entries + 1 trimmed: 460 > 400
    # 3 entries + 2 trimmed: 370 <= 400 ← 수렴
    patches = {f"f{i}.py": "@@ -1 +1 @@\n+x\n" for i in range(5)}
    pr = _pr(changed=tuple(f"f{i}.py" for i in range(5)), patches=patches)

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=100))

    assert fake_length(pr, dump) <= 400
    assert len(dump.entries) < 5
    assert len(dump.budget_trimmed) >= 1
    # 엔트리 + 트림 합은 항상 원본 변경 파일 수와 같아야 한다 (유실 없음).
    assert len(dump.entries) + len(dump.budget_trimmed) == 5


async def test_diff_collector_final_verify_is_noop_when_prompt_fits() -> None:
    """프롬프트가 이미 예산 안에 들어가면 verify 패스는 아무것도 하지 않는다 — 회귀 방지."""
    # estimator 는 항상 10 반환 — 언제나 예산 안에 들어감.
    collector = DiffContextCollector(overhead_estimator=lambda pr, d: 10)
    patches = {"a.py": "@@ -1 +1 @@\n+x\n", "b.py": "@@ -1 +1 @@\n+y\n"}
    pr = _pr(changed=("a.py", "b.py"), patches=patches)

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=100))

    # 두 파일 모두 담겼어야 한다 (verify 가 건드리지 않음).
    assert [e.path for e in dump.entries] == ["a.py", "b.py"]
    assert dump.budget_trimmed == ()
    assert dump.exceeded_budget is False


async def test_diff_collector_final_verify_uses_append_then_sort_for_ordering() -> None:
    """회귀 (gemini PR #17 Suggestion): `insert(0, ...)` 의 O(N²) 을 피하려고 루프 중엔
    append 만 하고 종료 후 한 번에 정렬한다. 이 테스트는 결과 순서가 여전히 원본
    `changed_files` 순서를 따르는지 직접 확인.
    """
    def fake_length(pr: PullRequest, dump: FileDump) -> int:
        # 8 files 를 쓰는데 6개가 축출되도록 설정 — insert 패턴이면 O(N²) 가 눈에 띌 수 있음.
        return 110 * len(dump.entries) + 20 * len(dump.budget_trimmed)

    collector = DiffContextCollector(overhead_estimator=fake_length)
    patches = {f"f{i}.py": "@@\n+x\n" for i in range(8)}
    changed = tuple(f"f{i}.py" for i in range(8))
    pr = _pr(changed=changed, patches=patches)

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=100))

    # budget_trimmed 는 changed_files 의 부분집합이며 원본 상대 순서를 유지.
    original_index = {p: i for i, p in enumerate(changed)}
    positions = [original_index[p] for p in dump.budget_trimmed]
    assert positions == sorted(positions), (
        f"budget_trimmed 순서가 원본을 벗어남: {dump.budget_trimmed}"
    )


async def test_diff_collector_final_verify_budget_trimmed_ordering_preserved() -> None:
    """verify 루프가 떨어뜨린 entries 는 `budget_trimmed` 앞쪽으로 삽입돼 원본 순서를 유지.

    뒤에서부터 f5 먼저, 그 다음 f4, … 순으로 떨어지므로 `budget_trimmed` 는
    [f4, f5] (원본 순서) 로 관찰된다.
    """
    def fake_length(pr: PullRequest, dump: FileDump) -> int:
        return 110 * len(dump.entries) + 20 * len(dump.budget_trimmed)

    collector = DiffContextCollector(overhead_estimator=fake_length)
    patches = {f"f{i}.py": "@@\n+x\n" for i in range(6)}
    changed = tuple(f"f{i}.py" for i in range(6))
    pr = _pr(changed=changed, patches=patches)

    # 400 chars 예산: 6 entries 는 초과, 수렴 지점까지 trim.
    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=100))

    # 남은 entries + budget_trimmed 의 합집합이 원본 changed_files 와 같아야 한다.
    covered = {e.path for e in dump.entries} | set(dump.budget_trimmed)
    assert covered == set(changed)
    # 마지막 파일(f5.py) 이 반드시 budget_trimmed 에 있어야 한다 (뒤에서부터 트림).
    assert "f5.py" in dump.budget_trimmed
    # budget_trimmed 는 원본 순서를 유지 — 마지막 요소가 f5.py (가장 나중에 밀려난 건
    # 가장 먼저 수집된 `f0.py` 는 아니고, 구조상 끝 인덱스 파일이 먼저 밀려남).
    # 중요 계약: 모든 항목이 changed_files 순서 내 원래 상대 위치 유지.
    positions_in_original = [changed.index(p) for p in dump.budget_trimmed]
    assert positions_in_original == sorted(positions_in_original)


async def test_default_overhead_estimator_measures_real_build_prompt() -> None:
    """실 운영 기본값(= build_prompt 를 직접 호출) 도 동작한다 — 이 테스트는 오버헤드
    계약이 실제 프롬프트 크기와 일치함을 확인해 production 경로를 pin.
    """
    # 기본 overhead_estimator 사용 (주입 안 함).
    collector = DiffContextCollector()
    # 넉넉한 예산 — overhead + patch 모두 들어가도 남는다.
    pr = _pr(
        changed=("a.py",),
        patches={"a.py": "@@ -1 +1 @@\n-x\n+y\n"},
    )
    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=100_000))

    # entries 1건 + 예산 초과 없음 확인 (정상 케이스가 overhead 때문에 깨지지 않음).
    assert len(dump.entries) == 1
    assert dump.exceeded_budget is False


async def test_diff_collector_budget_uses_utf8_bytes_for_cjk_patches() -> None:
    """회귀 (codex PR #17 Major): 예산 비교가 char 수 기준이면 한글/이모지 등 멀티바이트
    patch 에서 실제 stdin 바이트가 추정보다 커져 codex exec 한도 초과 위험. 단위는
    **UTF-8 바이트** 로 통일돼야 하고 `FileEntry.size_bytes` 와도 일치.

    시나리오: patch 가 모두 ASCII 인 세트와 동일한 구조를 한글로 채운 세트를 같은
    예산으로 수집하면, **한글 세트가 바이트 수로 예산을 더 빨리 소진** (= 더 적게
    담김) 해야 한다. 이전 char 기반 비교에서는 두 경우가 같게 보였다.
    """
    def zero_overhead(pr: PullRequest, empty_dump: FileDump) -> int:
        return 0

    collector = DiffContextCollector(overhead_estimator=zero_overhead)

    # ASCII: 각 patch 는 대략 50 bytes (body + header).
    ascii_patches = {
        f"a{i}.py": "@@ -1 +1 @@\n-old\n+new\n" for i in range(10)
    }
    ascii_pr = _pr(
        changed=tuple(f"a{i}.py" for i in range(10)), patches=ascii_patches
    )

    # 한글: 같은 라인 수, 한글은 UTF-8 에서 1자 = 3 bytes → 약 3배 큼.
    korean_patches = {
        f"a{i}.py": "@@ -1 +1 @@\n-이전값\n+새로운값\n" for i in range(10)
    }
    korean_pr = _pr(
        changed=tuple(f"a{i}.py" for i in range(10)), patches=korean_patches
    )

    # 예산 300 bytes — ASCII 는 여러 파일 담기지만 한글은 훨씬 적게 담겨야 한다.
    budget = TokenBudget(max_tokens=75)  # = 300 bytes

    ascii_dump = await collector.collect_diff(ascii_pr, budget)
    korean_dump = await collector.collect_diff(korean_pr, budget)

    # ASCII 는 더 많이 담긴다 — 예산이 바이트 기준이라 멀티바이트가 더 빨리 소진.
    assert len(ascii_dump.entries) > len(korean_dump.entries), (
        f"예산 단위가 char 였다면 두 세트가 비슷해야 하지만, UTF-8 bytes 기준이면 "
        f"한글이 적게 담긴다. ASCII={len(ascii_dump.entries)}, "
        f"Korean={len(korean_dump.entries)}"
    )
    # size_bytes 가 실제 바이트와 일치하고, total_chars (실은 bytes) 도 그 합과 일치.
    for entry in korean_dump.entries:
        assert entry.size_bytes == len(entry.content.encode("utf-8"))
    assert korean_dump.total_chars == sum(e.size_bytes for e in korean_dump.entries)


async def test_diff_collector_size_bytes_uses_utf8_byte_length() -> None:
    """회귀 (gemini PR #17 Major): 한글 등 멀티바이트가 섞이면 `len(body)` (char 개수)
    와 `len(body.encode('utf-8'))` (bytes) 가 크게 달라진다. FileEntry.size_bytes 는
    이름대로 실제 바이트를 담아야 모니터링·모델 로그가 정확하다.
    """
    collector = DiffContextCollector(overhead_estimator=_zero_overhead)
    # 한글 "한" 은 UTF-8 에서 3 bytes. patch 본문에 multibyte 포함.
    patches = {"a.py": "@@ -1,1 +1,1 @@\n-영어만\n+한글과 English\n"}
    pr = _pr(changed=("a.py",), patches=patches)

    dump = await collector.collect_diff(pr, TokenBudget(max_tokens=10_000))

    entry = dump.entries[0]
    assert entry.size_bytes == len(entry.content.encode("utf-8"))
    # 멀티바이트가 있으므로 byte 수 > char 수 여야 함.
    assert entry.size_bytes > len(entry.content)


# ---------------------------------------------------------------------------
# Prompt builder — mode branching
# ---------------------------------------------------------------------------


def test_build_prompt_full_mode_uses_standard_system_rules() -> None:
    """full 모드 프롬프트는 '전체 코드베이스' 리뷰 규칙을 써야 한다."""
    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),),
        total_chars=3,
        mode=DUMP_MODE_FULL,
    )
    prompt = build_prompt(_pr(), dump)

    assert "전체 코드베이스" in prompt
    assert "1-based 줄 번호" in prompt or "NNNNN|" in prompt
    # diff-only 배지는 있으면 안 된다.
    assert "diff-only mode" not in prompt


def test_build_prompt_diff_mode_switches_system_rules() -> None:
    """diff 모드 프롬프트는 '보이지 않는 코드에 대한 추측 금지' 규칙이 들어가야 한다."""
    patch_content = "=== PATCH: a.py ===\n@@ -1,1 +1,1 @@\n-old\n+new\n"
    dump = FileDump(
        entries=(
            FileEntry(path="a.py", content=patch_content, size_bytes=len(patch_content), is_changed=True),
        ),
        total_chars=len(patch_content),
        mode=DUMP_MODE_DIFF,
    )
    prompt = build_prompt(_pr(), dump)

    # 핵심 계약: 보이지 않는 코드 추측 금지 메시지 + diff 해석 가이드가 포함.
    assert "보이지 않는 코드" in prompt
    assert "추측" in prompt
    assert "@@ -a,b +c,d @@" in prompt or "hunk" in prompt.lower() or "@@" in prompt
    # diff 모드 배지
    assert "diff-only" in prompt
    # full 모드의 "전체 코드베이스를 리뷰한다" 라는 **행동 규칙** 문장이 들어오면 안 된다.
    # (diff 모드에선 "전체 코드베이스 컨텍스트가 초과됐다" 는 **설명** 에는 그 단어가 나오므로
    # 구문 자체를 체크한다.)
    assert "전체 코드베이스**를 한국어로 리뷰한다" not in prompt
    assert "**전체 코드베이스**를 한국어로 리뷰한다" not in prompt
    # patch 원문이 그대로 전달됨
    assert "=== PATCH: a.py ===" in prompt
    assert "+new" in prompt


def test_build_prompt_diff_mode_lists_patch_missing_and_trimmed() -> None:
    """diff 모드 SCOPE 섹션에 patch 누락 · 예산 컷 파일이 별도로 노출돼야 한다."""
    dump = FileDump(
        entries=(FileEntry(path="a.py", content="...", size_bytes=3, is_changed=True),),
        total_chars=3,
        excluded=("big.py", "bin.dat", "renamed.jpg"),
        exceeded_budget=True,
        patch_missing=("bin.dat", "renamed.jpg"),
        mode=DUMP_MODE_DIFF,
    )
    prompt = build_prompt(_pr(), dump)

    assert "patch 를 주지 않아" in prompt
    assert "bin.dat" in prompt
    assert "renamed.jpg" in prompt
    assert "예산 초과" in prompt
    assert "big.py" in prompt


# ---------------------------------------------------------------------------
# Use case — automatic fallback behavior
# ---------------------------------------------------------------------------


def _use_case(
    github: _CapturingGitHub,
    full_dump: FileDump,
    engine_result: ReviewResult,
    max_tokens: int = 1000,
    with_diff_collector: bool = True,
) -> tuple[ReviewPullRequestUseCase, _CapturingEngine]:
    engine = _CapturingEngine(result=engine_result)
    uc = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=_NoopFetcher(),
        file_collector=_StaticFullCollector(dump=full_dump),
        engine=engine,
        max_input_tokens=max_tokens,
        diff_context_collector=DiffContextCollector() if with_diff_collector else None,
    )
    return uc, engine


async def test_use_case_falls_back_to_diff_when_full_exceeds_and_changed_missing() -> None:
    """핵심 계약: 변경 파일이 **예산 때문에** 빠졌을 때 diff fallback 으로 리뷰가 게시돼야 한다."""
    github = _CapturingGitHub()
    # full 수집이 예산 초과 + 변경 파일 b.py 가 예산 컷 당함 (filter 가 아님).
    exceeded_full = FileDump(
        entries=(FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),),
        total_chars=3,
        excluded=("b.py",),
        exceeded_budget=True,
        mode=DUMP_MODE_FULL,
        # b.py 는 filter 가 아닌 예산 컷이므로 filter_excluded 는 비어 있다.
    )
    engine_result = ReviewResult(summary="OK", event=ReviewEvent.COMMENT)
    # 실 `build_prompt` overhead 가 ~4KB (시스템 규칙 + PR meta) 라 patch 를 담으려면
    # 넉넉한 예산이 필요. 10K tokens (=40KB) 면 system rules + 작은 patch 둘 다 fit.
    uc, engine = _use_case(github, exceeded_full, engine_result, max_tokens=10_000)

    patches = {
        "a.py": "@@ -1,1 +1,2 @@\n x = 1\n+y = 2\n",
        "b.py": "@@ -0,0 +1,1 @@\n+print('hi')\n",
    }
    pr = _pr(patches=patches)

    await uc.execute(pr)

    # 리뷰가 정상 게시돼야 하고(코멘트 안내 아님), diff dump 로 엔진이 돌았어야 한다.
    assert github.posted_comments == []
    assert len(github.posted_reviews) == 1
    assert len(engine.seen_dumps) == 1
    assert engine.seen_dumps[0].mode == DUMP_MODE_DIFF

    # 본문 배지가 summary 최상단에 붙어야 리뷰어가 diff-only 임을 인지한다.
    _, posted = github.posted_reviews[0]
    assert "diff-only" in posted.summary
    assert "자동 전환" in posted.summary


async def test_use_case_posts_budget_notice_when_diff_also_fails() -> None:
    """diff fallback 이 불가능한 경우(예: patch 하나도 없음) 기존 안내 경로로 떨어진다."""
    github = _CapturingGitHub()
    exceeded_full = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py", "b.py"),
        exceeded_budget=True,
        mode=DUMP_MODE_FULL,
    )
    engine_result = ReviewResult(summary="unused", event=ReviewEvent.COMMENT)
    uc, engine = _use_case(github, exceeded_full, engine_result)

    pr = _pr(patches={})  # GitHub 가 patch 를 전혀 안 줌

    await uc.execute(pr)

    assert engine.seen_dumps == []  # 엔진 호출 없어야 함
    assert github.posted_reviews == []
    assert len(github.posted_comments) == 1
    assert "예산 초과" in github.posted_comments[0][1]


async def test_use_case_uses_full_mode_when_budget_fits() -> None:
    """예산 안쪽에서 돌 때는 기존 full 모드 경로를 그대로 타야 한다 (회귀 방지)."""
    github = _CapturingGitHub()
    ok_dump = FileDump(
        entries=(
            FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),
            FileEntry(path="b.py", content="y=2", size_bytes=3, is_changed=True),
        ),
        total_chars=6,
        mode=DUMP_MODE_FULL,
    )
    engine_result = ReviewResult(summary="OK", event=ReviewEvent.COMMENT)
    uc, engine = _use_case(github, ok_dump, engine_result)

    pr = _pr(patches={"a.py": "@@\n+x\n"})  # patches 있어도 fallback 안 타야 한다.

    await uc.execute(pr)

    assert len(engine.seen_dumps) == 1
    assert engine.seen_dumps[0].mode == DUMP_MODE_FULL
    _, posted = github.posted_reviews[0]
    # full 모드 리뷰엔 diff 배지가 없어야 한다.
    assert "diff-only" not in posted.summary


async def test_use_case_fallback_disabled_returns_to_legacy_behavior(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """diff_context_collector=None 이면 이전 동작(안내만 게시) 이 그대로 유지된다.
    운영자가 의도적으로 fallback 을 끄고 싶은 경우 대비.
    """
    github = _CapturingGitHub()
    exceeded = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py",),
        exceeded_budget=True,
        mode=DUMP_MODE_FULL,
    )
    engine_result = ReviewResult(summary="unused", event=ReviewEvent.COMMENT)
    uc, engine = _use_case(
        github, exceeded, engine_result, with_diff_collector=False
    )
    pr = _pr(patches={"a.py": "@@ -1 +1 @@\n-x\n+y\n"})  # 있어도 무시돼야 함

    await uc.execute(pr)

    assert engine.seen_dumps == []
    assert github.posted_reviews == []
    assert len(github.posted_comments) == 1
    assert "예산 초과" in github.posted_comments[0][1]


async def test_use_case_does_not_fall_back_when_only_filter_excluded_changed_files_missing() -> None:
    """회귀 (gemini PR #17 Major): 변경 파일이 정책(바이너리/크기) 필터로만 제외된
    경우엔 diff fallback 을 트리거하면 안 된다. 이전 `_changed_missing` 은 filter 와
    budget 을 구분하지 못해 불필요한 강등이 발생했다.

    시나리오:
      - PR 이 a.py (source) + b.png (binary) 변경
      - full collector: a.py 정상 포함, b.png 는 filter_excluded 로 빠짐
      - 다른 거대 파일 때문에 exceeded_budget=True
      → a.py 는 full 모드로 리뷰돼야 함 (diff fallback 강등 금지)
    """
    github = _CapturingGitHub()
    # exceeded_budget=True, 하지만 b.png 는 "filter" 로 빠진 것 (budget 이 아님).
    full_with_filter_only = FileDump(
        entries=(FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),),
        total_chars=3,
        excluded=("b.png",),
        exceeded_budget=True,
        mode=DUMP_MODE_FULL,
        filter_excluded=("b.png",),  # 핵심: 필터로 빠진 거라 fallback 대상 아님
    )
    engine_result = ReviewResult(summary="OK", event=ReviewEvent.COMMENT)
    uc, engine = _use_case(github, full_with_filter_only, engine_result)

    patches = {"a.py": "@@ -1,1 +1,2 @@\n x\n+y\n"}  # b.png 는 patch 도 없음
    pr = _pr(changed=("a.py", "b.png"), patches=patches)

    await uc.execute(pr)

    # full 모드 그대로 리뷰돼야 하고, diff collector 는 호출되지 않아야 한다.
    assert len(engine.seen_dumps) == 1
    assert engine.seen_dumps[0].mode == DUMP_MODE_FULL
    _, posted = github.posted_reviews[0]
    assert "diff-only" not in posted.summary


async def test_use_case_still_falls_back_when_source_change_was_budget_trimmed() -> None:
    """대조군: 같은 시나리오라도 변경 파일이 **예산 컷** 으로 빠진 경우는 fallback 성공."""
    github = _CapturingGitHub()
    # a.py 가 실제로 예산 때문에 잘림 (filter_excluded 는 비어 있음)
    full_budget_cut = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py",),
        exceeded_budget=True,
        mode=DUMP_MODE_FULL,
        filter_excluded=(),  # 정책 배제 없음 — 순수 예산 컷
    )
    engine_result = ReviewResult(summary="OK", event=ReviewEvent.COMMENT)
    uc, engine = _use_case(github, full_budget_cut, engine_result)

    patches = {"a.py": "@@ -1 +1 @@\n-x\n+y\n"}
    pr = _pr(changed=("a.py",), patches=patches)

    await uc.execute(pr)

    # 이번에는 diff 모드로 강등돼야 정상.
    assert len(engine.seen_dumps) == 1
    assert engine.seen_dumps[0].mode == DUMP_MODE_DIFF


async def test_use_case_fallback_empty_result_goes_to_budget_notice() -> None:
    """변경 파일이 전부 `patch_missing` 이라 diff dump 가 비면 fallback 이 의미 없다 —
    안내 코멘트 경로로 떨어져야 한다.
    """
    github = _CapturingGitHub()
    exceeded_full = FileDump(
        entries=(),
        total_chars=0,
        excluded=("bin.dat",),
        exceeded_budget=True,
        mode=DUMP_MODE_FULL,
    )
    engine_result = ReviewResult(summary="unused", event=ReviewEvent.COMMENT)
    uc, engine = _use_case(github, exceeded_full, engine_result)

    pr = _pr(
        changed=("bin.dat",),
        patches={},  # patch 누락 — diff 에서도 볼 수 있는 게 없다
    )
    await uc.execute(pr)

    assert engine.seen_dumps == []
    assert len(github.posted_comments) == 1


# ---------------------------------------------------------------------------
# PullRequest.diff_patches immutability (domain 회귀)
# ---------------------------------------------------------------------------


def test_pull_request_diff_patches_is_wrapped_in_mapping_proxy() -> None:
    from types import MappingProxyType

    pr = _pr(patches={"a.py": "@@ -1 +1 @@\n+x\n"})
    assert isinstance(pr.diff_patches, MappingProxyType)
    with pytest.raises(TypeError):
        pr.diff_patches["b.py"] = "nope"  # type: ignore[index]


def test_pull_request_diff_patches_external_mutation_does_not_leak() -> None:
    mutable: dict[str, str] = {"a.py": "patch1"}
    pr = _pr(patches=mutable)
    mutable["b.py"] = "patch2"  # 생성 이후 원본 변경
    assert "b.py" not in pr.diff_patches
