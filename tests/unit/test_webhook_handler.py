import asyncio
import hashlib
import hmac
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from codex_review.application.review_pr_use_case import ReviewPullRequestUseCase
from codex_review.application.webhook_handler import WebhookHandler
from codex_review.domain import (
    FileDump,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
    TokenBudget,
)

SECRET = "top-secret"


@dataclass
class FakeGitHub:
    posted_reviews: list[tuple[PullRequest, ReviewResult]] = field(default_factory=list)
    posted_comments: list[tuple[PullRequest, str]] = field(default_factory=list)
    pr_to_return: PullRequest | None = None

    async def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest:
        assert self.pr_to_return is not None
        return self.pr_to_return

    async def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        self.posted_reviews.append((pr, result))

    async def post_comment(self, pr: PullRequest, body: str) -> None:
        self.posted_comments.append((pr, body))

    async def get_installation_token(self, installation_id: int) -> str:
        return "fake-token"

    async def fetch_review_history(self, pr, installation_id):
        from codex_review.domain import ReviewHistory
        return ReviewHistory()


@dataclass
class FakeFetcher:
    path: Path

    @asynccontextmanager
    async def session(
        self, pr: PullRequest, installation_token: str
    ) -> AsyncIterator[Path]:
        yield self.path


class FakeCollector:
    def __init__(self, dump: FileDump) -> None:
        self._dump = dump

    async def collect(
        self, root: Path, changed_files: tuple[str, ...], budget: TokenBudget
    ) -> FileDump:
        return self._dump


class FakeEngine:
    def __init__(self, result: ReviewResult) -> None:
        self._result = result

    async def review(self, pr: PullRequest, dump: FileDump, *, history=None) -> ReviewResult:
        return self._result


def _sign(body: bytes) -> str:
    return "sha256=" + hmac.new(SECRET.encode(), body, hashlib.sha256).hexdigest()


def _sample_pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=1,
        title="t",
        body="",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/x.git",
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
    )


def _build_handler(
    github: FakeGitHub,
    dump: FileDump,
    result: ReviewResult,
    tmp: Path,
    concurrency: int = 1,
) -> WebhookHandler:
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp),
        file_collector=FakeCollector(dump),
        engine=FakeEngine(result),
        max_input_tokens=1000,
    )
    return WebhookHandler(
        secret=SECRET, github=github, use_case=use_case, concurrency=concurrency
    )


def test_verify_signature_accepts_valid_and_rejects_invalid(tmp_path: Path) -> None:
    dump = FileDump(entries=(), total_chars=0)
    result = ReviewResult(summary="ok", event=ReviewEvent.COMMENT)
    handler = _build_handler(FakeGitHub(), dump, result, tmp_path)

    body = b'{"a":1}'
    assert handler.verify_signature(_sign(body), body) is True
    assert handler.verify_signature("sha256=wrong", body) is False
    assert handler.verify_signature(None, body) is False


async def test_accept_ignores_non_pr_events(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    code, _ = await handler.accept("issues", "d1", {})
    assert code == 202


async def test_accept_ignores_draft(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": True, "number": 1},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    code, reason = await handler.accept("pull_request", "d2", payload)
    assert code == 202
    assert reason == "skipped-draft"


async def test_accept_rejects_non_numeric_pr_number(tmp_path: Path) -> None:
    """회귀(codex 리뷰 지적): `int(pr["number"])` 가 ValueError 를 던져 FastAPI 가 500 을
    반환하지 않도록, 비정상 payload 는 전부 400 으로 수렴돼야 한다.
    """
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": False, "number": "abc"},   # 공격 payload
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    code, reason = await handler.accept("pull_request", "bad-1", payload)
    assert (code, reason) == (400, "invalid-payload")


async def test_accept_rejects_bool_installation_id(tmp_path: Path) -> None:
    """`isinstance(True, int) is True` 때문에 `int(True) == 1` 이 조용히 통과된다.
    bool 은 스키마 위반이므로 400 으로 거절해야 한다.
    """
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": False, "number": 1},
        "repository": {"full_name": "o/r"},
        "installation": {"id": True},
    }
    code, reason = await handler.accept("pull_request", "bad-2", payload)
    assert (code, reason) == (400, "invalid-payload")


async def test_accept_rejects_missing_installation_field(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": False, "number": 1},
        "repository": {"full_name": "o/r"},
        # installation 필드 자체 없음
    }
    code, reason = await handler.accept("pull_request", "bad-3", payload)
    assert (code, reason) == (400, "invalid-payload")


async def test_accept_rejects_malformed_installation_field(tmp_path: Path) -> None:
    """`installation` 이 dict 가 아니라 string/list 인 경우 `.get` 체이닝에서
    AttributeError 가 날 수 있다 — 검증 헬퍼가 방어해야 한다.
    """
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": False, "number": 1},
        "repository": {"full_name": "o/r"},
        "installation": "oops-not-a-dict",
    }
    code, reason = await handler.accept("pull_request", "bad-4", payload)
    assert (code, reason) == (400, "invalid-payload")


async def test_accept_accepts_numeric_string_pr_number(tmp_path: Path) -> None:
    """관대한 호환성: GitHub 자체는 숫자로 주지만, 프록시·재시도 툴을 거치며 문자열
    "123" 이 올 수 있다. 값만 양의 정수로 변환 가능하면 수용.
    """
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": False, "number": "42"},
        "repository": {"full_name": "o/r"},
        "installation": {"id": "7"},
    }
    code, reason = await handler.accept("pull_request", "ok-str", payload)
    assert (code, reason) == (202, "queued")


async def test_accept_ignores_unsupported_action(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "closed",
        "pull_request": {"number": 1},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    code, _ = await handler.accept("pull_request", "d3", payload)
    assert code == 202


async def test_use_case_posts_comment_when_budget_exceeded(tmp_path: Path) -> None:
    github = FakeGitHub()
    pr = _sample_pr()
    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("a.py",),
        exceeded_budget=True,
        budget=TokenBudget(1),
    )
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=FakeEngine(ReviewResult(summary="x", event=ReviewEvent.COMMENT)),
        max_input_tokens=1,
    )

    await use_case.execute(pr)

    assert github.posted_reviews == []
    assert len(github.posted_comments) == 1
    assert "예산 초과" in github.posted_comments[0][1]


async def test_use_case_posts_review_when_budget_fits(tmp_path: Path) -> None:
    github = FakeGitHub()
    pr = _sample_pr()
    from codex_review.domain import FileEntry

    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),),
        total_chars=3,
        exceeded_budget=False,
    )
    expected = ReviewResult(summary="good", event=ReviewEvent.COMMENT)
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(dump),
        engine=FakeEngine(expected),
        max_input_tokens=1000,
    )

    await use_case.execute(pr)

    assert github.posted_comments == []
    assert len(github.posted_reviews) == 1
    assert github.posted_reviews[0][1] is expected


async def test_followup_runs_on_synchronize_action(tmp_path: Path) -> None:
    """회귀 (Phase 1): `synchronize` 이벤트면 main review 직전에 follow-up use case 가
    호출돼야 한다 (PR 사이드바 unresolved 카운트가 새 review 게시 전에 갱신).
    """
    github = FakeGitHub(pr_to_return=_sample_pr())
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(FileDump(entries=(), total_chars=0)),
        engine=FakeEngine(ReviewResult(summary="ok", event=ReviewEvent.COMMENT)),
        max_input_tokens=1000,
    )

    @dataclass
    class _FakeFollowUp:
        called_with: list[Any] = field(default_factory=list)

        async def execute(self, pr: PullRequest) -> None:
            self.called_with.append(pr.number)

    follow_up = _FakeFollowUp()
    handler = WebhookHandler(
        secret=SECRET, github=github, use_case=use_case,
        follow_up_use_case=follow_up,  # type: ignore[arg-type]
    )
    payload: dict[str, Any] = {
        "action": "synchronize",
        "pull_request": {"draft": False, "number": 7},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    code, _reason = await handler.accept("pull_request", "d-syn", payload)
    assert code == 202

    await handler.start()
    try:
        await asyncio.wait_for(handler._queue.join(), timeout=2.0)  # type: ignore[attr-defined]
    finally:
        await handler.stop()

    # follow-up 이 호출됐고, main review 도 게시됐다.
    # (FakeGitHub.fetch_pull_request 는 _sample_pr() 그대로 돌려주므로 number=1)
    assert follow_up.called_with == [1]
    assert len(github.posted_reviews) == 1


async def test_followup_skipped_on_opened_action(tmp_path: Path) -> None:
    """opened 는 신규 PR 이라 옛 코멘트 자체가 없을 가능성이 크고, 있더라도 main review
    가 새 review 를 어차피 게시하므로 follow-up 으로 중복 트래픽을 만들지 않는다.
    """
    github = FakeGitHub(pr_to_return=_sample_pr())
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(FileDump(entries=(), total_chars=0)),
        engine=FakeEngine(ReviewResult(summary="ok", event=ReviewEvent.COMMENT)),
        max_input_tokens=1000,
    )

    @dataclass
    class _FakeFollowUp:
        called: bool = False

        async def execute(self, pr: PullRequest) -> None:
            self.called = True

    follow_up = _FakeFollowUp()
    handler = WebhookHandler(
        secret=SECRET, github=github, use_case=use_case,
        follow_up_use_case=follow_up,  # type: ignore[arg-type]
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": False, "number": 8},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    await handler.accept("pull_request", "d-open", payload)
    await handler.start()
    try:
        await asyncio.wait_for(handler._queue.join(), timeout=2.0)  # type: ignore[attr-defined]
    finally:
        await handler.stop()

    assert follow_up.called is False  # opened 는 follow-up 대상 아님
    assert len(github.posted_reviews) == 1  # main review 는 정상 게시


async def test_followup_failure_does_not_block_main_review(tmp_path: Path) -> None:
    """follow-up 자체 실패가 main review 까지 막으면 운영 가용성 떨어짐.
    follow-up 예외는 로그로만 남기고 main review 는 진행돼야 한다.
    """
    github = FakeGitHub(pr_to_return=_sample_pr())
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(tmp_path),
        file_collector=FakeCollector(FileDump(entries=(), total_chars=0)),
        engine=FakeEngine(ReviewResult(summary="ok", event=ReviewEvent.COMMENT)),
        max_input_tokens=1000,
    )

    @dataclass
    class _BoomFollowUp:
        async def execute(self, pr: PullRequest) -> None:
            raise RuntimeError("follow-up boom")

    handler = WebhookHandler(
        secret=SECRET, github=github, use_case=use_case,
        follow_up_use_case=_BoomFollowUp(),  # type: ignore[arg-type]
    )
    payload: dict[str, Any] = {
        "action": "synchronize",
        "pull_request": {"draft": False, "number": 9},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    await handler.accept("pull_request", "d-boom", payload)
    await handler.start()
    try:
        await asyncio.wait_for(handler._queue.join(), timeout=2.0)  # type: ignore[attr-defined]
    finally:
        await handler.stop()

    # follow-up 이 터졌지만 main review 는 정상 게시됐어야 한다.
    assert len(github.posted_reviews) == 1


async def test_accept_queues_valid_pr_and_returns_202(tmp_path: Path) -> None:
    handler = _build_handler(
        FakeGitHub(),
        FileDump(entries=(), total_chars=0),
        ReviewResult(summary="ok", event=ReviewEvent.COMMENT),
        tmp_path,
    )
    payload: dict[str, Any] = {
        "action": "opened",
        "pull_request": {"draft": False, "number": 42},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    code, reason = await handler.accept("pull_request", "d4", payload)
    assert code == 202
    assert reason == "queued"


# ---------------------------------------------------------------------------
# Concurrency behavior: Semaphore(N) 이 실제로 동시 실행 상한을 지키는지
# ---------------------------------------------------------------------------


class _SlowEngine:
    """review() 를 두 단계로 나눈 엔진 — 테스트가 '지금 몇 개가 병렬 실행 중인지' 관찰 가능."""

    def __init__(self) -> None:
        self.in_flight = 0
        self.peak = 0
        self.release = asyncio.Event()

    async def review(self, pr: PullRequest, dump: FileDump, *, history=None) -> ReviewResult:
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            await self.release.wait()
            return ReviewResult(summary="done", event=ReviewEvent.COMMENT)
        finally:
            self.in_flight -= 1


async def _queue_two_prs(handler: WebhookHandler) -> None:
    for n in (1, 2):
        await handler.accept(
            "pull_request",
            f"d-{n}",
            {
                "action": "opened",
                "pull_request": {"draft": False, "number": n},
                "repository": {"full_name": "o/r"},
                "installation": {"id": 7},
            },
        )


async def _run_handler_with_engine(engine: _SlowEngine, concurrency: int) -> None:
    github = FakeGitHub(pr_to_return=_sample_pr())
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(Path(".")),
        file_collector=FakeCollector(FileDump(entries=(), total_chars=0)),
        engine=engine,
        max_input_tokens=1000,
    )
    handler = WebhookHandler(
        secret=SECRET, github=github, use_case=use_case, concurrency=concurrency
    )
    await handler.start()
    try:
        await _queue_two_prs(handler)
        # 워커가 작업을 picking up 할 때까지 잠시 대기 (공평한 스케줄링 허용).
        for _ in range(100):
            if engine.in_flight > 0:
                break
            await asyncio.sleep(0.01)
        # 한 번 더 양보해 두 번째 워커도 진입할 기회를 준다.
        await asyncio.sleep(0.05)
        engine.release.set()
        # 큐 소진 대기.
        await asyncio.wait_for(handler._queue.join(), timeout=2.0)  # type: ignore[attr-defined]
    finally:
        await handler.stop()


async def test_concurrency_1_runs_one_at_a_time() -> None:
    engine = _SlowEngine()
    await _run_handler_with_engine(engine, concurrency=1)
    assert engine.peak == 1


async def test_concurrency_2_runs_two_in_parallel() -> None:
    engine = _SlowEngine()
    await _run_handler_with_engine(engine, concurrency=2)
    assert engine.peak == 2


async def test_stop_preserves_in_flight_work_when_queue_is_full() -> None:
    """회귀(gemini inline webhook_handler.py:91): 큐가 가득 찼다는 이유로 graceful
    shutdown 이 즉시 `cancel()` 로 전환되면, 진행 중 리뷰까지 중도 취소된다.
    수정된 `stop()` 은 `대기 job drop → tombstone 삽입 → 타임아웃` 순서로 동작해야
    한다. 이 테스트는 '진행 중 리뷰가 정상 완료된 뒤 자연 종료' 됨을 검증한다.
    """
    github = FakeGitHub(pr_to_return=_sample_pr())

    review_started = asyncio.Event()
    completed_reviews: list[int] = []
    resume = asyncio.Event()

    class _ControlledEngine:
        """워커가 꺼낸 직후 event 를 set → 테스트에서 큐에 추가 job 을 채워 full 상태로 만든 후
        resume 을 set 하면 진행 중 리뷰가 정상 완료되도록 한 엔진.
        """
        async def review(self, pr: PullRequest, dump: FileDump, *, history=None) -> ReviewResult:
            review_started.set()
            await resume.wait()
            completed_reviews.append(pr.number)
            return ReviewResult(summary="done", event=ReviewEvent.COMMENT)

    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(Path(".")),
        file_collector=FakeCollector(FileDump(entries=(), total_chars=0)),
        engine=_ControlledEngine(),
        max_input_tokens=1000,
    )
    handler = WebhookHandler(
        secret=SECRET,
        github=github,
        use_case=use_case,
        concurrency=1,
        queue_maxsize=1,           # 워커 1 + 큐 1 — 두 번째 accept 로 full 상태가 된다.
        shutdown_timeout=2.0,      # 취소로 빠지지 않도록 충분히 길게.
    )
    await handler.start()
    try:
        payload = {
            "action": "opened",
            "pull_request": {"draft": False, "number": 1},
            "repository": {"full_name": "o/r"},
            "installation": {"id": 7},
        }
        # 1) 첫 번째 → 워커가 꺼내 review 시작까지 대기.
        await handler.accept("pull_request", "d-1", payload)
        await asyncio.wait_for(review_started.wait(), timeout=1.0)
        # 2) 두 번째 → 큐에 남는다 (queue_depth=1=max)
        payload2 = {**payload, "pull_request": {"draft": False, "number": 2}}
        code, reason = await handler.accept("pull_request", "d-2", payload2)
        assert (code, reason) == (202, "queued")
        # 3) 이 시점에 stop() 을 호출 — drain 으로 #2 는 drop, 진행 중 #1 은 살아야 한다.
        stop_task = asyncio.create_task(handler.stop())
        await asyncio.sleep(0.05)     # drain+tombstone 까지 도달할 시간.
        assert not stop_task.done(), "shutdown 은 진행 중 리뷰 완료까지 기다려야 한다"
        # 4) 진행 중 리뷰 완료 허가.
        resume.set()
        await asyncio.wait_for(stop_task, timeout=2.0)
    finally:
        resume.set()

    # 기대 계약:
    # - in-flight #1 은 정상 완료 → completed_reviews 에 기록되고 post_review 까지 도달.
    # - drained #2 는 워커가 꺼내기 전 drop → review() 자체가 호출되지 않음.
    assert len(completed_reviews) == 1, (
        f"in-flight 리뷰 1건만 완료돼야 한다 (실제: {completed_reviews})"
    )
    assert len(github.posted_reviews) == 1, (
        "in-flight 리뷰의 post_review 가 정상적으로 호출돼야 한다"
    )


async def test_stop_does_not_deadlock_when_queue_is_full() -> None:
    """회귀(codex/gemini 지적): 큐가 가득 찬 상태에서도 `stop()` 이 유한 시간 안에
    끝나야 한다. 이전 구현은 `await put(None)` 이 무한 대기했거나, 그 패치조차
    `put_nowait` 실패 즉시 in-flight 까지 취소했다. 현재 구현은 drain→tombstone→
    graceful timeout→cancel 순서라서, 워커가 끝나지 않아도 `shutdown_timeout` 을
    약간 넘기는 선에서 확실히 종료돼야 한다.
    """
    github = FakeGitHub(pr_to_return=_sample_pr())
    # engine.review 가 절대 완료되지 않는 엔진 — 워커는 영원히 busy, 큐는 가득 참.
    release = asyncio.Event()

    class _BlockingEngine:
        async def review(self, pr: PullRequest, dump: FileDump, *, history=None) -> ReviewResult:
            await release.wait()  # forever
            return ReviewResult(summary="x", event=ReviewEvent.COMMENT)

    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=FakeFetcher(Path(".")),
        file_collector=FakeCollector(FileDump(entries=(), total_chars=0)),
        engine=_BlockingEngine(),
        max_input_tokens=1000,
    )
    handler = WebhookHandler(
        secret=SECRET,
        github=github,
        use_case=use_case,
        concurrency=1,
        queue_maxsize=1,             # 큐는 곧바로 가득 찬다.
        shutdown_timeout=0.5,        # 타임아웃 자체는 0.5s — 비블로킹 경로가 먼저 차단해야 한다.
    )
    await handler.start()

    # 워커가 busy 에 돌입할 때까지 1개 밀어넣고, 이어서 2개를 더 넣어 큐를 가득 채운다.
    payload = {
        "action": "opened",
        "pull_request": {"draft": False, "number": 42},
        "repository": {"full_name": "o/r"},
        "installation": {"id": 7},
    }
    # 1) 워커가 꺼내 블로킹 시작
    await handler.accept("pull_request", "d-1", payload)
    # 2) 큐에 남아 대기 중 (queue_depth=1 = maxsize)
    await handler.accept("pull_request", "d-2", payload)
    # 3) 이후 들어오는 건 503 queue-full. 그래도 stop 은 블록되면 안 됨.

    started = asyncio.get_running_loop().time()
    # drain 으로 대기 job 버린 뒤 tombstone, 워커는 블로킹이라 timeout 후 cancel 경로.
    # → `shutdown_timeout`(0.5s) + 약간의 cleanup 시간 안에 반드시 끝나야 한다.
    await asyncio.wait_for(handler.stop(), timeout=2.0)
    elapsed = asyncio.get_running_loop().time() - started

    # 1.5s 이내 종료면 "블록 없이 정상적인 종료 경로" 를 탄 것.
    assert elapsed < 1.5, f"stop() 이 {elapsed:.2f}s 걸림 — 큐 full 시 블록됐을 가능성"

    # teardown
    release.set()
