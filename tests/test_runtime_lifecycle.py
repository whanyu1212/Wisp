from __future__ import annotations

from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from pytest import MonkeyPatch

import wisp.cli as cli_module
import wisp.cli.rpc as rpc_module
import wisp.runtime.extensions as runtime_extensions
import wisp.tui.launch as tui_launch
from wisp.config import WispConfig
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.tools.process_manager import ProcessSupervisor
from wisp.tui.launch import TuiOptions


class _ExpectedFailure(Exception):
    pass


class _RecordingRuntime:
    def __init__(self) -> None:
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


@pytest.mark.parametrize("frontend", ["print", "rpc"])
def test_cli_frontends_close_runtime_when_execution_fails(
    frontend: str,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    config = WispConfig(provider="fake", session_dir=tmp_path)

    async def fake_build(_config: WispConfig) -> Any:
        return runtime

    async def fail(*_args: object, **_kwargs: object) -> None:
        raise _ExpectedFailure

    if frontend == "print":
        monkeypatch.setattr(cli_module, "_build_runtime_for_config", fake_build)
        monkeypatch.setattr(cli_module, "_run_print_with_runtime", fail)

        async def run() -> None:
            await cli_module._run_print("hello", config)

    else:
        monkeypatch.setattr(rpc_module, "_build_runtime_for_config", fake_build)
        monkeypatch.setattr(rpc_module, "_run_rpc_with_runtime", fail)

        async def run() -> None:
            await rpc_module._run_rpc(config)

    with pytest.raises(_ExpectedFailure):
        anyio.run(run)
    assert runtime.close_count == 1


@pytest.mark.parametrize("allowed_tools", [(), ("missing",)])
def test_tui_preflight_closes_temporary_runtime(
    allowed_tools: tuple[str, ...],
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def run() -> tuple[WispRuntime, BaseException | None]:
        runtime = await build_runtime()

        async def fake_build_runtime(**_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(tui_launch, "build_runtime", fake_build_runtime)
        failure: BaseException | None = None
        try:
            await tui_launch._preflight_tui_options(
                TuiOptions(
                    config=WispConfig(provider="fake", session_dir=tmp_path),
                    allowed_tools=allowed_tools,
                )
            )
        except BaseException as exc:
            failure = exc
        return runtime, failure

    runtime, failure = anyio.run(run)

    if allowed_tools:
        assert failure is not None
    else:
        assert failure is None

    async def start_after_close() -> None:
        await runtime.process_supervisor.start("true", cwd=tmp_path, timeout=1)

    with pytest.raises(RuntimeError, match="ProcessSupervisor is closed"):
        anyio.run(start_after_close)


def test_runtime_build_failure_closes_allocated_supervisor(
    monkeypatch: MonkeyPatch,
) -> None:
    captured: list[object] = []

    async def fail_activation(
        _api: object,
        **kwargs: object,
    ) -> None:
        captured.append(kwargs["process_supervisor"])
        raise _ExpectedFailure

    monkeypatch.setattr(runtime_extensions, "activate_builtin_extensions", fail_activation)

    with pytest.raises(_ExpectedFailure):
        anyio.run(runtime_extensions.build_runtime)
    assert len(captured) == 1

    async def start_after_close() -> None:
        supervisor = cast(ProcessSupervisor, captured[0])
        await supervisor.start("true", cwd=Path.cwd(), timeout=1)

    with pytest.raises(RuntimeError, match="ProcessSupervisor is closed"):
        anyio.run(start_after_close)
