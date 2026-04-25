"""Tests for `GitHubAppClient` follow-up Phase 1 wire-up.

검증:
  - `list_review_threads`: GraphQL query 가 올바른 endpoint(/graphql) 로 가고
    response 를 `ReviewThread` 도메인 객체로 정확히 매핑한다.
  - `reply_to_review_comment`: REST POST 가 올바른 path 로 발사된다.
  - `resolve_review_thread`: GraphQL mutation 이 `resolveReviewThread` 키워드를
    포함하고 thread_id variable 이 전달된다.
  - dry_run 모드에서는 mutation·POST 가 발사되지 않는다.
"""

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio

from codex_review.domain import PullRequest, RepoRef
from codex_review.infrastructure.github_app_client import (
    FOLLOWUP_MARKER,
    GitHubAppClient,
)


def _pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("octocat", "Hello-World"),
        number=42,
        title="t", body="",
        head_sha="abc", head_ref="feat",
        base_sha="def", base_ref="main",
        clone_url="https://example/x.git",
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
    )


@pytest_asyncio.fixture()
async def make_client(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """Returns a factory `(handler) -> GitHubAppClient` wired to MockTransport."""
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    async with AsyncExitStack() as stack:

        def factory(
            handler: Any, *, dry_run: bool = False
        ) -> GitHubAppClient:
            http_client = httpx.AsyncClient(
                base_url="https://api.github.com",
                transport=httpx.MockTransport(handler),
            )
            stack.push_async_callback(http_client.aclose)
            return GitHubAppClient(
                app_id=1, private_key_pem="-",
                http_client=http_client, dry_run=dry_run,
            )

        yield factory


# ---------------------------------------------------------------------------
# list_review_threads
# ---------------------------------------------------------------------------


def _graphql_threads_payload() -> dict[str, Any]:
    """샘플 GraphQL response — 봇 thread 1 + 다른 봇 thread 1 + outdated thread 1."""
    return {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "id": "T_1",
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 1001,
                                            "author": {"login": "codex-review-bot[bot]"},
                                            "path": "src/a.py",
                                            "line": 12,
                                            "body": "[Major] something",
                                            "commit": {"oid": "deadbeef"},
                                        },
                                    ],
                                },
                            },
                            {
                                "id": "T_2",
                                "isResolved": True,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 2002,
                                            "author": {"login": "other-bot[bot]"},
                                            "path": "src/b.py",
                                            "line": 5,
                                            "body": "...",
                                            "commit": {"oid": "cafef00d"},
                                        },
                                    ],
                                },
                            },
                            {
                                "id": "T_3",
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 3003,
                                            "author": {"login": "codex-review-bot[bot]"},
                                            "path": "src/c.py",
                                            "line": None,  # outdated
                                            "body": "[Minor] outdated",
                                            "commit": {"oid": "abcd1234"},
                                        },
                                        # root 와 다른 author 의 답글 → has_non_root_author_reply=True
                                        {
                                            "databaseId": 3004,
                                            "author": {"login": "human-reviewer"},
                                            "path": "src/c.py",
                                            "line": None,
                                            "body": "thanks bot",
                                            "commit": {"oid": "abcd1234"},
                                        },
                                    ],
                                },
                            },
                        ],
                    },
                },
            },
        },
    }


async def test_list_review_threads_parses_all_fields(make_client) -> None:
    requests: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        if req.url.path == "/graphql":
            requests.append(req)
            return httpx.Response(200, json=_graphql_threads_payload())
        raise AssertionError(f"unexpected request: {req.method} {req.url}")

    client = make_client(handler)
    threads = await client.list_review_threads(_pr(), 7)

    # GraphQL endpoint 1회 호출
    assert len(requests) == 1
    body = json.loads(requests[0].content.decode("utf-8"))
    assert "reviewThreads" in body["query"]
    assert body["variables"]["owner"] == "octocat"
    assert body["variables"]["name"] == "Hello-World"
    assert body["variables"]["number"] == 42

    # 도메인 객체 매핑
    assert len(threads) == 3
    t1, t2, t3 = threads
    assert t1.id == "T_1"
    assert t1.is_resolved is False
    assert t1.root_comment_id == 1001
    assert t1.root_author_login == "codex-review-bot[bot]"
    assert t1.path == "src/a.py"
    assert t1.line == 12
    assert t1.commit_id == "deadbeef"
    assert t1.body == "[Major] something"
    assert t1.has_non_root_author_reply is False
    assert t1.has_followup_marker is False

    assert t2.is_resolved is True
    assert t2.root_author_login == "other-bot[bot]"

    # T_3 은 outdated (line=None) + 사람 답글 존재
    assert t3.line is None
    assert t3.has_non_root_author_reply is True


async def test_list_review_threads_detects_existing_followup_marker(
    make_client,
) -> None:
    """우리 봇이 이미 follow-up 답글을 단 적 있는 스레드는 has_followup_marker=True 로 표시."""
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "id": "T_1",
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 1001,
                                            "author": {"login": "codex-review-bot[bot]"},
                                            "path": "src/a.py",
                                            "line": 12,
                                            "body": "original",
                                            "commit": {"oid": "x"},
                                        },
                                        {
                                            "databaseId": 1002,
                                            "author": {"login": "codex-review-bot[bot]"},
                                            "path": "src/a.py",
                                            "line": 12,
                                            "body": f"resolved\n\n{FOLLOWUP_MARKER}",
                                            "commit": {"oid": "x"},
                                        },
                                    ],
                                },
                            },
                        ],
                    },
                },
            },
        },
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        return httpx.Response(200, json=payload)

    client = make_client(handler)
    threads = await client.list_review_threads(_pr(), 7)
    assert len(threads) == 1
    assert threads[0].has_followup_marker is True
    # 같은 봇이 단 답글이라 has_non_root_author_reply 는 False (자기 답글로는 카운트 안 함).
    assert threads[0].has_non_root_author_reply is False


# ---------------------------------------------------------------------------
# reply_to_review_comment
# ---------------------------------------------------------------------------


async def test_reply_posts_to_replies_endpoint(make_client) -> None:
    posts: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        posts.append(req)
        return httpx.Response(201, json={"id": 9999})

    client = make_client(handler)
    await client.reply_to_review_comment(_pr(), 1001, "✅ 자동 해소")

    assert len(posts) == 1
    assert posts[0].method == "POST"
    assert posts[0].url.path == "/repos/octocat/Hello-World/pulls/42/comments/1001/replies"
    body = json.loads(posts[0].content.decode("utf-8"))
    assert body == {"body": "✅ 자동 해소"}


async def test_reply_skips_in_dry_run(make_client) -> None:
    posts: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        posts.append(req)
        return httpx.Response(201, json={})

    client = make_client(handler, dry_run=True)
    await client.reply_to_review_comment(_pr(), 1001, "ignored")
    assert posts == []  # 게시 안 됨


# ---------------------------------------------------------------------------
# resolve_review_thread
# ---------------------------------------------------------------------------


async def test_resolve_sends_graphql_mutation(make_client) -> None:
    posts: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        posts.append(req)
        return httpx.Response(200, json={"data": {"resolveReviewThread": {"thread": {"id": "T_1", "isResolved": True}}}})

    client = make_client(handler)
    await client.resolve_review_thread("T_1", 7)

    assert len(posts) == 1
    req = posts[0]
    assert req.url.path == "/graphql"
    body = json.loads(req.content.decode("utf-8"))
    assert "resolveReviewThread" in body["query"]
    assert body["variables"] == {"threadId": "T_1"}


async def test_resolve_skips_in_dry_run(make_client) -> None:
    posts: list[httpx.Request] = []

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        posts.append(req)
        return httpx.Response(200, json={})

    client = make_client(handler, dry_run=True)
    await client.resolve_review_thread("T_1", 7)
    # access_tokens 도 호출되지 않음 (dry-run 이라 token 도 필요 없음)
    assert posts == []
