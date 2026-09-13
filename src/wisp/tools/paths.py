"""Compatibility alias for :mod:`wisp.tools.files.paths`."""

from sys import modules

from wisp.tools.files import paths as _implementation

modules[__name__] = _implementation
