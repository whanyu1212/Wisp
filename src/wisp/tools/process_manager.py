"""Compatibility alias for :mod:`wisp.tools.shell.supervisor`."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wisp.tools.shell.supervisor import (
        DEFAULT_MAX_MANAGED_PROCESSES as DEFAULT_MAX_MANAGED_PROCESSES,
    )
    from wisp.tools.shell.supervisor import (
        DEFAULT_MAX_RETAINED_BYTES as DEFAULT_MAX_RETAINED_BYTES,
    )
    from wisp.tools.shell.supervisor import (
        DEFAULT_MAX_RETAINED_LINES as DEFAULT_MAX_RETAINED_LINES,
    )
    from wisp.tools.shell.supervisor import (
        POST_TERMINATION_DRAIN_TIMEOUT as POST_TERMINATION_DRAIN_TIMEOUT,
    )
    from wisp.tools.shell.supervisor import (
        PROCESS_TREE_CLEANUP_ERROR as PROCESS_TREE_CLEANUP_ERROR,
    )
    from wisp.tools.shell.supervisor import (
        ProcessState as ProcessState,
    )
    from wisp.tools.shell.supervisor import (
        ProcessSupervisor as ProcessSupervisor,
    )
    from wisp.tools.shell.supervisor import (
        ProcessUpdate as ProcessUpdate,
    )
else:
    from sys import modules

    from wisp.tools.shell import supervisor as _implementation

    modules[__name__] = _implementation
