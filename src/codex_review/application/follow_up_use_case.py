"""Phase 1 follow-up 처리 — 봇이 단 옛 라인 코멘트를 새 push 기준으로 결정론적
판정 (LLM 호출 없음).

판정 규칙:
  1) 파일 자체가 PR 의 head SHA 에서 사라짐    → "📁 자동 해소" 답글 + thread resolve.
  2) 라인 번호가 EOF 를 넘음 (그 줄이 잘림)    → "📐 자동 해소" 답글 + thread resolve.
  3) 라인이 그대로 유지됨                       → 무대응 (계속 유효, 스팸 방지).
  4) 라인이 수정됨                              → 무대응 (Phase 2 후보, LLM 판정 필요).

다음 경우 thread 자체를 처음부터 후보에서 제외:
  - 우리 봇이 단 root 가 아님 (다른 봇 / 사람 / 다른 GitHub App)
  - 이미 resolved 상태 (운영자가 이미 닫음)
  - 사람·다른 봇의 답글이 이미 있음 (대화 진행 중 — 자동 끼어들기 금지)
  - 우리가 이전에 follow-up 답글을 이미 단 스레드 (멱등성, FOLLOWUP_MARKER 검사)
  - GitHub 가 line=None 으로 outdated 처리한 스레드 (자체 처리됨)
"""

import logging

from codex_review.domain import PullRequest
from codex_review.interfaces import GitHubClient, RepoFetcher
from codex_review.infrastructure.github_app_client import FOLLOWUP_MARKER

logger = logging.getLogger(__name__)


class FollowUpReviewUseCase:
    """`bot_user_login` 가 단 unresolved 스레드 중 deterministic 판정 가능한 것만
    답글 + resolve 처리한다.

    `bot_user_login` 은 `f"{GITHUB_APP_SLUG}[bot]"` 형태 (예: "codex-review-bot[bot]").
    이 값이 None 이면 use case 자체가 wiring 되지 않는다 (옵트인 설계).
    """

    def __init__(
        self,
        github: GitHubClient,
        repo_fetcher: RepoFetcher,
        bot_user_login: str,
    ) -> None:
        self._github = github
        self._repo_fetcher = repo_fetcher
        self._bot_user_login = bot_user_login

    async def execute(self, pr: PullRequest) -> None:
        threads = await self._github.list_review_threads(pr, pr.installation_id)
        candidates = [t for t in threads if self._is_candidate(t)]

        if not candidates:
            logger.info(
                "follow-up: no candidate threads for %s#%d (total=%d)",
                pr.repo.full_name, pr.number, len(threads),
            )
            return

        logger.info(
            "follow-up: %d candidate thread(s) on %s#%d",
            len(candidates), pr.repo.full_name, pr.number,
        )

        token = await self._github.get_installation_token(pr.installation_id)
        async with self._repo_fetcher.session(pr, token) as repo_path:
            for thread in candidates:
                action = _classify_thread(thread, repo_path)
                if action is None:
                    continue  # 라인 그대로 / 수정만 됨 — 결정 불가, 스킵
                reply_body = _wrap_with_marker(action.reply_body)
                try:
                    await self._github.reply_to_review_comment(
                        pr, thread.root_comment_id, reply_body
                    )
                    await self._github.resolve_review_thread(thread.id, pr.installation_id)
                except Exception:
                    logger.exception(
                        "follow-up: failed to post reply/resolve for thread %s on %s#%d",
                        thread.id, pr.repo.full_name, pr.number,
                    )

    def _is_candidate(self, thread) -> bool:
        if thread.root_author_login != self._bot_user_login:
            return False
        if thread.is_resolved:
            return False
        if thread.has_non_root_author_reply:
            return False
        if thread.has_followup_marker:
            return False
        if thread.line is None:
            # GitHub 가 outdated 처리해 line 이 끊긴 스레드 — 별도 follow-up 의미 X.
            return False
        return True


# ---------------------------------------------------------------------------
# 분류 로직 (테스트 분리 가능하도록 모듈 함수)
# ---------------------------------------------------------------------------


from dataclasses import dataclass
from pathlib import Path

from codex_review.domain import ReviewThread


@dataclass(frozen=True)
class _Action:
    """결정된 follow-up 액션. 본문은 reply 직전 marker 가 wrap 된다."""

    reply_body: str


def _classify_thread(thread: ReviewThread, repo_path: Path) -> _Action | None:
    """현재 head 트리 기준으로 thread 가 가리키는 코드의 상태를 분류.

    반환값:
      - `_Action` (reply_body 포함) → 답글 게시 + thread resolve
      - `None`                      → 무대응 (라인 그대로 / 수정됨 / 판정 불가)
    """
    full_path = repo_path / thread.path
    if not full_path.exists() or not full_path.is_file():
        return _Action(
            reply_body=(
                "📁 **자동 해소** — 이 파일이 PR 의 최신 커밋에서 더 이상 존재하지 않습니다."
            )
        )

    # 파일 라인 수만 알면 충분 — 전체 본문을 메모리에 올릴 필요 없다.
    try:
        line_count = _count_lines(full_path)
    except OSError:
        # 권한·심볼릭 깨짐 등은 안전한 쪽으로 무대응.
        logger.warning(
            "follow-up: could not count lines of %s — skipping classification",
            thread.path,
        )
        return None

    if thread.line is not None and thread.line > line_count:
        return _Action(
            reply_body=(
                f"📐 **자동 해소** — 라인 `{thread.line}` 이 PR 의 최신 커밋에서 더 이상 "
                f"존재하지 않습니다 (현재 파일 {line_count}줄)."
            )
        )

    # Phase 1 의 결정론적 신호로는 더 이상 답할 수 없음. 라인이 변경됐는지 / 그대로인지
    # 는 hunk diff 비교가 필요해 Phase 2 (LLM 판정) 에서 처리.
    return None


def _count_lines(path: Path) -> int:
    """텍스트 라인 카운트 — 바이너리/이상 인코딩에도 견디게 errors='replace'."""
    count = 0
    with path.open("r", encoding="utf-8", errors="replace") as f:
        for _ in f:
            count += 1
    return count


def _wrap_with_marker(body: str) -> str:
    """답글 본문에 멱등성 마커를 박는다. 다음 push 때 같은 스레드에 또 답글 안 달기 위함.

    HTML 주석이라 GitHub UI 에선 안 보임. 마커는 본문 끝에 두어 사람이 읽을 때
    시각적 영향이 0.
    """
    return f"{body}\n\n{FOLLOWUP_MARKER}"
