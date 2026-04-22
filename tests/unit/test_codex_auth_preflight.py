import asyncio
from typing import Any

import pytest

from codex_review.infrastructure.codex_cli_engine import CodexAuthError, CodexCliEngine


class _FakeProc:
    def __init__(self, returncode: int, stdout: bytes = b"", stderr: bytes = b"") -> None:
        self.returncode = returncode
        self._stdout = stdout
        self._stderr = stderr

    async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
        return self._stdout, self._stderr

    async def wait(self) -> int:
        return self.returncode

    def kill(self) -> None:
        pass


def _patch_subprocess(monkeypatch: pytest.MonkeyPatch, result: Any) -> None:
    async def fake_create(*_args: Any, **_kwargs: Any) -> Any:
        if isinstance(result, Exception):
            raise result
        return result

    monkeypatch.setattr(
        "codex_review.infrastructure.codex_cli_engine.asyncio.create_subprocess_exec",
        fake_create,
    )


def _engine() -> CodexCliEngine:
    return CodexCliEngine(binary="codex", model="gpt-5.4")


async def test_verify_auth_passes_when_logged_in_on_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(monkeypatch, _FakeProc(0, stdout=b"Logged in using ChatGPT\n"))
    assert (await _engine().verify_auth()).startswith("Logged in")


async def test_verify_auth_passes_when_logged_in_on_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    """codex CLI 는 non-TTY 환경에서 상태를 stderr 로 보낸다."""
    _patch_subprocess(monkeypatch, _FakeProc(0, stderr=b"Logged in using ChatGPT\n"))
    assert (await _engine().verify_auth()).startswith("Logged in")


async def test_verify_auth_raises_when_not_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(monkeypatch, _FakeProc(1, stderr=b"Not logged in"))
    with pytest.raises(CodexAuthError) as exc:
        await _engine().verify_auth()
    assert "codex login" in str(exc.value)


async def test_verify_auth_raises_on_unexpected_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(monkeypatch, _FakeProc(0, stdout=b"Some unrelated output\n"))
    with pytest.raises(CodexAuthError):
        await _engine().verify_auth()


async def test_verify_auth_raises_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    _patch_subprocess(monkeypatch, FileNotFoundError("codex: not found"))
    with pytest.raises(CodexAuthError) as exc:
        await _engine().verify_auth()
    assert "CODEX_BIN" in str(exc.value)


async def test_verify_auth_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    class _TimingOutProc(_FakeProc):
        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            # `asyncio.timeout` 컨텍스트 매니저 안에서 `communicate` 가 느려 TimeoutError 가
            # 발생하는 상황을 직접 재현 — 예외 자체를 던져 같은 경로를 타게 한다.
            raise TimeoutError()

    _patch_subprocess(monkeypatch, _TimingOutProc(0))

    with pytest.raises(CodexAuthError) as exc:
        await _engine().verify_auth()
    assert "10초" in str(exc.value)


async def test_verify_auth_kills_subprocess_on_cancellation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """서버 종료/워커 취소로 `CancelledError` 가 전파되면 하위 프로세스가 좀비로 남지 않도록
    반드시 kill 되고 취소는 재전파돼야 한다.
    """
    events: list[str] = []

    class _CancelledInCommunicate(_FakeProc):
        async def communicate(self, input: bytes | None = None) -> tuple[bytes, bytes]:
            raise asyncio.CancelledError()

        def kill(self) -> None:
            events.append("kill")

        async def wait(self) -> int:
            events.append("wait")
            return -9

    _patch_subprocess(monkeypatch, _CancelledInCommunicate(0))

    with pytest.raises(asyncio.CancelledError):
        await _engine().verify_auth()

    assert events == ["kill", "wait"], "취소 시 kill → wait 순으로 정리돼야 한다"
