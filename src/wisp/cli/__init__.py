"""Public CLI exports; load the application lazily so the entry point can secure its pipe."""

from typing import TYPE_CHECKING

from wisp.cli.entrypoint import application_export, main

if TYPE_CHECKING:
    from wisp.cli.application import ToolApprovalDecision, app

__getattr__ = application_export
__all__ = ["ToolApprovalDecision", "app", "main"]
