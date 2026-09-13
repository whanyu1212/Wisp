"""Compatibility alias for :mod:`wisp.tools.shell.supervisor`."""

from sys import modules

from wisp.tools.shell import supervisor as _implementation

modules[__name__] = _implementation
