"""Terminal input delivery through the real Python launcher and a pseudo-terminal."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tui_e2e.support import frame_cursor, rendered_without_spacing
from tests.tui_e2e.test_readiness import _launch, _write_user_settings

_KEY_AND_RESIZE_ROUNDS = 60


@pytest.mark.process
def test_keys_sent_with_resizes_all_reach_the_composer(tmp_path: Path) -> None:
    """A key that arrives in the same input wake-up as a resize is still handled.

    With crossterm's default mio input source, stdin and the SIGWINCH pipe are
    edge-triggered: when a wake-up reports both and the resize is handled first, the
    key stays unread until the next keypress. On Linux this stranded a key within
    about 50 rounds of typing while resizing.

    Each round accepts either proof that the key arrived: a full frame showing the
    whole composer, or a frame leaving the cursor one cell further along. The second
    covers a resize that repaints before the key is read, after which the key paints
    alone in a small frame.
    """
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    tui, _session_dir = _launch(tmp_path, home=home)
    try:
        tui.wait_ready()
        empty_composer = tui.wait_until(
            lambda output: frame_cursor(output) is not None,
            timeout=5,
            failure="the empty composer's cursor did not render",
        )
        start_row, start_column = frame_cursor(empty_composer) or (0, 0)
        width = 101
        for typed in range(1, _KEY_AND_RESIZE_ROUNDS + 1):
            offset = len(tui.output)
            tui.send(b"a")
            width = 103 if width == 102 else 102
            tui.resize(width=width)
            composer = ("›" + "a" * typed).encode()
            cursor = (start_row, start_column + typed)
            tui.wait_until(
                lambda output, composer=composer, cursor=cursor, offset=offset: (
                    composer in rendered_without_spacing(output[offset:])
                    or frame_cursor(output) == cursor
                ),
                timeout=5,
                failure=f"key {typed} sent with a resize did not reach the composer",
            )
        tui.quit()
    finally:
        tui.close()
