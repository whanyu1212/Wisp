"""Compatibility alias for :mod:`wisp.tools.files.paths`."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wisp.tools.files.paths import (
        display_tool_path as display_tool_path,
    )
    from wisp.tools.files.paths import (
        is_protected_path as is_protected_path,
    )
    from wisp.tools.files.paths import (
        resolve_tool_path as resolve_tool_path,
    )
else:
    from sys import modules

    from wisp.tools.files import paths as _implementation

    modules[__name__] = _implementation
