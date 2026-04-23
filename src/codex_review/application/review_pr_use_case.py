import logging
from collections.abc import Mapping
from dataclasses import replace

from codex_review.domain import (
    DUMP_MODE_DIFF,
    FileDump,
    Finding,
    PullRequest,
    ReviewResult,
    TokenBudget,
)
from codex_review.interfaces import (
    DiffContextCollector,
    FileCollector,
    GitHubClient,
    RepoFetcher,
    ReviewEngine,
)

logger = logging.getLogger(__name__)


class ReviewPullRequestUseCase:
    """Orchestrates: fetch PR → checkout → collect files → review → post.

    전체-코드베이스 리뷰가 예산 초과로 성립하지 않을 때, diff-only 모드로 **자동
    fallback** 하여 unified patch 만 가지고 리뷰를 게시한다. diff 조차 예산을 넘으면
    그때서야 리뷰를 포기하고 안내 코멘트만 남긴다.
    """

    def __init__(
        self,
        github: GitHubClient,
        repo_fetcher: RepoFetcher,
        file_collector: FileCollector,
        engine: ReviewEngine,
        max_input_tokens: int,
        diff_context_collector: DiffContextCollector | None = None,
    ) -> None:
        self._github = github
        self._repo_fetcher = repo_fetcher
        self._file_collector = file_collector
        self._engine = engine
        self._budget = TokenBudget(max_tokens=max_input_tokens)
        # None 이면 fallback 을 비활성화한다 (기존 동작: 예산 초과 시 리뷰 스킵).
        # 운영자가 명시적으로 옵트인 할 수 있도록 DI 경계에서 결정.
        self._diff_collector = diff_context_collector

    async def execute(self, pr: PullRequest) -> None:
        token = await self._github.get_installation_token(pr.installation_id)

        # 저장소 락 범위를 checkout ~ 파일 수집 전체로 확대한다. 이전 구현은 `checkout()`
        # 리턴과 동시에 락이 풀려, 같은 저장소의 다른 PR 이 head SHA 를 바꾸는 동안
        # 이 쪽 collect 가 파일을 읽어 "다른 PR 의 트리" 를 수집하는 경쟁이 있었다.
        async with self._repo_fetcher.session(pr, token) as repo_path:
            dump = await self._file_collector.collect(repo_path, pr.changed_files, self._budget)

        # 이 지점 이후 파일 I/O 없음 — dump 는 메모리에 담긴 스냅샷. 락을 풀어도 안전.

        # 변경 파일이 예산 때문에 잘려 나갔다면 전체-코드베이스 리뷰가 성립하지 않는다.
        # diff-only 모드 fallback 이 설정돼 있으면 시도, 아니면 안내만 게시.
        if dump.exceeded_budget and _changed_missing(pr, dump):
            fallback_dump = await self._try_diff_fallback(pr)
            if fallback_dump is None:
                logger.warning(
                    "budget exceeded for %s#%d — skipping review, posting notice",
                    pr.repo.full_name, pr.number,
                )
                await self._github.post_comment(pr, _budget_exceeded_message(pr, dump))
                return
            dump = fallback_dump

        logger.info(
            "reviewing %s#%d — mode=%s files=%d chars=%d excluded=%d",
            pr.repo.full_name, pr.number, dump.mode,
            len(dump.entries), dump.total_chars, len(dump.excluded),
        )
        result = await self._engine.review(pr, dump)

        # 모델이 제안한 인라인 코멘트를 PR diff 의 RIGHT-side 라인 집합과 교차해 걸러낸다.
        # (변경되지 않은 파일/줄에 코멘트를 달면 GitHub 가 422 로 리뷰 전체를 거부한다.)
        # 걸러진 항목은 본문 렌더링에도 반영되도록 ReviewResult 자체를 새로 만든다.
        result = _filter_findings_to_diff(result, pr.diff_right_lines, pr.repo.full_name, pr.number)

        # diff-only 모드로 수행된 리뷰는 본문 상단에 배지를 달아, 리뷰어가 "왜 전체
        # 코드베이스 지적이 얕은지" 를 바로 인지하도록 한다. 모델이 아닌 infra 가 덧붙임.
        if dump.mode == DUMP_MODE_DIFF:
            result = _prepend_diff_scope_badge(result, dump)

        await self._github.post_review(pr, result)

    async def _try_diff_fallback(self, pr: PullRequest) -> FileDump | None:
        """diff-only 모드로 fallback 가능 여부를 판단해 성공 시 새 dump 를 반환."""
        if self._diff_collector is None:
            # 운영자가 fallback 을 끈 상태 — 기존 "포기" 경로 유지.
            return None
        if not pr.diff_patches:
            # GitHub 가 patch 를 단 한 건도 돌려주지 않음 (초거대 PR / binary-only 등).
            # diff 모드로도 볼 게 없으므로 fallback 의미가 없다.
            logger.warning(
                "diff fallback unavailable: no patches present for %s#%d",
                pr.repo.full_name, pr.number,
            )
            return None
        diff_dump = await self._diff_collector.collect_diff(pr, self._budget)
        if not diff_dump.entries:
            # 전부 patch_missing 이거나 예산 초과로 하나도 못 담았음 — 의미 없는 리뷰 방지.
            logger.warning(
                "diff fallback produced empty dump for %s#%d (patch_missing=%d)",
                pr.repo.full_name, pr.number, len(diff_dump.patch_missing),
            )
            return None
        if diff_dump.exceeded_budget:
            # excluded = budget_trimmed + patch_missing (collector 에서 보장)
            # → budget_trimmed 개수는 O(1) 뺄셈으로 구할 수 있다 (gemini 리뷰 지적).
            budget_trimmed_count = len(diff_dump.excluded) - len(diff_dump.patch_missing)
            logger.info(
                "diff fallback partial for %s#%d — %d files truncated by budget",
                pr.repo.full_name, pr.number, budget_trimmed_count,
            )
        logger.info(
            "falling back to diff-only review for %s#%d — files=%d chars=%d",
            pr.repo.full_name, pr.number, len(diff_dump.entries), diff_dump.total_chars,
        )
        return diff_dump


def _changed_missing(pr: PullRequest, dump: FileDump) -> bool:
    included = {e.path for e in dump.entries}
    return any(cf not in included for cf in pr.changed_files)


def _filter_findings_to_diff(
    result: ReviewResult,
    diff_right_lines: Mapping[str, frozenset[int]],
    repo_full_name: str,
    pr_number: int,
) -> ReviewResult:
    """Drop findings whose (path, line) is not in the PR's RIGHT-side diff.

    diff 정보가 비어 있으면(fetch 실패나 테스트 더블) 보수적으로 전부 드롭한다.
    드롭 건수는 로그로 남기고, **드롭된 finding 은 `dropped_findings` 에 누적해 리뷰
    본문에서 접이식 섹션으로 보존** 한다 (codex/gemini PR #17 지적 반영).
    이렇게 하지 않으면 라인 번호가 어긋난 순간 지적 자체가 조용히 사라져 리뷰 품질
    을 과대평가할 위험이 있다.
    """
    if not result.findings:
        return result

    kept: list[Finding] = []
    dropped: list[Finding] = []
    for f in result.findings:
        allowed = diff_right_lines.get(f.path)
        if allowed is not None and f.line in allowed:
            kept.append(f)
        else:
            dropped.append(f)

    if dropped:
        logger.info(
            "%s#%d — dropped %d/%d inline finding(s) not on RIGHT-side diff "
            "(preserved in body as collapsible section)",
            repo_full_name,
            pr_number,
            len(dropped),
            len(result.findings),
        )
        return replace(
            result,
            findings=tuple(kept),
            # 이전 단계에서 이미 dropped 된 항목(예: 422 재시도) 과 누적해야 한다.
            dropped_findings=result.dropped_findings + tuple(dropped),
        )
    return result


def _prepend_diff_scope_badge(result: ReviewResult, dump: FileDump) -> ReviewResult:
    """diff-only 모드 리뷰임을 알리는 안내를 summary 최상단에 붙인다.

    `summary` 에 주입하는 이유: `ReviewResult.render_body()` 가 `summary` 를 본문
    최상단에 렌더링하므로, 리뷰어가 제목 바로 밑에서 배지를 보게 된다. 별도 필드를
    추가해 도메인 모델을 오염시키는 것보다 간단하고 가시성이 동일.
    """
    patch_missing = dump.patch_missing
    # set 을 한 번만 생성해 O(N*M) 을 O(N+M) 으로 줄인다 (gemini 리뷰 지적).
    missing_set = set(patch_missing)
    budget_trimmed = tuple(p for p in dump.excluded if p not in missing_set)

    lines = [
        "> ⚠️ **리뷰 범위: diff-only (자동 전환)**",
        "> 전체 코드베이스가 입력 예산(`CODEX_MAX_INPUT_TOKENS`) 을 초과하여 "
        "PR 의 unified patch 만 근거로 리뷰했습니다.",
        f"> 포함된 diff 파일 {len(dump.entries)}건, "
        f"예산 초과로 제외 {len(budget_trimmed)}건, patch 누락 {len(patch_missing)}건.",
        "",
    ]
    return replace(result, summary="\n".join(lines) + result.summary)


def _budget_exceeded_message(pr: PullRequest, dump: FileDump) -> str:
    budget = dump.budget
    max_tokens = budget.max_tokens if budget is not None else 0
    included = len(dump.entries)
    excluded = len(dump.excluded)
    return (
        "⚠️ **Codex Review — 컨텍스트 예산 초과**\n\n"
        f"본 저장소의 전체 코드 크기가 설정된 입력 한도(`CODEX_MAX_INPUT_TOKENS={max_tokens}`)"
        "를 초과하여 리뷰를 수행하지 않았습니다.\n\n"
        f"- 포함된 파일: {included}개\n"
        f"- 제외된 파일: {excluded}개 (변경 파일 일부 포함)\n\n"
        "다음 중 하나를 조치해 주세요:\n"
        "1. PR 범위를 줄여 변경 파일이 컨텍스트에 들어가도록 분할\n"
        "2. `.codex-reviewignore` 등으로 제외 규칙 확장\n"
        "3. `CODEX_MAX_INPUT_TOKENS` 값을 상향 조정 (모델 컨텍스트 허용 범위 내)\n"
    )
