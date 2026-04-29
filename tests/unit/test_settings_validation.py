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
    "GITHUB_APP_SLUG",
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


def test_github_app_slug_default_is_none(monkeypatch: pytest.MonkeyPatch) -> None:
    """기본값 None 이면 follow-up 기능 자체 비활성화 (옵트인 설계)."""
    s = _settings(monkeypatch)
    assert s.github_app_slug is None


def test_github_app_slug_can_be_set_via_env(monkeypatch: pytest.MonkeyPatch) -> None:
    s = _settings(monkeypatch, GITHUB_APP_SLUG="codex-review-bot")
    assert s.github_app_slug == "codex-review-bot"


def test_webhook_secret_whitespace_is_stripped_when_valid(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """strip 후에도 내용이 남으면 정상 통과 — 주변 공백은 조용히 제거된다."""
    s = _settings(monkeypatch, GITHUB_WEBHOOK_SECRET="  real-secret  ")
    assert s.github_webhook_secret == "real-secret"


def test_normalize_bot_user_login_handles_all_input_shapes() -> None:
    """회귀 (coderabbitai PR #19 Minor): 운영자가 `GITHUB_APP_SLUG=codex-review-bot[bot]`
    같이 이미 `[bot]` 이 포함된 값을 넣어도 wiring 이 `[bot][bot]` 중복을 만들지
    않아야 한다. 정규화 로직은 `application/follow_up_use_case.normalize_bot_user_login`
    헬퍼로 분리됐고, 본 테스트는 그 헬퍼 자체의 계약을 검증한다 — 이전 테스트는
    문자열 조합만 확인해 main.py wiring 분기가 바뀌어도 통과해버리는 약점이 있었다.
    """
    from codex_review.application.follow_up_use_case import (
        normalize_bot_user_login,
    )

    # 일반 형태 (slug 만 들어옴)
    assert normalize_bot_user_login("codex-review-bot") == "codex-review-bot[bot]"
    # 이미 `[bot]` 이 붙어 있는 입력 — 중복 부착 안 됨
    assert normalize_bot_user_login("codex-review-bot[bot]") == "codex-review-bot[bot]"
    # 주변 공백은 strip
    assert (
        normalize_bot_user_login("  codex-review-bot[bot]  ") == "codex-review-bot[bot]"
    )
    # 공백만 있는 입력은 빈 slug 가 되어 `[bot]` 만 남는다 — 운영자 설정 오류 신호로
    # 그대로 통과시켜 후속 GitHub 비교에서 미스매치가 즉시 드러나도록 한다.
    assert normalize_bot_user_login("   ") == "[bot]"


def test_create_app_wires_followup_use_case_with_normalized_login(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """회귀 (coderabbitai PR #19 Minor 후속): 정규화 로직이 헬퍼로 분리됐다고 해서
    main.py 가 그 헬퍼를 실제로 호출하는지는 별개 — wiring 분기가 잘못 바뀌면
    헬퍼 단위 테스트만으론 회귀를 못 잡는다. 본 테스트는 `create_app()` 의 lifespan
    이 시작되기 직전까지 따라가, `FollowUpReviewUseCase` 가 정규화된 login 으로
    구성되는지 직접 검증.

    구현 노트: lifespan 안에서 외부 I/O (`codex auth preflight`, `httpx.AsyncClient`,
    `GitHubAppClient`) 를 다 만나기 전에 검증해야 한다. `FollowUpReviewUseCase.__init__`
    을 monkeypatch 로 가로채 호출 인자만 캡처하면 lifespan 이 그 이후 단계에서
    실패해도 원하는 시점의 wiring 결정은 잡혔다.
    """
    import asyncio
    import contextlib

    from codex_review import main as main_module
    from codex_review.application import follow_up_use_case as fu_module
    from codex_review.infrastructure import codex_cli_engine

    # `[bot]` suffix 가 이미 붙은 입력. 정상 wiring 이라면 헬퍼를 통과해 단일 `[bot]`
    # 만 남아야 한다. `_settings()` 헬퍼는 `_ALL_ALIASES` 환경 변수 정리 + 필수값 주입.
    _settings(monkeypatch, GITHUB_APP_SLUG="codex-review-bot[bot]")

    # 1) lifespan 안에서 호출되는 codex 인증 preflight 를 noop 으로 우회 — 이 테스트는
    #    follow-up wiring 까지 도달하는 게 목적이라 외부 binary 의존을 잘라낸다.
    async def _ok_preflight(self) -> str:  # type: ignore[no-untyped-def]
        return "ok (mocked in test)"

    monkeypatch.setattr(
        codex_cli_engine.CodexCliEngine, "verify_auth", _ok_preflight
    )

    # 2) `FollowUpReviewUseCase.__init__` 인자를 캡처. wiring 이 정규화된 login 을
    #    실제로 넘기는지 직접 확인.
    captured: dict[str, object] = {}
    original_init = fu_module.FollowUpReviewUseCase.__init__

    def spy_init(self, **kwargs):  # type: ignore[no-untyped-def]
        captured.update(kwargs)
        original_init(self, **kwargs)

    monkeypatch.setattr(fu_module.FollowUpReviewUseCase, "__init__", spy_init)

    # 3) GitHubAppClient 가 private key 형식 검증을 안 하도록 PEM 로딩을 가짜로.
    #    `load_private_key()` 는 `Settings.load_private_key` 인스턴스 메서드라
    #    Settings 자체를 패치한다.
    from codex_review.config import Settings

    monkeypatch.setattr(Settings, "load_private_key", lambda self: b"FAKE-PEM")

    app = main_module.create_app()

    async def _drive_lifespan() -> None:
        # lifespan 컨텍스트 진입 + 즉시 종료. handler.start() 까지 도달하지만 실
        # 작업은 일어나지 않는다.
        async with app.router.lifespan_context(app):
            pass

    # 다운스트림 (httpx 연결 등) 실패는 본 테스트 범위 밖 — 앞서 wiring 이 일어
    # 났으면 captured 가 채워져 있어야 한다.
    with contextlib.suppress(Exception):
        asyncio.run(_drive_lifespan())

    assert captured.get("bot_user_login") == "codex-review-bot[bot]", (
        f"main wiring 이 정규화된 bot login 으로 use case 를 만들지 않음: "
        f"{captured.get('bot_user_login')!r}"
    )
