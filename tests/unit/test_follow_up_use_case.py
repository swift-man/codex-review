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
    """tmp 경로의 실제 파일 시스템을 그대로 노출하는 fetcher 더블.

    `head_sha` 는 기본적으로 `pr.head_sha` 를 그대로 반환해 정상 경로를 시뮬레이션.
    SHA 불일치 회귀 테스트에서는 `forced_head_sha` 를 세팅해 어긋난 상태를 만든다.
    """
    root: Path
    # None 이면 실제 PR 의 head_sha 와 일치한다고 가정. 회귀 테스트가 다른 SHA 를
    # 강제할 때 채운다 — repo session 이 잘못된 commit 에 머문 상태를 시뮬레이션.
    forced_head_sha: str | None = None

    @asynccontextmanager
    async def session(self, pr, token) -> AsyncIterator[Path]:
        # 가짜로 인자만 묶어 두는 컨텍스트 매니저. 실제 git checkout 은 안 함.
        self._last_pr_sha = pr.head_sha
        yield self.root

    async def head_sha(self, repo_path: Path) -> str:
        if self.forced_head_sha is not None:
            return self.forced_head_sha
        return getattr(self, "_last_pr_sha", "")


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
    """후보가 0이면 reply / resolve API 가 호출되지 않는다.

    이전 회귀 테스트는 `token_calls == 0` 까지 단언했지만, 실제 GitHub 클라이언트는
    `list_review_threads()` 내부에서도 installation token 을 받아오므로 0 단언은
    fake 의 단순화 모형에만 맞고 실 동작과 어긋난다 (coderabbitai PR #19 Minor).
    의미 있는 보장은 "쓰기 작업이 없었다" 와 "list 1회" — 그쪽으로 좁힌다.
    """
    github = _FakeGitHub(threads=())
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    assert github.list_calls == 1
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


async def test_continues_processing_when_one_thread_reply_fails(
    tmp_path: Path, caplog
) -> None:
    """한 스레드 reply 실패가 다른 스레드 처리를 막지 않는다.

    회귀 (gemini + coderabbitai PR #19 Major): 액션 순서가 **resolve 먼저 → reply** 라
    reply 실패 시 마커가 안 박히고 thread 만 닫힌다. 다음 push 에서 thread 가 이미
    `is_resolved=True` 라 자연스럽게 후보 제외 — stuck state 안 남음.

    추가 (gemini PR #19 Major): 실패 시 thread.id 와 traceback 이 로그에 남아야 한다.
    개수만 합산해서는 어떤 thread 가 어떤 예외로 실패했는지 추적 불가.
    """
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

    import logging as _logging
    with caplog.at_level(_logging.WARNING, logger="codex_review.application.follow_up_use_case"):
        await uc.execute(_pr())

    # 두 thread 모두 resolve 가 먼저 호출돼 성공 — 가시적 효과(unresolved 카운트 감소)
    # 는 두 thread 다 달성. T_1 reply 만 실패해 마커 미부착 → 다음 push 에서 재시도
    # 가능 (thread 는 이미 resolved 라 실제로는 후보 제외).
    assert sorted(github.resolved_ids) == ["T_1", "T_2"]
    # T_1 의 reply 는 실패했으므로 게시된 reply 는 T_2 것 한 건만.
    assert [cid for cid, _ in github.replies] == [2]
    # 실패 로그에 thread.id 와 RuntimeError 가 같이 남아야 운영자가 원인을 추적 가능.
    failure_log = next(
        r for r in caplog.records
        if r.levelname == "WARNING" and "failed" in r.getMessage() and "T_1" in r.getMessage()
    )
    assert failure_log.exc_info is not None
    assert failure_log.exc_info[0] is RuntimeError


async def test_line_count_handles_file_without_trailing_newline(tmp_path: Path) -> None:
    """회귀 — 파일 마지막 줄이 newline 으로 끝나지 않아도 라인 수가 정확히 1줄 더 카운트
    된다. `\\n` 카운트만 하면 마지막 라인이 누락돼 EOF-초과 판정이 잘못된다.
    """
    p = tmp_path / "no_trailing_newline.py"
    p.write_bytes(b"line1\nline2\nlast-without-newline")  # 3줄, 마지막 newline 없음

    from codex_review.application.follow_up_use_case import _count_lines
    assert _count_lines(p) == 3

    p_with = tmp_path / "with_trailing.py"
    p_with.write_bytes(b"line1\nline2\nline3\n")  # 3줄, 마지막에 newline
    assert _count_lines(p_with) == 3

    empty = tmp_path / "empty.py"
    empty.write_bytes(b"")
    assert _count_lines(empty) == 0


async def test_line_count_handles_huge_single_line_file_without_oom(
    tmp_path: Path,
) -> None:
    """회귀 (gemini PR #19 Critical): 줄바꿈 없는 거대 파일 (난독화 JS, 바이너리 덤프)
    에서 `for _ in f:` 가 전체를 한 줄로 읽어 OOM 유발. 청크 기반으로 카운트하면
    메모리 사용량이 chunk_size 로 상한된다.

    1MB 파일은 OS 별 차이로 실제 OOM 까진 안 가지만, 메모리 폭발 위험이 있던 코드
    경로가 chunk 단위로 안전하게 처리됨을 동작 가능성 차원에서 검증.
    """
    huge = tmp_path / "huge_single_line.dat"
    # 1MB, newline 0개 — 한 줄짜리 파일
    huge.write_bytes(b"x" * (1024 * 1024))

    from codex_review.application.follow_up_use_case import _count_lines
    assert _count_lines(huge) == 1  # newline 0개 + 비어있지 않으므로 1줄


async def test_classify_returns_none_when_line_in_file(tmp_path: Path) -> None:
    """라인이 파일 안쪽이면 분류 결과 None (Phase 1 결정 불가, Phase 2 후보)."""
    f = tmp_path / "src" / "a.py"
    f.parent.mkdir()
    f.write_text("\n".join(f"l{i}" for i in range(50)), encoding="utf-8")

    from codex_review.application.follow_up_use_case import _classify_thread
    thread = _thread(path="src/a.py", line=10)
    assert _classify_thread(thread, tmp_path) is None


async def test_classify_skips_path_escaping_repo_root(tmp_path: Path) -> None:
    """회귀 (codex + gemini PR #19 Major): GitHub 가 반환한 thread.path 가
    `../../etc/passwd` 같은 이탈 경로일 때, 저장소 밖 파일 상태로 자동 해소
    판정을 하지 않는다. resolve() + is_relative_to(repo_root) 검증으로 떨어뜨리고
    분류 결과는 None — 즉 "안전한 무대응".
    """
    # 저장소 루트 밖에 실제 파일 하나를 만들어 두고, repo_root 안의 thread 가
    # 그 파일을 가리키는 ../ 경로를 보내는 상황을 시뮬레이션.
    repo_root = tmp_path / "repo"
    repo_root.mkdir()
    outside = tmp_path / "outside.py"
    outside.write_text("x = 1\n", encoding="utf-8")

    from codex_review.application.follow_up_use_case import _classify_thread

    # 명백한 이탈 (`../outside.py`): repo_root 밖을 가리킨다 → None 이어야 함.
    # exists()/line_count 어느 쪽도 호출되지 않아 자동 해소 답글이 만들어지면 안 됨.
    thread = _thread(path="../outside.py", line=99999)
    assert _classify_thread(thread, repo_root) is None

    # 절대 경로 형태로 들어오는 경우도 동일하게 차단되어야 한다.
    thread_abs = _thread(path=str(outside), line=99999)
    assert _classify_thread(thread_abs, repo_root) is None


async def test_resolve_failure_does_not_post_reply_marker(tmp_path: Path) -> None:
    """회귀 (gemini + coderabbitai PR #19 Major): resolve 가 먼저, 실패 시 reply 안 함.

    이전 순서(reply→resolve) 는 reply 가 마커를 박은 뒤 resolve 가 실패하면 thread
    는 미해결 상태인데 다음 실행에서 `has_followup_marker=True` 로 skip → 영원히
    stuck. 새 순서(resolve→reply) 는 resolve 실패 시 reply 도 안 해 마커가 안 박히고
    다음 push 에서 자연스럽게 재시도 가능.
    """
    @dataclass
    class _ResolveFailingGitHub(_FakeGitHub):
        async def resolve_review_thread(self, thread_id, installation_id):
            # resolve 실패 — reply 가 호출되면 안 된다.
            raise RuntimeError("GraphQL errors: thread already resolved")

    github = _ResolveFailingGitHub(
        threads=(_thread(id="T_X", root_comment_id=99, path="missing.py", line=1),),
    )
    uc = _make_use_case(github, tmp_path)

    await uc.execute(_pr())

    # 핵심 계약: resolve 실패 시 reply 절대 호출 X (마커 박지 않음 → 다음 push 재시도 가능).
    assert github.replies == []
    assert github.resolved_ids == []  # resolve 가 raise 했으므로 등록도 안 됨


async def test_aborts_when_repo_head_sha_does_not_match_pr_head(
    tmp_path: Path, caplog
) -> None:
    """회귀 (codex PR #19 Major): repo_fetcher.session() 은 작업 트리를 pr.head_sha
    로 checkout 한다는 계약이지만, 캐시 손상 등 극단 상황에서 다른 SHA 에 머무를
    수 있다. 그 상태로 follow-up 을 진행하면 다른 commit 의 파일 상태로 valid
    thread 를 잘못 resolve 해 silent feedback loss 가 발생.

    SHA 가 어긋나면 전체 follow-up 을 skip 하고 경고만 남기는지 확인.
    """
    # 파일이 실제로는 없지만, SHA 불일치로 follow-up 이 skip 되면 분류 단계까지 가지
    # 않으므로 로컬 파일 상태와 무관하게 결과는 "아무 작업 안 함" 이어야 한다.
    threads = (_thread(id="T_X", root_comment_id=1, path="ghost.py", line=1),)
    github = _FakeGitHub(threads=threads)
    fetcher = _DiskFetcher(root=tmp_path, forced_head_sha="0" * 40)
    uc = FollowUpReviewUseCase(
        github=github, repo_fetcher=fetcher, bot_user_login="codex-review-bot[bot]"
    )

    import logging as _logging
    with caplog.at_level(
        _logging.WARNING, logger="codex_review.application.follow_up_use_case"
    ):
        await uc.execute(_pr())

    # 분류조차 안 했으므로 어떤 thread 도 resolve / reply 되지 않음.
    assert github.resolved_ids == []
    assert github.replies == []
    # 경고 로그에 SHA 불일치 사유가 명시돼야 운영자가 추적 가능.
    abort_log = next(
        (r for r in caplog.records
         if r.levelname == "WARNING" and "aborting" in r.getMessage()),
        None,
    )
    assert abort_log is not None
    assert "0000000000000000000000000000000000000000" in abort_log.getMessage()
