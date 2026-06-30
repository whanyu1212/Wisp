"""Default Wisp capabilities registered through the extension API."""

from __future__ import annotations

from wisp.providers.fake import FakeProvider
from wisp.runtime.api import ExtensionAPI


def activate(api: ExtensionAPI) -> None:
    """Register Wisp's baseline capabilities."""

    api.register_provider(FakeProvider())
