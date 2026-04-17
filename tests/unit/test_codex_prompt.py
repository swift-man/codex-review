from codex_review.domain import FileDump, FileEntry, PullRequest, RepoRef
from codex_review.infrastructure.codex_prompt import build_prompt


def _pr() -> PullRequest:
    return PullRequest(
        repo=RepoRef("octo", "demo"),
        number=7,
        title="제목",
        body="본문",
        head_sha="abc",
        head_ref="feat",
        base_sha="def",
        base_ref="main",
        clone_url="https://github.com/octo/demo.git",
        changed_files=("src/a.py",),
        installation_id=1,
        is_draft=False,
    )


def test_prompt_contains_three_section_schema_and_korean_rule() -> None:
    dump = FileDump(
        entries=(FileEntry(path="src/a.py", content="x=1\ny=2", size_bytes=7, is_changed=True),),
        total_chars=7,
    )
    prompt = build_prompt(_pr(), dump)

    assert "한국어" in prompt
    assert "positives" in prompt
    assert "improvements" in prompt
    assert "comments" in prompt
    assert "--- FILE: src/a.py [CHANGED] ---" in prompt
    assert "    1| x=1" in prompt
    assert "    2| y=2" in prompt


def test_prompt_requires_line_numbers_for_comments() -> None:
    dump = FileDump(entries=(), total_chars=0)
    prompt = build_prompt(_pr(), dump)
    assert "라인 번호" in prompt or "line" in prompt
    assert "반드시" in prompt


def test_prompt_mentions_idiomatic_api_taste() -> None:
    dump = FileDump(entries=(), total_chars=0)
    prompt = build_prompt(_pr(), dump)
    # 기초 타입 수준 팁은 배제한다는 규칙이 프롬프트에 들어 있어야 한다.
    assert "pathlib" in prompt
    assert "useMemo" in prompt or "useCallback" in prompt
    assert "Protocol" in prompt


def test_prompt_mentions_exclusions_when_budget_truncated() -> None:
    dump = FileDump(
        entries=(),
        total_chars=0,
        excluded=("big/foo.py",),
        exceeded_budget=True,
    )
    prompt = build_prompt(_pr(), dump)
    assert "제외된 파일" in prompt
    assert "big/foo.py" in prompt
