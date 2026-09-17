"""Host policy and exact-request approval for a single-writer teaching fixture."""

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from examples.crafting_agents.checkpoint_02 import TOOLS, FixtureTools, bound_output
from examples.crafting_agents.core import ToolCall, ToolFailure


# ANCHOR: request
@dataclass(frozen=True)
class ApprovalRequest:
    """Immutable operation details and file contents shown to the host approver."""

    call_id: str
    name: str
    arguments: tuple[tuple[str, str], ...]
    files: tuple[tuple[str, str], ...]


@dataclass(frozen=True)
class ExecutionPolicy:
    """Host-owned allowlist, independent of the catalog sent to the provider."""

    allowed: frozenset[str] = frozenset({"read"})


# ANCHOR_END: request


def _host_callback[T](callback: Callable[[], T]) -> T:
    """Keep host failures distinct from the loop's recoverable tool-error channel.

    Args:
        callback (Callable[[], T]): Host-owned approval or reporting operation.

    Returns:
        T: The callback result.

    Raises:
        RuntimeError: A callback raises ToolFailure; the original is retained as cause.
        Exception: Other callback exceptions propagate unchanged.
    """
    try:
        return callback()
    except ToolFailure as exc:
        raise RuntimeError("host callback failed") from exc


class ControlledTools:
    """Gate the chapter 2 executor; assume a trusted fixture and no concurrent writer."""

    def __init__(
        self,
        root: Path,
        policy: ExecutionPolicy,
        approve: Callable[[ApprovalRequest], bool],
        *,
        report: Callable[[str], None] = print,
    ) -> None:
        self.root = root
        self.policy = policy
        self.approve = approve
        self.report = report
        self.fixture = FixtureTools(root)

    def _snapshot(self, name: str) -> str:
        path = self.root / name
        try:
            if path.is_symlink() or not path.is_file():
                raise ToolFailure("fixture inputs must be regular files, not symlinks")
            with path.open("r", encoding="utf-8") as file:
                text = file.read(16_001)
        except (OSError, UnicodeError) as exc:
            raise ToolFailure(str(exc)) from exc
        if len(text) > 16_000:
            raise ToolFailure("fixture input exceeds 16000 characters")
        return text

    # ANCHOR: execute
    def execute(self, call: ToolCall) -> str:
        """Validate, authorize, recheck, then execute one copied request.

        Args:
            call (ToolCall): Provider-selected operation; never an approval decision.

        Returns:
            str: Bounded observation, including policy, approval, or stale-input errors.
                Denied requests never reach the underlying executor.

        Raises:
            RuntimeError: A callback raises ToolFailure, which the loop would otherwise
                mistake for an ordinary tool observation. The original is the cause.
            Exception: Other approval or reporting callback failures propagate unchanged.
        """
        try:
            spec = next((tool for tool in TOOLS if tool.name == call.name), None)
            if spec is None:
                raise ToolFailure("unknown tool")
            if set(call.arguments) != set(spec.parameters) or not all(
                isinstance(value, str) for value in call.arguments.values()
            ):
                raise ToolFailure("arguments do not match the tool schema")
            # Copy before invoking external approval code; never dispatch mutable originals.
            arguments = tuple(
                (key, value) for key, value in call.arguments.items() if isinstance(value, str)
            )
            name, call_id = call.name, call.id
            if name not in self.policy.allowed:
                raise ToolFailure(f"policy_denied: {name}")
            if name in {"read", "edit"} and dict(arguments)["path"] != "calculator.py":
                raise ToolFailure("only calculator.py is exposed")
            names = (
                ("calculator.py", "test_calculator.py") if name == "test" else ("calculator.py",)
            )
            files = tuple((path, self._snapshot(path)) for path in names)
            if name == "edit":
                old = dict(arguments)["old"]
                if not old or files[0][1].count(old) != 1:
                    raise ToolFailure("old text must match exactly once")
        except ToolFailure as exc:
            return self._failure(str(exc))

        # External callbacks stay outside conversion of expected boundary failures.
        if name != "read":
            request = ApprovalRequest(call_id, name, arguments, files)
            _host_callback(lambda: self.report(f"approval requested: {call_id} {name}"))
            if not _host_callback(lambda: self.approve(request)):
                return self._failure(f"approval_denied: {name}")
            _host_callback(lambda: self.report(f"approval granted: {call_id} {name}"))
            try:
                if any(self._snapshot(path) != text for path, text in files):
                    raise ToolFailure("stale_input: files changed during approval; request again")
            except ToolFailure as exc:
                return self._failure(str(exc))
        _host_callback(lambda: self.report(f"dispatch: {call_id} {name}"))
        # The observer is external code too: recheck after its final invocation.
        try:
            if any(self._snapshot(path) != text for path, text in files):
                raise ToolFailure("stale_input: files changed during dispatch reporting")
        except ToolFailure as exc:
            return self._failure(str(exc))
        return self.fixture.execute(ToolCall(call_id, name, dict(arguments)))

    # ANCHOR_END: execute

    @staticmethod
    def _failure(message: str) -> str:
        result = bound_output(f"error: {message}")
        return f"truncated={str(result.truncated).lower()}\n{result.text}"
