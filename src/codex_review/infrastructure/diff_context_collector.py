"""전체 코드베이스 컨텍스트가 예산을 초과한 PR 에 대해 unified patch 만으로 리뷰
가능한 `FileDump` 를 만드는 collector.

핵심 결정:
  - GitHub 가 돌려준 patch 원문을 그대로 `FileEntry.content` 에 담는다. `@@ -a,b +c,d @@`
    hunk 헤더 + `+`/`-`/` ` 접두가 이미 LLM 에게 변경 범위를 명확히 전달한다.
  - 파일 단위 우선순위는 PR.changed_files 의 원래 순서를 유지한다. diff 를 GitHub 는
    대체로 중요도·변경량 순에 가깝게 돌려주는 경향이 있고, 임의 재정렬보다는 결정론적
    순서가 운영자가 뭐가 포함되고 뭐가 잘렸는지 추적하기 쉽다.
  - 예산 초과가 되는 순간 이후 파일을 건너뛰되 완전히 drop 한다. 부분 patch 를 잘라
    보내면 hunk 경계가 깨져 라인 번호 해석이 어긋날 수 있다 — 정확성을 희생하느니
    전체 단위로 빠지는 편이 안전.
  - **프롬프트 고정 오버헤드(system rules · PR metadata · SCOPE 섹션) 를 예약한 뒤**
    남은 공간에만 patch 를 담는다. 예산 판정을 patch 본문만으로 하면 최종
    `build_prompt()` 결과가 실제로는 `CODEX_MAX_INPUT_TOKENS` 를 넘어 `codex exec`
    단계에서 실패한다 — codex 리뷰 Major 지적 반영.
  - `patch_missing` 는 변경 파일 중 GitHub 가 patch 를 주지 않은 항목 (rename / delete /
    binary / 거대 diff). 리뷰 본문 배지로 운영자에게 노출한다.
"""

import logging
from collections.abc import Callable

from codex_review.domain import (
    DUMP_MODE_DIFF,
    FileDump,
    FileEntry,
    PullRequest,
    TokenBudget,
)

from .codex_prompt import build_prompt

logger = logging.getLogger(__name__)

PromptLengthEstimator = Callable[[PullRequest, FileDump], int]
# 이전 이름 호환용 alias — 콜러가 overhead 만 재는 이름으로 주입하던 자리도 그대로.
OverheadEstimator = PromptLengthEstimator


def _default_prompt_length(pr: PullRequest, dump: FileDump) -> int:
    """실 운영 기본값 — `build_prompt()` 결과의 **UTF-8 바이트 길이** 를 반환.

    char 수가 아니라 바이트 수를 쓰는 이유: 한글·이모지 같은 멀티바이트 문자가 많은
    patch 에서는 char 수가 실제 stdin 입력량을 과소평가한다 (예: 한글 1자 = 3 bytes).
    `TokenBudget.max_chars()` 에 대한 비교는 수치적으로 "bytes 가 chars 보다 같거나 큼"
    이라 더 **보수적**인 방향으로만 틀어져, 예산 초과를 회피하는 fallback 의 원래
    목적에 부합한다 (codex PR #17 Major 지적 반영).

    collector 는 이 함수를 두 번 쓴다:
      (1) 초기 오버헤드 산정 — 빈 덤프 기준
      (2) 최종 검증 — 실제 담은 entries + SCOPE 섹션 포함한 전체 프롬프트
    테스트는 `lambda pr, dump: 0` 같은 stub 을 주입해 두 경로 모두 중립화할 수 있다.
    """
    return len(build_prompt(pr, dump).encode("utf-8"))


def _build_dump(
    entries: list[FileEntry],
    budget_trimmed: list[str],
    patch_missing: tuple[str, ...],
    budget: TokenBudget | None,
) -> FileDump:
    """중간 상태에서 최종 FileDump 를 재구성하는 헬퍼 (최종 검증 루프용).

    `total_chars` 는 FileDump 필드명 호환을 위해 이름은 유지하지만, diff 모드에서는
    실제로 UTF-8 바이트 수를 담는다 (`FileEntry.size_bytes` 와 단위 통일).
    """
    return FileDump(
        entries=tuple(entries),
        total_chars=sum(e.size_bytes for e in entries),
        excluded=tuple(budget_trimmed) + patch_missing,
        exceeded_budget=bool(budget_trimmed),
        budget=budget,
        mode=DUMP_MODE_DIFF,
        patch_missing=patch_missing,
    )


class DiffContextCollector:
    """`DiffContextCollector` Protocol 의 기본 구현 — 원문 patch 를 예산 안에서 축적.

    `overhead_estimator` 는 "빈 덤프 + 확정된 patch_missing" 상태의 프롬프트 크기를
    돌려주는 콜백. 기본은 실제 `build_prompt()` 를 쓰지만, 단위 테스트는 고정값/0 을
    돌려주는 stub 을 주입해 overhead 비의존 truncation 동작만 분리 검증할 수 있다.
    """

    def __init__(self, overhead_estimator: OverheadEstimator | None = None) -> None:
        # 하나의 함수가 "dump 가 주어졌을 때 최종 프롬프트 길이" 를 모두 답한다.
        # 이름은 BC 유지 (이전 인자 이름 overhead_estimator) 지만 의미가 확장됐음.
        self._prompt_length = overhead_estimator or _default_prompt_length

    async def collect_diff(self, pr: PullRequest, budget: TokenBudget) -> FileDump:
        # `TokenBudget.max_chars()` 는 이름상 char 기준이지만, 실 운영에서 더 보수적인
        # UTF-8 바이트 기준으로 비교한다 (한글/이모지 많은 patch 의 CJK 과소평가 방어).
        # `_default_prompt_length` 가 bytes 를 반환하므로 같은 단위로 비교.
        budget_units = budget.max_chars()
        # 1st pass: patch 없는 파일을 먼저 분류한다. SCOPE 섹션에 들어가므로 오버헤드
        # 계산에도 정확히 반영돼야 한다.
        patch_missing = tuple(p for p in pr.changed_files if p not in pr.diff_patches)

        # 오버헤드 산정: "빈 덤프 + 이미 아는 patch_missing" 으로 프롬프트를 한 번 만들어
        # 그 길이를 측정한다. budget_trimmed 목록은 이 시점에 알 수 없지만, SCOPE 섹션에서
        # 각 항목이 차지하는 바이트는 수십 바이트 수준이라 실무적으로 무시 가능한 오차.
        overhead_estimate_dump = FileDump(
            entries=(),
            total_chars=0,
            excluded=patch_missing,
            patch_missing=patch_missing,
            mode=DUMP_MODE_DIFF,
            budget=budget,
        )
        overhead_bytes = self._prompt_length(pr, overhead_estimate_dump)
        # 오버헤드가 예산 전체를 삼킨 경우 정직하게 0 반환. use case 가 "빈 덤프" 로
        # 판정해 fallback 불가 안내로 떨어지게 한다. 인위적 floor 로 "사실상 초과" 상태
        # 를 숨기면 codex 단계에서 더 혼란스러운 실패로 이어진다.
        patch_budget = max(0, budget_units - overhead_bytes)

        # Early return: 오버헤드만으로 예산이 다 소진됐다면 patch 파일 순회해도 전부
        # budget_trimmed 처리만 하게 된다. 불필요한 반복을 생략하고 같은 결과를 즉시
        # 반환한다 (gemini PR #17 Minor 지적 반영). patch 가 있는 변경 파일은 "예산 컷"
        # 으로 기록해 리뷰 본문/프롬프트 SCOPE 에 정확히 노출.
        if patch_budget <= 0:
            logger.warning(
                "diff collector: overhead %d bytes already exceeds budget %d — "
                "skipping file loop, marking all patched changed files as budget-trimmed",
                overhead_bytes, budget_units,
            )
            all_trimmed = tuple(p for p in pr.changed_files if p in pr.diff_patches)
            return FileDump(
                entries=(),
                total_chars=0,
                excluded=all_trimmed + patch_missing,
                exceeded_budget=True,
                budget=budget,
                mode=DUMP_MODE_DIFF,
                patch_missing=patch_missing,
            )

        entries: list[FileEntry] = []
        budget_trimmed: list[str] = []
        total_bytes = 0
        budget_full = False  # early return 이후 경로이므로 예산은 양수 보장.

        for path in pr.changed_files:
            patch = pr.diff_patches.get(path)
            if patch is None:
                # 이미 1st pass 에서 patch_missing 리스트에 담긴 파일 — skip.
                continue

            if budget_full:
                # 이미 예산이 찼으므로 더 담지 않지만, 이 파일이 "리뷰되지 않았다" 는 사실은
                # budget_trimmed 에 남겨 배지·프롬프트 SCOPE 섹션에 정확히 표시되도록 한다.
                budget_trimmed.append(path)
                continue

            # `@@ -... +... @@` hunk 가 이미 파일 경로를 포함하지 않는다. LLM 이 어떤
            # 파일의 변경인지 알 수 있도록 얇은 파일 헤더를 붙여 내보낸다.
            # 예산 누적·비교·`FileEntry.size_bytes` 모두 동일한 UTF-8 바이트 길이를
            # 사용해 단위를 통일 (codex PR #17 Major 지적).
            body = f"=== PATCH: {path} ===\n{patch.rstrip()}\n"
            size_bytes = len(body.encode("utf-8"))

            if total_bytes + size_bytes > patch_budget:
                budget_trimmed.append(path)
                budget_full = True
                logger.warning(
                    "diff collector: patch budget exceeded after %d entries (%d/%d bytes, "
                    "overhead=%d) — dropping %s and subsequent changed files",
                    len(entries), total_bytes, patch_budget, overhead_bytes, path,
                )
                continue

            entries.append(
                FileEntry(path=path, content=body, size_bytes=size_bytes, is_changed=True)
            )
            total_bytes += size_bytes

        # `FileDump.total_chars` 필드명은 역사적 호환을 위해 유지하지만, diff 모드에서는
        # 실제로 UTF-8 바이트 수가 담긴다 (_build_dump 가 entries.size_bytes 를 합산).
        dump = _build_dump(entries, budget_trimmed, patch_missing, budget)

        # 4th pass — **최종 검증**: 오버헤드 산정은 budget_trimmed SCOPE 섹션이 아직
        # 비어 있는 상태로 했다. 이제 그 목록이 생겼으므로 실제 `build_prompt()` 길이를
        # 한 번 더 측정해서 예산을 넘으면 뒤에서부터 entries 를 떨어뜨린다.
        # 떨어진 entry 는 budget_trimmed 쪽으로 옮겨져 SCOPE 섹션에 추가되지만,
        # 그 추가 overhead 까지 포함해 re-check 하므로 단조 감소 → 반드시 수렴한다
        # (codex PR #17 Major 지적 반영).
        dump = self._enforce_final_prompt_budget(pr, dump, budget_units)

        logger.info(
            "diff collector: files=%d total_chars=%d (patch_budget=%d, overhead=%d) "
            "budget_trimmed=%d patch_missing=%d",
            len(dump.entries), dump.total_chars, patch_budget, overhead_bytes,
            len(dump.budget_trimmed), len(dump.patch_missing),
        )
        return dump

    def _enforce_final_prompt_budget(
        self, pr: PullRequest, dump: FileDump, max_chars: int
    ) -> FileDump:
        """`build_prompt(pr, dump)` 결과가 `max_chars` 이하가 될 때까지 뒤에서부터 entry
        를 떨어뜨린다. 떨어진 entry 는 `budget_trimmed` 로 승격.

        각 반복은 entry 1개 제거 + prompt 1회 재생성. entries 가 더는 없어도 오버헤드
        자체가 초과인 케이스(운영자가 비정상적으로 작은 예산 설정) 는 그대로 반환 —
        상위 use case 가 "빈 덤프 → 안내 코멘트" 경로로 처리한다.

        성능: 루프 안에서는 `append` 로 trimmed 목록을 쌓고(O(1) × N), 반복 종료 후
        `pr.changed_files` 순서로 한 번에 정렬한다. 이전 구현의 `list.insert(0, ...)`
        는 원소당 O(N) 이어서 전체 O(N²) 였음 (gemini PR #17 Suggestion 반영).
        루프 중간 `_build_dump` 가 쓰는 길이 측정에는 순서가 영향 없으므로(목록 항목
        수만 계산됨) 정렬은 맨 마지막에만 하면 된다.
        """
        entries = list(dump.entries)
        # 초기 budget_trimmed 와 루프에서 추가되는 항목을 분리 관리.
        initial_trimmed = list(dump.budget_trimmed)
        new_victims: list[str] = []
        patch_missing = dump.patch_missing
        budget = dump.budget

        current_len = self._prompt_length(pr, dump)
        while current_len > max_chars and entries:
            victim = entries.pop()
            new_victims.append(victim.path)
            # 임시 dump — 순서는 길이 산정에 영향 없으므로 append 조합 그대로 사용.
            tmp_trimmed = initial_trimmed + new_victims
            dump = _build_dump(entries, tmp_trimmed, patch_missing, budget)
            new_len = self._prompt_length(pr, dump)
            logger.warning(
                "diff collector: final prompt %d > max %d — trimmed %s "
                "(new prompt %d)",
                current_len, max_chars, victim.path, new_len,
            )
            current_len = new_len

        if not new_victims:
            return dump

        # 최종 정렬: `pr.changed_files` 의 원래 인덱스 기준으로 한 번에 sort.
        # 리뷰어가 "어느 순서로 잘렸는가" 를 추적할 수 있도록 결정론적 순서 보장.
        original_index = {p: i for i, p in enumerate(pr.changed_files)}
        # 알 수 없는 path 는 리스트 맨 뒤로 (방어적).
        sentinel = len(pr.changed_files)
        sorted_trimmed = sorted(
            initial_trimmed + new_victims,
            key=lambda p: original_index.get(p, sentinel),
        )
        return _build_dump(entries, sorted_trimmed, patch_missing, budget)
