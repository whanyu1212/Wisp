"""Compatibility alias for :mod:`wisp.tools.shell.process`."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wisp.tools.shell.process import (
        ProcessResult as ProcessResult,
    )
    from wisp.tools.shell.process import (
        _kill_process_tree as _kill_process_tree,
    )
    from wisp.tools.shell.process import (
        _run_exec_limited_stdout as _run_exec_limited_stdout,
    )
else:
    from sys import modules

    from wisp.tools.shell import process as _implementation

    modules[__name__] = _implementation
