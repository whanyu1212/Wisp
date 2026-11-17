"""Run one eval scenario through Wisp's shared RPC command host."""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator, Awaitable, Callable, Sequence
from pathlib import Path

import anyio
from pydantic import Field

from evals.tool_use.scenario import (
    ApprovalDecision,
    CheckpointStep,
    CompactStep,
    NavigateStep,
    PromptStep,
    ResumeStep,
    Scenario,
    ScenarioModel,
    ScenarioResult,
    Step,
)
from wisp.events import (
    AgentCompleted,
    CompactionCompleted,
    KnownWispEvent,
    MessageCompleted,
    RpcCommandFinished,
    RpcSessionTreeReported,
    ToolApprovalRequested,
    WispEvent,
)
from wisp.rpc.client import JsonlSubprocessRpcTransport, RpcController, RpcTransport


class ScenarioConfig(ScenarioModel):
    """Run one scenario with isolated backend paths and an overall deadline."""

    scenario: Scenario
    command: tuple[str, ...] = Field(min_length=1)
    cwd: Path
    timeout_seconds: float = Field(gt=0, allow_inf_nan=False)
    result_path: Path


class _CheckFailed(Exception):
    """A scenario assertion failed and further execution must stop."""


class _ScenarioDriver:
    def __init__(
        self,
        transport_factory: Callable[[], Awaitable[RpcTransport]],
        event_sink: Callable[[WispEvent], None],
    ) -> None:
        self.transport_factory = transport_factory
        self.event_sink = event_sink
        self.transport: RpcTransport | None = None
        self.controller: RpcController
        self.events: AsyncIterator[KnownWispEvent]
        self.checkpoints: dict[str, tuple[str, str]] = {}
        self.failed_checks: list[str] = []
        self.completed_steps = 0

    async def start(self) -> None:
        self.transport = await self.transport_factory()
        self.controller = RpcController(self.transport)
        self.events = self.transport.events()

    async def close(self) -> None:
        if self.transport is not None:
            transport, self.transport = self.transport, None
            with anyio.CancelScope(shield=True):
                await transport.close()

    async def wait(self, command_id: str, prompt: PromptStep | None = None) -> list[KnownWispEvent]:
        """Consume one stream, resolving approvals while awaiting command completion."""
        events: list[KnownWispEvent] = []
        pending = {command_id}
        decision_index = 0
        denied_request_id: str | None = None
        async for event in self.events:
            self.event_sink(event)
            events.append(event)
            if isinstance(event, ToolApprovalRequested):
                decision = (
                    prompt.approvals[decision_index]
                    if prompt is not None and decision_index < len(prompt.approvals)
                    else None
                )
                approval_id, matches = await self.answer_approval(event, decision)
                if matches:
                    decision_index += 1
                pending.add(approval_id)
                if not matches:
                    denied_request_id = approval_id
            elif isinstance(event, RpcCommandFinished) and event.command_id in pending:
                pending.remove(event.command_id)
                if event.command_id == denied_request_id:
                    raise _CheckFailed("stopped after denying an unexpected approval")
                self.check_command(event, events)
                if not pending:
                    if prompt is not None and decision_index != len(prompt.approvals):
                        self.failed_checks.append(
                            f"missing approvals: expected {len(prompt.approvals)}, "
                            f"received {decision_index}"
                        )
                    return events
        raise RuntimeError(f"RPC stream ended before command {command_id!r} completed")

    async def answer_approval(
        self, event: ToolApprovalRequested, decision: ApprovalDecision | None
    ) -> tuple[str, bool]:
        """Match the effective target and exact JSON arguments before granting access."""
        matches = (
            decision is not None
            and decision.name == (event.target or event.name)
            and any(
                json.dumps(arguments, sort_keys=True) == json.dumps(event.arguments, sort_keys=True)
                for arguments in (decision.arguments, *decision.alternative_arguments)
            )
            and (decision.scope != "tool_session" or event.grants_allowed)
        )
        if not matches:
            self.failed_checks.append(
                f"unexpected or mismatched approval for {event.target or event.name!r}"
            )
        approval_id = await self.controller.approve(
            event.call_id,
            approved=decision.approved if matches and decision is not None else False,
            scope=decision.scope if matches and decision is not None else None,
        )
        return approval_id, matches

    def check_command(self, event: RpcCommandFinished, observed: Sequence[KnownWispEvent]) -> None:
        """Separate scenario/model failures from failed backend operations."""
        if event.ok:
            return
        message = f"{event.command_type} command failed: {event.error or 'unspecified error'}"
        # AgentCompleted has no command id. Serial waits isolate this prompt's
        # events, so an earlier prompt cannot establish a failed run here.
        failed_run = event.command_type == "prompt" and any(
            isinstance(item, AgentCompleted) and item.outcome == "failed" for item in observed
        )
        invalid_summary = event.command_type == "compact" and _invalid_compaction_summary(
            event.error or ""
        )
        if failed_run or invalid_summary:
            # The parent uses failed-run events to distinguish model resource
            # limits from provider/runtime failures; do not override that verdict.
            self.failed_checks.append(message)
            return
        raise RuntimeError(message)

    async def tree(self) -> RpcSessionTreeReported:
        command_id = await self.controller.get_session_tree()
        events = await self.wait(command_id)
        for event in events:
            if isinstance(event, RpcSessionTreeReported) and event.command_id == command_id:
                return event
        raise _CheckFailed("get_session_tree returned no tree report")

    async def prompt(self, step: PromptStep) -> None:
        """Require successful prompt completion and any expected assistant text."""
        events = await self.wait(await self.controller.prompt(step.prompt), step)
        terminals = [event for event in events if isinstance(event, AgentCompleted)]
        if len(terminals) != 1 or terminals[0].outcome != "completed":
            self.failed_checks.append("prompt did not emit one successful agent.completed")
        if step.expect_text is not None:
            text = "\n".join(
                event.content
                for event in events
                if isinstance(event, MessageCompleted) and event.role == "assistant"
            )
            if step.expect_text not in text:
                self.failed_checks.append(f"prompt response missing text {step.expect_text!r}")

    async def step(self, step: Step) -> None:
        if isinstance(step, PromptStep):
            await self.prompt(step)
        elif isinstance(step, CheckpointStep):
            tree = await self.tree()
            if tree.session_id is None or tree.active_leaf_id is None:
                raise _CheckFailed(f"checkpoint {step.name!r} has no active leaf")
            self.checkpoints[step.name] = (tree.session_id, tree.active_leaf_id)
        elif isinstance(step, NavigateStep):
            session_id, leaf = self.checkpoints[step.checkpoint]
            if (await self.tree()).session_id != session_id:
                raise _CheckFailed(f"checkpoint {step.checkpoint!r} belongs to another session")
            await self.wait(await self.controller.navigate_session_tree(leaf))
            restored = await self.tree()
            if (restored.session_id, restored.active_leaf_id) != (session_id, leaf):
                raise _CheckFailed(f"navigation did not restore checkpoint {step.checkpoint!r}")
        elif isinstance(step, ResumeStep):
            previous = await self.tree()
            if previous.session_id is None:
                raise _CheckFailed("cannot resume before a session exists")
            await self.close()
            await self.start()
            await self.wait(await self.controller.select_session(previous.session_id))
            restored = await self.tree()
            if (restored.session_id, restored.active_leaf_id) != (
                previous.session_id,
                previous.active_leaf_id,
            ):
                raise _CheckFailed("resume did not restore the session and active leaf")
        elif isinstance(step, CompactStep):
            events = await self.wait(await self.controller.compact(step.instructions))
            compactions = [event for event in events if isinstance(event, CompactionCompleted)]
            if len(compactions) != 1 or compactions[0].outcome != "completed":
                self.failed_checks.append(
                    "compact did not emit one successful compaction.completed"
                )


def _invalid_compaction_summary(message: str) -> bool:
    """Recognize same-release model-output diagnostics carried as untyped RPC text."""
    # RPC exposes no error codes. Keep this list tied to coding/compaction.py;
    # notably, "Compaction summary failed:" wraps provider/runtime failures.
    return message.startswith(
        (
            "Compaction summary is missing required section:",
            "Compaction summary section is empty:",
            "Compaction summary was blank",
            "Compaction summary ended with finish reason ",
            "Compaction summary attempted a tool call",
            "Compaction summary attempted forbidden tool call:",
            "Compaction summary requires exactly one completed message; got ",
            "Compaction summary turn did not complete successfully",
            "Compaction transcript could not be bounded within ",
        )
    )


async def execute_scenario(
    scenario: Scenario,
    transport_factory: Callable[[], Awaitable[RpcTransport]],
    event_sink: Callable[[WispEvent], None],
    *,
    timeout_seconds: float = 300,
) -> ScenarioResult:
    """Execute ordered steps, preserving cancellation and closing every backend.

    Args:
        scenario (Scenario): Validated steps and expected approval decisions.
        transport_factory (Callable): Start a fresh backend with the same session store.
        event_sink (Callable): Receive each consumed event, including RPC controls.
        timeout_seconds (float): Deadline covering startup and all scenario steps.

    Returns:
        ScenarioResult: Assertion failures or a handled infrastructure error.
    """
    driver = _ScenarioDriver(transport_factory, event_sink)
    error = None
    timed_out = False
    try:
        with anyio.fail_after(timeout_seconds):
            try:
                await driver.start()
                for step in scenario.steps:
                    await driver.step(step)
                    driver.completed_steps += 1
                    if driver.failed_checks:
                        break
            finally:
                await driver.close()
    except _CheckFailed as exc:
        driver.failed_checks.append(str(exc))
    except TimeoutError:
        timed_out = True
        driver.failed_checks.append("scenario timed out")
    except Exception as exc:
        # This process boundary reports infrastructure failures; cancellation is
        # a BaseException and deliberately propagates to its owner.
        error = f"{type(exc).__name__}: {exc}"
    return ScenarioResult(
        completed_steps=driver.completed_steps,
        failed_checks=tuple(driver.failed_checks),
        error=error,
        timed_out=timed_out,
    )


async def _run(config: ScenarioConfig) -> ScenarioResult:
    async def start() -> RpcTransport:
        return await JsonlSubprocessRpcTransport.start(
            config.command, cwd=config.cwd, env=os.environ, stderr=2
        )

    return await execute_scenario(
        config.scenario,
        start,
        lambda event: print(event.model_dump_json(), flush=True),
        timeout_seconds=config.timeout_seconds,
    )


def main() -> int:
    config = ScenarioConfig.model_validate_json(Path(sys.argv[1]).read_text(encoding="utf-8"))
    result = anyio.run(_run, config)
    config.result_path.write_text(result.model_dump_json() + "\n", encoding="utf-8")
    return 1 if result.error is not None else 0


if __name__ == "__main__":
    raise SystemExit(main())
