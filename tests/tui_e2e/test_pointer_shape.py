"""PTY E2E test for the resize pointer over the docked side panel's border."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tui_e2e.support import context_stats_ready, screen_shows
from tests.tui_e2e.test_browser_takeover import _start_wide_tui

_SET = b"\x1b]22;ew-resize\x1b\\"
_RESET = b"\x1b]22;\x1b\\"
_WIDTH = 160
_ROW = 12
# The variables the pointer-shape gate reads, cleared so the developer's own terminal
# cannot turn it on or off.
_NEUTRAL_TERMINAL = {
    "TERM": "xterm-256color",
    "TERM_PROGRAM": "",
    "KITTY_WINDOW_ID": "",
    "TMUX": "",
}


def _border_column(width: int) -> int:
    """Return the 1-based column of the docked panel's left border.

    Mirrors the panel's automatic width (``side_panel::auto_width``): 44 columns plus
    half of what is left beyond the conversation's 104, at most 52.
    """
    panel = min(44 + max(width - 44 - 104, 0) // 2, 52)
    return width - panel + 1


def _hover(column: int) -> bytes:
    return f"\x1b[<35;{column};{_ROW}M".encode()


@pytest.mark.process
@pytest.mark.parametrize("kitty", [True, False], ids=["kitty", "unsupported"])
def test_border_hover_pointer(tmp_path: Path, kitty: bool) -> None:
    """Kitty gets the resize pointer on hover and a reset on leave and exit; others get none."""
    home = tmp_path / "home"
    (home / ".wisp").mkdir(parents=True)
    environment = {**_NEUTRAL_TERMINAL, "KITTY_WINDOW_ID": "1" if kitty else ""}
    tui = _start_wide_tui(tmp_path, home, width=_WIDTH, environment=environment)
    try:
        tui.wait_until(
            lambda output: context_stats_ready(output) and screen_shows(output, "─ fake "),
            timeout=25,
            failure="wide TUI did not finish startup",
        )
        border = _border_column(_WIDTH)
        if kitty:
            tui.send(_hover(border))
            tui.wait_until(
                lambda output: _SET in output,
                failure="hovering the border did not set the resize pointer",
            )
            offset = len(tui.output)
            tui.send(_hover(border - 10))
            tui.wait_until(
                lambda output: _RESET in output[offset:],
                failure="leaving the border did not reset the pointer",
            )
            offset = len(tui.output)
            tui.send(_hover(border + 1))
            tui.wait_until(
                lambda output: _SET in output[offset:],
                failure="hovering the border again did not set the resize pointer",
            )
        else:
            tui.send(_hover(border))
        tui.quit()
        output = bytes(tui.output)
        if kitty:
            assert output.rfind(_RESET) > output.rfind(_SET)
        else:
            assert b"\x1b]22;" not in output
    finally:
        tui.close()
