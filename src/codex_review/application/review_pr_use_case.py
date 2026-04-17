import logging

from codex_review.domain import FileDump, PullRequest, TokenBudget
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

    def execute(self, pr: PullRequest) -> None:
        token = self._github.get_installation_token(pr.installation_id)
        repo_path = self._repo_fetcher.checkout(pr, token)

        dump = self._file_collector.collect(repo_path, pr.changed_files, self._budget)

        # 변경 파일이 예산 때문에 잘려 나갔다면 "전체 리뷰"가 성립하지 않는다. 저품질 리뷰를
        # 게시하느니 리뷰를 건너뛰고 운영자에게 조치 방법을 안내 코멘트로 남긴다.
        # 변경 파일이 모두 들어간 경우(잘려도 비변경 파일만 제외)는 그대로 리뷰를 수행한다.
        if dump.exceeded_budget and _changed_missing(pr, dump):
            logger.warning(
                "budget exceeded for %s#%d — skipping review, posting notice",
                pr.repo.full_name,
                pr.number,
            )
            self._github.post_comment(pr, _budget_exceeded_message(pr, dump))
            return

        logger.info(
            "reviewing %s#%d — files=%d chars=%d excluded=%d",
            pr.repo.full_name,
            pr.number,
            len(dump.entries),
            dump.total_chars,
            len(dump.excluded),
        )
        result = self._engine.review(pr, dump)
        self._github.post_review(pr, result)


def _changed_missing(pr: PullRequest, dump: FileDump) -> bool:
    included = {e.path for e in dump.entries}
    return any(cf not in included for cf in pr.changed_files)


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
