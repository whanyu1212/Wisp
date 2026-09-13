"""Compatibility alias for :mod:`wisp.tools.files.operations`."""

from sys import modules

from wisp.tools.files import operations as _implementation

modules[__name__] = _implementation
