"""Unified diff parser for extracting RIGHT-side comment-able line numbers.

GitHub PR Reviews API 는 인라인 코멘트의 `line` 이 해당 commit 의 RIGHT-side diff
라인에 속해야만 허용한다. 이 모듈은 `GET /repos/.../pulls/N/files` 가 돌려주는
`patch` 문자열을 분석해 각 파일의 유효 라인 집합을 만든다.

- 추가 라인(`+`) : RIGHT 증가, 포함
- 컨텍스트 라인(` `): LEFT / RIGHT 모두 증가, 포함 (GitHub 이 허용함)
- 삭제 라인(`-`) : LEFT 만 증가, 포함하지 않음
- 메타 라인(\\ No newline at end of file 등): 스킵
"""

import re

_HUNK_HEADER = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,(\d+))? @@")


def parse_right_lines(patch: str | None) -> frozenset[int]:
    """Return the set of RIGHT-side line numbers that accept inline comments."""
    if not patch:
        return frozenset()

    right_lines: set[int] = set()
    cursor: int | None = None

    for raw in patch.splitlines():
        match = _HUNK_HEADER.match(raw)
        if match:
            cursor = int(match.group(1))
            continue
        if cursor is None:
            continue
        if not raw:
            # 빈 라인이 컨텍스트로 들어오는 경우가 있다. 보수적으로 허용.
            right_lines.add(cursor)
            cursor += 1
            continue
        prefix = raw[0]
        if prefix == "+":
            right_lines.add(cursor)
            cursor += 1
        elif prefix == " ":
            right_lines.add(cursor)
            cursor += 1
        elif prefix == "-":
            continue
        elif prefix == "\\":
            # "\ No newline at end of file" 같은 메타; cursor 이동 없음
            continue
        else:
            # 예상치 못한 접두사는 방어적으로 스킵
            continue

    return frozenset(right_lines)
