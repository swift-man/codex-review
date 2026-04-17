import subprocess
from typing import Any

import pytest

from codex_review.infrastructure.codex_cli_engine import CodexAuthError, CodexCliEngine


class _FakeCompleted:
    def __init__(self, returncode: int, stdout: str = "", stderr: str = "") -> None:
        self.returncode = returncode
        self.stdout = stdout
        self.stderr = stderr


def _engine() -> CodexCliEngine:
    return CodexCliEngine(binary="codex", model="gpt-5.4")


def test_verify_auth_passes_when_logged_in_on_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(0, "Logged in using ChatGPT\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _engine().verify_auth().startswith("Logged in")


def test_verify_auth_passes_when_logged_in_on_stderr(monkeypatch: pytest.MonkeyPatch) -> None:
    # codex CLI 가 non-TTY 환경에서 상태를 stderr 로 보내는 실제 동작을 재현.
    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(0, "", "Logged in using ChatGPT\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    assert _engine().verify_auth().startswith("Logged in")


def test_verify_auth_raises_when_not_logged_in(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(1, "", "Not logged in")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexAuthError) as exc:
        _engine().verify_auth()
    assert "codex login" in str(exc.value)


def test_verify_auth_raises_on_unexpected_stdout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        return _FakeCompleted(0, "Some unrelated output\n")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexAuthError):
        _engine().verify_auth()


def test_verify_auth_raises_when_binary_missing(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        raise FileNotFoundError("codex: not found")

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexAuthError) as exc:
        _engine().verify_auth()
    assert "CODEX_BIN" in str(exc.value)


def test_verify_auth_raises_on_timeout(monkeypatch: pytest.MonkeyPatch) -> None:
    def fake_run(*_args: Any, **_kwargs: Any) -> _FakeCompleted:
        raise subprocess.TimeoutExpired(cmd="codex", timeout=10)

    monkeypatch.setattr(subprocess, "run", fake_run)
    with pytest.raises(CodexAuthError) as exc:
        _engine().verify_auth()
    assert "10초" in str(exc.value)
