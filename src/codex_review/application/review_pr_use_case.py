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
    ReviewEngineError,
)
from codex_review.logging_utils import redact_text

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

        # ── 1차 fallback: PRE-EMPTIVE (사전 예산 계산 기반) ────────────────
        # 변경 파일이 **예산 때문에** 잘려 나갔다면 전체-코드베이스 리뷰가 성립하지 않는다.
        # 단 바이너리/정책 필터로 제외된 변경 파일(예: .png) 은 fallback 을 트리거하면 안 된다
        # — 의미상 "diff 에서 봐도 못 보는 파일" 이라 fallback 해봐야 품질만 떨어진다.
        if dump.exceeded_budget and _changed_trimmed_by_budget(pr, dump):
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

        # ── 2차 fallback: REACTIVE (엔진 실패 기반) ───────────────────────
        # 우리 예산 추정(`max_tokens × 4 chars`)은 모델의 실제 토큰 한도와 다를 수 있다.
        # 특히 한글 등 멀티바이트 코드베이스에서는 우리가 "fit" 으로 판정해도 모델이 입력
        # 거부 → `codex exec` 가 returncode 1 로 실패. 이때 봇이 그대로 죽으면 PR 에 아무
        # 메시지도 안 달려 운영 가시성이 크게 떨어진다. 따라서 **full 모드에서 엔진이
        # 실패하면 자동으로 diff 모드로 재시도** 해 가용성을 보장한다.
        result = await self._review_with_fallback(pr, dump)
        if result is None:
            return  # 진단 코멘트 게시 후 정리 종료

        # 모델이 제안한 인라인 코멘트를 PR diff 의 RIGHT-side 라인 집합과 교차해 걸러낸다.
        # (변경되지 않은 파일/줄에 코멘트를 달면 GitHub 가 422 로 리뷰 전체를 거부한다.)
        # 걸러진 항목은 본문 렌더링에도 반영되도록 ReviewResult 자체를 새로 만든다.
        result = _filter_findings_to_diff(result, pr.diff_right_lines, pr.repo.full_name, pr.number)

        await self._github.post_review(pr, result)

    async def _review_with_fallback(
        self, pr: PullRequest, dump: FileDump
    ) -> ReviewResult | None:
        """엔진 호출을 시도하고, full 모드 실패 시 diff 모드로 재시도. 둘 다 실패하면
        PR 에 진단 코멘트를 게시하고 None 반환 — 호출자가 종료하도록 한다.

        반환값:
          - 성공한 `ReviewResult` (full 또는 diff 모드, 배지 prepend 포함)
          - 모든 시도 실패 시 None (이미 진단 코멘트 게시 완료)
        """
        try:
            result = await self._engine.review(pr, dump)
        except ReviewEngineError as exc:
            # 이미 diff 모드인데 또 실패 → 더 줄일 수 없다. 진단 코멘트 + 종료.
            if dump.mode == DUMP_MODE_DIFF:
                logger.exception(
                    "engine failed in diff-only mode for %s#%d — no further fallback",
                    pr.repo.full_name, pr.number,
                )
                await self._github.post_comment(
                    pr, _engine_failure_message(pr, dump, exc, attempted_diff=True)
                )
                return None

            # full 모드 실패 — 모델이 입력 거부했을 가능성 높다. diff 모드로 재시도.
            # 예외 타입은 항상 ReviewEngineError 라 type(exc).__name__ 은 정보가 없어
            # 마스킹된 메시지(str(exc)) 를 직접 노출 (gemini PR #18 Minor 반영).
            logger.warning(
                "engine failed on full mode for %s#%d — retrying in diff-only mode (cause: %s)",
                pr.repo.full_name, pr.number, str(exc),
            )
            fallback_dump = await self._try_diff_fallback(pr)
            if fallback_dump is None:
                # diff fallback 자체가 불가 — patch 없거나 운영자가 옵트아웃.
                logger.exception(
                    "engine failed and diff fallback unavailable for %s#%d",
                    pr.repo.full_name, pr.number,
                )
                await self._github.post_comment(
                    pr, _engine_failure_message(pr, dump, exc, attempted_diff=False)
                )
                return None
            try:
                result = await self._engine.review(pr, fallback_dump)
            except ReviewEngineError as retry_exc:
                logger.exception(
                    "engine retry in diff mode also failed for %s#%d",
                    pr.repo.full_name, pr.number,
                )
                await self._github.post_comment(
                    pr,
                    _engine_failure_message(
                        pr, fallback_dump, retry_exc, attempted_diff=True
                    ),
                )
                return None
            dump = fallback_dump  # 이후 배지 결정 용

        # diff-only 모드로 수행된 리뷰는 본문 상단에 배지를 달아, 리뷰어가 "왜 전체
        # 코드베이스 지적이 얕은지" 를 바로 인지하도록 한다.
        if dump.mode == DUMP_MODE_DIFF:
            result = _prepend_diff_scope_badge(result, dump)
        return result

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
            # 두 카테고리(patch 누락 vs 예산 컷) 를 함께 노출해 운영자가 원인을 정확히
            # 추적할 수 있게 한다 (gemini PR #18 Minor 반영).
            logger.warning(
                "diff fallback produced empty dump for %s#%d "
                "(patch_missing=%d, budget_trimmed=%d)",
                pr.repo.full_name, pr.number,
                len(diff_dump.patch_missing), len(diff_dump.budget_trimmed),
            )
            return None
        if diff_dump.exceeded_budget:
            logger.info(
                "diff fallback partial for %s#%d — %d files truncated by budget",
                pr.repo.full_name, pr.number, len(diff_dump.budget_trimmed),
            )
        logger.info(
            "falling back to diff-only review for %s#%d — files=%d chars=%d",
            pr.repo.full_name, pr.number, len(diff_dump.entries), diff_dump.total_chars,
        )
        return diff_dump


def _changed_trimmed_by_budget(pr: PullRequest, dump: FileDump) -> bool:
    """변경 파일 중 **예산 초과로** 덤프에서 빠진 파일이 있는지.

    이전 `_changed_missing` 은 정책(바이너리/크기) 으로 제외된 파일까지 "누락" 으로
    판정해 불필요한 diff fallback 을 유발했다. `dump.budget_trimmed` 는 이제 정확히
    예산 컷 집합만 담으므로 여기서 교차 검사만 하면 된다 (gemini 리뷰 Major 반영).

    `set.isdisjoint` 가 `any(... in set ...)` 보다 C 레벨 최적화로 더 빠르다 — 큰
    PR 에서 micro perf 이긴 하지만 표현이 깔끔 (gemini PR #18 Suggestion).
    """
    budget_cut = set(dump.budget_trimmed)
    if not budget_cut:
        return False
    return not budget_cut.isdisjoint(pr.changed_files)


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
    lines = [
        "> ⚠️ **리뷰 범위: diff-only (자동 전환)**",
        "> 전체 코드베이스가 입력 예산(`CODEX_MAX_INPUT_TOKENS`) 을 초과하여 "
        "PR 의 unified patch 만 근거로 리뷰했습니다.",
        f"> 포함된 diff 파일 {len(dump.entries)}건, "
        f"예산 초과로 제외 {len(dump.budget_trimmed)}건, "
        f"patch 누락 {len(dump.patch_missing)}건.",
        "",
    ]
    return replace(result, summary="\n".join(lines) + result.summary)


def _make_code_fence_safe(text: str) -> str:
    """입력 안의 ``` 시퀀스를 zero-width-space 로 분리해 markdown 코드펜스 깨짐 방어.

    PR 진단 코멘트는 detail 을 ``` … ``` 코드펜스에 감싸 게시하는데, detail 자체에
    ``` 가 있으면 GitHub 마크다운이 fence 를 그 위치에서 닫아 본문 나머지가 깨진다.
    각 백틱 사이에 U+200B(zero-width space) 를 끼워 시각적으로는 거의 같지만 fence
    파서엔 더 이상 ``` 로 인식되지 않게 만든다.
    """
    return text.replace("```", "`\u200b`\u200b`")


def _engine_failure_message(
    pr: PullRequest,
    dump: FileDump,
    exc: BaseException,
    *,
    attempted_diff: bool,
) -> str:
    """엔진 호출이 모두 실패했을 때 PR 에 게시할 진단 코멘트.

    `attempted_diff` 가 True 면 full→diff 재시도까지 둘 다 실패한 상황. False 면
    diff fallback 이 아예 불가능해서 시도조차 안 한 상황 (patch 없음·옵트아웃 등).

    PR 에 아무 메시지도 안 달리는 "조용한 실패" 를 막는 것이 목적 — 운영자가
    무엇이 잘못됐는지 즉시 인지할 수 있도록 모드/모델/원인을 노출한다.

    보안 고려:
      - `str(exc)` 는 stderr 마지막 줄을 포함할 수 있어 토큰 URL / 인증 헤더가
        섞일 위험이 있다. 엔진 단에서 마스킹했더라도 다른 ReviewEngine 구현에서
        새 누출 표면이 생길 수 있으므로 **본문 게시 직전 한 번 더 redact_text**
        를 적용 (defense-in-depth, codex PR #18 Critical+Major 반영).
      - 코드펜스 안에 백틱 3개가 들어 있으면 ``` 가 풀려 본문 전체 markdown 이
        깨진다. detail 의 백틱을 zero-width-space 로 분리해 fence 깨짐 방어
        (codex PR #18 Suggestion 반영).
    """
    detail = redact_text(str(exc))
    if len(detail) > 1000:
        detail = detail[:1000] + "…"
    detail = _make_code_fence_safe(detail)

    mode_desc = "diff-only 모드까지 재시도" if attempted_diff else "full 모드만 시도"
    advice = (
        "1. `CODEX_MAX_INPUT_TOKENS` 를 모델 실제 윈도우보다 작게 조정 "
        "(예: 150000) → 큰 PR 은 자동 diff 모드로 떨어집니다.\n"
        "2. 더 큰 컨텍스트 윈도우의 모델로 `CODEX_MODEL` 변경.\n"
        "3. 서버 로그(stderr 전체) 를 확인해 모델/CLI 측 메시지 검증.\n"
    )
    if not attempted_diff:
        advice += (
            "4. `CODEX_ENABLE_DIFF_FALLBACK=true` 확인 또는 GitHub 가 patch 를 반환했는지 "
            "확인 (큰 PR / binary 변경만으로 구성된 경우 patch 누락 가능).\n"
        )

    return (
        "⚠️ **Codex Review — 리뷰 엔진 실패**\n\n"
        f"이 PR 은 자동 리뷰를 완료하지 못했습니다 ({mode_desc}).\n\n"
        f"- 마지막 시도 모드: `{dump.mode}`\n"
        f"- 컨텍스트 파일 수: {len(dump.entries)}\n"
        f"- 실패 원인:\n"
        f"```\n{detail}\n```\n\n"
        "**조치 제안**\n"
        f"{advice}"
    )


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
