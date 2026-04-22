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


def _settings(monkeypatch: pytest.MonkeyPatch, **overrides: str) -> Settings:
    # `Settings()` 는 실제 환경변수를 읽는다 — 테스트 안정성을 위해 monkeypatch 로만 주입.
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
