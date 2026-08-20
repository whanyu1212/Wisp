"""Keep the hand-written SDK reference aligned with its public controller surface."""

from __future__ import annotations

import inspect
import re
from dataclasses import fields
from pathlib import Path

from wisp.rpc import RpcController
from wisp.sdk import InProcessOptions

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_REFERENCE = (_REPOSITORY_ROOT / "site" / "reference" / "sdk.md").read_text(encoding="utf-8")


def test_sdk_reference_covers_every_controller_method() -> None:
    public_methods = {
        name
        for name, method in inspect.getmembers(RpcController, predicate=inspect.isfunction)
        if not name.startswith("_") and method.__qualname__.startswith("RpcController.")
    }

    missing = {
        name
        for name in public_methods
        if f"`{name}`" not in _REFERENCE and f"def {name}(" not in _REFERENCE
    }

    assert missing == set()


def test_sdk_reference_covers_every_in_process_option() -> None:
    documented_fields = set(re.findall(r"^\| `([a-z_]+)` \|", _REFERENCE, flags=re.MULTILINE))
    option_fields = {field.name for field in fields(InProcessOptions)}

    assert option_fields <= documented_fields
