from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import anyio
import pytest
from pytest import MonkeyPatch
from typer.testing import CliRunner

import wisp.cli.application as cli_module
import wisp.cli.native_tui.launch as tui_launch
import wisp.cli.rpc as rpc_module
import wisp.runtime.extensions as runtime_extensions
from wisp.cli.native_tui.launch import TuiOptions
from wisp.config.runtime import WispConfig
from wisp.providers.anthropic import AnthropicCacheTtl, AnthropicProvider
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.runtime.registry import UnknownProviderError
from wisp.tools.result import ToolError
from wisp.tools.shell.supervisor import ProcessSupervisor


class _ExpectedFailure(Exception):
    pass


class _RecordingRuntime:
    def __init__(self) -> None:
        self.close_count = 0

    async def aclose(self) -> None:
        self.close_count += 1


@pytest.mark.parametrize("output_mode", ["text", "json"])
def test_cli_renders_runtime_cleanup_failure(
    output_mode: str,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    class CleanupFailRuntime:
        async def aclose(self) -> None:
            raise ToolError("Failed to terminate process tree")

    async def fake_build(_config: WispConfig) -> CleanupFailRuntime:
        return CleanupFailRuntime()

    async def succeed(*_args: object, **_kwargs: object) -> None:
        pass

    monkeypatch.setattr(cli_module, "build_runtime_for_config", fake_build)
    monkeypatch.setattr(cli_module, "_run_print_with_runtime", succeed)

    result = CliRunner().invoke(
        cli_module.app,
        [
            "-p",
            "hello",
            "--mode",
            output_mode,
            "--session-dir",
            str(tmp_path),
        ],
        env={"WISP_PROVIDER": "fake", "WISP_TRUST": "1"},
    )

    assert result.exit_code == 1
    if output_mode == "json":
        assert result.stderr == ""
        record = json.loads(result.stdout)
        assert record["type"] == "error"
        assert record["message"] == "Failed to terminate process tree"
    else:
        assert "error: Failed to terminate process tree" in result.stderr


@pytest.mark.parametrize("frontend", ["print", "rpc"])
def test_cli_frontends_close_runtime_when_execution_fails(
    frontend: str,
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    runtime = _RecordingRuntime()
    config = WispConfig(provider="fake", session_dir=tmp_path)

    async def fake_build(_config: WispConfig, **_kwargs: object) -> Any:
        return runtime

    async def fail(*_args: object, **_kwargs: object) -> None:
        raise _ExpectedFailure

    if frontend == "print":
        monkeypatch.setattr(cli_module, "build_runtime_for_config", fake_build)
        monkeypatch.setattr(cli_module, "_run_print_with_runtime", fail)

        async def run() -> None:
            await cli_module._run_print("hello", config)

    else:

        async def accept_handshake() -> bool:
            return True

        monkeypatch.setattr(rpc_module, "build_runtime_for_config", fake_build)
        monkeypatch.setattr(rpc_module, "_negotiate_rpc_connection", accept_handshake)
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


def test_tui_preflight_validates_provider_without_constructing_it(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def run() -> tuple[WispRuntime, int]:
        runtime = await build_runtime()
        construction_count = 0

        def factory() -> Any:
            nonlocal construction_count
            construction_count += 1
            raise AssertionError("preflight constructed the provider")

        runtime.providers.register_factory("deferred-test", factory)

        async def fake_build_runtime(**_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(tui_launch, "build_runtime", fake_build_runtime)
        await tui_launch._preflight_tui_options(
            TuiOptions(
                config=WispConfig(provider="deferred-test", session_dir=tmp_path),
            )
        )
        return runtime, construction_count

    runtime, construction_count = anyio.run(run)

    assert construction_count == 0

    async def start_after_close() -> None:
        await runtime.process_supervisor.start("true", cwd=tmp_path, timeout=1)

    with pytest.raises(RuntimeError, match="ProcessSupervisor is closed"):
        anyio.run(start_after_close)


def test_tui_preflight_rejects_unknown_provider_and_closes_runtime(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    async def run() -> WispRuntime:
        runtime = await build_runtime()

        async def fake_build_runtime(**_kwargs: object) -> Any:
            return runtime

        monkeypatch.setattr(tui_launch, "build_runtime", fake_build_runtime)
        with pytest.raises(UnknownProviderError, match="Unknown provider: missing"):
            await tui_launch._preflight_tui_options(
                TuiOptions(config=WispConfig(provider="missing", session_dir=tmp_path))
            )
        return runtime

    runtime = anyio.run(run)

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


@pytest.mark.parametrize("cache_ttl", ["5m", "1h"])
def test_build_runtime_passes_anthropic_cache_ttl_to_provider(
    tmp_path: Path, cache_ttl: AnthropicCacheTtl
) -> None:
    async def run() -> AnthropicCacheTtl:
        runtime = await build_runtime(
            auth_path=tmp_path / "auth.json", anthropic_cache_ttl=cache_ttl
        )
        try:
            provider = runtime.providers.get("anthropic")
            assert isinstance(provider, AnthropicProvider)
            return provider._cache_ttl  # noqa: SLF001
        finally:
            await runtime.aclose()

    assert anyio.run(run) == cache_ttl


def test_build_runtime_defaults_anthropic_cache_ttl_to_five_minutes(tmp_path: Path) -> None:
    async def run() -> AnthropicCacheTtl:
        runtime = await build_runtime(auth_path=tmp_path / "auth.json")
        try:
            provider = runtime.providers.get("anthropic")
            assert isinstance(provider, AnthropicProvider)
            return provider._cache_ttl  # noqa: SLF001
        finally:
            await runtime.aclose()

    assert anyio.run(run) == "5m"


def test_rpc_backend_builds_runtimes_with_one_hour_anthropic_cache(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """RPC sessions stay open between prompts, so they use the 1-hour lifetime.

    The host reuses the same builder for trust-time and configuration rebuilds.
    """

    config = WispConfig(session_dir=tmp_path / "sessions", auth_path=tmp_path / "auth.json")
    build_kwargs: list[dict[str, object]] = []
    host_builders: list[object] = []

    async def record_build(_config: WispConfig, **kwargs: object) -> Any:
        build_kwargs.append(kwargs)
        return object()

    async def stop_create(*_args: object, **kwargs: object) -> Any:
        host_builders.append(kwargs["runtime_builder"])
        raise RuntimeError("stop after host creation")

    monkeypatch.setattr(rpc_module, "build_runtime_for_config", record_build)
    monkeypatch.setattr(rpc_module.RpcHost, "create", stop_create)

    anyio.run(rpc_module._build_rpc_runtime, config)
    with pytest.raises(RuntimeError, match="stop after host creation"):
        anyio.run(rpc_module._run_rpc_with_runtime, config, cast(WispRuntime, object()))

    assert build_kwargs == [{"anthropic_cache_ttl": "1h"}]
    assert host_builders == [rpc_module._build_rpc_runtime]


def test_print_mode_builds_runtime_with_default_anthropic_cache(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    """A one-off print run gains nothing from the 1-hour lifetime's higher write price."""

    config = WispConfig(session_dir=tmp_path / "sessions", auth_path=tmp_path / "auth.json")
    build_kwargs: list[dict[str, object]] = []

    async def record_build(_config: WispConfig, **kwargs: object) -> Any:
        build_kwargs.append(kwargs)
        raise RuntimeError("stop after runtime build")

    monkeypatch.setattr(cli_module, "build_runtime_for_config", record_build)

    with pytest.raises(RuntimeError, match="stop after runtime build"):
        anyio.run(cli_module._run_print, "hello", config)

    assert build_kwargs == [{}]
