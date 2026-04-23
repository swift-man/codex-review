"""Regression coverage for:
  - use-case filters findings against the PR's RIGHT-side diff lines
  - post_review retries with empty comments when GitHub returns 422
"""

import json
from collections.abc import AsyncIterator
from contextlib import AsyncExitStack, asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import httpx
import jwt
import pytest
import pytest_asyncio

from codex_review.application.review_pr_use_case import ReviewPullRequestUseCase
from codex_review.domain import (
    FileDump,
    Finding,
    PullRequest,
    RepoRef,
    ReviewEvent,
    ReviewResult,
    TokenBudget,
)
from codex_review.infrastructure.github_app_client import GitHubAppClient


def _pr(diff_right_lines: dict[str, frozenset[int]] | None = None) -> PullRequest:
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
        changed_files=("a.py", "b.py"),
        installation_id=7,
        is_draft=False,
        diff_right_lines=diff_right_lines or {},
    )


# ---------------------------------------------------------------------------
# Use-case filtering (uses async fakes)
# ---------------------------------------------------------------------------


@dataclass
class _CapturingGitHub:
    posted: list[tuple[PullRequest, ReviewResult]] = field(default_factory=list)
    comments: list[tuple[PullRequest, str]] = field(default_factory=list)

    async def fetch_pull_request(
        self, repo: RepoRef, number: int, installation_id: int
    ) -> PullRequest:
        raise AssertionError("not used in these tests")

    async def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        self.posted.append((pr, result))

    async def post_comment(self, pr: PullRequest, body: str) -> None:
        self.comments.append((pr, body))

    async def get_installation_token(self, installation_id: int) -> str:
        return "tkn"


class _NoopFetcher:
    @asynccontextmanager
    async def session(
        self, pr: PullRequest, installation_token: str
    ) -> AsyncIterator[Path]:
        yield Path(".")


class _ConstCollector:
    def __init__(self, dump: FileDump) -> None:
        self._dump = dump

    async def collect(
        self, root: Path, changed_files: tuple[str, ...], budget: TokenBudget
    ) -> FileDump:
        return self._dump


class _StaticEngine:
    def __init__(self, result: ReviewResult) -> None:
        self._result = result

    async def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        return self._result


async def _run_use_case(
    github: _CapturingGitHub, pr: PullRequest, result: ReviewResult
) -> None:
    dump = FileDump(entries=(), total_chars=0)
    uc = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=_NoopFetcher(),
        file_collector=_ConstCollector(dump),
        engine=_StaticEngine(result),
        max_input_tokens=1000,
    )
    await uc.execute(pr)


async def test_use_case_drops_findings_outside_diff() -> None:
    github = _CapturingGitHub()
    pr = _pr(diff_right_lines={"a.py": frozenset({10, 11, 12})})
    original = ReviewResult(
        summary="s",
        event=ReviewEvent.COMMENT,
        findings=(
            Finding(path="a.py", line=10, body="kept"),
            Finding(path="a.py", line=99, body="invalid line"),
            Finding(path="unchanged.py", line=1, body="unchanged file"),
            Finding(path="b.py", line=1, body="no diff for b.py"),
        ),
    )

    await _run_use_case(github, pr, original)

    assert len(github.posted) == 1
    _, posted = github.posted[0]
    assert [f.body for f in posted.findings] == ["kept"]
    # 회귀 (codex/gemini PR #17): drop 된 3건은 조용히 사라지지 않고 dropped_findings
    # 에 보존돼 render_body() 가 접이식 섹션으로 노출해야 한다.
    dropped_bodies = {f.body for f in posted.dropped_findings}
    assert dropped_bodies == {"invalid line", "unchanged file", "no diff for b.py"}
    body = posted.render_body()
    assert "인라인 게시에서 제외된 지적 3건" in body
    assert "<details>" in body and "</details>" in body
    # 라인·경로·원문이 모두 섹션에 있어 리뷰어가 찾아볼 수 있어야 한다.
    assert "`a.py:99`" in body
    assert "invalid line" in body


async def test_use_case_drops_all_findings_when_diff_info_missing() -> None:
    """If diff_right_lines is empty the use case must drop *all* findings
    (cannot validate → safer to never post inline than risk 422)."""
    github = _CapturingGitHub()
    pr = _pr(diff_right_lines={})
    original = ReviewResult(
        summary="s",
        event=ReviewEvent.COMMENT,
        findings=(
            Finding(path="a.py", line=1, body="x"),
            Finding(path="b.py", line=2, body="y"),
        ),
    )

    await _run_use_case(github, pr, original)

    _, posted = github.posted[0]
    assert posted.findings == ()


async def test_use_case_passes_result_through_when_no_findings() -> None:
    github = _CapturingGitHub()
    pr = _pr(diff_right_lines={"a.py": frozenset({1})})
    original = ReviewResult(summary="s", event=ReviewEvent.COMMENT)

    await _run_use_case(github, pr, original)

    _, posted = github.posted[0]
    assert posted is original


# ---------------------------------------------------------------------------
# post_review 422 fallback (uses httpx MockTransport)
# ---------------------------------------------------------------------------


@pytest_asyncio.fixture()
async def stubbed_github(monkeypatch: pytest.MonkeyPatch) -> AsyncIterator[Any]:
    """Factory: returns `(make_client, posts)` where make_client(*, responses, ...)
    builds a GitHubAppClient wired to a MockTransport.

    `pytest_asyncio.fixture` 로 async teardown 을 돌려 `async with httpx.AsyncClient(...)`
    를 쓰게 한다 — 이전 구현은 동기 teardown 에서 `run_until_complete(aclose())` 를 불러
    이벤트 루프 충돌 위험이 있었다.
    """
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
    posts: list[httpx.Request] = []

    async with AsyncExitStack() as stack:

        def make_client(
            *, responses: list[Any] | None = None, review_model_label: str | None = None
        ) -> GitHubAppClient:
            response_queue = list(responses or [])

            def handler(req: httpx.Request) -> httpx.Response:
                if req.url.path.endswith("/access_tokens"):
                    return httpx.Response(
                        200, json={"token": "ITOK", "expires_at": "2026-04-22T00:00:00Z"}
                    )
                if "/reviews" in req.url.path and req.method == "POST":
                    posts.append(req)
                    if not response_queue:
                        return httpx.Response(200, json={})
                    nxt = response_queue.pop(0)
                    if isinstance(nxt, int):
                        return httpx.Response(nxt, json={"message": "fail"})
                    return nxt
                return httpx.Response(200, json={})

            http_client = httpx.AsyncClient(
                base_url="https://api.github.com",
                transport=httpx.MockTransport(handler),
            )
            # ExitStack 이 teardown 시 모든 클라이언트를 자동 정리 → 각자의 `async with`
            # 를 일일이 쓸 필요가 없다.
            stack.push_async_callback(http_client.aclose)
            return GitHubAppClient(
                app_id=1,
                private_key_pem="-",
                http_client=http_client,
                review_model_label=review_model_label,
            )

        yield make_client, posts


def _review_result_with_inline() -> ReviewResult:
    return ReviewResult(
        summary="s",
        event=ReviewEvent.COMMENT,
        findings=(Finding(path="a.py", line=10, body="x"),),
    )


def _body_of(req: httpx.Request) -> dict[str, Any]:
    return json.loads(req.content.decode("utf-8"))


async def test_post_review_retries_without_comments_on_422(stubbed_github) -> None:
    make_client, posts = stubbed_github
    client = make_client(responses=[422])  # 1차만 실패, 2차는 기본 200

    pr = _pr(diff_right_lines={"a.py": frozenset({10})})
    await client.post_review(pr, _review_result_with_inline())

    assert len(posts) == 2
    # 기본 severity='suggestion' 이라 `[Suggestion]` 접두가 일관되게 붙는다.
    assert _body_of(posts[0])["comments"] == [
        {"path": "a.py", "line": 10, "side": "RIGHT", "body": "[Suggestion] x"}
    ]
    assert _body_of(posts[1])["comments"] == []


async def test_post_review_422_retry_also_rerenders_body_without_findings_notice(
    stubbed_github,
) -> None:
    """회귀: 재시도 본문이 findings 제거 상태로 다시 렌더링돼야 한다."""
    make_client, posts = stubbed_github
    client = make_client(responses=[422])

    result = ReviewResult(
        summary="전체 요약",
        event=ReviewEvent.COMMENT,
        positives=("좋은 점 1",),
        improvements=("개선할 점 1",),
        findings=(Finding(path="a.py", line=10, body="x"),),
    )
    pr = _pr(diff_right_lines={"a.py": frozenset({10})})
    await client.post_review(pr, result)

    first_body = _body_of(posts[0])["body"]
    retry_body = _body_of(posts[1])["body"]

    assert "기술 단위 코멘트" in first_body
    assert "기술 단위 코멘트" not in retry_body
    assert "전체 요약" in retry_body
    assert "좋은 점 1" in retry_body
    assert "개선할 점 1" in retry_body


async def test_post_review_422_retry_preserves_dropped_findings_in_body(
    stubbed_github,
) -> None:
    """회귀 (codex/gemini PR #17): 422 재시도로 인라인이 포기돼도 모델이 낸 지적은
    본문 `<details>` 섹션으로 보존돼야 한다. 조용히 사라지면 리뷰 품질을 과대평가.
    """
    make_client, posts = stubbed_github
    client = make_client(responses=[422])

    result = ReviewResult(
        summary="요약",
        event=ReviewEvent.COMMENT,
        findings=(
            Finding(path="a.py", line=10, body="핵심 지적 A"),
            Finding(path="b.py", line=5, body="핵심 지적 B"),
        ),
    )
    pr = _pr(diff_right_lines={"a.py": frozenset({10}), "b.py": frozenset({5})})
    await client.post_review(pr, result)

    first_body = _body_of(posts[0])["body"]
    retry_body = _body_of(posts[1])["body"]

    # 1차 시도엔 인라인 안내가 있고 dropped 섹션은 없다.
    assert "기술 단위 코멘트 2건" in first_body
    assert "인라인 게시에서 제외된 지적" not in first_body
    # 2차(재시도) 에서 findings 는 빠졌지만 두 지적이 접이식 섹션에 모두 남아야 한다.
    assert "인라인 게시에서 제외된 지적 2건" in retry_body
    assert "<details>" in retry_body
    assert "핵심 지적 A" in retry_body
    assert "핵심 지적 B" in retry_body
    assert "`a.py:10`" in retry_body
    assert "`b.py:5`" in retry_body


async def test_post_review_reraises_non_422_http_errors(stubbed_github) -> None:
    make_client, _posts = stubbed_github
    client = make_client(responses=[500])

    with pytest.raises(httpx.HTTPStatusError) as exc:
        await client.post_review(_pr(), _review_result_with_inline())
    assert exc.value.response.status_code == 500


async def test_post_review_does_not_retry_when_no_comments(stubbed_github) -> None:
    make_client, posts = stubbed_github
    client = make_client(responses=[422])

    result = ReviewResult(summary="s", event=ReviewEvent.COMMENT)  # no findings
    with pytest.raises(httpx.HTTPStatusError):
        await client.post_review(_pr(), result)
    assert len(posts) == 1  # 재시도 안 함 (애초에 drop 할 코멘트가 없음)


# ---------------------------------------------------------------------------
# Model footer (constant) — 인프라 계층에서만 붙는다.
# ---------------------------------------------------------------------------


async def test_post_review_appends_model_footer_from_constant_label(stubbed_github) -> None:
    make_client, posts = stubbed_github
    client = make_client(review_model_label="gpt-5.4")

    await client.post_review(
        _pr(diff_right_lines={"a.py": frozenset({10})}),
        ReviewResult(
            summary="요약",
            event=ReviewEvent.COMMENT,
            findings=(Finding(path="a.py", line=10, body="x"),),
        ),
    )

    body = _body_of(posts[0])["body"]
    assert body.rstrip().endswith("<code>gpt-5.4</code></sub>")
    assert "리뷰 모델" in body


async def test_post_review_omits_footer_when_label_is_none(stubbed_github) -> None:
    make_client, posts = stubbed_github
    client = make_client(review_model_label=None)

    await client.post_review(_pr(), ReviewResult(summary="요약", event=ReviewEvent.COMMENT))

    body = _body_of(posts[0])["body"]
    assert "리뷰 모델" not in body
    assert "<sub>" not in body


async def test_post_review_422_retry_keeps_model_footer(stubbed_github) -> None:
    make_client, posts = stubbed_github
    client = make_client(review_model_label="gpt-5.4", responses=[422])

    await client.post_review(
        _pr(diff_right_lines={"a.py": frozenset({10})}),
        _review_result_with_inline(),
    )

    assert "리뷰 모델" in _body_of(posts[0])["body"]
    assert "리뷰 모델" in _body_of(posts[1])["body"]
    assert "기술 단위 코멘트" not in _body_of(posts[1])["body"]
