"""Regression coverage for GitHub `patch` omission handling in fetch_pull_request."""

import logging

import httpx
import jwt
import pytest

from codex_review.domain import RepoRef
from codex_review.infrastructure.github_app_client import GitHubAppClient

_PR_JSON = {
    "title": "t",
    "body": "",
    "head": {"sha": "abc", "ref": "feat", "repo": {"clone_url": "https://x.git"}},
    "base": {"sha": "def", "ref": "main"},
    "draft": False,
}

_TOKEN_JSON = {"token": "ITOK", "expires_at": "2026-04-22T00:00:00Z"}


def _router(files_payload: list[dict]) -> httpx.MockTransport:
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json=_TOKEN_JSON)
        if req.url.path.endswith("/pulls/5"):
            return httpx.Response(200, json=_PR_JSON)
        if "/pulls/5/files" in req.url.path:
            return httpx.Response(200, json=files_payload)
        return httpx.Response(404, json={"message": "not found"})

    return httpx.MockTransport(handler)


async def test_patch_missing_file_emits_warning_and_yields_empty_line_set(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GitHub 이 `patch` 키를 생략한 파일에 대해:
    (1) 해당 파일의 diff_right_lines 가 빈 집합으로 설정되고
    (2) 운영자가 알아볼 수 있도록 warning 로그가 남아야 한다.

    `async with httpx.AsyncClient(...)` 를 테스트 내부에서 직접 관리해 fixture teardown
    단계의 이벤트 루프 충돌(동기 fixture 에서 `run_until_complete` 호출)을 피한다.
    """
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
    transport = _router([
        {"filename": "small.py", "patch": "@@ -1,1 +1,1 @@\n hello"},
        {"filename": "huge.bin", "status": "modified"},  # patch 누락
    ])
    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=transport
    ) as http_client:
        client = GitHubAppClient(app_id=1, private_key_pem="-", http_client=http_client)
        with caplog.at_level(logging.WARNING):
            pr = await client.fetch_pull_request(
                RepoRef("o", "r"), number=5, installation_id=7
            )

    assert pr.diff_right_lines["small.py"] == frozenset({1})
    assert pr.diff_right_lines["huge.bin"] == frozenset()

    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("huge.bin" in r.getMessage() for r in warnings)
    assert not any("small.py" in r.getMessage() for r in warnings)


async def test_patch_present_does_not_emit_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
    transport = _router([{"filename": "a.py", "patch": "@@ -1,1 +1,1 @@\n hello"}])
    async with httpx.AsyncClient(
        base_url="https://api.github.com", transport=transport
    ) as http_client:
        client = GitHubAppClient(app_id=1, private_key_pem="-", http_client=http_client)
        with caplog.at_level(logging.WARNING):
            await client.fetch_pull_request(
                RepoRef("o", "r"), number=5, installation_id=7
            )

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
