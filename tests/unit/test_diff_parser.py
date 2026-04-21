from codex_review.infrastructure.diff_parser import parse_right_lines


def test_parse_simple_add_and_context() -> None:
    patch = (
        "@@ -1,3 +1,4 @@\n"
        " keep-1\n"
        " keep-2\n"
        "+added-3\n"
        " keep-4"
    )
    assert parse_right_lines(patch) == {1, 2, 3, 4}


def test_parse_skips_deletions() -> None:
    patch = (
        "@@ -1,3 +1,2 @@\n"
        " a\n"
        "-removed\n"
        " c"
    )
    # RIGHT-side line numbers after deletion: 1 (a), 2 (c)
    assert parse_right_lines(patch) == {1, 2}


def test_parse_multiple_hunks() -> None:
    patch = (
        "@@ -1,1 +1,2 @@\n"
        " line1\n"
        "+line2\n"
        "@@ -10,1 +11,1 @@\n"
        " line11"
    )
    assert parse_right_lines(patch) == {1, 2, 11}


def test_parse_handles_no_newline_at_eof_marker() -> None:
    patch = (
        "@@ -1,1 +1,2 @@\n"
        " a\n"
        "+b\n"
        "\\ No newline at end of file"
    )
    assert parse_right_lines(patch) == {1, 2}


def test_parse_empty_or_none_patch() -> None:
    assert parse_right_lines(None) == frozenset()
    assert parse_right_lines("") == frozenset()


def test_parse_ignores_garbage_before_first_hunk() -> None:
    # 실제 GitHub patch 는 hunk 헤더 앞에 설명이 붙는 일이 거의 없지만 방어적.
    patch = (
        "diff --git a/x b/x\n"
        "--- a/x\n"
        "+++ b/x\n"
        "@@ -1,1 +1,1 @@\n"
        " hello"
    )
    assert parse_right_lines(patch) == {1}
