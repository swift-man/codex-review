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


# ---------------------------------------------------------------------------
# review() error logging contract — full stderr to logger, concise summary in exception
# ---------------------------------------------------------------------------


async def test_review_logs_full_stderr_and_raises_concise_summary(
    monkeypatch: pytest.MonkeyPatch, caplog: pytest.LogCaptureFixture
) -> None:
    """회귀 (운영 사고): 이전 구현은 stderr 를 RuntimeError 메시지에 통째로 박아
    traceback summary 가 첫 줄(=Codex 시작 배너)만 보여 진단을 못 하게 했다.
    이제는 stderr 전체를 별도 ERROR 로그에, RuntimeError 에는 마지막 줄만.
    """
    import logging as _logging

    from codex_review.domain import FileDump, FileEntry, PullRequest, RepoRef

    multi_line_stderr = (
        b"OpenAI Codex v0.124.0-alpha.2 (research preview)\n"
        b"--------\n"
        b"workdir: /tmp\n"
        b"model: gpt-5.5\n"
        b"--------\n"
        b"Error: model 'gpt-5.5' not available in this account\n"
    )
    _patch_subprocess(monkeypatch, _FakeProc(1, stdout=b"", stderr=multi_line_stderr))

    pr = PullRequest(
        repo=RepoRef("o", "r"), number=1, title="t", body="",
        head_sha="abc", head_ref="feat", base_sha="def", base_ref="main",
        clone_url="https://example/x.git", changed_files=("a.py",),
        installation_id=7, is_draft=False,
    )
    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x=1", size_bytes=3, is_changed=True),),
        total_chars=3,
    )

    eng = CodexCliEngine(binary="codex", model="gpt-5.5")

    with caplog.at_level(
        _logging.ERROR, logger="codex_review.infrastructure.codex_cli_engine"
    ):
        with pytest.raises(RuntimeError) as exc_info:
            await eng.review(pr, dump)

    # (1) RuntimeError 메시지엔 stderr 의 **마지막 줄** + 모델명이 포함돼 진단 가능.
    msg = str(exc_info.value)
    assert "gpt-5.5" in msg
    assert "model 'gpt-5.5' not available" in msg
    # 시작 배너는 메시지에 없음 (이전엔 첫 줄로 잘려 진단 어려웠음).
    assert "research preview" not in msg

    # (2) ERROR 로그에는 multi-line stderr 전체가 보존됨.
    full_log = "\n".join(rec.getMessage() for rec in caplog.records)
    assert "research preview" in full_log
    assert "model 'gpt-5.5' not available" in full_log
    assert "rc=1" in full_log
    assert "model=gpt-5.5" in full_log


async def test_review_masks_credentials_in_review_engine_error_message(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회귀 (codex PR #18 Critical): stderr 마지막 줄에 토큰 URL 이 있으면
    `ReviewEngineError` 메시지에도 마스킹된 형태로만 들어가야 한다.
    이 메시지는 logger.exception traceback 이나 PR 진단 코멘트로 흘러가는데, 현재
    `_RedactFilter` 는 traceback 안의 exc 문자열을 재마스킹하지 않으므로 **예외 생성
    시점에 직접 마스킹** 해야 누출 표면이 막힌다.
    """
    from codex_review.domain import FileDump, FileEntry, PullRequest, RepoRef
    from codex_review.interfaces import ReviewEngineError

    stderr = (
        b"OpenAI Codex v0.124.0\n"
        b"--------\n"
        b"fatal: unable to access 'https://x-access-token:ghs_LEAKED@github.com/o/r.git'\n"
    )
    _patch_subprocess(monkeypatch, _FakeProc(1, stdout=b"", stderr=stderr))

    pr = PullRequest(
        repo=RepoRef("o", "r"), number=1, title="t", body="",
        head_sha="abc", head_ref="feat", base_sha="def", base_ref="main",
        clone_url="https://example/x.git", changed_files=("a.py",),
        installation_id=7, is_draft=False,
    )
    dump = FileDump(
        entries=(FileEntry(path="a.py", content="x", size_bytes=1, is_changed=True),),
        total_chars=1,
    )

    eng = CodexCliEngine(binary="codex", model="gpt-5.5")
    with pytest.raises(ReviewEngineError) as exc_info:
        await eng.review(pr, dump)

    msg = str(exc_info.value)
    # 토큰은 절대 메시지에 들어가면 안 된다.
    assert "ghs_LEAKED" not in msg
    # URL 자격증명은 마스킹된 형태로 표시.
    assert "https://***@github.com" in msg
    # returncode 정보는 유지 (도메인 메타데이터).
    assert exc_info.value.returncode == 1
