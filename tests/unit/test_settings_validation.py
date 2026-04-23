"""Regression coverage for `Settings` field validation.

운영자가 `REVIEW_CONCURRENCY=0` 처럼 무효한 값을 넣었을 때 기동 시점에 명확한 에러로
실패하는지 확인 (codex 본문 피드백). 이전엔 핸들러의 `max(1, concurrency)` 에서
조용히 보정돼 운영자가 실수를 인지하지 못했다.
"""

import pytest
from pydantic import ValidationError

from codex_review.config import Settings


_REQUIRED_ENV = {
    "GITHUB_APP_ID": "1",
    "GITHUB_WEBHOOK_SECRET": "s",
    "GITHUB_APP_PRIVATE_KEY": "-",  # load_private_key 호출 전엔 형식 검증 없음
}

# `Settings` 가 읽는 모든 alias. 테스트 시작 시 이 목록 전체를 `delenv` 하여
# 개발자 셸·CI 환경에 남아 있던 `PORT=0` / `REVIEW_QUEUE_MAXSIZE=0` 같은 값이
# 테스트 결과를 오염시키지 않도록 한다 (codex 리뷰).
_ALL_ALIASES = (
    "GITHUB_APP_ID",
    "GITHUB_APP_PRIVATE_KEY_PATH",
    "GITHUB_APP_PRIVATE_KEY",
    "GITHUB_WEBHOOK_SECRET",
    "GITHUB_API_BASE",
    "CODEX_BIN",
    "CODEX_MODEL",
    "CODEX_REASONING_EFFORT",
    "CODEX_TIMEOUT_SEC",
    "CODEX_MAX_INPUT_TOKENS",
    "REPO_CACHE_DIR",
    "FILE_MAX_BYTES",
    "DATA_FILE_MAX_BYTES",
    "HOST",
    "PORT",
    "DRY_RUN",
    "REVIEW_CONCURRENCY",
    "REVIEW_QUEUE_MAXSIZE",
    "CODEX_ENABLE_DIFF_FALLBACK",
)


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    """실제 env 를 한 번 비운 뒤 필수값만 주입해 결정론적인 Settings 를 반환."""
    for k in _ALL_ALIASES:
        monkeypatch.delenv(k, raising=False)
    for k, v in {**_REQUIRED_ENV, **overrides}.items():
        monkeypatch.setenv(k, v)
    # 로컬 개발자의 실제 `.env` 가 테스트 결과에 영향 주지 않도록 명시적으로 무력화.
    return Settings(_env_file=None)  # type: ignore[call-arg]


def test_defaults_are_all_valid(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch)
    assert s.review_concurrency == 1
    assert s.codex_timeout_sec == 600
    assert s.review_queue_maxsize is None


def test_review_concurrency_zero_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """회귀(codex 본문 피드백): 0 은 '리뷰를 전혀 처리하지 않는' 무의미한 설정이므로
    조용히 1 로 보정하는 대신 기동 시점에 실패시켜 운영자가 원인을 인지하도록.
    """
    with pytest.raises(ValidationError) as exc:
        _settings(monkeypatch, REVIEW_CONCURRENCY="0")
    assert "review_concurrency" in str(exc.value).lower()


def test_review_concurrency_negative_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, REVIEW_CONCURRENCY="-3")


def test_review_queue_maxsize_zero_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """0 은 '모든 요청을 즉시 503 으로 거절' 을 뜻해 사고 가능성이 높다. None(미설정)
    과는 완전히 다른 의미이므로 구분이 필요."""
    with pytest.raises(ValidationError) as exc:
        _settings(monkeypatch, REVIEW_QUEUE_MAXSIZE="0")
    assert "review_queue_maxsize" in str(exc.value).lower()


def test_review_queue_maxsize_unset_stays_none(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch)
    assert s.review_queue_maxsize is None


def test_review_queue_maxsize_positive_accepted(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, REVIEW_QUEUE_MAXSIZE="50")
    assert s.review_queue_maxsize == 50


def test_codex_timeout_zero_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, CODEX_TIMEOUT_SEC="0")


def test_codex_max_input_tokens_zero_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, CODEX_MAX_INPUT_TOKENS="0")


def test_file_max_bytes_negative_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, FILE_MAX_BYTES="-1")


def test_port_out_of_range_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, PORT="70000")
    with pytest.raises(ValidationError):
        _settings(monkeypatch, PORT="0")


def test_github_app_id_zero_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """GitHub App ID 는 1 이상 정수 — 0 이면 JWT 발급 자체가 거부된다."""
    with pytest.raises(ValidationError):
        _settings(monkeypatch, GITHUB_APP_ID="0")


def test_empty_webhook_secret_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    """빈 시크릿은 HMAC 검증을 사실상 무력화 — 보안 사고."""
    with pytest.raises(ValidationError):
        _settings(monkeypatch, GITHUB_WEBHOOK_SECRET="")


def test_whitespace_only_webhook_secret_is_rejected(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회귀(codex 리뷰): `min_length=1` 만으로는 `"   "` 공백 시크릿이 통과한다.
    `StringConstraints(strip_whitespace=True, ...)` 로 공백 제거 후 길이 검사해야 한다.
    """
    with pytest.raises(ValidationError):
        _settings(monkeypatch, GITHUB_WEBHOOK_SECRET="   ")


def test_whitespace_only_host_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, HOST="   ")


def test_whitespace_only_codex_model_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    with pytest.raises(ValidationError):
        _settings(monkeypatch, CODEX_MODEL="\t\n ")


def test_enable_diff_fallback_default_is_true(monkeypatch: pytest.MonkeyPatch) -> None:
    """회귀 (gemini PR #17): 미설정 시 자동 fallback 이 켜져 있어야 한다 (기존 배포 동작)."""
    s = _settings(monkeypatch)
    assert s.enable_diff_fallback is True


def test_enable_diff_fallback_can_be_disabled_via_env(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """운영자가 품질 보장 우선 정책으로 fallback 을 옵트아웃 가능해야 한다."""
    s = _settings(monkeypatch, CODEX_ENABLE_DIFF_FALLBACK="false")
    assert s.enable_diff_fallback is False


def test_webhook_secret_whitespace_is_stripped_when_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """strip 후에도 내용이 남으면 정상 통과 — 주변 공백은 조용히 제거된다."""
    s = _settings(monkeypatch, GITHUB_WEBHOOK_SECRET="  real-secret  ")
    assert s.github_webhook_secret == "real-secret"
