"""Regression coverage for GitHub installation token expiry parsing.

이전 구현은 `time.mktime(time.strptime(..., "%Y-%m-%dT%H:%M:%SZ"))` 를 썼는데
이 조합은 UTC 로 파싱되지 않고 로컬 타임존 기준으로 초가 계산되어 만료 시각이
타임존 오프셋(예: KST 에서 -9시간) 만큼 어긋나는 버그가 있었다.
"""

import json
import os
import time
import urllib.request
from collections.abc import Callable, Iterator
from datetime import datetime, timezone

import jwt
import pytest

from codex_review.infrastructure.github_app_client import GitHubAppClient


class _FakeResp:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResp":
        return self

    def __exit__(self, *_a: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


@pytest.fixture()
def tz_sandbox() -> Iterator[Callable[[str], None]]:
    """타임존 안전 조작 픽스처.

    왜 `monkeypatch.setenv` 를 쓰지 않는가:
      monkeypatch 의 env 복원 finalizer 와 `time.tzset()` 호출 순서가 엇갈린다.
      monkeypatch 가 먼저 설정되고 나중에 복원되므로, 같은 테스트에 쓰인 다른 fixture
      의 teardown 이 `monkeypatch` 복원 **전에** 실행된다. 그 안에서 `tzset()` 을 불러도
      아직 env 는 테스트가 설정한 값이라 C 런타임은 여전히 마지막 TZ 를 유지한다.
      이후 monkeypatch 가 env 만 원복하고 `tzset()` 은 재호출되지 않아 테스트 간 오염.

    해결:
      fixture 가 직접 `TZ` env 를 보관/복원하고 teardown 에서 env 복원 **직후** `tzset()`
      을 호출. 복원 순서가 원자적이다.
    """
    original_tz = os.environ.get("TZ")

    def set_tz(name: str) -> None:
        os.environ["TZ"] = name
        time.tzset()

    try:
        yield set_tz
    finally:
        if original_tz is None:
            os.environ.pop("TZ", None)
        else:
            os.environ["TZ"] = original_tz
        time.tzset()


def _make_client(monkeypatch: pytest.MonkeyPatch, expires_at_iso: str) -> GitHubAppClient:
    response_body = json.dumps({"token": "TK", "expires_at": expires_at_iso}).encode("utf-8")

    def fake_urlopen(req, *, timeout=None, context=None):
        return _FakeResp(response_body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
    return GitHubAppClient(app_id=1, private_key_pem="-")


def _cached_expires_at(client: GitHubAppClient, installation_id: int) -> float:
    return client._token_cache[installation_id].expires_at  # type: ignore[attr-defined]


def test_expires_at_parsed_as_utc_regardless_of_local_tz(
    monkeypatch: pytest.MonkeyPatch, tz_sandbox: Callable[[str], None]
) -> None:
    """로컬 TZ 를 어떤 값으로 바꿔도 동일한 UTC 문자열은 동일한 epoch 로 변환돼야 한다."""
    iso = "2026-04-22T00:00:00Z"
    expected_epoch = datetime(2026, 4, 22, 0, 0, 0, tzinfo=timezone.utc).timestamp()

    for tz in ("UTC", "Asia/Seoul", "America/Los_Angeles"):
        tz_sandbox(tz)
        client = _make_client(monkeypatch, iso)
        client.get_installation_token(installation_id=7)
        got = _cached_expires_at(client, 7)
        assert got == pytest.approx(expected_epoch, abs=1.0), f"TZ={tz} 에서 epoch 불일치"


def test_invalid_expires_at_falls_back_to_55min_default(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, "not-a-real-timestamp")
    before = time.time()
    client.get_installation_token(installation_id=7)
    got = _cached_expires_at(client, 7)

    # 5분 여유가 걸린 55분 뒤 기본값이 쓰여야 한다.
    expected = before + 55 * 60
    assert abs(got - expected) < 5.0


def test_empty_expires_at_falls_back_to_55min_default(monkeypatch: pytest.MonkeyPatch) -> None:
    client = _make_client(monkeypatch, "")
    before = time.time()
    client.get_installation_token(installation_id=7)
    got = _cached_expires_at(client, 7)

    expected = before + 55 * 60
    assert abs(got - expected) < 5.0
