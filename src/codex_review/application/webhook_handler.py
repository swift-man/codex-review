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

# 큐가 가득 차 거절할 때 운영자가 볼 상한. 기본은 동시성 × 10 으로 잡아 일시적 버스트를
# 흡수하되 메모리가 무한히 쌓이지 않도록 한다.
_DEFAULT_QUEUE_MULTIPLIER = 10


@dataclass(frozen=True)
class WebhookJob:
    delivery_id: str
    repo: RepoRef
    number: int
    installation_id: int


class WebhookHandler:
    """Verifies webhooks, enqueues review jobs, drains them with bounded concurrency.

    구조:
      asyncio.Queue(maxsize=Q) <- `accept()` 가 `put_nowait`. 가득 차면 503.
      N 개의 워커 코루틴         <- 큐에서 꺼내 순차 처리. 워커 수 자체가 동시 실행 상한
                                     이므로 별도 Semaphore 는 불필요 (Gemini 지적 반영).
    """

    def __init__(
        self,
        secret: str,
        github: GitHubClient,
        use_case: ReviewPullRequestUseCase,
        concurrency: int = 1,
        queue_maxsize: int | None = None,
        shutdown_timeout: float = _DEFAULT_SHUTDOWN_TIMEOUT,
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._github = github
        self._use_case = use_case
        self._concurrency = max(1, concurrency)
        qmax = queue_maxsize if queue_maxsize is not None else (
            self._concurrency * _DEFAULT_QUEUE_MULTIPLIER
        )
        # `None` tombstone 으로 graceful shutdown 신호를 보낸다 — 워커가 pop 시 빠져나옴.
        self._queue: asyncio.Queue[WebhookJob | None] = asyncio.Queue(maxsize=qmax)
        self._workers: list[asyncio.Task[None]] = []
        self._shutdown_timeout = shutdown_timeout

    # --- Lifecycle ----------------------------------------------------------

    async def start(self) -> None:
        if self._workers:
            return
        # 워커 개수 = 동시 실행 상한. 각 워커가 큐에서 꺼내 바로 처리하므로 Semaphore 가
        # 있어도 중복된 락 오버헤드일 뿐이다.
        for i in range(self._concurrency):
            task = asyncio.create_task(self._run(), name=f"review-worker-{i}")
            self._workers.append(task)
        logger.info(
            "webhook handler started: concurrency=%d queue_maxsize=%d",
            self._concurrency, self._queue.maxsize,
        )

    async def stop(self) -> None:
        """Graceful shutdown — 진행 중 리뷰는 끝까지, 큐 대기분은 drop 후 종료.

        순서:
          1) 큐에서 '아직 워커가 꺼내지 않은' job 을 먼저 비운다 (GitHub 가 재전송하거나
             운영자가 수동 재처리). busy 워커 본인은 건드리지 않으므로 진행 중 리뷰는
             그대로 완료까지 진행된다.
          2) 이제 확보된 큐 공간에 worker 수만큼 tombstone 을 `put_nowait` — 블로킹 없음.
          3) `shutdown_timeout` 동안 tombstone 도달 후 자연 종료를 기다린다.
          4) 타임아웃을 초과하면 그때서야 `cancel()` 로 강제 종료.

        이전 구현은 큐가 가득 찬 상태에서 `put_nowait` 이 실패하자마자 즉시
        `_cancel_workers()` 로 진행 중 리뷰까지 죽였다 — Gemini 지적.
        """
        dropped = self._drain_pending_jobs()

        failed_tombstone = False
        for _ in self._workers:
            try:
                self._queue.put_nowait(None)
            except asyncio.QueueFull:
                # maxsize < concurrency 인 엣지 케이스에만 해당. graceful 보장이 어렵다.
                logger.warning(
                    "cannot enqueue tombstone after draining %d job(s); "
                    "queue_maxsize=%d < concurrency=%d",
                    dropped, self._queue.maxsize, self._concurrency,
                )
                failed_tombstone = True
                break

        if not failed_tombstone:
            try:
                async with asyncio.timeout(self._shutdown_timeout):
                    await asyncio.gather(*self._workers, return_exceptions=True)
            except TimeoutError:
                logger.warning(
                    "graceful shutdown exceeded %.0fs; cancelling workers",
                    self._shutdown_timeout,
                )
                self._cancel_workers()
        else:
            self._cancel_workers()

        # 최종 정리 — CancelledError 는 정상 신호로 suppress, 다른 예외는 가시성 위해 로그.
        for task in self._workers:
            with contextlib.suppress(asyncio.CancelledError):
                try:
                    await task
                except Exception:
                    logger.exception("worker task crashed during shutdown")

        self._workers.clear()
        logger.info(
            "webhook handler stopped (dropped %d pending job(s) at shutdown)", dropped
        )

    def _drain_pending_jobs(self) -> int:
        """큐에 남아 있는 `WebhookJob` 만 버려서 tombstone 삽입 공간을 확보.

        이 메서드는 워커가 실제로 '처리 중' 인 job 은 건드리지 않는다 — 그 job 은 이미
        `queue.get()` 으로 꺼내져 워커 로컬 상태에 있기 때문. 따라서 '취소하지 않고
        완료까지 기다린다' 는 graceful 의 계약은 유지된다.

        tombstone(None) 이 어떤 이유로 이미 들어 있다면 다시 삽입할 것이므로 여기서
        함께 비워도 무방하다.
        """
        dropped = 0
        while True:
            try:
                item = self._queue.get_nowait()
            except asyncio.QueueEmpty:
                break
            self._queue.task_done()
            if item is None:
                continue
            dropped += 1
            logger.warning(
                "dropping pending webhook at shutdown: %s#%d (delivery=%s)",
                item.repo.full_name, item.number, item.delivery_id,
            )
        return dropped

    def _cancel_workers(self) -> None:
        for task in self._workers:
            task.cancel()

    # --- Verification -------------------------------------------------------

    def verify_signature(self, signature_header: str | None, body: bytes) -> bool:
        # 원문 body 로 HMAC 계산. json.loads 후 재직렬화하면 서명이 달라져 정상 요청 거부.
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
        # 큐가 가득 차면 즉시 거절 — GitHub 가 재전송하거나 운영자가 원인을 찾도록.
        # 무제한 큐는 Codex 쿼터 장애·장시간 리뷰 시 메모리와 대기시간을 무한 증가시킬 수 있다.
        try:
            self._queue.put_nowait(job)
        except asyncio.QueueFull:
            dlog.warning(
                "webhook queue full (maxsize=%d); rejecting %s#%d",
                self._queue.maxsize, job.repo.full_name, job.number,
            )
            return 503, "queue-full"

        dlog.info(
            "queued review for %s#%d (queue_depth=%d/%d)",
            job.repo.full_name,
            job.number,
            self._queue.qsize(),
            self._queue.maxsize,
        )
        return 202, "queued"

    # --- Worker -------------------------------------------------------------

    async def _run(self) -> None:
        # 워커 수가 곧 동시성 상한. 별도 semaphore 없이 바로 처리 — 단순화.
        while True:
            job = await self._queue.get()
            try:
                if job is None:
                    # Graceful shutdown tombstone. 워커 하나를 종료.
                    return
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
