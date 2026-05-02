from pathlib import Path
from typing import Annotated

from pydantic import Field, StringConstraints
from pydantic_settings import BaseSettings, SettingsConfigDict

# 공백만으로 이뤄진 시크릿·호스트·모델명을 차단 — 빈 문자열뿐 아니라 `"   "` 도 거절해야
# HMAC 무력화·바인딩 실패 같은 조용한 설정 사고를 기동 단계에서 막을 수 있다 (codex 리뷰).
# `strip_whitespace=True` 로 주변 공백을 제거한 뒤 `min_length=1` 을 평가한다.
NonBlankStr = Annotated[str, StringConstraints(strip_whitespace=True, min_length=1)]


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
    github_webhook_secret: NonBlankStr = Field(..., alias="GITHUB_WEBHOOK_SECRET")
    github_api_base: str = Field(default="https://api.github.com", alias="GITHUB_API_BASE")
    # PR 댓글 follow-up 기능 활성화에 필요한 봇 슬러그 (예: "codex-review-bot").
    # GitHub 가 게시한 본인 댓글의 `user.login` 은 `f"{slug}[bot]"` 형태이므로, 이 값으로
    # 우리 봇이 단 코멘트만 골라 follow-up 한다. 미설정 (None) 이면 follow-up 기능 자체
    # 비활성화 — 운영자가 슬러그를 알고 명시적으로 옵트인 해야 작동한다.
    github_app_slug: str | None = Field(default=None, alias="GITHUB_APP_SLUG")

    # Codex CLI — 음수/0 타임아웃이나 토큰 한도는 리뷰를 즉시 실패시키므로 `gt=0` 로 고정.
    codex_bin: str = Field(default="codex", alias="CODEX_BIN")
    codex_model: NonBlankStr = Field(default="gpt-5.5", alias="CODEX_MODEL")
    codex_reasoning_effort: str = Field(default="high", alias="CODEX_REASONING_EFFORT")
    codex_timeout_sec: int = Field(default=600, gt=0, alias="CODEX_TIMEOUT_SEC")
    codex_max_input_tokens: int = Field(default=300_000, gt=0, alias="CODEX_MAX_INPUT_TOKENS")
    # 예산 초과 시 diff-only 모드 자동 fallback 활성화 여부 (기본 True).
    # False 로 내리면 기존 "리뷰 스킵 + 안내 코멘트" 경로만 남는다 — 리뷰 품질을
    # 보수적으로 보장하고 싶은 운영 환경 대비 옵트아웃. (gemini PR #17 제안)
    enable_diff_fallback: bool = Field(default=True, alias="CODEX_ENABLE_DIFF_FALLBACK")

    # Repo / files
    repo_cache_dir: Path = Field(
        default=Path.home() / ".codex-review" / "repos", alias="REPO_CACHE_DIR"
    )
    file_max_bytes: int = Field(default=204_800, gt=0, alias="FILE_MAX_BYTES")
    data_file_max_bytes: int = Field(default=20_000, gt=0, alias="DATA_FILE_MAX_BYTES")

    # Server — 포트는 TCP 유효 범위(1–65535) 로 제한.
    host: NonBlankStr = Field(default="127.0.0.1", alias="HOST")
    port: int = Field(default=8000, ge=1, le=65535, alias="PORT")
    dry_run: bool = Field(default=False, alias="DRY_RUN")
    # 동시에 처리할 리뷰 최대 개수. 1 이면 직렬. 2~ 로 올리면 병렬 처리. 0/음수는 의미 없음.
    review_concurrency: int = Field(default=1, gt=0, alias="REVIEW_CONCURRENCY")
    # 웹훅 큐 상한. None 이면 `review_concurrency * 10` 으로 자동 계산. 가득 차면 503 반환.
    # pydantic V2 는 `int | None` 타입에 `gt=0` 을 걸어도 None 은 검증을 건너뛰므로 별도
    # validator 없이 "None 이거나 양수" 계약이 자동 적용된다 (gemini 리뷰).
    review_queue_maxsize: int | None = Field(default=None, gt=0, alias="REVIEW_QUEUE_MAXSIZE")

    def load_private_key(self) -> str:
        if self.github_app_private_key:
            return self.github_app_private_key
        if self.github_app_private_key_path:
            return self.github_app_private_key_path.read_text(encoding="utf-8")
        raise RuntimeError(
            "GITHUB_APP_PRIVATE_KEY 또는 GITHUB_APP_PRIVATE_KEY_PATH 중 하나가 필요합니다."
        )
