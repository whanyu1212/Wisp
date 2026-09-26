"""Providers, summaries, and replay builders shared by the compaction tests."""

from __future__ import annotations

from collections.abc import AsyncIterator, Sequence

import anyio

from wisp.agent.messages import Message
from wisp.providers.base import ToolCallResult, ToolSpec
from wisp.providers.catalog import ModelCatalog, ModelCatalogProviderEntry, ModelRegistry
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
)
from wisp.providers.fake import ScriptedProvider
from wisp.sessions.jsonl import JsonlSession
from wisp.sessions.replay import (
    SessionContextRow,
    SessionReplay,
)


class CacheAwareScriptedProvider(ScriptedProvider):
    """Scripted provider opting into the prompt-cache-key capability."""

    supports_prompt_cache_key = True


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


def context_row(entry_id: str, message: Message) -> SessionContextRow:
    return SessionContextRow(entry_id=entry_id, message=message)


def complete_turn(prefix: str) -> tuple[SessionContextRow, SessionContextRow]:
    return (
        context_row(f"{prefix}-user", Message(role="user", content=f"question {prefix}")),
        context_row(
            f"{prefix}-assistant",
            Message(role="assistant", content=f"answer {prefix}", finish_reason="stop"),
        ),
    )


def two_turn_replay() -> SessionReplay:
    return SessionReplay(rows=(*complete_turn("one"), *complete_turn("two")))


def build_model_registry(
    *,
    context_window: int = 100,
    auto_compact_token_limit: int | None = None,
) -> ModelRegistry:
    return ModelRegistry(
        ModelCatalog(
            schema_version=2,
            providers=(
                ModelCatalogProviderEntry(
                    name="scripted",
                    display_name="Scripted",
                    default_model="model",
                    docs_url="https://example.com",
                    models=("model",),
                    context_windows={"model": context_window},
                    auto_compact_token_limits=(
                        {"model": auto_compact_token_limit}
                        if auto_compact_token_limit is not None
                        else {}
                    ),
                ),
            ),
        )
    )


class GatedPreflightSummaryProvider:
    name = "scripted"
    default_model: str | None = "model"

    def __init__(self, summary_started: anyio.Event, release_summary: anyio.Event) -> None:
        self.summary_started = summary_started
        self.release_summary = release_summary
        self.calls: list[tuple[Message, ...]] = []

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
        del tools, tool_results, previous_response_id, effort
        self.calls.append(tuple(messages))
        yield ProviderResponseStarted(model=model or self.default_model or self.name)
        if len(self.calls) == 1:
            self.summary_started.set()
            await self.release_summary.wait()
        yield ProviderResponseCompleted(content=VALID_COMPACTION_SUMMARY)


async def _append_turn(session: JsonlSession, prefix: str) -> tuple[str, str]:
    user = await session.append_message(Message(role="user", content=f"question {prefix}"))
    assistant = await session.append_message(
        Message(role="assistant", content=f"answer {prefix}", finish_reason="stop")
    )
    return user.id, assistant.id
