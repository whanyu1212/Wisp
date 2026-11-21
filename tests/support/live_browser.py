"""Launch real Chrome in live browser tests without touching the login Keychain."""

from __future__ import annotations

import os
import shlex
import sys
from pathlib import Path

from wisp.coding.browser_live import _chrome_binary


def keychain_safe_chrome_path(directory: Path, path: str) -> str:
    """Return a ``PATH`` whose Chrome cannot prompt for or modify the Keychain.

    Args:
        directory: Private directory for the launcher script.
        path: The ``PATH`` to extend.

    Returns:
        ``path`` unchanged off macOS. On macOS, ``path`` behind a ``google-chrome``
        launcher that runs the installed browser with a mock keychain.
    """
    if sys.platform != "darwin":
        return path
    launcher = directory / "google-chrome"
    command = shlex.join([_chrome_binary(), "--use-mock-keychain"])
    launcher.write_text(f'#!/bin/sh\nexec {command} "$@"\n')
    launcher.chmod(0o700)
    return str(directory) + os.pathsep + path
