"""Runtime, session, and provider fixtures shared by the RPC mode CLI tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence
from pathlib import Path

import anyio

from tests.support.cli import _last_user_prompt
from wisp.messages import Message
from wisp.providers.base import Provider, ToolCallResult, ToolSpec
from wisp.providers.catalog import (
    ModelCatalog,
    ModelCatalogProviderEntry,
    ModelRegistry,
    effective_catalog,
)
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
)
from wisp.runtime.api import ExtensionAPI, WispRuntime
from wisp.runtime.event_bus import EventBus
from wisp.runtime.registry import ProviderRegistry, ToolRegistry
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore

VALID_COMPACTION_SUMMARY = """## Goal
Preserve the active coding objective.
## Constraints & Preferences
Keep changes focused.
## Progress
### Done
Reviewed the prior turn.
### In Progress
Continue implementation.
### Blocked
None.
## Already Investigated
Reviewed the prior turn's transcript.
## Key Decisions
Use append-only replay.
## Next Steps
Run the tests.
## Critical Context
The session audit remains intact."""


async def runtime_with_provider(
    provider: Provider, *, context_window: int | None = None
) -> WispRuntime:
    providers = ProviderRegistry()
    tools = ToolRegistry()
    events = EventBus()
    api = ExtensionAPI(providers=providers, tools=tools, events=events)
    providers.register(provider)
    models = ModelRegistry(effective_catalog(home_dir=Path("/nonexistent-test-home")))
    if context_window is not None:
        assert provider.default_model is not None
        models = ModelRegistry(
            ModelCatalog(
                schema_version=2,
                providers=(
                    ModelCatalogProviderEntry(
                        name=provider.name,
                        display_name=provider.name,
                        default_model=provider.default_model,
                        docs_url="https://example.com",
                        models=(provider.default_model,),
                        context_windows={provider.default_model: context_window},
                    ),
                ),
            )
        )
    return WispRuntime(
        providers=providers,
        tools=tools,
        events=events,
        api=api,
        models=models,
    )


def create_two_turn_session(tmp_path: Path) -> JsonlSession:
    session = JsonlSessionStore(tmp_path).create()

    async def write() -> None:
        for prompt, answer in (("first", "answer one"), ("second", "answer two")):
            await session.append_message(Message(role="user", content=prompt))
            await session.append_message(
                Message(role="assistant", content=answer, finish_reason="stop")
            )

    anyio.run(write)
    return session


def is_compaction_request(messages: Sequence[Message]) -> bool:
    return any(
        message.role == "system"
        and "Create a concise, durable coding-session checkpoint" in message.content
        for message in messages
    )


class BlockingOperationProvider:
    name = "blocking-operation-test"
    default_model: str | None = "blocking-operation-test"

    def __init__(self, *, block_compaction: bool = False, block_prompt: bool = False) -> None:
        self.block_compaction = block_compaction
        self.block_prompt = block_prompt

    async def stream(
        self,
        messages: Sequence[Message],
        *,
        model: str | None = None,
        tools: Sequence[ToolSpec] = (),
        tool_results: Sequence[ToolCallResult] = (),
        previous_response_id: str | None = None,
        effort: str | None = None,
    ) -> AsyncIterator[ProviderEvent]:
        compacting = is_compaction_request(messages)
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        if (compacting and self.block_compaction) or (not compacting and self.block_prompt):
            await anyio.sleep_forever()
        yield ProviderResponseCompleted(
            content=VALID_COMPACTION_SUMMARY
            if compacting
            else f"done {_last_user_prompt(messages)}"
        )
