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

실행 순서 (PR #19 review 반영):
  1) GitHub 에서 thread 목록 조회 (락 밖, 단일 GraphQL).
  2) 후보 분류 — repo lock **안에서만** 수행 (로컬 file 읽기). 락 보유 시간 최소화.
  3) 락 해제 후 답글 + resolve API 를 `asyncio.gather` 로 병렬 발사.
  4) 각 thread 별 액션 순서: **resolve 먼저 → 성공 시 reply**.
     이유: reply 가 마커를 박은 뒤 resolve 가 실패하면 다음 push 에서 `has_followup_marker=True`
     로 인식돼 **영원히 후보에서 제외** 되는 stuck 상태 발생. resolve 가 실패하면 답글도
     남기지 않아 다음 push 에서 다시 시도 가능 (gemini + coderabbitai PR #19 Major).
"""

import asyncio
import logging
from dataclasses import dataclass
from pathlib import Path

from codex_review.domain import FOLLOWUP_MARKER, PullRequest, ReviewThread
from codex_review.interfaces import GitHubClient, RepoFetcher

logger = logging.getLogger(__name__)


# 라인 수 카운트할 때 한 번에 읽을 청크 크기. `for _ in f:` 는 줄바꿈이 없는 거대 파일
# (난독화 JS, 바이너리 덤프 등) 에서 전체를 한 줄로 읽어 OOM 유발 위험 (gemini PR #19
# Critical). 청크 단위로 `\n` 만 세면 메모리 사용량이 chunk_size 로 상한.
_LINE_COUNT_CHUNK_BYTES = 64 * 1024

# follow-up 처리에서 GitHub API 동시 호출 상한. 너무 크면 GitHub 의 secondary rate
# limit (REST: 30 mut/min, GraphQL: 동일 수준) 에 걸려 403/429 가 발생, follow-up
# 전체가 흔들린다. 한 PR 당 후보 스레드는 보통 0~10건이고 각 액션은 resolve+reply
# 두 호출이라 실제로는 수십 건 이내. 5 동시성이면 burst 흡수 + rate limit 안전
# (gemini PR #19 Major).
_FOLLOWUP_API_CONCURRENCY = 5


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

        # 1) repo lock 안에서는 로컬 file 읽기만 수행 (네트워크 I/O 금지) — 같은 저장소의
        #    다른 PR 처리가 락 때문에 차단되는 시간을 최소화 (gemini PR #19 Major).
        #    분류는 동기 file I/O 라 `asyncio.to_thread` 로 워커에 오프로드 — 같은 이벤트
        #    루프의 다른 webhook 핸들링이 큰 파일 카운트로 블로킹되지 않게 한다
        #    (gemini PR #19 Major).
        token = await self._github.get_installation_token(pr.installation_id)
        actions: list[tuple[ReviewThread, _Action]] = []
        async with self._repo_fetcher.session(pr, token) as repo_path:
            # Sanity check (codex PR #19 Major): repo_fetcher 의 session() 은 작업 트리
            # 를 pr.head_sha 로 checkout 하기로 계약하지만, 캐시 손상이나 git 의 비정상
            # 동작으로 다른 SHA 에 머무는 극단 상황이 가능하다. 그 상태에서 follow-up 을
            # 진행하면 다른 commit 의 파일 상태로 유효한 review thread 를 잘못 resolve 할
            # 수 있어 silent feedback loss 가 발생. SHA 가 어긋나면 전체 follow-up 을 skip
            # 하고 경고만 남긴다 — main review 는 별도 use case 라 영향 없음.
            actual_sha = await self._repo_fetcher.head_sha(repo_path)
            if actual_sha != pr.head_sha:
                logger.warning(
                    "follow-up: aborting — repo HEAD %s does not match pr.head_sha %s "
                    "on %s#%d (skipping classification to avoid stale-state resolve)",
                    actual_sha, pr.head_sha, pr.repo.full_name, pr.number,
                )
                return
            for thread in candidates:
                action = await asyncio.to_thread(
                    _classify_thread, thread, repo_path
                )
                if action is not None:
                    actions.append((thread, action))

        if not actions:
            logger.info(
                "follow-up: no actionable threads for %s#%d (all candidates require Phase 2)",
                pr.repo.full_name, pr.number,
            )
            return

        # 2) 락 밖에서 API 호출 — `asyncio.gather` 로 병렬화하되, GitHub secondary rate
        #    limit (~30 mut/min) 에 걸리지 않도록 `Semaphore` 로 동시성 상한. 한 thread
        #    실패가 다른 thread 처리를 막지 않도록 `return_exceptions=True`.
        sem = asyncio.Semaphore(_FOLLOWUP_API_CONCURRENCY)
        thread_ids = [thread.id for thread, _ in actions]
        results = await asyncio.gather(
            *(
                self._apply_action_with_limit(sem, pr, thread, action)
                for thread, action in actions
            ),
            return_exceptions=True,
        )
        # 실패는 개수만 합산하지 말고 thread.id 와 traceback 을 같이 남긴다
        # (gemini PR #19 Major). 운영 환경에서 GraphQL rate limit / network 오류 등의
        # 원인을 추적하려면 어떤 thread 가 어떤 예외로 실패했는지 알아야 한다.
        failures = 0
        for thread_id, result in zip(thread_ids, results, strict=True):
            if isinstance(result, BaseException):
                failures += 1
                logger.warning(
                    "follow-up: thread %s failed on %s#%d",
                    thread_id, pr.repo.full_name, pr.number,
                    exc_info=result,
                )
        if failures:
            logger.warning(
                "follow-up: %d/%d thread(s) failed during reply/resolve on %s#%d",
                failures, len(results), pr.repo.full_name, pr.number,
            )

    async def _apply_action_with_limit(
        self,
        sem: asyncio.Semaphore,
        pr: PullRequest,
        thread: ReviewThread,
        action: "_Action",
    ) -> None:
        """`Semaphore` 로 동시 API 호출 수 상한. 본 작업은 `_apply_action` 위임."""
        async with sem:
            await self._apply_action(pr, thread, action)

    async def _apply_action(
        self, pr: PullRequest, thread: ReviewThread, action: "_Action"
    ) -> None:
        """`resolve 먼저 → 성공 시 reply` 순서로 처리.

        이유: reply 가 마커를 박은 뒤 resolve 가 실패하면 thread 는 미해결 상태인데
        다음 실행에서 `has_followup_marker=True` 로 분류돼 영원히 skip 되는 **stuck
        state** 가 발생한다 (gemini + coderabbitai PR #19 Major).

        반대 순서면 resolve 실패 시 마커가 안 박히므로 다음 push 에서 자연스럽게 재시도
        된다. resolve 가 성공하고 reply 가 실패해도 GitHub UI 상으론 thread 가 닫혀
        있어 follow-up 의 관측 가능한 효과(unresolved 카운트 감소) 는 달성된다.
        """
        # resolve 가 실패하면 ReviewEngineError-style raise 가 아니라 평범한 예외라
        # 직접 try 로 감싸 reply 도 안 하고 빠져나오게 한다.
        await self._github.resolve_review_thread(thread.id, pr.installation_id)
        reply_body = _wrap_with_marker(action.reply_body)
        await self._github.reply_to_review_comment(
            pr, thread.root_comment_id, reply_body
        )

    def _is_candidate(self, thread: ReviewThread) -> bool:
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


@dataclass(frozen=True)
class _Action:
    """결정된 follow-up 액션. 본문은 reply 직전 marker 가 wrap 된다."""

    reply_body: str


def _classify_thread(thread: ReviewThread, repo_path: Path) -> _Action | None:
    """현재 head 트리 기준으로 thread 가 가리키는 코드의 상태를 분류.

    보안: GitHub 가 반환한 `thread.path` 는 원칙적으로 repo 경로지만, API 응답이
    예상과 다르거나 (`/etc/passwd`, `../sibling-repo/...`) 같은 이탈 입력이 들어오면
    저장소 밖 파일의 존재 여부를 자동 해소 판정의 근거로 쓰게 돼, 유효한 스레드를
    잘못 닫거나 반대로 닫지 않을 수 있다 (codex + gemini PR #19 Major). `resolve()` 후
    `is_relative_to(repo_root)` 가 아니면 안전하게 무대응으로 떨어뜨려, 분류 단계에선
    "부정확한 정보로 자동 결정하지 않음" 원칙을 지킨다.

    반환값:
      - `_Action` (reply_body 포함) → 답글 게시 + thread resolve
      - `None`                      → 무대응 (라인 그대로 / 수정됨 / 판정 불가 / 경계 이탈)
    """
    repo_root = repo_path.resolve()
    try:
        full_path = (repo_root / thread.path).resolve()
    except OSError:
        # 너무 긴 경로 / 깨진 심볼릭 등은 OS 가 resolve 단계에서 거부할 수 있다.
        logger.warning(
            "follow-up: failed to resolve %r — skipping classification",
            thread.path,
        )
        return None
    if not full_path.is_relative_to(repo_root):
        # 저장소 밖을 가리키는 경로 — GitHub API 가 이런 값을 줄 일은 거의 없지만,
        # 만에 하나 들어와도 로컬 파일시스템 상태로 자동 해소 판정을 하지 않는다.
        logger.warning(
            "follow-up: thread path %r escapes repo root — skipping classification",
            thread.path,
        )
        return None

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
    """파일의 `\\n` 개수 + 마지막 라인이 newline 으로 끝나지 않으면 +1.

    **chunk 기반** 으로 카운트 — `for _ in f:` 는 줄바꿈이 없는 거대 파일에서 전체를
    한 줄로 읽어 메모리 폭주 위험이 있어, 64KB 청크씩 바이트로 읽으면서 `\\n` 만
    세는 방식으로 변경 (gemini PR #19 Critical 반영).
    바이너리 mode 로 열어 인코딩 비용도 절약. 텍스트 디코딩 결과는 라인 카운트에
    영향 없으므로 안전.
    """
    count = 0
    last_byte: int | None = None
    with path.open("rb") as f:
        while True:
            chunk = f.read(_LINE_COUNT_CHUNK_BYTES)
            if not chunk:
                break
            count += chunk.count(b"\n")
            last_byte = chunk[-1]
    # 파일이 newline 으로 끝나지 않는다면 마지막 줄도 1줄로 계산해야 한다 (`\n` 카운트
    # 만 쓰면 누락). 빈 파일은 last_byte 가 None 이므로 자연스럽게 0줄.
    if last_byte is not None and last_byte != 0x0A:
        count += 1
    return count


def normalize_bot_user_login(github_app_slug: str) -> str:
    """`GITHUB_APP_SLUG` 환경변수 값을 GitHub bot login (`<slug>[bot]`) 으로 정규화.

    운영자 실수 방지: `GITHUB_APP_SLUG=codex-review-bot[bot]` 처럼 이미 `[bot]` 이
    포함된 값이 들어오면 그대로 `f"{slug}[bot]"` 으로 합치면 `...[bot][bot]` 라는
    잘못된 login 이 만들어진다. `removesuffix("[bot]")` 로 한 번 벗긴 뒤 다시 붙여
    어떤 입력 형식이든 단일 표준형으로 수렴 (coderabbitai PR #19 Minor 반영).

    공백 트림 + 소문자화는 하지 않는다 — GitHub login 은 대소문자 보존이지만 비교
    시 소문자화는 호출자에서 책임 (현 시점 비교는 정확히 일치 형태로만 사용).
    """
    return f"{github_app_slug.strip().removesuffix('[bot]')}[bot]"


def _wrap_with_marker(body: str) -> str:
    """답글 본문에 멱등성 마커를 박는다. 다음 push 때 같은 스레드에 또 답글 안 달기 위함.

    HTML 주석이라 GitHub UI 에선 안 보임. 마커는 본문 끝에 두어 사람이 읽을 때
    시각적 영향이 0.
    """
    return f"{body}\n\n{FOLLOWUP_MARKER}"
