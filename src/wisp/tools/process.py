"""Compatibility alias for :mod:`wisp.tools.shell.process`."""

from sys import modules

from wisp.tools.shell import process as _implementation

modules[__name__] = _implementation
