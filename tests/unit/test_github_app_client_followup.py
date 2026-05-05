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


# ---------------------------------------------------------------------------
# Pagination + GraphQL error semantics (PR #19 review)
# ---------------------------------------------------------------------------


async def test_list_review_threads_paginates_until_no_more_pages(make_client) -> None:
    """회귀 (gemini PR #19 Major): 100 thread 초과 PR 도 누락 없이 모두 조회.

    이전 구현은 1 페이지만 보고 has_next_page=True 면 경고 로그만 남기고 끝냈다.
    이제 cursor 로 끝까지 순회.
    """
    page_calls = 0

    def make_thread(idx: int, page_id: int) -> dict[str, Any]:
        return {
            "id": f"T_{page_id}_{idx}",
            "isResolved": False,
            "comments": {
                "pageInfo": {"hasNextPage": False},
                "nodes": [
                    {
                        "databaseId": page_id * 1000 + idx,
                        "author": {"login": "codex-review-bot[bot]"},
                        "path": f"src/p{page_id}_{idx}.py",
                        "line": 1,
                        "body": "x",
                        "commit": {"oid": "x"},
                    },
                ],
            },
        }

    def handler(req: httpx.Request) -> httpx.Response:
        nonlocal page_calls
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(
                200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"}
            )
        # GraphQL 호출 — variables 의 `after` 로 페이지 식별.
        body = json.loads(req.content.decode("utf-8"))
        after = body["variables"].get("after")
        page_calls += 1
        if after is None:
            return httpx.Response(200, json={
                "data": {"repository": {"pullRequest": {"reviewThreads": {
                    "pageInfo": {"hasNextPage": True, "endCursor": "CURSOR_2"},
                    "nodes": [make_thread(i, page_id=1) for i in range(2)],
                }}}},
            })
        if after == "CURSOR_2":
            return httpx.Response(200, json={
                "data": {"repository": {"pullRequest": {"reviewThreads": {
                    "pageInfo": {"hasNextPage": False, "endCursor": None},
                    "nodes": [make_thread(i, page_id=2) for i in range(3)],
                }}}},
            })
        raise AssertionError(f"unexpected cursor: {after}")

    client = make_client(handler)
    threads = await client.list_review_threads(_pr(), 7)

    assert page_calls == 2  # 두 페이지 모두 조회
    assert len(threads) == 5  # page1: 2개 + page2: 3개
    ids = [t.id for t in threads]
    assert ids == ["T_1_0", "T_1_1", "T_2_0", "T_2_1", "T_2_2"]


async def test_list_review_threads_marks_threads_with_truncated_comments_as_skip(
    make_client,
) -> None:
    """회귀 (coderabbitai PR #19 Major): comments 가 50건 초과 (`hasNextPage=True`)
    이면 root 이후 어떤 comment 가 있는지 모두 보지 못해 has_followup_marker /
    has_non_root_author_reply 판정이 부정확. 이런 thread 는 follow-up 후보에서
    안전하게 제외하기 위해 `has_non_root_author_reply=True` 로 강제.
    """
    payload = {
        "data": {"repository": {"pullRequest": {"reviewThreads": {
            "pageInfo": {"hasNextPage": False},
            "nodes": [
                {
                    "id": "T_truncated",
                    "isResolved": False,
                    "comments": {
                        "pageInfo": {"hasNextPage": True},  # 50 초과 — truncated
                        "nodes": [
                            {
                                "databaseId": 1,
                                "author": {"login": "codex-review-bot[bot]"},
                                "path": "src/x.py",
                                "line": 1,
                                "body": "root comment",
                                "commit": {"oid": "x"},
                            },
                        ],
                    },
                },
            ],
        }}}},
    }

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        return httpx.Response(200, json=payload)

    client = make_client(handler)
    threads = await client.list_review_threads(_pr(), 7)
    assert len(threads) == 1
    # 핵심 계약: 안전을 위해 has_non_root_author_reply 가 강제로 True → use case 가 skip.
    assert threads[0].has_non_root_author_reply is True


async def test_graphql_raises_on_errors_for_mutations(make_client) -> None:
    """회귀 (gemini + coderabbitai PR #19 Major): mutation 의 GraphQL errors 가 raise
    되지 않으면 호출자가 success 로 오인해 후속 동작(reply 게시) 을 진행한다.
    이제 errors 배열이 있으면 예외를 던져 호출자가 분리 처리할 수 있게 한다.
    """
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        return httpx.Response(200, json={
            "data": {"resolveReviewThread": None},
            "errors": [{"message": "Could not resolve to a node with the global id."}],
        })

    client = make_client(handler)
    with pytest.raises(RuntimeError) as exc:
        await client.resolve_review_thread("T_unknown", 7)
    assert "GraphQL errors" in str(exc.value)
    assert "Could not resolve" in str(exc.value)


async def test_list_review_threads_treats_deleted_user_reply_as_other_author(
    make_client,
) -> None:
    """회귀 (codex PR #19 Major): GitHub 은 삭제된 사용자나 식별 불가 actor 의 댓글에서
    `author=null` 을 반환한다. follow-up marker 도 없는 그런 답글이 있으면 사람 답글이
    소실됐을 가능성이 있어 보수적으로 has_non_root_author_reply=True 로 카운트해야 한다.
    이전엔 빈 author 를 무시해 자동 resolve 후보로 잘못 통과시켰다.
    """
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "id": "T_ghost",
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 1001,
                                            "author": {"login": "codex-review-bot[bot]"},
                                            "path": "src/a.py",
                                            "line": 12,
                                            "body": "original review",
                                            "commit": {"oid": "x"},
                                        },
                                        {
                                            "databaseId": 1002,
                                            "author": None,  # 삭제된 사용자 / ghost
                                            "path": "src/a.py",
                                            "line": 12,
                                            "body": "이건 사람이 단 답글일 수 있다",
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
    # 삭제된 사용자 답글은 follow-up marker 가 없으므로 보수적으로 타인 답글로 간주.
    assert threads[0].has_non_root_author_reply is True


async def test_list_review_threads_marker_with_null_author_still_recognized(
    make_client,
) -> None:
    """회귀 (codex PR #19 Major): 우리 봇이 단 답글이지만 author 표시가 빠진 경우
    (GitHub 가 author 메타데이터를 잃은 케이스), follow-up marker 자체가 신원 보증이므로
    has_other_author 로 카운트하면 안 된다 — 자기 답글을 타인 답글로 오인해 영원히
    auto-resolve 하지 못하는 dead-lock 방지.
    """
    payload = {
        "data": {
            "repository": {
                "pullRequest": {
                    "reviewThreads": {
                        "pageInfo": {"hasNextPage": False},
                        "nodes": [
                            {
                                "id": "T_marker_no_author",
                                "isResolved": False,
                                "comments": {
                                    "nodes": [
                                        {
                                            "databaseId": 2001,
                                            "author": {"login": "codex-review-bot[bot]"},
                                            "path": "src/a.py",
                                            "line": 5,
                                            "body": "original review",
                                            "commit": {"oid": "x"},
                                        },
                                        {
                                            "databaseId": 2002,
                                            "author": None,  # author 메타 소실
                                            "path": "src/a.py",
                                            "line": 5,
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
    # marker 가 신원 보증 → 타인 답글로 카운트하지 않음.
    assert threads[0].has_non_root_author_reply is False
    # 멱등성 보강 (coderabbit PR #19 Major 회귀): marker 가 있으면 author 메타와 무관
    # 하게 has_followup_marker=True. 이전 정책은 `author==root_author` 일 때만 True
    # 로 둬서, GitHub 가 우리 follow-up 댓글의 author 메타를 잃어버린 케이스에 다음
    # 사이클이 같은 스레드에 또 답글을 다는 중복 게시가 발생했다.
    assert threads[0].has_followup_marker is True


# ---------------------------------------------------------------------------
# fetch_review_history — 3개 엔드포인트 병렬 fetch + 시간순 merge + filter
# ---------------------------------------------------------------------------


async def test_fetch_review_history_merges_three_sources_chronologically(make_client) -> None:
    """issue / inline / review summaries 가 모두 가져와지고 created_at 시간순 정렬되는지."""
    issue_payload = [
        {"id": 1, "user": {"login": "alice"}, "body": "리뷰 부탁",
         "created_at": "2026-05-01T10:00:00Z"},
    ]
    inline_payload = [
        {"id": 12345, "user": {"login": "gemini-pr-review-bot[bot]"},
         "body": "[Major] phantom 가능성", "path": "x.py", "line": 10,
         "created_at": "2026-05-02T03:00:00Z"},
    ]
    reviews_payload = [
        {"user": {"login": "codex-review-bot[bot]"},
         "body": "이전 라운드 codex 리뷰 본문",
         "submitted_at": "2026-05-02T05:00:00Z"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        path = req.url.path
        if path.endswith("/issues/42/comments"):
            return httpx.Response(200, json=issue_payload)
        if path.endswith("/pulls/42/comments"):
            return httpx.Response(200, json=inline_payload)
        if path.endswith("/pulls/42/reviews"):
            return httpx.Response(200, json=reviews_payload)
        raise AssertionError(f"unexpected path: {path}")

    client = make_client(handler)
    history = await client.fetch_review_history(_pr(), 7)

    assert len(history.comments) == 3
    # 시간순: 5/1 issue → 5/2 03:00 inline → 5/2 05:00 review-summary
    assert history.comments[0].kind == "issue"
    assert history.comments[1].kind == "inline"
    assert history.comments[2].kind == "review-summary"
    # inline 의 comment_id 가 보존되어야 메타리플라이 타깃 회수 가능.
    assert history.comments[1].comment_id == 12345


async def test_fetch_review_history_filters_our_followup_marker(make_client) -> None:
    """우리 봇이 단 follow-up 자동 답글은 history 에서 제외."""
    inline_payload = [
        {"id": 1, "user": {"login": "codex-review-bot[bot]"},
         "body": f"resolved\n\n{FOLLOWUP_MARKER}",
         "path": "x.py", "line": 10,
         "created_at": "2026-05-01T10:00:00Z"},
        {"id": 2, "user": {"login": "gemini-pr-review-bot[bot]"},
         "body": "정상 리뷰 코멘트",
         "path": "x.py", "line": 20,
         "created_at": "2026-05-01T11:00:00Z"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        path = req.url.path
        if path.endswith("/pulls/42/comments"):
            return httpx.Response(200, json=inline_payload)
        return httpx.Response(200, json=[])

    client = make_client(handler)
    history = await client.fetch_review_history(_pr(), 7)

    # FOLLOWUP_MARKER 박힌 첫 항목은 제외 — gemini 의 정상 코멘트만 남음.
    assert len(history.comments) == 1
    assert history.comments[0].comment_id == 2


async def test_fetch_review_history_empty_when_pr_has_no_comments(make_client) -> None:
    """3개 엔드포인트 모두 빈 배열이면 빈 history — 첫 리뷰 호환성."""
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        return httpx.Response(200, json=[])

    client = make_client(handler)
    history = await client.fetch_review_history(_pr(), 7)
    assert history.is_empty


async def test_fetch_review_history_respects_page_safety_cap(make_client) -> None:
    """회귀 (gemini PR #24 Major): GitHub 가 비정상적으로 무한 Link rel=next 를 반환해도
    `_collect_pages` 가 100 페이지 cap 에서 멈춰 무한 루프 / OOM 을 방지.

    handler 가 항상 다음 페이지로 가리키는 Link 헤더를 반환하도록 만들고, 호출 횟수가
    일정 상한 안에 머무는지 (≤100 페이지/엔드포인트 x 3 엔드포인트 + 토큰 1) 확인.
    """
    call_counts = {"issues": 0, "pulls_comments": 0, "pulls_reviews": 0, "token": 0}

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/access_tokens"):
            call_counts["token"] += 1
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        if path.endswith("/issues/42/comments"):
            call_counts["issues"] += 1
        elif path.endswith("/pulls/42/comments"):
            call_counts["pulls_comments"] += 1
        elif path.endswith("/pulls/42/reviews"):
            call_counts["pulls_reviews"] += 1
        # 한 항목만 담아 응답 + 항상 다음 페이지로 가리키는 Link 헤더 → 무한 루프 시뮬.
        next_url = "https://api.github.com" + path + "?page=next"
        return httpx.Response(
            200, json=[],  # 빈 배열로도 cap 동작 확인 가능
            headers={"Link": f'<{next_url}>; rel="next"'},
        )

    client = make_client(handler)
    history = await client.fetch_review_history(_pr(), 7)

    # 각 엔드포인트가 정확히 100 번까지만 호출됐는지 (101 번째에서 cap).
    assert call_counts["issues"] == 100
    assert call_counts["pulls_comments"] == 100
    assert call_counts["pulls_reviews"] == 100
    # body 는 빈 응답이라 history 도 비어 있음 — 핵심 계약은 "무한 루프 안 빠짐".
    assert history.is_empty


async def test_fetch_review_history_partial_failure_preserves_other_endpoints(
    make_client,
) -> None:
    """회귀 (gemini PR #24 Critical+Major): 한 엔드포인트가 일시 장애여도 다른 엔드포인트
    의 정상 데이터는 보존된다. 이전 TaskGroup 구현은 첫 실패 시 형제 태스크를 자동
    취소해 정상 받은 데이터까지 통째 유실했으나, 이제 gather(return_exceptions=True)
    로 graceful degradation.
    """
    issue_payload = [
        {"id": 1, "user": {"login": "alice"}, "body": "issue 정상",
         "created_at": "2026-05-01T10:00:00Z"},
    ]
    inline_payload = [
        {"id": 12345, "user": {"login": "gemini-pr-review-bot[bot]"},
         "body": "inline 정상", "path": "x.py", "line": 10,
         "created_at": "2026-05-01T11:00:00Z"},
    ]

    def handler(req: httpx.Request) -> httpx.Response:
        path = req.url.path
        if path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        if path.endswith("/issues/42/comments"):
            return httpx.Response(200, json=issue_payload)
        if path.endswith("/pulls/42/comments"):
            return httpx.Response(200, json=inline_payload)
        if path.endswith("/pulls/42/reviews"):
            # reviews 엔드포인트만 일시 장애 — 503 Service Unavailable.
            return httpx.Response(503, json={"message": "transient outage"})
        raise AssertionError(f"unexpected path: {path}")

    client = make_client(handler)
    history = await client.fetch_review_history(_pr(), 7)

    # reviews 가 실패해도 issue / inline 은 보존돼야 한다.
    assert len(history.comments) == 2
    kinds = sorted(c.kind for c in history.comments)
    assert kinds == ["inline", "issue"]


async def test_fetch_review_history_returns_empty_when_all_endpoints_fail(
    make_client,
) -> None:
    """모든 엔드포인트가 실패하면 빈 ReviewHistory — 호출자는 추가 catch 없이 안전.

    DIP 회복 (gemini PR #24): 앱 계층이 httpx 등 인프라 라이브러리 예외를 catch 할
    필요가 없도록 인프라 단에서 자체 처리.
    """
    def handler(req: httpx.Request) -> httpx.Response:
        if req.url.path.endswith("/access_tokens"):
            return httpx.Response(200, json={"token": "ITOK", "expires_at": "2030-01-01T00:00:00Z"})
        # 모든 데이터 엔드포인트 503.
        return httpx.Response(503, json={"message": "outage"})

    client = make_client(handler)
    history = await client.fetch_review_history(_pr(), 7)

    assert history.is_empty
