"""Compatibility alias for :mod:`wisp.tools.files.operations`."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wisp.tools.files.operations import (
        CreateOnlyWriteReceipt as CreateOnlyWriteReceipt,
    )
    from wisp.tools.files.operations import (
        EditTool as EditTool,
    )
    from wisp.tools.files.operations import (
        ReadTool as ReadTool,
    )
    from wisp.tools.files.operations import (
        WriteTool as WriteTool,
    )
else:
    from sys import modules

    from wisp.tools.files import operations as _implementation

    modules[__name__] = _implementation
