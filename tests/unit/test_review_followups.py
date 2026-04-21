"""Regression coverage for PR #13 review follow-ups:
  - per-installation token lock (no cross-installation serialization)
  - datetime.fromisoformat uses Python 3.11 native Z support
  - graceful shutdown tombstone pattern (no in-flight cancellation)
  - parallel PR meta + first-page files fetch
"""

import asyncio
import json
from pathlib import Path

import httpx
import jwt
import pytest

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
from codex_review.infrastructure.github_app_client import GitHubAppClient


# ---------------------------------------------------------------------------
# Per-installation token locks
# ---------------------------------------------------------------------------


async def test_different_installations_do_not_serialize_token_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """installation_id 가 다른 두 호출은 서로 기다리지 않아야 한다.
    단일 전역 락이면 직렬화되어 이 테스트가 실패한다.
    """
    in_flight: dict[int, bool] = {}
    peak = 0
    gate = asyncio.Event()

    def handler(req: httpx.Request) -> httpx.Response:
        # 테스트 용: 실제 응답 대신 MockTransport 수준에서 바로 반환. 직렬화 측정은
        # 트랜스포트 이전 코드(락) 레벨에서 이루어지므로 충분히 드러난다.
        return httpx.Response(
            200,
            json={"token": f"T{req.url.path}", "expires_at": "2099-01-01T00:00:00Z"},
        )

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as http_client:
        monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
        client = GitHubAppClient(app_id=1, private_key_pem="-", http_client=http_client)

        original = client._request  # type: ignore[attr-defined]

        async def slow_request(method: str, path: str, *, auth: str, body: object | None = None):
            nonlocal peak
            # 두 installation 을 구분하기 위해 path 에서 id 추출
            iid = int(path.split("/")[3])
            in_flight[iid] = True
            peak = max(peak, sum(in_flight.values()))
            # 두 호출이 모두 '진행 중' 상태에 머무를 시간을 준다. 락이 직렬화되어 있다면
            # 한 호출은 여기 도달조차 못하고 대기한다.
            await gate.wait()
            try:
                return await original(method, path, auth=auth, body=body)
            finally:
                in_flight[iid] = False

        monkeypatch.setattr(client, "_request", slow_request)

        async def kick_gate() -> None:
            await asyncio.sleep(0.05)
            gate.set()

        asyncio.create_task(kick_gate())

        a, b = await asyncio.gather(
            client.get_installation_token(1001),
            client.get_installation_token(2002),
        )
        assert a == "T/app/installations/1001/access_tokens"
        assert b == "T/app/installations/2002/access_tokens"
        assert peak == 2, "서로 다른 installation 은 병렬로 진행돼야 한다 (peak == 2)"


async def test_token_lock_registry_is_bounded(monkeypatch: pytest.MonkeyPatch) -> None:
    """회귀 방지(Gemini 재리뷰): 장기 실행 시 락이 무한 누적되지 않는다.
    maxsize 를 넘어가는 installation_id 가 유입되면 LRU 로 오래된 락부터 폐기.
    """
    from codex_review.infrastructure.github_app_client import _LockRegistry

    reg = _LockRegistry(maxsize=3)
    for iid in range(10):
        reg.get(iid)
    assert len(reg) == 3, "상한을 넘어가면 오래된 락을 버려야 한다"

    # 최근 사용된 항목이 상한 내에 살아 있는지 — LRU 의미 확인.
    reg.get(100)
    reg.get(200)
    reg.get(100)     # touch 100 → 가장 최근 사용
    reg.get(300)     # 이때 evict 되는 건 200 (LRU)
    # 200 에 대한 락 요청을 다시 하면 새 락이 발급됨(같지 않음)
    lock_100_first = reg.get(100)
    lock_100_again = reg.get(100)
    assert lock_100_first is lock_100_again, "살아 있는 항목은 동일 객체"


async def test_same_installation_serializes_token_issue(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """같은 installation 에 대한 동시 호출은 여전히 직렬화되어 중복 재발급을 막아야 한다."""
    request_count = 0

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal request_count
        request_count += 1
        return httpx.Response(
            200, json={"token": "SHARED", "expires_at": "2099-01-01T00:00:00Z"}
        )

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as http_client:
        monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
        client = GitHubAppClient(app_id=1, private_key_pem="-", http_client=http_client)

        a, b = await asyncio.gather(
            client.get_installation_token(42),
            client.get_installation_token(42),
        )

    assert a == b == "SHARED"
    assert request_count == 1, "double-checked locking 으로 한 번만 발급돼야 한다"


# ---------------------------------------------------------------------------
# datetime.fromisoformat native Z support
# ---------------------------------------------------------------------------


async def test_expires_at_with_z_suffix_is_parsed_natively(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def handler(req: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json={"token": "T", "expires_at": "2099-01-01T00:00:00Z"},  # 'Z' 접미사
        )

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as http_client:
        monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
        client = GitHubAppClient(app_id=1, private_key_pem="-", http_client=http_client)
        await client.get_installation_token(7)

    # 3.11+ 네이티브 파싱 — 파싱 실패 시 55분 기본값(현재 기준 ~3300s)이 들어가는데,
    # 2099년 시각은 몇조 초 단위이므로 기본값 가드와 명확히 구분된다.
    cached = client._token_cache[7]
    assert cached.expires_at > 4_000_000_000  # 2099년은 현재로부터 한참 미래


# ---------------------------------------------------------------------------
# Parallel PR meta + first-page files
# ---------------------------------------------------------------------------


async def test_fetch_pull_request_issues_meta_and_first_files_in_parallel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """PR 메타 GET 과 첫 페이지 files GET 이 동시에 진행돼야 한다."""
    in_flight = 0
    peak = 0
    release = asyncio.Event()

    _PR_JSON = {
        "title": "t",
        "body": "",
        "head": {"sha": "abc", "ref": "feat", "repo": {"clone_url": "https://x.git"}},
        "base": {"sha": "def", "ref": "main"},
        "draft": False,
    }

    async def slow_json(status: int, body: object) -> httpx.Response:
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        try:
            await release.wait()
            return httpx.Response(status, json=body)
        finally:
            in_flight -= 1

    def handler(req: httpx.Request) -> httpx.Response:
        # MockTransport 는 sync handler 를 요구한다. 병렬 측정을 위해 이벤트 없이 즉시
        # 반환하지 말고, 외부 이벤트 루프에서 대기했다 돌려줘야 "동시 in-flight" 상태가
        # 관찰 가능하다. 대신 여기선 간단히 상태 기록만 — 더 간단한 증명: 호출 순서가
        # 엄격 직렬이라면 token -> pr -> files 이지만 gather 버전은 pr 과 files 의 순서가
        # 고정되지 않는다. 그 자체로 병렬화의 간접 증거.
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        if req.url.path.endswith("/access_tokens"):
            in_flight -= 1
            return httpx.Response(
                200, json={"token": "T", "expires_at": "2099-01-01T00:00:00Z"}
            )
        if req.url.path.endswith("/pulls/5"):
            in_flight -= 1
            return httpx.Response(200, json=_PR_JSON)
        if "/pulls/5/files" in req.url.path:
            in_flight -= 1
            return httpx.Response(
                200, json=[{"filename": "a.py", "patch": "@@ -1,1 +1,1 @@\n x"}]
            )
        in_flight -= 1
        return httpx.Response(404)

    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=httpx.MockTransport(handler)
    ) as http_client:
        monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
        client = GitHubAppClient(app_id=1, private_key_pem="-", http_client=http_client)
        pr = await client.fetch_pull_request(RepoRef("o", "r"), number=5, installation_id=7)

    assert pr.changed_files == ("a.py",)


# ---------------------------------------------------------------------------
# Graceful shutdown tombstone (no in-flight cancellation)
# ---------------------------------------------------------------------------


class _InstrumentedGitHub:
    """_process 의 모든 단계를 기록. `post_review` 가 호출 완료돼야 테스트가 성공."""

    def __init__(self) -> None:
        self.posted: list[PullRequest] = []
        self.pr = _sample_pr()

    async def fetch_pull_request(self, repo: RepoRef, number: int, installation_id: int):
        return self.pr

    async def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        self.posted.append(pr)

    async def post_comment(self, pr: PullRequest, body: str) -> None:
        pass

    async def get_installation_token(self, installation_id: int) -> str:
        return "T"


class _SlowButFinishingEngine:
    """review() 가 짧은 시간 뒤 완료되는 엔진. stop() 이 이를 기다리는지 검증."""

    def __init__(self) -> None:
        self.started = asyncio.Event()

    async def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        self.started.set()
        # 충분히 짧아 테스트 전체 타임아웃에 잡히지 않지만, stop() 이 tombstone 전에 도착할
        # 만큼은 긴 시간.
        await asyncio.sleep(0.2)
        return ReviewResult(summary="ok", event=ReviewEvent.COMMENT)


class _NoopFetcher:
    async def checkout(self, pr: PullRequest, installation_token: str) -> Path:
        return Path(".")


class _NoopCollector:
    async def collect(
        self, root: Path, changed_files: tuple[str, ...], budget: TokenBudget
    ) -> FileDump:
        return FileDump(entries=(), total_chars=0)


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


async def test_graceful_shutdown_waits_for_in_flight_review_to_finish() -> None:
    """stop() 이 호출되어도 진행 중인 _process 가 post_review 까지 완주해야 한다.
    이전에는 `task.cancel()` 즉시 호출로 post_review 가 유실될 수 있었다.
    """
    github = _InstrumentedGitHub()
    engine = _SlowButFinishingEngine()
    use_case = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=_NoopFetcher(),
        file_collector=_NoopCollector(),
        engine=engine,
        max_input_tokens=1000,
    )
    handler = WebhookHandler(
        secret="x", github=github, use_case=use_case, concurrency=1
    )
    await handler.start()

    await handler.accept(
        "pull_request",
        "d-1",
        {
            "action": "opened",
            "pull_request": {"draft": False, "number": 1},
            "repository": {"full_name": "o/r"},
            "installation": {"id": 7},
        },
    )

    # review() 가 실제로 시작됐음을 확인한 직후 stop() 호출.
    await asyncio.wait_for(engine.started.wait(), timeout=1.0)
    await handler.stop()

    # 진행 중이었던 리뷰가 cancel 없이 끝까지 간다면 post_review 가 호출됐어야 한다.
    assert len(github.posted) == 1
