from pathlib import Path

from pydantic import Field
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(env_file=".env", extra="ignore")

    # GitHub App
    github_app_id: int = Field(..., alias="GITHUB_APP_ID")
    github_app_private_key_path: Path | None = Field(
        default=None, alias="GITHUB_APP_PRIVATE_KEY_PATH"
    )
    github_app_private_key: str | None = Field(default=None, alias="GITHUB_APP_PRIVATE_KEY")
    github_webhook_secret: str = Field(..., alias="GITHUB_WEBHOOK_SECRET")
    github_api_base: str = Field(default="https://api.github.com", alias="GITHUB_API_BASE")

    # Codex CLI
    codex_bin: str = Field(default="codex", alias="CODEX_BIN")
    codex_model: str = Field(default="gpt-5.4", alias="CODEX_MODEL")
    codex_reasoning_effort: str = Field(default="high", alias="CODEX_REASONING_EFFORT")
    codex_timeout_sec: int = Field(default=600, alias="CODEX_TIMEOUT_SEC")
    codex_max_input_tokens: int = Field(default=300_000, alias="CODEX_MAX_INPUT_TOKENS")

    # Repo / files
    repo_cache_dir: Path = Field(
        default=Path.home() / ".codex-review" / "repos", alias="REPO_CACHE_DIR"
    )
    file_max_bytes: int = Field(default=204_800, alias="FILE_MAX_BYTES")
    data_file_max_bytes: int = Field(default=20_000, alias="DATA_FILE_MAX_BYTES")

    # Server
    host: str = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8000, alias="PORT")
    dry_run: bool = Field(default=False, alias="DRY_RUN")

    def load_private_key(self) -> str:
        if self.github_app_private_key:
            return self.github_app_private_key
        if self.github_app_private_key_path:
            return self.github_app_private_key_path.read_text(encoding="utf-8")
        raise RuntimeError(
            "GITHUB_APP_PRIVATE_KEY 또는 GITHUB_APP_PRIVATE_KEY_PATH 중 하나가 필요합니다."
        )
