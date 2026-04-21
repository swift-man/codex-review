import asyncio
import contextlib
import hashlib
import hmac
import logging
from dataclasses import dataclass

from codex_review.domain import RepoRef
from codex_review.interfaces import GitHubClient
from codex_review.logging_utils import get_delivery_logger

from .review_pr_use_case import ReviewPullRequestUseCase

logger = logging.getLogger(__name__)

_SUPPORTED_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}

# Graceful shutdown 의 기본 타임아웃(초). 진행 중 리뷰가 이보다 오래 걸리면 강제 취소.
_DEFAULT_SHUTDOWN_TIMEOUT = 60.0


@dataclass(frozen=True)
class WebhookJob:
    delivery_id: str
    repo: RepoRef
    number: int
    installation_id: int


class WebhookHandler:
    """Verifies webhooks, enqueues review jobs, drains them with bounded concurrency.

    구조:
      asyncio.Queue <- `accept()` 가 넣고
      N 개의 워커 코루틴 <- 병렬로 꺼내 처리하되, `asyncio.Semaphore(concurrency)` 로
                         동시 실행 수를 제한. 기본 N=1 (직렬) — 필요 시 env 로 상향.
    """

    def __init__(
        self,
        secret: str,
        github: GitHubClient,
        use_case: ReviewPullRequestUseCase,
        concurrency: int = 1,
        shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT,
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._github = github
        self._use_case = use_case
        self._concurrency = max(1, concurrency)
        # `None` 은 종료 신호(tombstone). 큐 pop 시점에 워커가 자연스럽게 빠져나간다 —
        # 진행 중 `_process` 를 도중에 끊어 리뷰가 유실되는 것을 방지한다.
        self._queue: asyncio.Queue[WebhookJob | None] = asyncio.Queue()
        self._workers: list[asyncio.Task[None]] = []
        self._sem = asyncio.Semaphore(self._concurrency)
        self._shutdown_timeout = shutdown_timeout

    # --- Lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._workers:
            return
        # Semaphore 가 동시 실행 상한을 관리하므로 워커 수를 concurrency 와 맞춰 만들면
        # "큐에서 꺼내는 일" 과 "실제 실행" 이 모두 병렬 가능.
        for i in range(self._concurrency):
            task = asyncio.create_task(self._run(), name=f"review-worker-{i}")
            self._workers.append(task)
        logger.info("webhook handler started with concurrency=%d", self._concurrency)

    async def stop(self) -> None:
        """Graceful shutdown — 진행 중 리뷰는 완료시키고 큐 잔여는 drain 후 종료.

        타임아웃(기본 60s) 을 넘기면 워커를 강제 취소한다 — 운영 환경의 짧은 graceful
        window (k8s/systemd) 를 넘지 않도록 제한을 둔다.
        """
        # 각 워커 앞으로 tombstone 하나씩 — 진행 중 작업을 완료한 뒤 자연 종료.
        for _ in self._workers:
            await self._queue.put(None)

        try:
            async with asyncio.timeout(self._shutdown_timeout):
                await asyncio.gather(*self._workers, return_exceptions=True)
        except TimeoutError:
            logger.warning(
                "graceful shutdown exceeded %.0fs; cancelling workers",
                self._shutdown_timeout,
            )
            for task in self._workers:
                task.cancel()
            # 취소 후에도 최종 await 로 리소스 정리. CancelledError 는 정상 신호이므로 suppress,
            # 그 외 예외는 조용히 넘기지 말고 가시성을 위해 exception 로그로 남긴다.
            for task in self._workers:
                with contextlib.suppress(asyncio.CancelledError):
                    try:
                        await task
                    except Exception:
                        logger.exception("worker task crashed during shutdown")

        self._workers.clear()
        logger.info("webhook handler stopped")

    # --- Verification -------------------------------------------------------

    def verify_signature(self, signature_header: str | None, body: bytes) -> bool:
        # 원문 body 로 HMAC 계산. json.loads 후 재직렬화하면 서명이 달라져 정상 요청을 거부.
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature_header.removeprefix("sha256="), expected)

    # --- Dispatch -----------------------------------------------------------

    async def accept(
        self,
        event: str,
        delivery_id: str,
        payload: dict,
    ) -> tuple[int, str]:
        dlog = get_delivery_logger(__name__, delivery_id)
        if event == "ping":
            return 200, "pong"
        if event != "pull_request":
            dlog.info("ignoring event: %s", event)
            return 202, "ignored"

        action = str(payload.get("action", ""))
        if action not in _SUPPORTED_ACTIONS:
            dlog.info("ignoring action: %s", action)
            return 202, "ignored-action"

        pr = payload.get("pull_request") or {}
        # webhook payload 의 draft 값과 실제 처리 시점 상태가 다를 수 있어 _process 에서 재확인.
        if bool(pr.get("draft")):
            dlog.info("skipping draft PR")
            return 202, "skipped-draft"

        repo_full = str(payload.get("repository", {}).get("full_name", ""))
        if "/" not in repo_full:
            dlog.warning("missing repository full_name in payload")
            return 400, "invalid-payload"
        owner, name = repo_full.split("/", 1)

        number = int(pr.get("number", 0))
        installation_id = int(payload.get("installation", {}).get("id", 0))
        if number == 0 or installation_id == 0:
            dlog.warning("missing number=%s or installation_id=%s", number, installation_id)
            return 400, "invalid-payload"

        job = WebhookJob(
            delivery_id=delivery_id,
            repo=RepoRef(owner=owner, name=name),
            number=number,
            installation_id=installation_id,
        )
        await self._queue.put(job)
        dlog.info(
            "queued review for %s#%d (queue_depth=%d)",
            job.repo.full_name,
            job.number,
            self._queue.qsize(),
        )
        return 202, "queued"

    # --- Worker -------------------------------------------------------------

    async def _run(self) -> None:
        # 동시성 상한은 Semaphore 로 통제. 모든 워커가 같은 세마포어를 공유하므로 워커 수와
        # 무관하게 순간 병렬 실행 수는 `concurrency` 를 넘지 않는다.
        while True:
            job = await self._queue.get()
            try:
                if job is None:
                    # Graceful shutdown tombstone. 워커 하나를 종료.
                    return
                async with self._sem:
                    await self._process(job)
            finally:
                self._queue.task_done()

    async def _process(self, job: WebhookJob) -> None:
        dlog = get_delivery_logger(__name__, job.delivery_id)
        try:
            dlog.info("processing %s#%d", job.repo.full_name, job.number)
            pr = await self._github.fetch_pull_request(
                job.repo, job.number, job.installation_id
            )
            if pr.is_draft:
                dlog.info("skipping draft at fetch time")
                return
            await self._use_case.execute(pr)
            dlog.info("done %s#%d", job.repo.full_name, job.number)
        except asyncio.CancelledError:
            raise
        except Exception:
            dlog.exception("review failed for %s#%d", job.repo.full_name, job.number)
