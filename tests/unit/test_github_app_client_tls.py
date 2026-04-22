"""Regression coverage for the httpx.AsyncClient wiring in GitHubAppClient.

이전에는 urllib 에 certifi CA 번들을 주입하는 흐름이 핵심이었다. 지금은
httpx.AsyncClient 생성 시 `verify=_default_tls_context()` 를 넘기면 되며,
이 파일은 "certifi 번들이 실제로 SSLContext 의 cafile 로 전달되는지" 를 고정한다.
"""

import ssl

import httpx
import jwt
import pytest

from codex_review.infrastructure import github_app_client


def test_default_tls_context_uses_certifi_bundle(monkeypatch: pytest.MonkeyPatch) -> None:
    """회귀 방지: `_default_tls_context` 가 `certifi.where()` 결과를 `cafile` 로 넘긴다.
    누군가 certifi 호출을 제거해도 정확히 이 테스트가 실패하도록 훅을 건다.
    """
    captured: dict[str, object] = {}

    monkeypatch.setattr(
        github_app_client.certifi, "where", lambda: "/fake/certifi/bundle.pem"
    )
    original = github_app_client.ssl.create_default_context

    def spy(*args: object, cafile: str | None = None, **kwargs: object) -> ssl.SSLContext:
        captured["cafile"] = cafile
        return original()

    monkeypatch.setattr(github_app_client.ssl, "create_default_context", spy)
    github_app_client._default_tls_context()
    assert captured["cafile"] == "/fake/certifi/bundle.pem"


async def test_request_uses_injected_http_client(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """DIP 검증: GitHubAppClient 가 주입된 httpx.AsyncClient 로만 요청한다.

    `async with httpx.AsyncClient(...)` 를 테스트 함수 안에서 직접 열어 fixture teardown
    과 이벤트 루프 충돌(동기 fixture 에서 `run_until_complete` 호출)을 피한다.
    """
    captured: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        captured.append(req)
        return httpx.Response(
            200, json={"token": "ITOK", "expires_at": "2026-04-22T00:00:00Z"}
        )

    transport = httpx.MockTransport(handler)
    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=transport
    ) as http_client:
        monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
        client = github_app_client.GitHubAppClient(
            app_id=1, private_key_pem="-", http_client=http_client
        )
        token = await client.get_installation_token(installation_id=7)

    assert token == "ITOK"
    assert captured, "request should have been issued"
    assert captured[0].url.path == "/app/installations/7/access_tokens"
    assert captured[0].headers["Accept"] == "application/vnd.github+json"
    assert captured[0].headers["Authorization"].startswith("Bearer ")
