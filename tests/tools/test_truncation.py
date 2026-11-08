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
    return truncate_text_middle(
        text, text, whole=True, total_bytes=len(text.encode()), **(limits | budgets)
    )


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
        whole=True,
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
        whole=False,
        total_bytes=5000,
        start_label="stdout",
        head_bytes=100,
        head_lines=5,
        tail_bytes=100,
        tail_lines=5,
    )

    assert result == TruncatedText(
        text="start\n[truncated: 4,990 bytes omitted after the first 1 lines of stdout]\nend\n",
        truncated=True,
    )


def test_middle_keeps_whole_lines_within_bytes() -> None:
    text = "".join(f"{'x' * 30} {number}\n" for number in range(100))

    result = _middle(text, head_bytes=70, head_lines=50, tail_bytes=70, tail_lines=50)

    head, _, tail = result.text.partition("]\n")
    assert head.splitlines()[:-1] == [f"{'x' * 30} 0", f"{'x' * 30} 1"]
    assert tail.splitlines() == [f"{'x' * 30} 98", f"{'x' * 30} 99"]


def test_middle_keeps_lines_cut_on_boundaries() -> None:
    # Each cut lands exactly on a line edge, so no whole line may be lost.
    result = _middle("aaa\nbbb\nccc\nddd\neee\nfff", head_bytes=7, tail_bytes=7, tail_lines=5)

    assert result.text == (
        "aaa\nbbb\n[truncated: 9 bytes omitted after the first 2 lines]\neee\nfff"
    )


def test_middle_counts_newline_lines_only() -> None:
    # A bare carriage return redraws a progress line; it does not end one.
    progress = "".join(f"\r{percent}%" for percent in range(100)) + "\ndone\n"

    result = _middle(progress * 3, head_lines=1, tail_lines=1, head_bytes=5000, tail_bytes=5000)

    assert "omitted after the first 1 lines]" in result.text
    assert result.text.endswith("[truncated: " + result.text.split("[truncated: ")[1])
    assert result.text.splitlines()[-1] == "done"
