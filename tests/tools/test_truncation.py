from __future__ import annotations

import pytest

from wisp.tools.truncation import TruncatedText, truncate_text_middle, truncate_text_tail


def test_tail_truncation_with_marker_only_budget_stays_bounded() -> None:
    result = truncate_text_tail("diagnostic tail", max_bytes=12, max_lines=10)

    assert result.text == "[truncated]"
    assert len(result.text.encode("utf-8")) <= 12
    assert result.truncated is True


def _numbered(count: int) -> str:
    return "".join(f"line {number}\n" for number in range(1, count + 1))


def _middle(text: str, **budgets: int) -> TruncatedText:
    limits = {"head_bytes": 1000, "head_lines": 2, "tail_bytes": 1000, "tail_lines": 3}
    return truncate_text_middle(text, text, total_bytes=len(text.encode()), **(limits | budgets))


def test_middle_keeps_output_that_fits() -> None:
    text = _numbered(5)

    assert _middle(text) == TruncatedText(text=text, truncated=False)


def test_middle_drops_lines_between_ends() -> None:
    result = _middle(_numbered(6))

    assert result.truncated is True
    assert result.text == (
        "line 1\nline 2\n[truncated: 7 bytes omitted after the first 2 lines]\n"
        "line 4\nline 5\nline 6\n"
    )


@pytest.mark.parametrize("cut", range(1, 12))
def test_middle_never_splits_characters(cut: int) -> None:
    text = "é" * 40 + "\n" + "語" * 40

    result = truncate_text_middle(
        text,
        text,
        total_bytes=len(text.encode()),
        head_bytes=cut,
        head_lines=5,
        tail_bytes=cut,
        tail_lines=5,
    )

    head, _, rest = result.text.partition("[truncated: ")
    tail = rest.partition("]\n")[2]
    assert "�" not in result.text and rest
    assert len(head.rstrip("\n").encode()) <= cut and len(tail.encode()) <= cut
    assert set(head.rstrip("\n")) <= {"é"} and set(tail) <= {"語"}


def test_middle_marks_prefix_and_suffix() -> None:
    # Capture kept only the two ends, so even short text is a preview.
    result = truncate_text_middle(
        "start\n",
        "end\n",
        total_bytes=5000,
        head_bytes=100,
        head_lines=5,
        tail_bytes=100,
        tail_lines=5,
    )

    assert result == TruncatedText(
        text="start\n[truncated: 4,990 bytes omitted after the first 1 lines]\nend\n",
        truncated=True,
    )


def test_middle_keeps_whole_lines_within_bytes() -> None:
    text = "".join(f"{'x' * 30} {number}\n" for number in range(100))

    result = _middle(text, head_bytes=70, head_lines=50, tail_bytes=70, tail_lines=50)

    head, _, tail = result.text.partition("]\n")
    assert head.splitlines()[:-1] == [f"{'x' * 30} 0", f"{'x' * 30} 1"]
    assert tail.splitlines() == [f"{'x' * 30} 98", f"{'x' * 30} 99"]
