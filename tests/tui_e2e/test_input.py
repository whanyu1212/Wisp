"""Terminal input delivery through the real Python launcher and a pseudo-terminal."""

from __future__ import annotations

from pathlib import Path

import pytest

from tests.tui_e2e.support import rendered_without_spacing
from tests.tui_e2e.test_readiness import _launch, _write_user_settings

_KEY_AND_RESIZE_ROUNDS = 60


@pytest.mark.process
def test_keys_sent_with_resizes_all_reach_the_composer(tmp_path: Path) -> None:
    """A key that arrives in the same input wake-up as a resize is still handled.

    With crossterm's default mio input source, stdin and the SIGWINCH pipe are
    edge-triggered: when a wake-up reports both and the resize is handled first, the
    key stays unread until the next keypress. On Linux this stranded a key within
    about 50 rounds of typing while resizing. The resize repaints a full frame, so
    each round can check the whole composer.
    """
    home = tmp_path / "home"
    _write_user_settings(home, provider="fake")
    tui, _session_dir = _launch(tmp_path, home=home)
    try:
        tui.wait_ready()
        width = 101
        for typed in range(1, _KEY_AND_RESIZE_ROUNDS + 1):
            offset = len(tui.output)
            tui.send(b"a")
            width = 103 if width == 102 else 102
            tui.resize(width=width)
            composer = ("›" + "a" * typed).encode()
            tui.wait_until(
                lambda output, composer=composer, offset=offset: (
                    composer in rendered_without_spacing(output[offset:])
                ),
                timeout=5,
                failure=f"key {typed} sent with a resize did not reach the composer",
            )
        tui.quit()
    finally:
        tui.close()
