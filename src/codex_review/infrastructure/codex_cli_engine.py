import logging
import subprocess
from dataclasses import replace

from codex_review.domain import FileDump, PullRequest, ReviewResult

from .codex_parser import parse_review
from .codex_prompt import build_prompt

logger = logging.getLogger(__name__)


class CodexAuthError(RuntimeError):
    """Raised when the Codex CLI is not authenticated (manual `codex login` required)."""


class CodexCliEngine:
    """Calls the Codex CLI (`codex exec`) over stdin and parses a JSON review."""

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

    def verify_auth(self) -> str:
        """Run `codex login status` and return the status line, or raise CodexAuthError.

        Called once at startup so the process fails fast instead of surfacing auth
        errors during the first PR review. The server operator must run
        `codex login` manually (server stays out of the interactive browser flow).
        """
        try:
            result = subprocess.run(  # noqa: S603
                [self._binary, "login", "status"],
                capture_output=True,
                text=True,
                timeout=10,
                check=False,
            )
        except FileNotFoundError as exc:
            raise CodexAuthError(
                f"CODEX_BIN='{self._binary}' 을(를) 실행할 수 없습니다. "
                "경로를 확인하거나 `codex` CLI를 설치하세요."
            ) from exc
        except subprocess.TimeoutExpired as exc:
            raise CodexAuthError("codex login status 가 10초 내에 응답하지 않았습니다.") from exc

        # codex CLI 는 TTY 가 아닐 때 상태 메시지를 stderr 로 보내므로 두 스트림을 함께 본다.
        combined = (result.stdout + result.stderr).strip()
        if result.returncode != 0 or "Logged in" not in combined:
            raise CodexAuthError(
                "Codex CLI 가 로그인되어 있지 않습니다.\n"
                f"출력: {combined or '(empty)'}\n"
                f"해결: 터미널에서 `{self._binary} login` 을 실행해 ChatGPT 로 로그인한 뒤 서버를 재기동하세요."
            )
        return combined.splitlines()[0] if combined else "Logged in"

    def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        prompt = build_prompt(pr, dump)
        # "-" positional: Codex CLI 에 stdin 에서 프롬프트를 읽도록 지시.
        # argv 로 넘기지 않는 이유는 전체 레포 덤프가 수백 KB ~ 수 MB 라 ARG_MAX 를 초과할 수 있어서.
        cmd = [
            self._binary,
            "exec",
            "--model",
            self._model,
            # reasoning_effort 는 config 오버라이드로 넘긴다. `codex exec` 가 별도 CLI 플래그로
            # 지원하지 않고 ~/.codex/config.toml 값만 읽기 때문에 `-c key=value` 가 정석 경로.
            "--config",
            f"model_reasoning_effort={self._reasoning_effort}",
            "-",
        ]
        logger.info(
            "invoking codex: files=%d chars=%d model=%s effort=%s",
            len(dump.entries),
            dump.total_chars,
            self._model,
            self._reasoning_effort,
        )
        try:
            result = subprocess.run(  # noqa: S603
                cmd,
                input=prompt,
                capture_output=True,
                text=True,
                timeout=self._timeout_sec,
                check=False,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError(
                f"codex exec timed out after {self._timeout_sec}s"
            ) from exc

        if result.returncode != 0:
            raise RuntimeError(
                f"codex exec failed ({result.returncode}): {result.stderr.strip()[:1000]}"
            )

        # 파서는 모델이 뭔지 모르므로 엔진에서 자기 식별자를 덧붙인다. PR 화면에서
        # "어느 모델이 찍었는지" 확인이 필요할 때 본문 footer 로 바로 보인다.
        return replace(parse_review(result.stdout), model=self._model)
