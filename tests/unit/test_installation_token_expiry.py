"""Regression coverage for GitHub installation token expiry parsing.

이전 구현은 `time.mktime(time.strptime(..., "%Y-%m-%dT%H:%M:%SZ"))` 를 썼는데
이 조합은 UTC 로 파싱되지 않고 로컬 타임존 기준으로 초가 계산되어 만료 시각이
타임존 오프셋(예: KST 에서 -9시간) 만큼 어긋나는 버그가 있었다.
"""

import json
import time
import urllib.request
from collections.abc import Iterator
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
def tz_sandbox(monkeypatch: pytest.MonkeyPatch) -> Iterator[None]:
    """`TZ` env 를 테스트가 임의로 바꿔도 통과/실패와 무관하게 `time.tzset()` 을
    이용해 원래 상태로 복구한다.

    `monkeypatch.setenv` 만으로는 C 라이브러리 레벨의 타임존 상태를 되돌리지 못해
    한 테스트가 실패하면 이후 테스트가 오염될 수 있다 (Gemini 지적).
    """
    yield
    # teardown — assert 가 중간에 실패해도 여기까지 오는 `finally` 의미.
    # monkeypatch 가 TZ env 를 원복시키고 나면 tzset() 을 불러 C 계층 상태까지 맞춘다.
    time.tzset()


def _make_client(monkeypatch: pytest.MonkeyPatch, expires_at_iso: str) -> GitHubAppClient:
    response_body = json.dumps({"token": "TK", "expires_at": expires_at_iso}).encode("utf-8")

    def fake_urlopen(req, *, timeout=None, context=None):
        return _FakeResp(response_body)

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
    return GitHubAppClient(app_id=1, private_key_pem="-")


def _cached_expires_at(client: GitHubAppClient, installation_id: int) -> float:
    # 테스트 전용: 캐시 엔트리 확인. 공개 속성이 없어 내부 필드를 참조한다.
    return client._token_cache[installation_id].expires_at  # type: ignore[attr-defined]


def test_expires_at_parsed_as_utc_regardless_of_local_tz(
    monkeypatch: pytest.MonkeyPatch, tz_sandbox: None
) -> None:
    """로컬 TZ 를 어떤 값으로 바꿔도 동일한 UTC 문자열은 동일한 epoch 로 변환돼야 한다.

    `tz_sandbox` 픽스처가 assert 실패 여부와 무관하게 TZ 상태를 복구한다.
    """
    iso = "2026-04-22T00:00:00Z"
    expected_epoch = datetime(2026, 4, 22, 0, 0, 0, tzinfo=timezone.utc).timestamp()

    for tz, label in (
        ("UTC", "got_utc"),
        ("Asia/Seoul", "got_kst"),
        ("America/Los_Angeles", "got_pacific"),
    ):
        monkeypatch.setenv("TZ", tz)
        time.tzset()
        client = _make_client(monkeypatch, iso)
        client.get_installation_token(installation_id=7)
        got = _cached_expires_at(client, 7)
        assert got == pytest.approx(expected_epoch, abs=1.0), (
            f"TZ={tz} ({label}) 에서 epoch 불일치"
        )


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
