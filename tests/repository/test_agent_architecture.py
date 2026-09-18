from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import pytest

from tests.paths import REPO_ROOT

_PURE_AGENT_MODULES = (
    "validation.py",
    "context_budget.py",
    "history.py",
    "transcript_repair.py",
    "request_boundary.py",
    "tool_contracts.py",
    "harness/__init__.py",
    "harness/config.py",
    "harness/boundaries.py",
    "harness/runner.py",
    "loop/__init__.py",
    "loop/config.py",
    "loop/continuation.py",
    "loop/model_response.py",
    "loop/prepared_tools.py",
    "loop/provider_lifecycle.py",
    "loop/runner.py",
    "loop/stream_cleanup.py",
    "loop/tool_execution.py",
)
_FORBIDDEN_IMPORTS = (
    "wisp.agent.compat",
    "wisp.agent.prompt",
    "wisp.cli",
    "wisp.coding",
    "wisp.config",
    "wisp.rpc",
    "wisp.runtime",
    "wisp.sessions",
    "wisp.settings",
    "wisp.trust",
    "wisp.cli.native_tui",
)
_CODING_MODULES = ("compaction.py", "session.py", "tool_execution.py")
_CODING_FORBIDDEN_IMPORTS = (
    "wisp.agent.compat",
    "wisp.cli",
    "wisp.config",
    "wisp.rpc",
    "wisp.settings",
    "wisp.trust",
    "wisp.cli.native_tui",
)
_FRONTEND_MODULES = (Path("cli/application.py"),)
_FRESH_IMPORT_MODULES = (
    "wisp.agent.harness",
    "wisp.agent.prompt",
    "wisp.agent.history",
    "wisp.agent.messages",
    "wisp.agent.request_boundary",
    "wisp.agent.tool_contracts",
    "wisp.coding.compaction",
    "wisp.coding.session",
    "wisp.coding.tool_execution",
    "wisp.providers.base",
    "wisp.runtime.api",
)
_FRESH_IMPORT_TIMEOUT_SECONDS = 30
_RPC_COORDINATOR_FORBIDDEN_IMPORTS = (
    "os",
    "stat",
    "sys",
    "threading",
    "wisp.coding",
    "wisp.config",
    "wisp.runtime",
    "wisp.trust",
    "wisp.cli.native_tui",
)
_RPC_TRANSPORT_FORBIDDEN_IMPORTS = (
    "wisp.agent",
    "wisp.coding",
    "wisp.config",
    "wisp.runtime",
    "wisp.sessions",
    "wisp.trust",
    "wisp.cli.native_tui",
)
_RPC_EXECUTION_FORBIDDEN_IMPORTS = (
    "os",
    "queue",
    "sys",
    "threading",
    "wisp.cli.rpc",
    "wisp.trust",
    "wisp.cli.native_tui",
)
# Handler modules may use stdlib concurrency for physical workers, but must not reach
# back into frontends or trust resolution; that stays the direction ``execution.py`` sets.
_RPC_HANDLER_FORBIDDEN_IMPORTS = (
    "wisp.cli.rpc",
    "wisp.trust",
    "wisp.cli.native_tui",
)
_CLI_RPC_ADAPTER_FORBIDDEN_IMPORTS = (
    "wisp.coding",
    "wisp.rpc.execution",
    "wisp.sessions",
    "wisp.tools",
    "wisp.trust",
)


def _module_imports(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    imports: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imports.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module is not None:
            imports.add(node.module)
    return imports


def test_pure_agent_modules_do_not_import_application_layers() -> None:
    agent_dir = REPO_ROOT / "src" / "wisp" / "agent"

    violations: list[str] = []
    for filename in _PURE_AGENT_MODULES:
        for imported in sorted(_module_imports(agent_dir / filename)):
            if imported.startswith(_FORBIDDEN_IMPORTS):
                violations.append(f"{filename}: {imported}")

    assert violations == []


@pytest.mark.parametrize(
    ("public_module", "implementation_module", "symbol"),
    [
        ("harness", "harness.config", "AgentHarnessConfig"),
        ("harness", "harness.runner", "AgentHarness"),
        ("prompt", "prompt.builder", "build_prompt_messages"),
    ],
)
def test_agent_public_packages_export_the_implementation(
    public_module: str, implementation_module: str, symbol: str
) -> None:
    public = importlib.import_module(f"wisp.agent.{public_module}")
    implementation = importlib.import_module(f"wisp.agent.{implementation_module}")

    assert getattr(public, symbol) is getattr(implementation, symbol)


def test_obsolete_agent_compatibility_modules_are_removed() -> None:
    agent_dir = REPO_ROOT / "src" / "wisp" / "agent"

    for filename in ("configuration.py", "context.py", "execution.py", "transcript.py"):
        assert not (agent_dir / filename).exists()


def test_obsolete_tools_and_providers_compatibility_modules_are_removed() -> None:
    wisp_dir = REPO_ROOT / "src" / "wisp"

    for relative in (
        "tools/process.py",
        "tools/file_ops.py",
        "tools/paths.py",
        "tools/process_manager.py",
        "tools/secure_fs.py",
        "providers/retry.py",
    ):
        assert not (wisp_dir / relative).exists()


def test_coding_modules_do_not_import_frontends_or_trust_resolution() -> None:
    coding_dir = REPO_ROOT / "src" / "wisp" / "coding"

    violations: list[str] = []
    for filename in _CODING_MODULES:
        for imported in sorted(_module_imports(coding_dir / filename)):
            if imported.startswith(_CODING_FORBIDDEN_IMPORTS):
                violations.append(f"{filename}: {imported}")

    assert violations == []


def test_frontends_import_coding_session_directly() -> None:
    wisp_dir = REPO_ROOT / "src" / "wisp"

    for module in _FRONTEND_MODULES:
        imports = _module_imports(wisp_dir / module)
        assert "wisp.coding" in imports
        assert "wisp.agent.compat" not in imports


def test_rpc_coordinator_does_not_own_transport_or_runtime_policy() -> None:
    path = REPO_ROOT / "src" / "wisp" / "rpc" / "coordinator.py"

    violations = [
        imported
        for imported in sorted(_module_imports(path))
        if imported.startswith(_RPC_COORDINATOR_FORBIDDEN_IMPORTS)
    ]

    assert violations == []


def test_obsolete_flat_rpc_handler_modules_are_removed() -> None:
    rpc_dir = REPO_ROOT / "src" / "wisp" / "rpc"

    for filename in (
        "session_run.py",
        "session_mutation.py",
        "session_read.py",
        "session_state.py",
        "session_queue.py",
        "configure.py",
        "connections.py",
        "control.py",
        "inspection.py",
        "project_files.py",
    ):
        assert not (rpc_dir / filename).exists()


def test_rpc_handler_subpackages_do_not_reexport_modules() -> None:
    # Keeping these packages empty avoids import cycles with ``wisp.rpc.execution``,
    # which imports every handler module while the handlers import ``coordinator``.
    for package in ("session", "handlers"):
        init = REPO_ROOT / "src" / "wisp" / "rpc" / package / "__init__.py"
        assert _module_imports(init) == set()


@pytest.mark.parametrize(
    ("relative_path", "forbidden"),
    [
        (Path("cli/rpc_transport.py"), _RPC_TRANSPORT_FORBIDDEN_IMPORTS),
        (Path("rpc/execution.py"), _RPC_EXECUTION_FORBIDDEN_IMPORTS),
        *(
            (path.relative_to(REPO_ROOT / "src" / "wisp"), _RPC_HANDLER_FORBIDDEN_IMPORTS)
            for package in ("session", "handlers")
            for path in sorted((REPO_ROOT / "src" / "wisp" / "rpc" / package).glob("*.py"))
            if path.name != "__init__.py"
        ),
    ],
    ids=str,
)
def test_rpc_layers_preserve_dependency_direction(
    relative_path: Path,
    forbidden: tuple[str, ...],
) -> None:
    path = REPO_ROOT / "src" / "wisp" / relative_path

    violations = [
        imported for imported in sorted(_module_imports(path)) if imported.startswith(forbidden)
    ]

    assert violations == []


def test_cli_rpc_adapter_does_not_reimplement_shared_runtime_policy() -> None:
    path = REPO_ROOT / "src" / "wisp" / "cli" / "rpc.py"

    violations = [
        imported
        for imported in sorted(_module_imports(path))
        if imported.startswith(_CLI_RPC_ADAPTER_FORBIDDEN_IMPORTS)
    ]

    assert violations == []


def test_obsolete_cli_rpc_compatibility_modules_are_removed() -> None:
    cli_dir = REPO_ROOT / "src" / "wisp" / "cli"

    assert not (cli_dir / "rpc_configuration.py").exists()
    assert not (cli_dir / "rpc_coordinator.py").exists()
    assert not (cli_dir / "rpc_execution.py").exists()


def test_cli_package_init_only_re_exports_the_application() -> None:
    """Keep the CLI implementation in ``wisp.cli.application``, not the package marker."""

    init_path = REPO_ROOT / "src" / "wisp" / "cli" / "__init__.py"
    tree = ast.parse(init_path.read_text(encoding="utf-8"), filename=str(init_path))

    definitions = [
        type(node).__name__
        for node in tree.body
        if isinstance(node, ast.FunctionDef | ast.AsyncFunctionDef | ast.ClassDef)
    ]

    assert definitions == []
    assert "wisp.cli.application" in _module_imports(init_path)


def test_legacy_agent_compatibility_exports_are_removed() -> None:
    import wisp.agent.loop as agent_loop

    compat_path = REPO_ROOT / "src" / "wisp" / "agent" / "compat.py"

    assert not compat_path.exists()
    assert not hasattr(agent_loop, "Agent")
    assert "Agent" not in agent_loop.__all__


def test_events_carry_no_per_event_schema_version() -> None:
    """The live RPC protocol bundle is the single event contract; do not reintroduce a counter."""

    import wisp.events as events
    from wisp.events import WispEvent

    assert "schema_version" not in WispEvent.model_fields
    for name in dir(events):
        member = getattr(events, name)
        if isinstance(member, type) and issubclass(member, WispEvent):
            assert "schema_version" not in member.model_fields, name
    assert not hasattr(events, "EVENT_SCHEMA_VERSION")
    assert not any(name.endswith("_SCHEMA_VERSION") for name in dir(events)), (
        "per-event schema version constants were removed with live RPC v9"
    )


def test_agent_loop_package_exports_public_contracts() -> None:
    from wisp.agent import loop
    from wisp.agent.loop.config import AgentLoopConfig, CancellationToken, UsageCostEstimator
    from wisp.agent.loop.runner import AgentLoopEvent, run_agent_loop

    assert loop.AgentLoopConfig is AgentLoopConfig
    assert loop.AgentLoopEvent is AgentLoopEvent
    assert loop.CancellationToken is CancellationToken
    assert loop.UsageCostEstimator is UsageCostEstimator
    assert loop.run_agent_loop is run_agent_loop
    assert loop.__all__ == [
        "AgentLoopConfig",
        "AgentLoopEvent",
        "CancellationToken",
        "UsageCostEstimator",
        "run_agent_loop",
    ]


def test_coding_package_exports_session_coordinator() -> None:
    from wisp.coding import CodingSession as ExportedCodingSession
    from wisp.coding.session import CodingSession

    assert ExportedCodingSession is CodingSession


def _fresh_import_failure(module: str, *, root: Path, pythonpath: str) -> str | None:
    try:
        result = subprocess.run(
            [sys.executable, "-c", f"import {module}"],
            cwd=root,
            env={**os.environ, "PYTHONPATH": pythonpath},
            capture_output=True,
            text=True,
            check=False,
            timeout=_FRESH_IMPORT_TIMEOUT_SECONDS,
        )
    except subprocess.TimeoutExpired:
        return f"{module}: timed out after {_FRESH_IMPORT_TIMEOUT_SECONDS} seconds"
    if result.returncode == 0:
        return None
    return f"{module} (exit {result.returncode}):\n{result.stderr}"


@pytest.mark.slow
def test_layer_modules_import_cleanly_in_fresh_processes() -> None:
    root = REPO_ROOT
    existing_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = str(root / "src")
    if existing_pythonpath:
        pythonpath = f"{pythonpath}{os.pathsep}{existing_pythonpath}"

    check_import = partial(_fresh_import_failure, root=root, pythonpath=pythonpath)
    with ThreadPoolExecutor(max_workers=len(_FRESH_IMPORT_MODULES)) as executor:
        failures = [
            failure
            for failure in executor.map(check_import, _FRESH_IMPORT_MODULES)
            if failure is not None
        ]

    assert not failures, "\n\n".join(failures)
