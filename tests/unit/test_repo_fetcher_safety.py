"""Regression coverage for GitRepoFetcher concurrency + secret-handling:
  - 같은 저장소에 대한 session 이 lock 으로 직렬화되는지
  - fetch/checkout 실패 시에도 원격 URL 이 원상 복구되는지 (토큰 누수 방지)
  - 디버그 로그에 토큰이 포함된 URL 이 마스킹되어 기록되는지
  - lock registry 가 WeakValueDictionary 기반이라 활성 락을 evict 하지 않는지
"""

import asyncio
import gc
import logging
from pathlib import Path
from typing import Any

import pytest

from codex_review.domain import PullRequest, RepoRef
from codex_review.infrastructure import git_repo_fetcher


def _pr(owner: str = "o", name: str = "r", head: str = "abc") -> PullRequest:
    return PullRequest(
        repo=RepoRef(owner, name),
        number=1,
        title="t",
        body="",
        head_sha=head,
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://github.com/o/r.git",
        changed_files=(),
        installation_id=7,
        is_draft=False,
    )


async def test_same_repo_sessions_serialize_across_entire_block(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """중요: session 블록 안에서 대기하는 동안 같은 저장소의 다른 session 은 checkout 시작
    조차 못 해야 한다 (이전 구현은 checkout 반환과 동시에 락이 풀려 race 발생).
    """
    checkouts_started: list[str] = []
    release = asyncio.Event()

    async def fake_run(cmd: list[str], *, check: bool = True) -> None:
        if "fetch" in cmd:
            checkouts_started.append(_extract_repo_key(cmd, tmp_path))

    monkeypatch.setattr(git_repo_fetcher, "_run", fake_run)
    fetcher = git_repo_fetcher.GitRepoFetcher(cache_dir=tmp_path)

    second_call_started = asyncio.Event()

    async def first_session() -> None:
        async with fetcher.session(_pr("acme", "a"), "tok") as _:
            # 이 블록 안에서 두 번째 호출이 시도되는지 관찰.
            await asyncio.sleep(0.1)
        release.set()

    async def second_session() -> None:
        second_call_started.set()
        async with fetcher.session(_pr("acme", "a", head="def"), "tok") as _:
            pass

    t1 = asyncio.create_task(first_session())
    # first_session 이 세션 블록 안으로 들어가도록 양보.
    await asyncio.sleep(0.02)
    t2 = asyncio.create_task(second_session())

    # first_session 이 끝나기 전에 두 번째가 실제 checkout(fetch) 를 시작하지 못해야 한다.
    # checkouts_started 에는 첫 번째(acme/a) 하나만 있어야 한다.
    await asyncio.sleep(0.05)
    assert len(checkouts_started) == 1

    await asyncio.gather(t1, t2)
    assert len(checkouts_started) == 2  # 최종적으로는 둘 다 실행


async def test_different_repos_sessions_run_in_parallel(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """다른 저장소 session 은 서로 막지 않고 병렬로 진행돼야 한다."""
    in_flight = 0
    peak = 0
    release = asyncio.Event()

    async def fake_run(cmd: list[str], *, check: bool = True) -> None:
        nonlocal in_flight, peak
        if "fetch" in cmd:
            in_flight += 1
            peak = max(peak, in_flight)
            try:
                await release.wait()
            finally:
                in_flight -= 1

    monkeypatch.setattr(git_repo_fetcher, "_run", fake_run)
    fetcher = git_repo_fetcher.GitRepoFetcher(cache_dir=tmp_path)

    async def gate() -> None:
        for _ in range(200):
            if in_flight >= 2:
                break
            await asyncio.sleep(0.005)
        release.set()

    asyncio.create_task(gate())

    async def run(pr: PullRequest) -> None:
        async with fetcher.session(pr, "tok"):
            pass

    await asyncio.gather(
        run(_pr("acme", "alpha")),
        run(_pr("acme", "beta")),
    )
    assert peak == 2


async def test_remote_url_is_restored_even_when_fetch_fails(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """fetch 에서 예외가 나도 `remote set-url ... <original>` 이 호출돼 토큰이 `.git/config`
    에 남지 않아야 한다.
    """
    restore_calls: list[list[str]] = []

    async def fake_run(cmd: list[str], *, check: bool = True) -> None:
        if "fetch" in cmd:
            raise RuntimeError("boom")
        if "set-url" in cmd and not check:
            restore_calls.append(cmd)

    monkeypatch.setattr(git_repo_fetcher, "_run", fake_run)
    repo_dir = tmp_path / "acme" / "a" / ".git"
    repo_dir.mkdir(parents=True)

    fetcher = git_repo_fetcher.GitRepoFetcher(cache_dir=tmp_path)

    with pytest.raises(RuntimeError, match="boom"):
        async with fetcher.session(_pr("acme", "a"), "secret-token"):
            pass

    assert restore_calls, "fetch 실패 후에도 remote URL 복구가 호출돼야 한다"
    restored_url = restore_calls[-1][-1]
    assert "secret-token" not in restored_url
    assert restored_url == "https://github.com/o/r.git"


async def test_run_kills_subprocess_and_reraises_on_cancel(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회귀(codex inline git_repo_fetcher.py:138): `_run()` 가 취소될 때 생성된 하위 git
    프로세스를 `kill()/wait()` 로 정리하지 않으면, 토큰이 들어간 remote URL 로 백그라운드
    통신을 계속하거나 프로세스 누수가 발생한다.
    """
    kill_calls: list[int] = []
    wait_calls: list[int] = []

    class _HangingProc:
        returncode: int | None = None

        async def communicate(self) -> tuple[bytes, bytes]:
            # 호출자가 `cancel()` 할 때까지 영원히 대기 — 실제 communicate 의 취소 경로를 모사.
            await asyncio.Event().wait()
            return b"", b""  # pragma: no cover - 도달 불가

        def kill(self) -> None:
            kill_calls.append(1)
            self.returncode = -9

        async def wait(self) -> int:
            wait_calls.append(1)
            return -9

    async def fake_create(*_args: Any, **_kwargs: Any) -> _HangingProc:
        return _HangingProc()

    monkeypatch.setattr(
        "codex_review.infrastructure.git_repo_fetcher.asyncio.create_subprocess_exec",
        fake_create,
    )

    task = asyncio.create_task(
        git_repo_fetcher._run(["git", "clone", "https://x-access-token:S@h/r.git", "/tmp"])
    )
    # `_run` 안의 `communicate()` 대기 지점까지 진입할 시간을 준다.
    await asyncio.sleep(0.02)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    # 취소 전파 이전에 하위 프로세스가 반드시 정리돼야 한다 — 토큰 누수·좀비 프로세스 방지.
    assert kill_calls, "취소 시 proc.kill() 이 호출돼야 한다"
    assert wait_calls, "취소 시 proc.wait() 로 수거까지 이뤄져야 한다"


async def test_remote_url_restored_on_cancellation_too(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """CancelledError 경로에서도 URL 복구가 실행돼야 한다 (Gemini 지적)."""
    restore_calls: list[list[str]] = []

    async def fake_run(cmd: list[str], *, check: bool = True) -> None:
        if "fetch" in cmd:
            raise asyncio.CancelledError()
        if "set-url" in cmd and not check:
            restore_calls.append(cmd)

    monkeypatch.setattr(git_repo_fetcher, "_run", fake_run)
    (tmp_path / "acme" / "a" / ".git").mkdir(parents=True)
    fetcher = git_repo_fetcher.GitRepoFetcher(cache_dir=tmp_path)

    with pytest.raises(asyncio.CancelledError):
        async with fetcher.session(_pr("acme", "a"), "secret-token"):
            pass

    assert restore_calls, "취소 경로에서도 URL 복구가 실행돼야 한다"


async def test_exception_message_masks_tokens_in_stderr(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회귀 방지(codex inline): git 이 stderr 에 authed URL 을 실어 보내도 예외 메시지엔
    토큰이 남지 않아야 한다. 실제로 `git clone` 실패는 stderr 에 URL 이 포함된 메시지를
    낼 수 있다.
    """

    class _FailingProc:
        returncode = 128

        async def communicate(self) -> tuple[bytes, bytes]:
            # git 이 내는 전형적인 실패 메시지 — URL 에 토큰 포함.
            stderr = (
                b"fatal: unable to access "
                b"'https://x-access-token:SECRET123@github.com/o/r.git/': "
                b"The requested URL returned error: 404\n"
            )
            return b"", stderr

    async def fake_create(*_args: Any, **_kwargs: Any) -> _FailingProc:
        return _FailingProc()

    monkeypatch.setattr(
        "codex_review.infrastructure.git_repo_fetcher.asyncio.create_subprocess_exec",
        fake_create,
    )

    with pytest.raises(RuntimeError) as exc:
        await git_repo_fetcher._run([
            "git", "clone", "https://x-access-token:SECRET123@github.com/o/r.git", "/tmp/r",
        ])

    msg = str(exc.value)
    assert "SECRET123" not in msg, "예외 메시지에 토큰이 노출돼선 안 된다"
    assert "***@github.com" in msg, "URL 은 마스킹된 형태로 남아야 한다"


def test_mask_tokens_in_text_handles_multiple_urls() -> None:
    text = (
        "first https://x-access-token:AAA@github.com/a.git fail\n"
        "second https://user:BBB@other.example/b.git also fail"
    )
    masked = git_repo_fetcher._mask_tokens_in_text(text)
    assert "AAA" not in masked
    assert "BBB" not in masked
    assert "***@github.com" in masked
    assert "***@other.example" in masked


def test_mask_token_in_url_strips_userinfo() -> None:
    masked = git_repo_fetcher._mask_token_in_url(
        "https://x-access-token:SECRET123@github.com/o/r.git"
    )
    assert "SECRET123" not in masked
    assert masked.startswith("https://***@github.com")
    assert git_repo_fetcher._mask_token_in_url("--force") == "--force"
    assert git_repo_fetcher._mask_token_in_url("/tmp/repo") == "/tmp/repo"


async def test_debug_log_does_not_leak_token(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    class _DummyProc:
        returncode = 0

        async def communicate(self) -> tuple[bytes, bytes]:
            return b"", b""

    async def fake_create(*_args: Any, **_kwargs: Any) -> _DummyProc:
        return _DummyProc()

    monkeypatch.setattr(
        "codex_review.infrastructure.git_repo_fetcher.asyncio.create_subprocess_exec",
        fake_create,
    )

    with caplog.at_level(logging.DEBUG, logger="codex_review.infrastructure.git_repo_fetcher"):
        await git_repo_fetcher._run([
            "git", "clone", "--filter=blob:none",
            "https://x-access-token:TOKEN123@github.com/o/r.git", "/tmp/r",
        ])

    all_text = "\n".join(r.getMessage() for r in caplog.records)
    assert "TOKEN123" not in all_text
    assert "***@github.com" in all_text


# ---------------------------------------------------------------------------
# _RepoLockRegistry: WeakValueDictionary — 활성 락 evict 금지 회귀 방지
# ---------------------------------------------------------------------------


async def test_repo_lock_registry_does_not_evict_live_locks() -> None:
    """잠긴 락이 보유된 동안에는 레지스트리가 절대 같은 키로 새 락을 발급하지 않아야 한다."""
    reg = git_repo_fetcher._RepoLockRegistry()
    lock_a1 = reg.get("o/a")

    async def hold() -> None:
        async with lock_a1:
            await asyncio.sleep(0.05)

    holder = asyncio.create_task(hold())
    # 즉시 양보해서 holder 가 락을 취득하도록
    await asyncio.sleep(0)

    # 다른 요청자도 같은 객체를 받아야 한다 (배타성 유지).
    lock_a2 = reg.get("o/a")
    assert lock_a2 is lock_a1

    # GC 를 명시적으로 돌려도 살아있는 참조 때문에 유지돼야 한다.
    gc.collect()
    lock_a3 = reg.get("o/a")
    assert lock_a3 is lock_a1

    await holder
    # 이제 lock_a1 의 강한 참조가 모두 풀렸으면 weakref 가 정리될 수 있다 —
    # 다만 파이썬 구현에 따라 즉시 GC 는 아니므로 단순 동치 비교는 생략.


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _extract_repo_key(cmd: list[str], cache_root: Path) -> str:
    for i, a in enumerate(cmd):
        if a == "-C" and i + 1 < len(cmd):
            return cmd[i + 1]
    return cmd[-1]
