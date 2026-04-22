import asyncio
import logging

from codex_review.domain import FileDump, PullRequest, ReviewResult

from ._subprocess import kill_and_reap
from .codex_parser import parse_review
from .codex_prompt import build_prompt

logger = logging.getLogger(__name__)


class CodexAuthError(RuntimeError):
    """Raised when the Codex CLI is not authenticated (manual `codex login` required)."""


class CodexCliEngine:
    """Async wrapper around `codex exec`. stdin 으로 프롬프트를 넘기고 stdout JSON 을 파싱."""

    def __init__(
        self,
        binary: str = "codex",
        model: str = "codex-auto-review",
        reasoning_effort: str = "high",
        timeout_sec: int = 600,
    ) -> None:
        self._binary = binary
        self._model = model
        self._reasoning_effort = reasoning_effort
        self._timeout_sec = timeout_sec

    async def verify_auth(self) -> str:
        """Run `codex login status` and return the status line, or raise CodexAuthError.

        기동 시 호출해 토큰이 살아 있는지 선점검한다. 실패하면 서버 기동 자체를 막아
        운영자가 `codex login` 을 먼저 실행하도록 유도.
        """
        try:
            proc = await asyncio.create_subprocess_exec(
                self._binary, "login", "status",
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE,
            )
        except FileNotFoundError as exc:
            raise CodexAuthError(
                f"CODEX_BIN='{self._binary}' 을(를) 실행할 수 없습니다. "
                "경로를 확인하거나 `codex` CLI를 설치하세요."
            ) from exc

        try:
            async with asyncio.timeout(10.0):
                stdout, stderr = await proc.communicate()
        except TimeoutError as exc:
            # kill 후 wait 자체에도 상한을 둔다 — 수거가 지연돼도 서버 기동 경로가 붙잡히지 않도록.
            await kill_and_reap(proc)
            raise CodexAuthError("codex login status 가 10초 내에 응답하지 않았습니다.") from exc
        except asyncio.CancelledError:
            # 워커 취소/서버 종료 신호 시 하위 프로세스가 좀비로 남지 않도록 반드시 정리.
            await kill_and_reap(proc)
            raise

        # codex CLI 는 TTY 가 아닐 때 상태 메시지를 stderr 로 보내므로 두 스트림 모두 확인.
        combined = (stdout.decode(errors="replace") + stderr.decode(errors="replace")).strip()
        if proc.returncode != 0 or "Logged in" not in combined:
            raise CodexAuthError(
                "Codex CLI 가 로그인되어 있지 않습니다.\n"
                f"출력: {combined or '(empty)'}\n"
                f"해결: 터미널에서 `{self._binary} login` 을 실행해 ChatGPT 로 로그인한 뒤 서버를 재기동하세요."
            )
        return combined.splitlines()[0] if combined else "Logged in"

    async def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        prompt = build_prompt(pr, dump)
        # "-" positional 은 codex exec 에 stdin 에서 프롬프트를 읽으라는 지시.
        # argv 로 넘기면 전체 레포 덤프가 ARG_MAX 를 초과할 수 있어 stdin 이 안전.
        logger.info(
            "invoking codex: files=%d chars=%d model=%s effort=%s",
            len(dump.entries),
            dump.total_chars,
            self._model,
            self._reasoning_effort,
        )
        proc = await asyncio.create_subprocess_exec(
            self._binary, "exec",
            "--model", self._model,
            # reasoning_effort 는 config 오버라이드로 넘긴다 — `codex exec` 가 별도 CLI 플래그로
            # 지원하지 않고 ~/.codex/config.toml 값만 읽기 때문.
            "--config", f"model_reasoning_effort={self._reasoning_effort}",
            "-",
            stdin=asyncio.subprocess.PIPE,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )

        try:
            async with asyncio.timeout(self._timeout_sec):
                stdout, stderr = await proc.communicate(input=prompt.encode("utf-8"))
        except TimeoutError as exc:
            # 하위 프로세스 수거 대기에도 상한 — 큐 동시성 상한이 `CODEX_TIMEOUT_SEC` 을
            # 훨씬 넘겨 점유되는 걸 막는다 (codex 리뷰 지적).
            await kill_and_reap(proc)
            raise RuntimeError(
                f"codex exec timed out after {self._timeout_sec}s"
            ) from exc
        except asyncio.CancelledError:
            # 서버 종료/워커 취소 시 `codex exec` 하위 프로세스가 좀비로 남아 토큰·쿼터·CPU 를
            # 계속 소모하지 않도록 확실히 kill + wait 후 취소를 재전파한다.
            await kill_and_reap(proc)
            raise

        if proc.returncode != 0:
            err = stderr.decode(errors="replace").strip()
            raise RuntimeError(
                f"codex exec failed ({proc.returncode}): {err[:1000]}"
            )

        return parse_review(stdout.decode(errors="replace"))
