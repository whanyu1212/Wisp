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
            Exception: Unexpected approval or reporting failures propagate. ToolFailure
                is reserved for expected, model-visible boundary failures.
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
            if name != "read":
                request = ApprovalRequest(call_id, name, arguments, files)
                self.report(f"approval requested: {call_id} {name}")
                if not self.approve(request):
                    raise ToolFailure(f"approval_denied: {name}")
                self.report(f"approval granted: {call_id} {name}")
                if any(self._snapshot(path) != text for path, text in files):
                    raise ToolFailure("stale_input: files changed during approval; request again")
            self.report(f"dispatch: {call_id} {name}")
            return self.fixture.execute(ToolCall(call_id, name, dict(arguments)))
        except ToolFailure as exc:
            result = bound_output(f"error: {exc}")
            return f"truncated={str(result.truncated).lower()}\n{result.text}"

    # ANCHOR_END: execute
