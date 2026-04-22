import logging
from collections.abc import Mapping
from dataclasses import replace

from codex_review.domain import FileDump, Finding, PullRequest, ReviewResult, TokenBudget
from codex_review.interfaces import FileCollector, GitHubClient, RepoFetcher, ReviewEngine

logger = logging.getLogger(__name__)


class ReviewPullRequestUseCase:
    """Orchestrates: fetch PR → checkout → collect files → review → post."""

    def __init__(
        self,
        github: GitHubClient,
        repo_fetcher: RepoFetcher,
        file_collector: FileCollector,
        engine: ReviewEngine,
        max_input_tokens: int,
    ) -> None:
        self._github = github
        self._repo_fetcher = repo_fetcher
        self._file_collector = file_collector
        self._engine = engine
        self._budget = TokenBudget(max_tokens=max_input_tokens)

    async def execute(self, pr: PullRequest) -> None:
        token = await self._github.get_installation_token(pr.installation_id)

        # 저장소 락 범위를 checkout ~ 파일 수집 전체로 확대한다. 이전 구현은 `checkout()`
        # 리턴과 동시에 락이 풀려, 같은 저장소의 다른 PR 이 head SHA 를 바꾸는 동안
        # 이 쪽 collect 가 파일을 읽어 "다른 PR 의 트리" 를 수집하는 경쟁이 있었다.
        async with self._repo_fetcher.session(pr, token) as repo_path:
            dump = await self._file_collector.collect(repo_path, pr.changed_files, self._budget)

        # 이 지점 이후 파일 I/O 없음 — dump 는 메모리에 담긴 스냅샷. 락을 풀어도 안전.

        # 변경 파일이 예산 때문에 잘려 나갔다면 "전체 리뷰"가 성립하지 않는다. 저품질 리뷰를
        # 게시하느니 리뷰를 건너뛰고 운영자에게 조치 방법을 안내 코멘트로 남긴다.
        # 변경 파일이 모두 들어간 경우(잘려도 비변경 파일만 제외)는 그대로 리뷰를 수행한다.
        if dump.exceeded_budget and _changed_missing(pr, dump):
            logger.warning(
                "budget exceeded for %s#%d — skipping review, posting notice",
                pr.repo.full_name,
                pr.number,
            )
            await self._github.post_comment(pr, _budget_exceeded_message(pr, dump))
            return

        logger.info(
            "reviewing %s#%d — files=%d chars=%d excluded=%d",
            pr.repo.full_name,
            pr.number,
            len(dump.entries),
            dump.total_chars,
            len(dump.excluded),
        )
        result = await self._engine.review(pr, dump)

        # 모델이 제안한 인라인 코멘트를 PR diff 의 RIGHT-side 라인 집합과 교차해 걸러낸다.
        # (변경되지 않은 파일/줄에 코멘트를 달면 GitHub 가 422 로 리뷰 전체를 거부한다.)
        # 걸러진 항목은 본문 렌더링에도 반영되도록 ReviewResult 자체를 새로 만든다.
        result = _filter_findings_to_diff(result, pr.diff_right_lines, pr.repo.full_name, pr.number)

        await self._github.post_review(pr, result)


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
    드롭 건수는 로그로 남겨 튜닝/디버깅에 활용한다.
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
            "%s#%d — dropped %d/%d inline finding(s) not on RIGHT-side diff",
            repo_full_name,
            pr_number,
            len(dropped),
            len(result.findings),
        )
        return replace(result, findings=tuple(kept))
    return result


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
