"""Compatibility alias for :mod:`wisp.tools.files.secure_fs`."""

from sys import modules

from wisp.tools.files import secure_fs as _implementation

modules[__name__] = _implementation
