import hashlib
import hmac
import logging
import queue
import threading
from dataclasses import dataclass

from codex_review.domain import RepoRef
from codex_review.interfaces import GitHubClient
from codex_review.logging_utils import get_delivery_logger

from .review_pr_use_case import ReviewPullRequestUseCase

logger = logging.getLogger(__name__)

_SUPPORTED_ACTIONS = {"opened", "synchronize", "reopened", "ready_for_review"}


@dataclass(frozen=True)
class WebhookJob:
    delivery_id: str
    repo: RepoRef
    number: int
    installation_id: int


class WebhookHandler:
    """Verifies webhooks, enqueues review jobs, and drains them serially."""

    def __init__(
        self,
        secret: str,
        github: GitHubClient,
        use_case: ReviewPullRequestUseCase,
    ) -> None:
        self._secret = secret.encode("utf-8")
        self._github = github
        self._use_case = use_case
        self._queue: queue.Queue[WebhookJob] = queue.Queue()
        self._worker: threading.Thread | None = None
        self._stop = threading.Event()

    # --- Lifecycle ----------------------------------------------------------

    def start(self) -> None:
        if self._worker is not None:
            return
        self._stop.clear()
        self._worker = threading.Thread(
            target=self._run, name="review-worker", daemon=True
        )
        self._worker.start()

    def stop(self) -> None:
        self._stop.set()

    # --- Verification -------------------------------------------------------

    def verify_signature(self, signature_header: str | None, body: bytes) -> bool:
        if not signature_header or not signature_header.startswith("sha256="):
            return False
        expected = hmac.new(self._secret, body, hashlib.sha256).hexdigest()
        return hmac.compare_digest(signature_header.removeprefix("sha256="), expected)

    # --- Dispatch -----------------------------------------------------------

    def accept(
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
        self._queue.put(job)
        dlog.info(
            "queued review for %s#%d (queue_depth=%d)",
            job.repo.full_name,
            job.number,
            self._queue.qsize(),
        )
        return 202, "queued"

    # --- Worker -------------------------------------------------------------

    def _run(self) -> None:
        while not self._stop.is_set():
            try:
                job = self._queue.get(timeout=1.0)
            except queue.Empty:
                continue
            self._process(job)
            self._queue.task_done()

    def _process(self, job: WebhookJob) -> None:
        dlog = get_delivery_logger(__name__, job.delivery_id)
        try:
            dlog.info("processing %s#%d", job.repo.full_name, job.number)
            pr = self._github.fetch_pull_request(job.repo, job.number, job.installation_id)
            if pr.is_draft:
                dlog.info("skipping draft at fetch time")
                return
            self._use_case.execute(pr)
            dlog.info("done %s#%d", job.repo.full_name, job.number)
        except Exception:
            dlog.exception("review failed for %s#%d", job.repo.full_name, job.number)
