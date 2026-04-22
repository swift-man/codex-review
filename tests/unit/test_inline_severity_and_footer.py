"""Tests for severity-based inline prefix + model footer preservation.

async 전환 이후에도 severity → 인라인 접두, `review_model_label` → footer 동작이
그대로 유지돼야 한다. `httpx.AsyncClient(transport=MockTransport(...))` 로 주입해
실제 POST payload 를 캡처하고 검증.
"""

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack

import httpx
import jwt
import pytest
import pytest_asyncio

from codex_review.domain import (
    Finding,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
)
from codex_review.domain.finding import SEVERITY_MUST_FIX, SEVERITY_SUGGEST
from codex_review.infrastructure.github_app_client import (
    GitHubAppClient,
    _finding_to_comment,
)


def test_finding_to_comment_prefixes_must_fix_body() -> None:
    body = _finding_to_comment(
        Finding(path="a.py", line=1, body="None 체크 누락", severity=SEVERITY_MUST_FIX)
    )
    assert body["body"].startswith("🔴 **반드시 수정**")
    assert "None 체크 누락" in body["body"]


def test_finding_to_comment_leaves_suggest_body_unprefixed() -> None:
    body = _finding_to_comment(
        Finding(path="a.py", line=1, body="pathlib.Path 사용 고려", severity=SEVERITY_SUGGEST)
    )
    assert body["body"] == "pathlib.Path 사용 고려"


def _pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=1,
        title="t",
        body="",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://example/x.git",
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
        diff_right_lines={"a.py": frozenset({10})},
    )


@pytest_asyncio.fixture()
async def capturing_client(
    monkeypatch: pytest.MonkeyPatch,
) -> AsyncIterator[tuple[GitHubAppClient, list[httpx.Request]]]:
    """POST payload 를 캡처하는 mock transport 가 달린 async client 팩토리."""
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
    posts: list[httpx.Request] = []

    async with AsyncExitStack() as stack:

        def handler(req: httpx.Request) -> httpx.Response:
            if req.url.path.endswith("/access_tokens"):
                return httpx.Response(
                    200, json={"token": "ITOK", "expires_at": "2026-04-22T00:00:00Z"}
                )
            if "/reviews" in req.url.path and req.method == "POST":
                posts.append(req)
            return httpx.Response(200, json={})

        http_client = httpx.AsyncClient(
            base_url="https://api.github.com",
            transport=httpx.MockTransport(handler),
        )
        stack.push_async_callback(http_client.aclose)
        client = GitHubAppClient(
            app_id=1,
            private_key_pem="-",
            http_client=http_client,
            review_model_label="gpt-5.4",
        )
        yield client, posts


async def test_post_review_sends_severity_prefixed_comments_and_model_footer(
    capturing_client: tuple[GitHubAppClient, list[httpx.Request]],
) -> None:
    """회귀 방지: severity 접두 + footer 가 실제 POST 페이로드에 함께 들어간다."""
    client, posts = capturing_client

    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.REQUEST_CHANGES,
        must_fix=("핵심 보안 결함",),
        findings=(
            Finding(
                path="a.py", line=10, body="덮어쓰기 경쟁",
                severity=SEVERITY_MUST_FIX,
            ),
        ),
    )
    await client.post_review(_pr(), result)

    assert len(posts) == 1
    posted = json.loads(posts[0].content.decode("utf-8"))

    # (1) 본문 섹션: 반드시 수정 + footer
    assert "**🔴 반드시 수정할 사항**" in posted["body"]
    assert posted["body"].rstrip().endswith("<code>gpt-5.4</code></sub>")

    # (2) 인라인 코멘트: severity 접두 적용
    assert len(posted["comments"]) == 1
    assert posted["comments"][0]["body"].startswith("🔴 **반드시 수정**")
