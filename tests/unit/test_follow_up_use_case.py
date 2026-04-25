"""Unit tests for `FollowUpReviewUseCase` — Phase 1 deterministic resolution.

검증 시나리오:
  - 파일이 삭제된 thread → reply + resolve 호출
  - 라인이 EOF 를 넘은 thread → reply + resolve 호출
  - 라인이 그대로 유지되는 thread → no-op (Phase 2 후보)
  - 다른 봇/사람의 thread → 후보 제외
  - 이미 resolved thread → 후보 제외
  - 사람 답글 있는 thread → 후보 제외
  - 우리가 이미 follow-up 답글 단 thread → 후보 제외 (멱등성)
  - line=None outdated thread → 후보 제외
  - 후보 0 → API 호출 0 (list 만 1회)
"""

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from pathlib import Path

from codex_review.application.follow_up_use_case import FollowUpReviewUseCase
from codex_review.domain import PullRequest, RepoRef, ReviewThread


BOT_LOGIN = "codex-review-bot[bot]"


def _pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("o", "r"),
        number=42,
        title="t", body="",
        head_sha="abc", head_ref="feat",
        base_sha="def", base_ref="main",
        clone_url="https://example/x.git",
        changed_files=("a.py",),
        installation_id=7,
        is_draft=False,
    )


def _thread(
    *,
    id: str = "T_abc",
    is_resolved: bool = False,
    root_comment_id: int = 100,
    root_author: str = BOT_LOGIN,
    path: str = "src/a.py",
    line: int | None = 10,
    has_other: bool = False,
    has_marker: bool = False,
) -> ReviewThread:
    return ReviewThread(
        id=id,
        is_resolved=is_resolved,
        root_comment_id=root_comment_id,
        root_author_login=root_author,
        path=path,
        line=line,
        commit_id="oldsha",
        body="[Major] something needs fixing",
        has_non_root_author_reply=has_other,
        has_followup_marker=has_marker,
    )


@dataclass
class _FakeGitHub:
    threads: tuple[ReviewThread, ...] = ()
    replies: list[tuple[int, str]] = field(default_factory=list)
    resolved_ids: list[str] = field(default_factory=list)
    list_calls: int = 0
    token_calls: int = 0

    async def fetch_pull_request(self, *args, **kwargs):
        raise AssertionError("not used")

    async def post_review(self, *args, **kwargs):
        raise AssertionError("not used")

    async def post_comment(self, *args, **kwargs):
        raise AssertionError("not used")

    async def get_installation_token(self, installation_id: int) -> str:
        self.token_calls += 1
        return "TOKEN"

    async def list_review_threads(self, pr, installation_id):
        self.list_calls += 1
        return self.threads

    async def reply_to_review_comment(self, pr, comment_id, body):
        self.replies.append((comment_id, body))

    async def resolve_review_thread(self, thread_id, installation_id):
        self.resolved_ids.append(thread_id)


@dataclass
class _DiskFetcher:
    """tmp 경로의 실제 파일 시스템을 그대로 노출하는 fetcher 더블."""
    root: Path

    @asynccontextmanager
    async def session(self, pr, token) -> AsyncIterator[Path]:
        yield self.root


def _make_use_case(github: _FakeGitHub, root: Path) -> FollowUpReviewUseCase:
    return FollowUpReviewUseCase(
        github=github, repo_fetcher=_DiskFetcher(root), bot_user_login=BOT_LOGIN,
    )


# ---------------------------------------------------------------------------
# Resolution paths
# ---------------------------------------------------------------------------


async def test_replies_and_resolves_when_file_is_deleted(tmp_path: Path) -> None:
    """파일이 PR head 에서 사라지면 자동 해소 답글 + resolve."""
    # tmp 안에 파일 없음 → "삭제됨" 시나리오
    github = _FakeGitHub(threads=(_thread(path="removed.py", line=5),))
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    assert len(github.replies) == 1
    comment_id, body = github.replies[0]
    assert comment_id == 100
    assert "📁" in body and "자동 해소" in body
    assert "<!-- codex-review-followup:v1 -->" in body
    assert github.resolved_ids == ["T_abc"]


async def test_replies_and_resolves_when_line_is_beyond_eof(tmp_path: Path) -> None:
    """파일은 있지만 라인 번호가 현재 파일보다 길면 자동 해소."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("line1\nline2\n", encoding="utf-8")
    github = _FakeGitHub(threads=(_thread(path="src/a.py", line=99),))
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    assert len(github.replies) == 1
    body = github.replies[0][1]
    assert "📐" in body and "자동 해소" in body
    assert "99" in body  # 원본 라인 번호 노출
    assert github.resolved_ids == ["T_abc"]


async def test_no_action_when_line_still_in_range(tmp_path: Path) -> None:
    """라인이 EOF 안쪽에 있으면 결정 불가 — Phase 2 후보로 남기고 무대응."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("\n".join(f"l{i}" for i in range(50)), encoding="utf-8")
    github = _FakeGitHub(threads=(_thread(path="src/a.py", line=10),))
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    assert github.replies == []
    assert github.resolved_ids == []


# ---------------------------------------------------------------------------
# Candidate filtering — should NOT trigger any reply
# ---------------------------------------------------------------------------


async def test_skips_thread_from_other_author(tmp_path: Path) -> None:
    """다른 봇/사람이 단 thread 는 우리 follow-up 대상이 아님."""
    github = _FakeGitHub(threads=(_thread(root_author="some-other-bot[bot]"),))
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    assert github.replies == []
    assert github.resolved_ids == []


async def test_skips_already_resolved_thread(tmp_path: Path) -> None:
    github = _FakeGitHub(threads=(_thread(is_resolved=True),))
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    assert github.replies == []
    assert github.resolved_ids == []


async def test_skips_thread_with_human_reply(tmp_path: Path) -> None:
    """사람·다른 봇이 답글을 단 스레드엔 자동 follow-up 으로 끼어들지 않는다."""
    github = _FakeGitHub(threads=(_thread(has_other=True),))
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    assert github.replies == []
    assert github.resolved_ids == []


async def test_skips_thread_already_followed_up(tmp_path: Path) -> None:
    """우리가 이미 답글 단 thread → 같은 push 의 webhook 재전송이나 재실행에서 중복 답글
    안 단다 (멱등성, FOLLOWUP_MARKER 검사)."""
    github = _FakeGitHub(threads=(_thread(has_marker=True, path="ghost.py"),))
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    assert github.replies == []
    assert github.resolved_ids == []


async def test_skips_thread_with_outdated_line(tmp_path: Path) -> None:
    """GitHub 가 line=None 으로 outdated 처리한 thread 는 자체 처리 — 추가 follow-up 불필요."""
    github = _FakeGitHub(threads=(_thread(line=None, path="ghost.py"),))
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    assert github.replies == []
    assert github.resolved_ids == []


async def test_no_threads_means_only_list_call(tmp_path: Path) -> None:
    """후보가 0이면 repo session 도 안 열어 git fetch 비용 안 든다."""
    github = _FakeGitHub(threads=())
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    assert github.list_calls == 1
    assert github.token_calls == 0  # session 도 안 열렸음
    assert github.replies == []
    assert github.resolved_ids == []


# ---------------------------------------------------------------------------
# Mixed scenario
# ---------------------------------------------------------------------------


async def test_processes_each_candidate_independently(tmp_path: Path) -> None:
    """여러 thread 가 섞여 있을 때 후보만 걸러 각각 처리."""
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text("line1\nline2\n", encoding="utf-8")
    threads = (
        _thread(id="T_1", root_comment_id=1, path="removed.py", line=5),       # 삭제 → 해소
        _thread(id="T_2", root_comment_id=2, path="src/a.py", line=99),         # OOR → 해소
        _thread(id="T_3", root_comment_id=3, path="src/a.py", line=1),          # 그대로 → skip
        _thread(id="T_4", root_comment_id=4, has_other=True),                   # 사람 답글 → skip
        _thread(id="T_5", root_comment_id=5, root_author="other[bot]"),         # 다른 봇 → skip
    )
    github = _FakeGitHub(threads=threads)
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    # T_1, T_2 만 처리됨
    assert {cid for cid, _ in github.replies} == {1, 2}
    assert sorted(github.resolved_ids) == ["T_1", "T_2"]


async def test_continues_processing_when_one_thread_reply_fails(tmp_path: Path) -> None:
    """한 스레드 답글 실패가 다른 스레드 처리를 막지 않는다."""
    (tmp_path / "f1.py").write_text("x\n", encoding="utf-8")
    (tmp_path / "f2.py").write_text("y\n", encoding="utf-8")

    @dataclass
    class _FlakyGitHub(_FakeGitHub):
        async def reply_to_review_comment(self, pr, comment_id, body):
            if comment_id == 1:
                raise RuntimeError("transient API failure on T_1")
            self.replies.append((comment_id, body))

    threads = (
        _thread(id="T_1", root_comment_id=1, path="ghost1.py", line=1),
        _thread(id="T_2", root_comment_id=2, path="ghost2.py", line=1),
    )
    github = _FlakyGitHub(threads=threads)
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    # T_1 은 실패했지만 T_2 는 정상 처리.
    assert [cid for cid, _ in github.replies] == [2]
    assert github.resolved_ids == ["T_2"]
