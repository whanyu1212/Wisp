"""Codemode presentation across the in-process SDK boundary."""

from __future__ import annotations

from pathlib import Path

import anyio

from wisp.config.runtime import WispConfig
from wisp.events import RpcCodemodeReported, RpcCommandFinished
from wisp.sdk import InProcessWisp


def test_start_preserves_presentation(tmp_path: Path) -> None:
    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(
                provider="fake",
                session_dir=tmp_path,
                codemode=False,
                codemode_presentation="codemode_only",
            )
        )
        try:
            command_id = await controller.get_codemode()
            snapshot: RpcCodemodeReported | None = None
            with anyio.fail_after(10):
                async for event in controller.events():
                    if isinstance(event, RpcCodemodeReported) and event.command_id == command_id:
                        snapshot = event
                    if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                        assert event.ok, event.error
                        break
            assert snapshot is not None
            assert snapshot.codemode.presentation == "codemode_only"
            assert snapshot.codemode.effective_presentation == "direct_and_codemode"
            assert snapshot.codemode.setting == "never"
        finally:
            await controller.aclose()

    anyio.run(scenario)
