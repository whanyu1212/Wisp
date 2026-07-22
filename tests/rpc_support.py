from __future__ import annotations

from collections.abc import AsyncIterator
from dataclasses import dataclass, field
from pathlib import Path

from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream

from wisp.cli.rpc_configuration import _RpcConfigureOverrides
from wisp.cli.rpc_coordinator import RpcCoordinator, _RpcControlEvent, _RpcSessionState
from wisp.cli.rpc_execution import RpcCommandExecutor
from wisp.coding import CodingSession
from wisp.config import WispConfig
from wisp.events import WispEvent
from wisp.runtime.api import WispRuntime
from wisp.runtime.extensions import build_runtime
from wisp.sessions.jsonl import JsonlSessionStore


class RecordingEventWriter:
    def __init__(self) -> None:
        self.events: list[WispEvent] = []

    def __call__(self, event: WispEvent) -> None:
        self.events.append(event)

    async def render_events(self, stream: AsyncIterator[WispEvent]) -> None:
        async for event in stream:
            self.events.append(event)


class ApprovalResolver:
    def resolve_approval(self, **_kwargs: object) -> bool:
        return False


class TrustResolver:
    async def resolve(self) -> bool:
        return True

    def resolve_request(self, **_kwargs: object) -> bool:
        return False


@dataclass
class RpcExecutorFixture:
    runtime: WispRuntime
    sessions: JsonlSessionStore
    agent: CodingSession
    session_state: _RpcSessionState
    coordinator: RpcCoordinator
    writer: RecordingEventWriter = field(default_factory=RecordingEventWriter)
    approval_policy: ApprovalResolver = field(default_factory=ApprovalResolver)
    trust_gate: TrustResolver = field(default_factory=TrustResolver)
    configure_overrides: _RpcConfigureOverrides = field(default_factory=_RpcConfigureOverrides)

    @property
    def events(self) -> list[WispEvent]:
        return self.writer.events

    def executor(
        self,
        *,
        task_group: TaskGroup,
        send: MemoryObjectSendStream[_RpcControlEvent],
    ) -> RpcCommandExecutor:
        return RpcCommandExecutor(
            agent=self.agent,
            runtime=self.runtime,
            sessions=self.sessions,
            session_state=self.session_state,
            task_group=task_group,
            send=send,
            approval_policy=self.approval_policy,
            trust_gate=self.trust_gate,
            configure_overrides=self.configure_overrides,
            coordinator=self.coordinator,
            write_event=self.writer,
            render_events=self.writer.render_events,
        )


async def build_rpc_executor_fixture(tmp_path: Path) -> RpcExecutorFixture:
    config = WispConfig(provider="fake", session_dir=tmp_path)
    runtime = await build_runtime(
        auth_path=config.auth_path,
        retry_policy=config.retry_policy,
    )
    sessions = JsonlSessionStore(tmp_path)
    agent = CodingSession(provider=runtime.providers.get("fake"), sessions=sessions)
    state = _RpcSessionState(None, (), 0)
    return RpcExecutorFixture(
        runtime=runtime,
        sessions=sessions,
        agent=agent,
        session_state=state,
        coordinator=RpcCoordinator(state),
    )
