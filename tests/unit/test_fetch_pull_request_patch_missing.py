"""Regression coverage for GitHub `patch` omission handling in fetch_pull_request."""

import json
import logging
import urllib.request
from typing import Any

import jwt
import pytest

from codex_review.domain import RepoRef
from codex_review.infrastructure.github_app_client import GitHubAppClient


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


_PR_JSON = {
    "title": "t",
    "body": "",
    "head": {"sha": "abc", "ref": "feat", "repo": {"clone_url": "https://x.git"}},
    "base": {"sha": "def", "ref": "main"},
    "draft": False,
}


def _responses_for(files_payload: list[dict[str, Any]]) -> list[_FakeResponse]:
    return [
        # /pulls/N (메타)
        _FakeResponse(json.dumps(_PR_JSON).encode("utf-8")),
        # /pulls/N/files?page=1
        _FakeResponse(json.dumps(files_payload).encode("utf-8")),
    ]


def test_patch_missing_file_emits_warning_and_yields_empty_line_set(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """GitHub 이 `patch` 키를 생략한 파일에 대해:
    (1) 해당 파일의 diff_right_lines 가 빈 집합으로 설정되고
    (2) 운영자가 알아볼 수 있도록 warning 로그가 남아야 한다.
    """
    responses = iter(_responses_for([
        {"filename": "small.py", "patch": "@@ -1,1 +1,1 @@\n hello"},
        {"filename": "huge.bin", "status": "modified"},  # patch 누락
    ]))

    def fake_urlopen(req: urllib.request.Request, *, timeout=None, context=None):
        return next(responses)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    monkeypatch.setattr(client, "get_installation_token", lambda _iid: "ITOK")

    with caplog.at_level(logging.WARNING):
        pr = client.fetch_pull_request(RepoRef("o", "r"), number=5, installation_id=7)

    # 경로별 허용 라인 집합 — small.py 는 파싱됐고 huge.bin 은 빈 집합
    assert pr.diff_right_lines["small.py"] == frozenset({1})
    assert pr.diff_right_lines["huge.bin"] == frozenset()

    # 경고 로그 검증 — 파일명이 포함돼야 운영자가 어떤 파일인지 알 수 있다.
    warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
    assert any("huge.bin" in r.getMessage() for r in warnings)
    # small.py 는 patch 가 있으니 경고 없음
    assert not any("small.py" in r.getMessage() for r in warnings)


def test_patch_present_does_not_emit_warning(
    monkeypatch: pytest.MonkeyPatch,
    caplog: pytest.LogCaptureFixture,
) -> None:
    responses = iter(_responses_for([
        {"filename": "a.py", "patch": "@@ -1,1 +1,1 @@\n hello"},
    ]))

    def fake_urlopen(req, *, timeout=None, context=None):
        return next(responses)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    monkeypatch.setattr(client, "get_installation_token", lambda _iid: "ITOK")

    with caplog.at_level(logging.WARNING):
        client.fetch_pull_request(RepoRef("o", "r"), number=5, installation_id=7)

    assert not [r for r in caplog.records if r.levelno == logging.WARNING]
