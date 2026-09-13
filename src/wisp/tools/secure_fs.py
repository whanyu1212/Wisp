"""Compatibility alias for :mod:`wisp.tools.files.secure_fs`."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from wisp.tools.files.secure_fs import (
        OpenParent as OpenParent,
    )
    from wisp.tools.files.secure_fs import (
        SecureToolPath as SecureToolPath,
    )
    from wisp.tools.files.secure_fs import (
        file_version as file_version,
    )
    from wisp.tools.files.secure_fs import (
        open_directory as open_directory,
    )
    from wisp.tools.files.secure_fs import (
        open_file as open_file,
    )
    from wisp.tools.files.secure_fs import (
        open_parent as open_parent,
    )
    from wisp.tools.files.secure_fs import (
        open_windows_parent as open_windows_parent,
    )
    from wisp.tools.files.secure_fs import (
        secure_tool_path as secure_tool_path,
    )
    from wisp.tools.files.secure_fs import (
        stat_leaf as stat_leaf,
    )
else:
    from sys import modules

    from wisp.tools.files import secure_fs as _implementation

    modules[__name__] = _implementation
