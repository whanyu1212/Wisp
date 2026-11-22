"""RPC command handler for user-initiated `!` shell commands."""

from __future__ import annotations

import glob
import re
import shlex
import time
from collections.abc import Callable
from pathlib import Path

import anyio
from anyio.abc import TaskGroup
from anyio.streams.memory import MemoryObjectSendStream
from typing_extensions import TypeAliasType

from wisp.coding.session import CodingSession
from wisp.coding.tool_progress import bounded_tail
from wisp.events import RpcShellCompleted, RpcShellProgress
from wisp.rpc.commands import RunShellCommand
from wisp.rpc.coordinator import (
    _RpcCommandCompleted,
    _RpcControlEvent,
    _RpcRunningCommand,
    _RpcSessionState,
)
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter
from wisp.runtime.api import WispRuntime
from wisp.tools.context import ToolContext
from wisp.tools.files.paths import is_protected_path
from wisp.tools.progress import OutputProgress
from wisp.tools.result import ToolError
from wisp.tools.shell.process import _format_process_output_preview
from wisp.tools.shell.tool import _DEFAULT_BASH_TIMEOUT_SECONDS, _command_environment

RunningCommandFactory = TypeAliasType("RunningCommandFactory", Callable[..., _RpcRunningCommand])
CommandCompletedFactory = TypeAliasType(
    "CommandCompletedFactory", Callable[..., _RpcCommandCompleted]
)

_MAX_SHELL_EVENT_TEXT_CHARS = 65_536
_MAX_GLOB_EXPANSION = 2_048
_MAX_TOKEN_RECURSION_DEPTH = 4
_PROGRESS_MIN_INTERVAL_SECONDS = 0.2
_CONTROL_DIRECTORIES = frozenset({".git", ".wisp", ".claude", ".codex", ".agents"})
_REDIRECTION_PREFIX_RE = re.compile(r"^(?:\d*(?:>>?|<<?|<&|>&|&>|&>>)|<>)")
_UNVALIDATABLE_EXPANSION_RE = re.compile(r"(?:`|\$\(|<\(|>\(|\$[A-Za-z_{@*?#0-9])")
_BRACE_EXPANSION_RE = re.compile(r"\{[^{}\s]*(?:,|\.\.)[^{}\s]*\}")
_QUOTED_LITERAL_RE = re.compile(r"'([^']*)'|\"([^\"]*)\"")


class _ShellPolicyError(ValueError):
    """Raised when a user shell command violates trust or protected-path policy."""


class _ShellProgressReporter:
    """Forwards throttled live process output to the RPC client."""

    def __init__(self, *, command_id: str, write_event: RpcEventWriter) -> None:
        self._command_id = command_id
        self._write_event = write_event
        self._started = time.monotonic()
        self._last_sent = 0.0
        self.latest: OutputProgress | None = None

    def report(self, progress: OutputProgress) -> None:
        self.latest = progress
        now = time.monotonic()
        if self._last_sent > 0.0 and now - self._last_sent < _PROGRESS_MIN_INTERVAL_SECONDS:
            return
        self._last_sent = now
        self._write_event(
            RpcShellProgress(
                command_id=self._command_id,
                stdout_tail=bounded_tail(progress.stdout_tail),
                stderr_tail=bounded_tail(progress.stderr_tail),
                stdout_bytes=max(0, progress.stdout_bytes),
                stderr_bytes=max(0, progress.stderr_bytes),
                elapsed_ms=max(0, int((now - self._started) * 1000)),
            )
        )

    def latest_output(self) -> tuple[str, str, str]:
        if self.latest is None:
            return "", "", ""
        stdout = _clamp_text(self.latest.stdout_tail)
        stderr = _clamp_text(self.latest.stderr_tail)
        parts = [part for part in (stdout.rstrip("\n"), stderr.rstrip("\n")) if part]
        return _clamp_text("\n".join(parts)), stdout, stderr


def start_rpc_shell_command(
    command: RunShellCommand,
    *,
    agent: CodingSession,
    runtime: WispRuntime,
    session_state: _RpcSessionState,
    task_group: TaskGroup,
    send: MemoryObjectSendStream[_RpcControlEvent],
    write_event: RpcEventWriter,
    running_command_factory: RunningCommandFactory = _RpcRunningCommand,
    command_completed_factory: CommandCompletedFactory = _RpcCommandCompleted,
) -> _RpcRunningCommand:
    """Start one user-initiated `!` shell command sequentially on ``task_group``.

    Commands run through ``runtime.process_supervisor`` with the session's working
    directory, withheld environment, and protected-path/trust guards, without
    persisting anything into session JSONL history.

    Args:
        command (RunShellCommand): Validated RPC shell command.
        agent (CodingSession): Session providing trust, cwd, and tool context.
        runtime (WispRuntime): Runtime owning the shared ``ProcessSupervisor``.
        session_state (_RpcSessionState): Current RPC session metadata.
        task_group (TaskGroup): Task group executing the command asynchronously.
        send (MemoryObjectSendStream[_RpcControlEvent]): Coordinator completion channel.
        write_event (RpcEventWriter): Writer for lifecycle, progress, and completion events.
        running_command_factory (RunningCommandFactory): Factory for the active handle.
        command_completed_factory (CommandCompletedFactory): Factory for completion notice.

    Returns:
        _RpcRunningCommand: Active handle the coordinator uses for serialization and cancel.
    """

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    command_id = lifecycle.command_id
    cancel_scope = anyio.CancelScope()
    task_group.start_soon(
        _run_shell_command,
        command,
        lifecycle,
        agent,
        runtime,
        session_state,
        cancel_scope,
        send.clone(),
        command_completed_factory,
    )
    return running_command_factory(
        command_id=command_id,
        command_type=command.type,
        cancel_scope=cancel_scope,
    )


def _clamp_text(text: str, limit: int = _MAX_SHELL_EVENT_TEXT_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[-limit:]


def _check_shell_policy(command: str, *, agent: CodingSession) -> ToolContext:
    if not agent.trusted:
        raise _ShellPolicyError("Running shell commands requires a trusted project")

    context = agent.tool_context
    boundary = context.boundary()
    cwd = context.cwd.resolve(strict=False)
    for ancestor in (cwd, *cwd.parents):
        if is_protected_path(ancestor, context):
            raise _ShellPolicyError("Shell command refused inside a protected directory")
        if ancestor == boundary:
            break
        if ancestor.name.casefold() in _CONTROL_DIRECTORIES:
            raise _ShellPolicyError("Shell command refused inside a protected directory")

    if not context.protected_paths:
        return context

    if _UNVALIDATABLE_EXPANSION_RE.search(command) or _BRACE_EXPANSION_RE.search(command):
        raise _ShellPolicyError(
            "Shell command refused: dynamic expansion cannot be validated against protected paths"
        )

    for candidate in _command_path_candidates(command):
        if candidate == "eval":
            raise _ShellPolicyError(
                "Shell command refused: 'eval' cannot be validated against protected paths"
            )
        _assert_candidate_unprotected(candidate, context)
        if any(ch in candidate for ch in "*?["):
            _check_glob_candidate(candidate, context=context, cwd=cwd)
    return context


def _assert_candidate_unprotected(candidate: str, context: ToolContext) -> None:
    path = Path(candidate)
    if is_protected_path(path, context):
        raise _ShellPolicyError(f"Shell command refused: '{candidate}' is a protected path")
    if candidate.startswith("~") and is_protected_path(path.expanduser(), context):
        raise _ShellPolicyError(f"Shell command refused: '{candidate}' is a protected path")


def _check_glob_candidate(candidate: str, *, context: ToolContext, cwd: Path) -> None:
    pattern = candidate
    root_dir: str | None = str(cwd)
    if candidate.startswith("~"):
        pattern = str(Path(candidate).expanduser())
        root_dir = None
    elif Path(candidate).is_absolute():
        root_dir = None

    try:
        matches = glob.iglob(pattern, root_dir=root_dir, recursive=True)
    except OSError:
        return

    count = 0
    for match in matches:
        count += 1
        if count > _MAX_GLOB_EXPANSION:
            raise _ShellPolicyError(
                "Shell command refused: glob expansion exceeds validation limit"
            )
        matched_path = Path(match)
        if is_protected_path(matched_path, context) or (
            root_dir is not None and is_protected_path(cwd / matched_path, context)
        ):
            raise _ShellPolicyError(f"Shell command refused: '{candidate}' matches protected path")


def _tokenize_shell_fragment(fragment: str) -> list[str]:
    try:
        lexer = shlex.shlex(fragment, posix=True, punctuation_chars=True)
        lexer.whitespace_split = True
        return list(lexer)
    except ValueError:
        return re.split(r"[\s;|&<>()]+", fragment)


def _command_path_candidates(command: str) -> list[str]:
    candidates: list[str] = []
    seen: set[str] = set()
    _collect_candidates(command, depth=0, candidates=candidates, seen=seen)
    return candidates


def _collect_candidates(
    fragment: str, *, depth: int, candidates: list[str], seen: set[str]
) -> None:
    for raw in _tokenize_shell_fragment(fragment):
        stripped = _REDIRECTION_PREFIX_RE.sub("", raw).strip()
        if not stripped:
            continue
        _record_candidate(stripped, depth=depth, candidates=candidates, seen=seen)
        if "=" in stripped:
            _, rhs = stripped.split("=", 1)
            rhs = rhs.strip()
            if rhs:
                _record_candidate(rhs, depth=depth, candidates=candidates, seen=seen)


def _record_candidate(value: str, *, depth: int, candidates: list[str], seen: set[str]) -> None:
    if value not in seen:
        seen.add(value)
        candidates.append(value)
    if depth >= _MAX_TOKEN_RECURSION_DEPTH:
        return
    # Inspect quoted literals (e.g. `python -c "open('.env')"`) and inner subshell strings
    # (e.g. `sh -c 'cat .env'`) so nested scripts cannot smuggle protected targets past checks.
    for match in _QUOTED_LITERAL_RE.finditer(value):
        inner = match.group(1) if match.group(1) is not None else match.group(2)
        if inner:
            _collect_candidates(inner, depth=depth + 1, candidates=candidates, seen=seen)
    if any(ch in value for ch in " \t\n;|&<>()'\""):
        _collect_candidates(value, depth=depth + 1, candidates=candidates, seen=seen)


async def _run_shell_command(
    command: RunShellCommand,
    lifecycle: RpcCommandLifecycle,
    agent: CodingSession,
    runtime: WispRuntime,
    session_state: _RpcSessionState,
    cancel_scope: anyio.CancelScope,
    send: MemoryObjectSendStream[_RpcControlEvent],
    command_completed_factory: CommandCompletedFactory,
) -> None:
    entry_count = session_state.entry_count
    started = time.monotonic()
    reporter = _ShellProgressReporter(
        command_id=lifecycle.command_id,
        write_event=lifecycle.write_event,
    )
    error: str | None = None
    try:
        with cancel_scope:
            context = _check_shell_policy(command.command, agent=agent)
            timeout = float(command.timeout or _DEFAULT_BASH_TIMEOUT_SECONDS)
            try:
                result = await runtime.process_supervisor.run_to_completion(
                    command.command,
                    cwd=context.cwd,
                    timeout=timeout,
                    max_output_bytes=context.max_output_bytes,
                    env=_command_environment(context),
                    progress=reporter,
                )
            except ToolError as exc:
                elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
                if exc.failure_code == "timeout":
                    output, stdout, stderr = reporter.latest_output()
                    lifecycle.write_event(
                        RpcShellCompleted(
                            command_id=lifecycle.command_id,
                            command=command.command,
                            output=_clamp_text(str(exc)) if not output else output,
                            stdout=stdout,
                            stderr=stderr,
                            timed_out=True,
                            duration_ms=elapsed_ms,
                            error=_clamp_text(str(exc), 2_048),
                        )
                    )
                    return
                raise

            elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
            formatted = _format_process_output_preview(
                result,
                max_bytes=min(context.max_output_bytes, _MAX_SHELL_EVENT_TEXT_CHARS),
                max_lines=context.max_output_lines,
            )
            lifecycle.write_event(
                RpcShellCompleted(
                    command_id=lifecycle.command_id,
                    command=command.command,
                    exit_code=result.exit_code,
                    output=_clamp_text(formatted.text),
                    stdout=_clamp_text(result.stdout),
                    stderr=_clamp_text(result.stderr),
                    truncated=(
                        result.stdout_truncated or result.stderr_truncated or formatted.truncated
                    ),
                    duration_ms=elapsed_ms,
                )
            )

        if cancel_scope.cancelled_caught:
            elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
            output, stdout, stderr = reporter.latest_output()
            lifecycle.write_event(
                RpcShellCompleted(
                    command_id=lifecycle.command_id,
                    command=command.command,
                    output=output,
                    stdout=stdout,
                    stderr=stderr,
                    cancelled=True,
                    duration_ms=elapsed_ms,
                    error="Command cancelled",
                )
            )
            error = f"RPC command cancelled: {lifecycle.command_id}"
    except (_ShellPolicyError, ToolError) as exc:
        error = str(exc) or "Shell command failed"
    except anyio.get_cancelled_exc_class():
        elapsed_ms = max(0, int((time.monotonic() - started) * 1000))
        output, stdout, stderr = reporter.latest_output()
        lifecycle.write_event(
            RpcShellCompleted(
                command_id=lifecycle.command_id,
                command=command.command,
                output=output,
                stdout=stdout,
                stderr=stderr,
                cancelled=True,
                duration_ms=elapsed_ms,
                error="Command cancelled",
            )
        )
        error = f"RPC command cancelled: {lifecycle.command_id}"
        raise
    except Exception as exc:  # noqa: BLE001 - isolate failures without leaking command text
        error = f"Shell command failed: {type(exc).__name__}"
    finally:
        with anyio.CancelScope(shield=True):
            async with send:
                if error is not None:
                    lifecycle.fail(error)
                else:
                    lifecycle.finish()
                await send.send(
                    command_completed_factory(
                        command_id=lifecycle.command_id,
                        command_type=command.type,
                        ok=error is None,
                        history=None,
                        entry_count=entry_count,
                    )
                )
