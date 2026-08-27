# ruff: noqa: F401

from __future__ import annotations

import io
import json
import os
import sys
from collections.abc import AsyncIterator, Sequence
from pathlib import Path
from queue import Queue

import anyio
from pytest import MonkeyPatch
from typer.testing import CliRunner as RawCliRunner

from wisp import __version__
from wisp import cli as cli_module
from wisp.agent.messages import Message
from wisp.cli import _print_mode_tool_approval_policy, _print_mode_tool_registry, app
from wisp.coding import CodingSession
from wisp.events import (
    EVENT_SCHEMA_VERSION,
    ToolApprovalRequested,
    ToolApprovalResolved,
    ToolResultReady,
)
from wisp.providers.base import ToolCall, ToolCallResult, ToolSpec
from wisp.providers.catalog import ModelRegistry, effective_catalog
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseFailed,
    ProviderResponseStarted,
    ProviderTextDelta,
    ProviderToolCallCompleted,
)
from wisp.rpc import host as rpc_host_module
from wisp.rpc.protocol import (
    LIVE_RPC_PROTOCOL_VERSION,
    RpcHandshakeAccepted,
    RpcHandshakeRequest,
    RpcTransportLimits,
    negotiate_rpc_handshake,
)
from wisp.runtime.api import ExtensionAPI, WispRuntime
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ProviderRegistry, ToolRegistry
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.approval import ToolApprovalPolicy
from wisp.tools.base import ToolArguments, ToolInputSchema
from wisp.tools.builtin import BashTool, EditTool, FindTool, GrepTool, LsTool, ReadTool, WriteTool
from wisp.tools.context import ToolContext
from wisp.tools.result import ToolResult

_RPC_TEST_HANDSHAKE = (
    RpcHandshakeRequest(
        frontend_name="wisp-python-tests",
        frontend_version=__version__,
        min_protocol_version=LIVE_RPC_PROTOCOL_VERSION,
        max_protocol_version=LIVE_RPC_PROTOCOL_VERSION,
        min_event_schema_version=EVENT_SCHEMA_VERSION,
        max_event_schema_version=EVENT_SCHEMA_VERSION,
        supported_capabilities=(),
        required_capabilities=(),
    ).model_dump_json()
    + "\n"
)


class CliRunner(RawCliRunner):
    def invoke(self, cli, args=None, input=None, env=None, **extra):  # type: ignore[no-untyped-def,override]
        selected_args = list(args or ())
        if "--mode" in selected_args and "rpc" in selected_args and input is not None:
            input = _RPC_TEST_HANDSHAKE + input
        return super().invoke(cli, args=args, input=input, env=env, **extra)


async def _read_rpc_test_handshake(
    _stdin: object,
    *,
    backend_package_version: str,
    supported_capabilities: Sequence[str],
    limits: RpcTransportLimits,
    write_response: object,
) -> RpcHandshakeAccepted:
    del write_response
    response = negotiate_rpc_handshake(
        RpcHandshakeRequest.model_validate_json(_RPC_TEST_HANDSHAKE),
        backend_package_version=backend_package_version,
        supported_capabilities=supported_capabilities,
        limits=limits,
    )
    assert isinstance(response, RpcHandshakeAccepted)
    return response


class MixedTextToolProvider:
    name = "mixed-tool-test"
    default_model: str | None = "mixed-tool-test"

    async def stream(
        self,
        messages: Sequence[object],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        if not tool_results:
            yield ProviderTextDelta(delta="prefix")
            tool_call = ToolCall(
                call_id="call-1",
                name="danger",
                arguments={"path": "file.txt"},
                response_id="response-1",
            )
            yield ProviderToolCallCompleted(tool_call=tool_call)
            yield ProviderResponseCompleted(
                content="prefix",
                tool_calls=(tool_call,),
                response_id="response-1",
                finish_reason="tool_calls",
            )
            return
        yield ProviderTextDelta(delta="suffix")
        yield ProviderResponseCompleted(content="suffix")


class CompletionOnlyProvider:
    name = "completion-only-test"
    default_model: str | None = "completion-only-test"

    async def stream(
        self,
        messages: Sequence[object],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        yield ProviderResponseCompleted(content="completion-only response")


class CancellableProvider:
    name = "cancellable-test"
    default_model: str | None = "cancellable-test"

    async def stream(
        self,
        messages: Sequence[object],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        user_prompts = _user_prompts(messages)
        prompt = user_prompts[-1] if user_prompts else ""
        if prompt == "slow":
            yield ProviderTextDelta(delta="working")
            await anyio.sleep_forever()
        if "slow" in user_prompts[:-1]:
            yield ProviderTextDelta(delta="leaked slow")
            yield ProviderResponseCompleted(content="leaked slow")
            return
        content = f"done {prompt}"
        yield ProviderTextDelta(delta=content)
        yield ProviderResponseCompleted(content=content)


class InBandFailingProvider:
    name = "in-band-failing-test"
    default_model: str | None = "in-band-failing-test"

    async def stream(
        self,
        messages: Sequence[object],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        del messages, tools, tool_results, previous_response_id, effort
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        yield ProviderResponseFailed(message="provider failed")


class FailingProvider:
    name = "failing-test"
    default_model: str | None = "failing-test"

    async def stream(
        self,
        messages: Sequence[object],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        user_prompts = _user_prompts(messages)
        prompt = user_prompts[-1] if user_prompts else ""
        if prompt == "fail":
            raise RuntimeError("provider failed")
        if "fail" in user_prompts[:-1]:
            yield ProviderTextDelta(delta="saw failed history")
            yield ProviderResponseCompleted(content="saw failed history")
            return
        content = f"done {prompt}"
        yield ProviderTextDelta(delta=content)
        yield ProviderResponseCompleted(content=content)


class ToolCallingProvider:
    name = "tool-test"
    default_model: str | None = "tool-test"

    async def stream(
        self,
        messages: Sequence[object],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        if not tool_results:
            tool_call = ToolCall(
                call_id="call-1",
                name="danger",
                arguments={"path": "file.txt"},
                response_id="response-1",
            )
            yield ProviderToolCallCompleted(tool_call=tool_call)
            yield ProviderResponseCompleted(
                content="",
                tool_calls=(tool_call,),
                response_id="response-1",
                finish_reason="tool_calls",
            )
            return
        yield ProviderTextDelta(delta="done")
        yield ProviderResponseCompleted(content="done")


class DangerTool:
    name = "danger"
    safety = "mutating"
    description = "Pretend to mutate a file."
    input_schema: ToolInputSchema = {
        "type": "object",
        "properties": {"path": {"type": "string"}},
        "required": ["path"],
    }

    async def run(self, arguments: ToolArguments, context: ToolContext) -> ToolResult:
        return ToolResult(text=f"changed {arguments['path']}")


def _test_model_registry() -> ModelRegistry:
    """Return a `ModelRegistry` for test runtimes, built from the packaged catalog.

    Uses the built-in catalog only (no user overlay) so tests stay hermetic
    regardless of what, if anything, is on disk at ``~/.wisp/catalog.toml``.
    """

    return ModelRegistry(effective_catalog(home_dir=Path("/nonexistent-test-home")))


async def build_tool_runtime() -> WispRuntime:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    providers.register(ToolCallingProvider())
    tools.register(DangerTool())
    return WispRuntime(
        providers=providers, tools=tools, events=events, api=api, models=_test_model_registry()
    )


async def build_cancellable_runtime() -> WispRuntime:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    providers.register(CancellableProvider())
    return WispRuntime(
        providers=providers, tools=tools, events=events, api=api, models=_test_model_registry()
    )


async def build_completion_only_runtime() -> WispRuntime:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    providers.register(CompletionOnlyProvider())
    return WispRuntime(
        providers=providers, tools=tools, events=events, api=api, models=_test_model_registry()
    )


async def build_in_band_failing_runtime() -> WispRuntime:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    providers.register(InBandFailingProvider())
    return WispRuntime(
        providers=providers, tools=tools, events=events, api=api, models=_test_model_registry()
    )


async def build_failing_runtime() -> WispRuntime:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    providers.register(FailingProvider())
    return WispRuntime(
        providers=providers, tools=tools, events=events, api=api, models=_test_model_registry()
    )


async def build_mixed_tool_runtime() -> WispRuntime:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    providers.register(MixedTextToolProvider())
    tools.register(DangerTool())
    return WispRuntime(
        providers=providers, tools=tools, events=events, api=api, models=_test_model_registry()
    )


def _jsonl_records(output: str) -> list[dict[str, object]]:
    return [
        record
        for line in output.splitlines()
        if (record := json.loads(line)).get("type") != "rpc.handshake.accepted"
    ]


def _last_user_prompt(messages: Sequence[object]) -> str:
    user_prompts = _user_prompts(messages)
    return user_prompts[-1] if user_prompts else ""


def _user_prompts(messages: Sequence[object]) -> list[str]:
    return [
        str(getattr(message, "content", ""))
        for message in messages
        if getattr(message, "role", None) == "user"
    ]


__all__ = [
    "AsyncIterator",
    "BashTool",
    "CancellableProvider",
    "CliRunner",
    "CompletionOnlyProvider",
    "CodingSession",
    "DangerTool",
    "EditTool",
    "EventBus",
    "ExtensionAPI",
    "FailingProvider",
    "FindTool",
    "GrepTool",
    "JsonlSession",
    "JsonlSessionStore",
    "LsTool",
    "Message",
    "MixedTextToolProvider",
    "MonkeyPatch",
    "Path",
    "ProviderRegistry",
    "ProviderEvent",
    "Queue",
    "RawCliRunner",
    "ReadTool",
    "Sequence",
    "ToolApprovalPolicy",
    "ToolApprovalRequested",
    "ToolApprovalResolved",
    "ToolArguments",
    "ToolCall",
    "ToolCallResult",
    "ToolCallingProvider",
    "ToolContext",
    "ToolInputSchema",
    "ToolRegistry",
    "ToolResult",
    "ToolResultReady",
    "ToolSpec",
    "WispRuntime",
    "WriteTool",
    "_jsonl_records",
    "_last_user_prompt",
    "_print_mode_tool_approval_policy",
    "_print_mode_tool_registry",
    "_read_rpc_test_handshake",
    "_user_prompts",
    "anyio",
    "app",
    "build_cancellable_runtime",
    "build_completion_only_runtime",
    "build_failing_runtime",
    "build_in_band_failing_runtime",
    "build_mixed_tool_runtime",
    "build_tool_runtime",
    "cli_module",
    "io",
    "json",
    "os",
    "rpc_host_module",
    "sys",
]
