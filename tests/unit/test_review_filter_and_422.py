"""Regression coverage for:
  - use-case filters findings against the PR's RIGHT-side diff lines
  - post_review retries with empty comments when GitHub returns 422
"""

import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import jwt
import pytest

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
# Use-case filtering
# ---------------------------------------------------------------------------


@dataclass
class _CapturingGitHub:
    posted: list[tuple[PullRequest, ReviewResult]] = field(default_factory=list)
    comments: list[tuple[PullRequest, str]] = field(default_factory=list)

    def fetch_pull_request(self, repo: RepoRef, number: int, installation_id: int):  # noqa: D401,E501
        raise AssertionError("not used in these tests")

    def post_review(self, pr: PullRequest, result: ReviewResult) -> None:
        self.posted.append((pr, result))

    def post_comment(self, pr: PullRequest, body: str) -> None:
        self.comments.append((pr, body))

    def get_installation_token(self, installation_id: int) -> str:
        return "tkn"


class _NoopFetcher:
    def checkout(self, pr: PullRequest, installation_token: str) -> Path:
        return Path(".")


class _ConstCollector:
    def __init__(self, dump: FileDump) -> None:
        self._dump = dump

    def collect(self, root: Path, changed_files: tuple[str, ...], budget: TokenBudget) -> FileDump:
        return self._dump


class _StaticEngine:
    def __init__(self, result: ReviewResult) -> None:
        self._result = result

    def review(self, pr: PullRequest, dump: FileDump) -> ReviewResult:
        return self._result


def _run_use_case(github: _CapturingGitHub, pr: PullRequest, result: ReviewResult) -> None:
    dump = FileDump(entries=(), total_chars=0)
    uc = ReviewPullRequestUseCase(
        github=github,
        repo_fetcher=_NoopFetcher(),
        file_collector=_ConstCollector(dump),
        engine=_StaticEngine(result),
        max_input_tokens=1000,
    )
    uc.execute(pr)


def test_use_case_drops_findings_outside_diff() -> None:
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

    _run_use_case(github, pr, original)

    assert len(github.posted) == 1
    _, posted = github.posted[0]
    assert [f.body for f in posted.findings] == ["kept"]


def test_use_case_drops_all_findings_when_diff_info_missing() -> None:
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

    _run_use_case(github, pr, original)

    _, posted = github.posted[0]
    assert posted.findings == ()


def test_use_case_passes_result_through_when_no_findings() -> None:
    github = _CapturingGitHub()
    pr = _pr(diff_right_lines={"a.py": frozenset({1})})
    original = ReviewResult(summary="s", event=ReviewEvent.COMMENT)

    _run_use_case(github, pr, original)

    _, posted = github.posted[0]
    assert posted is original


# ---------------------------------------------------------------------------
# post_review 422 fallback
# ---------------------------------------------------------------------------


class _FakeResponse:
    def __init__(self, body: bytes) -> None:
        self._body = body

    def __enter__(self) -> "_FakeResponse":
        return self

    def __exit__(self, *_args: object) -> None:
        return None

    def read(self) -> bytes:
        return self._body


def _make_http_error(code: int) -> urllib.error.HTTPError:
    # HTTPError 는 filelike body 를 요구 — io 대체 대신 간단한 래퍼.
    import io

    return urllib.error.HTTPError(
        url="https://api.github.com/whatever",
        code=code,
        msg="fail",
        hdrs=None,  # type: ignore[arg-type]
        fp=io.BytesIO(b'{"message": "fail"}'),
    )


@pytest.fixture()
def client_with_stubbed_urlopen(monkeypatch: pytest.MonkeyPatch):
    calls: list[dict[str, Any]] = []
    responses: list[Any] = []  # filled in per test

    def fake_urlopen(
        req: urllib.request.Request,
        *,
        timeout: float | None = None,
        context: ssl.SSLContext | None = None,
    ) -> _FakeResponse:
        body = req.data.decode("utf-8") if req.data else ""
        parsed = json.loads(body) if body else None
        calls.append({"url": req.full_url, "method": req.get_method(), "body": parsed})
        nxt = responses.pop(0) if responses else _FakeResponse(b'{}')
        if isinstance(nxt, Exception):
            raise nxt
        return nxt

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")

    client = GitHubAppClient(app_id=1, private_key_pem="-")
    # installation token 캐시에 직접 넣어 `/access_tokens` 라운드트립을 스킵
    client._token_cache[7] = type(  # type: ignore[attr-defined]
        "Tok", (), {"token": "ITOK", "is_valid": lambda self: True}
    )()
    return client, calls, responses


def _review_result_with_inline() -> ReviewResult:
    return ReviewResult(
        summary="s",
        event=ReviewEvent.COMMENT,
        findings=(Finding(path="a.py", line=10, body="x"),),
    )


def test_post_review_retries_without_comments_on_422(client_with_stubbed_urlopen) -> None:
    client, calls, responses = client_with_stubbed_urlopen
    # 1st call: 422. 2nd call: 200 (empty response).
    responses.extend([_make_http_error(422), _FakeResponse(b"{}")])

    pr = _pr(diff_right_lines={"a.py": frozenset({10})})
    client.post_review(pr, _review_result_with_inline())

    assert len(calls) == 2
    assert calls[0]["body"]["comments"] == [
        {"path": "a.py", "line": 10, "side": "RIGHT", "body": "x"}
    ]
    assert calls[1]["body"]["comments"] == []


def test_post_review_422_retry_also_rerenders_body_without_findings_notice(
    client_with_stubbed_urlopen,
) -> None:
    """회귀 방지: 재시도 본문이 findings 제거 상태로 다시 렌더링돼야 한다.
    그러지 않으면 '기술 단위 코멘트 N건' 안내가 남아 실제로는 인라인이 없는데도
    있는 것처럼 보이는 거짓 상태가 된다.
    """
    client, calls, responses = client_with_stubbed_urlopen
    responses.extend([_make_http_error(422), _FakeResponse(b"{}")])

    result = ReviewResult(
        summary="전체 요약",
        event=ReviewEvent.COMMENT,
        positives=("좋은 점 1",),
        improvements=("개선할 점 1",),
        findings=(Finding(path="a.py", line=10, body="x"),),
    )
    pr = _pr(diff_right_lines={"a.py": frozenset({10})})
    client.post_review(pr, result)

    first_body = calls[0]["body"]["body"]
    retry_body = calls[1]["body"]["body"]

    # 1차 시도 본문엔 "기술 단위 코멘트 N건..." 안내가 있어야 한다.
    assert "기술 단위 코멘트" in first_body
    # 재시도 본문엔 안내가 **없어야** 한다(인라인이 실제로 빠졌으므로).
    assert "기술 단위 코멘트" not in retry_body
    # 다른 섹션(요약/좋은 점/개선할 점)은 재시도에도 그대로 남는다.
    assert "전체 요약" in retry_body
    assert "좋은 점 1" in retry_body
    assert "개선할 점 1" in retry_body


def test_post_review_reraises_non_422_http_errors(client_with_stubbed_urlopen) -> None:
    client, _calls, responses = client_with_stubbed_urlopen
    responses.append(_make_http_error(500))

    with pytest.raises(urllib.error.HTTPError) as exc:
        client.post_review(_pr(), _review_result_with_inline())
    assert exc.value.code == 500


def test_post_review_does_not_retry_when_no_comments(client_with_stubbed_urlopen) -> None:
    client, calls, responses = client_with_stubbed_urlopen
    responses.append(_make_http_error(422))

    result = ReviewResult(summary="s", event=ReviewEvent.COMMENT)  # no findings
    with pytest.raises(urllib.error.HTTPError):
        client.post_review(_pr(), result)
    assert len(calls) == 1  # no retry when there were no comments to drop


# ---------------------------------------------------------------------------
# Model footer (constant) — 인프라 계층에서만 붙는다.
# ---------------------------------------------------------------------------


def _capture_urlopen(monkeypatch: pytest.MonkeyPatch) -> list[dict[str, Any]]:
    calls: list[dict[str, Any]] = []

    def fake_urlopen(req, *, timeout=None, context=None):
        body = json.loads(req.data.decode("utf-8")) if req.data else None
        calls.append({"body": body})
        return _FakeResponse(b"{}")

    monkeypatch.setattr(urllib.request, "urlopen", fake_urlopen)
    monkeypatch.setattr(jwt, "encode", lambda *a, **k: "fake.jwt")
    return calls


def _client_with_token_cache(model_label: str | None = None) -> GitHubAppClient:
    client = GitHubAppClient(
        app_id=1, private_key_pem="-", review_model_label=model_label
    )
    client._token_cache[7] = type(  # type: ignore[attr-defined]
        "Tok", (), {"token": "ITOK", "is_valid": lambda self: True}
    )()
    return client


def test_post_review_appends_model_footer_from_constant_label(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """리뷰 본문 맨 아래에 `review_model_label` 상수가 footer 로 붙는지 확인.
    모델명은 LLM 이 아니라 GitHubAppClient 생성자 상수에서 온다.
    """
    calls = _capture_urlopen(monkeypatch)
    client = _client_with_token_cache(model_label="gpt-5.4")

    client.post_review(
        _pr(diff_right_lines={"a.py": frozenset({10})}),
        ReviewResult(
            summary="요약",
            event=ReviewEvent.COMMENT,
            findings=(Finding(path="a.py", line=10, body="x"),),
        ),
    )

    body = calls[0]["body"]["body"]
    assert body.rstrip().endswith("<code>gpt-5.4</code></sub>")
    assert "리뷰 모델" in body


def test_post_review_omits_footer_when_label_is_none(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    calls = _capture_urlopen(monkeypatch)
    client = _client_with_token_cache(model_label=None)

    client.post_review(_pr(), ReviewResult(summary="요약", event=ReviewEvent.COMMENT))

    body = calls[0]["body"]["body"]
    assert "리뷰 모델" not in body
    assert "<sub>" not in body


def test_post_review_422_retry_keeps_model_footer(
    client_with_stubbed_urlopen,
) -> None:
    """회귀 방지: 422 재시도 본문도 동일한 모델 footer 를 달고 나가야 한다.
    모델 라벨은 상수라 1차/재시도 모두 같은 문자열이 붙는다.
    """
    client, calls, responses = client_with_stubbed_urlopen
    client._review_model_label = "gpt-5.4"  # type: ignore[attr-defined]
    responses.extend([_make_http_error(422), _FakeResponse(b"{}")])

    client.post_review(
        _pr(diff_right_lines={"a.py": frozenset({10})}),
        _review_result_with_inline(),
    )

    assert "리뷰 모델" in calls[0]["body"]["body"]
    assert "리뷰 모델" in calls[1]["body"]["body"]
    # 재시도에서는 "기술 단위 코멘트" 안내가 빠져야 한다 (findings 제거)
    assert "기술 단위 코멘트" not in calls[1]["body"]["body"]
