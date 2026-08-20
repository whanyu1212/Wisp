"""Verify the supported SDK surface from an installed Wisp wheel."""

from __future__ import annotations

import importlib
from pathlib import Path

import wisp
from wisp.config import WispConfig
from wisp.events import (
    EVENT_SCHEMA_VERSION,
    KnownWispEvent,
    RpcCommandFinished,
    ToolApprovalRequested,
    TrustRequested,
    WispEvent,
    wisp_event_from_dict,
    wisp_event_from_json,
)
from wisp.rpc import RpcCommand, RpcController, RpcTransport, TrustCommand
from wisp.sdk import InProcessOptions, InProcessWisp

_PUBLIC_EXPORT_MODULES = (
    "wisp.providers",
    "wisp.rpc",
    "wisp.runtime",
    "wisp.sdk",
    "wisp.sessions",
    "wisp.tools",
)


def main() -> None:
    """Fail if wheel metadata or supported imports are incomplete."""

    package_root = Path(wisp.__file__).resolve().parent
    source_root = Path(__file__).resolve().parents[1] / "src"
    if package_root.is_relative_to(source_root):
        raise RuntimeError(f"Expected an installed wheel, imported source tree at {package_root}")
    if not (package_root / "py.typed").is_file():
        raise RuntimeError("Installed wheel is missing wisp/py.typed")

    for module_name in _PUBLIC_EXPORT_MODULES:
        module = importlib.import_module(module_name)
        exports = getattr(module, "__all__", None)
        if not isinstance(exports, list) or not exports:
            raise RuntimeError(f"{module_name} must define a non-empty __all__")
        missing = [name for name in exports if not hasattr(module, name)]
        if missing:
            raise RuntimeError(f"{module_name} is missing declared exports: {missing}")

    required_symbols = (
        WispConfig,
        EVENT_SCHEMA_VERSION,
        KnownWispEvent,
        RpcCommandFinished,
        ToolApprovalRequested,
        TrustRequested,
        WispEvent,
        wisp_event_from_dict,
        wisp_event_from_json,
        RpcCommand,
        RpcController,
        RpcTransport,
        TrustCommand,
        InProcessOptions,
        InProcessWisp,
    )
    if any(symbol is None for symbol in required_symbols):
        raise RuntimeError("A supported SDK symbol was not importable")


if __name__ == "__main__":
    main()
