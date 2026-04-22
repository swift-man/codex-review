from pathlib import Path

from pydantic import Field, field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    """환경 변수 기반 서버 설정.

    `Field(..., gt=0)` / `le=…` 형태로 기본 제약을 걸어 운영자가 `REVIEW_CONCURRENCY=0`
    같은 무효한 값을 넣었을 때 핸들러에서 조용히 `max(1, …)` 로 보정되는 대신 기동
    시점에 `ValidationError` 로 즉시 실패하게 한다. 문제의 원인을 이른 시점에 드러내
    야 운영 사고(쿼터 폭주·무한 대기 등) 를 피할 수 있다 — codex 본문 피드백 반영.
    """

    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GitHub App
    github_app_id: int = Field(..., gt=0, alias="GITHUB_APP_ID")
    github_app_private_key_path: Path | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY_PATH"
    )
    github_app_private_key: str | None = Field(default=None, alias="GITHUB_APP_PRIVATE_KEY")
    github_webhook_secret: str = Field(..., min_length=1, alias="GITHUB_WEBHOOK_SECRET")
    github_api_base: str = Field(default="https://api.github.com", alias="GITHUB_API_BASE")

    # Codex CLI — 음수/0 타임아웃이나 토큰 한도는 리뷰를 즉시 실패시키므로 `gt=0` 로 고정.
    codex_bin: str = Field(default="codex", alias="CODEX_BIN")
    codex_model: str = Field(default="gpt-5.4", min_length=1, alias="CODEX_MODEL")
    codex_reasoning_effort: str = Field(default="high", alias="CODEX_REASONING_EFFORT")
    codex_timeout_sec: int = Field(default=600, gt=0, alias="CODEX_TIMEOUT_SEC")
    codex_max_input_tokens: int = Field(default=300_000, gt=0, alias="CODEX_MAX_INPUT_TOKENS")

    # Repo / files
    repo_cache_dir: Path = Field(
        default=Path.home() / ".codex-review" / "repos", alias="REPO_CACHE_DIR"
    )
    file_max_bytes: int = Field(default=204_800, gt=0, alias="FILE_MAX_BYTES")
    data_file_max_bytes: int = Field(default=20_000, gt=0, alias="DATA_FILE_MAX_BYTES")

    # Server — 포트는 TCP 유효 범위(1–65535) 로 제한.
    host: str = Field(default="127.0.0.1", min_length=1, alias="HOST")
    port: int = Field(default=8000, ge=1, le=65535, alias="PORT")
    dry_run: bool = Field(default=False, alias="DRY_RUN")
    # 동시에 처리할 리뷰 최대 개수. 1 이면 직렬. 2~ 로 올리면 병렬 처리. 0/음수는 의미 없음.
    review_concurrency: int = Field(default=1, gt=0, alias="REVIEW_CONCURRENCY")
    # 웹훅 큐 상한. None 이면 `review_concurrency * 10` 으로 자동 계산. 가득 차면 503 반환.
    # 명시적으로 주어진 경우 반드시 양수여야 한다 — 0 은 "모든 요청 즉시 거절"을 의미해
    # 사고 가능성이 높다.
    review_queue_maxsize: int | None = Field(default=None, alias="REVIEW_QUEUE_MAXSIZE")

    @field_validator("review_queue_maxsize")
    @classmethod
    def _validate_queue_maxsize(cls, v: int | None) -> int | None:
        # Field(..., gt=0) 은 `int | None` 에 직접 못 걸린다 (None 에서 실패).
        # 수동 validator 로 "None 이거나 양수" 조건만 강제.
        if v is not None and v <= 0:
            raise ValueError(
                f"REVIEW_QUEUE_MAXSIZE must be a positive integer or unset; got {v}"
            )
        return v

    def load_private_key(self) -> str:
        if self.github_app_private_key:
            return self.github_app_private_key
        if self.github_app_private_key_path:
            return self.github_app_private_key_path.read_text(encoding="utf-8")
        raise RuntimeError(
            "GITHUB_APP_PRIVATE_KEY 또는 GITHUB_APP_PRIVATE_KEY_PATH 중 하나가 필요합니다."
        )
