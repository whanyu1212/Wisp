from __future__ import annotations

import ast
import importlib
import os
import subprocess
import sys
import typing
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import pytest

from tests.support.paths import REPO_ROOT

_PURE_AGENT_MODULES = (
    "validation.py",
    "context_budget.py",
    "history.py",
    "transcript_repair.py",
    "request_boundary.py",
    "tool_contracts.py",
    "turn_lifecycle.py",
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
    "loop/provider_request.py",
    "loop/response_projection.py",
    "loop/runner.py",
    "loop/stream_cleanup.py",
    "loop/tool_execution.py",
    "loop/tool_lifecycle.py",
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
    "wisp.trust",
    "wisp.cli.native_tui",
)
_CODING_MODULES = ("compaction.py", "persistence.py", "session.py", "tool_execution.py")
_CODING_FORBIDDEN_IMPORTS = (
    "wisp.agent.compat",
    "wisp.cli",
    "wisp.config",
    "wisp.rpc",
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
    "wisp.coding.persistence",
    "wisp.coding.session",
    "wisp.coding.tool_execution",
    # The config root re-exports eagerly; importing an MCP module first must not
    # re-enter wisp.config while wisp.mcp.config is still initializing.
    "wisp.config",
    "wisp.config.settings",
    "wisp.mcp.config",
    "wisp.mcp.transport",
    "wisp.providers.base",
    "wisp.runtime.api",
    # wisp.trust.permissions is a leaf below wisp.config; it must not import
    # wisp.events at runtime or the events package cannot initialize first.
    "wisp.trust.flow",
    "wisp.trust.permissions",
    "wisp.trust.records",
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


def _loop_module_dependencies() -> dict[str, set[str]]:
    """Map each loop module to the sibling loop modules it imports anywhere.

    Relative, absolute, package-level, type-only, and function-local imports all
    count, so a cycle cannot hide behind an alternative import spelling.
    """

    loop_dir = REPO_ROOT / "src" / "wisp" / "agent" / "loop"
    module_names = {path.stem for path in loop_dir.glob("*.py")}
    package = "wisp.agent.loop"
    dependencies: dict[str, set[str]] = {}
    for path in sorted(loop_dir.glob("*.py")):
        imported: set[str] = set()
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Import):
                imported.update(
                    alias.name.removeprefix(f"{package}.").split(".")[0]
                    for alias in node.names
                    if alias.name.startswith(f"{package}.")
                )
            elif isinstance(node, ast.ImportFrom):
                module = node.module or ""
                if node.level == 1 and module:
                    imported.add(module.split(".")[0])
                elif (node.level == 1 and not module) or module == package:
                    imported.update(alias.name for alias in node.names)
                elif node.level == 0 and module.startswith(f"{package}."):
                    imported.add(module.removeprefix(f"{package}.").split(".")[0])
        dependencies[path.stem] = (imported & module_names) - {path.stem}
    return dependencies


def test_loop_modules_have_no_import_cycles() -> None:
    dependencies = _loop_module_dependencies()
    visiting: list[str] = []
    finished: set[str] = set()
    cycles: list[str] = []

    def visit(module: str) -> None:
        if module in finished:
            return
        if module in visiting:
            cycles.append(" -> ".join((*visiting[visiting.index(module) :], module)))
            return
        visiting.append(module)
        for dependency in sorted(dependencies[module]):
            visit(dependency)
        visiting.pop()
        finished.add(module)

    for module in sorted(dependencies):
        visit(module)

    assert cycles == []


def test_prepared_tools_depend_on_shared_lifecycle_not_tool_execution() -> None:
    dependencies = _loop_module_dependencies()

    assert "tool_lifecycle" in dependencies["prepared_tools"]
    assert "tool_execution" not in dependencies["prepared_tools"]
    assert {"prepared_tools", "tool_lifecycle"} <= dependencies["tool_execution"]


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


def test_events_package_exports_every_lifecycle_module_name() -> None:
    """``wisp.events`` is the only public import surface for event models.

    Every class, constant, and alias defined in a lifecycle submodule must be
    re-exported from the package root so callers never depend on submodule layout,
    and every union member must be exported so the schema bundle and importers agree.
    """

    import wisp.events as events

    package_dir = REPO_ROOT / "src" / "wisp" / "events"
    defined: set[str] = set()
    for module_path in sorted(package_dir.glob("*.py")):
        if module_path.name == "__init__.py":
            continue
        for node in ast.parse(module_path.read_text()).body:
            if isinstance(node, ast.ClassDef | ast.FunctionDef):
                defined.add(node.name)
            elif isinstance(node, ast.Assign):
                defined.update(target.id for target in node.targets if isinstance(target, ast.Name))

    exported = set(events.__all__)
    assert defined <= exported, sorted(defined - exported)
    assert all(hasattr(events, name) for name in exported)
    assert not (REPO_ROOT / "src" / "wisp" / "events.py").exists()

    union_members = {
        member.__name__
        for member in typing.get_args(typing.get_args(events.KnownWispEvent.__value__)[0])
    }
    assert union_members <= exported, sorted(union_members - exported)


def test_config_package_keeps_sdk_import_path_and_defining_modules() -> None:
    """``wisp.config`` stays the documented SDK path while internals live in submodules.

    The package root re-exports only the runtime configuration surface. Settings
    resolution and validation helpers are imported from their defining modules so
    the root never grows into a second copy of the package.
    """

    import wisp.config as config
    from wisp.config.runtime import WispConfig, default_auth_path, default_session_dir

    assert config.WispConfig is WispConfig
    assert config.default_auth_path is default_auth_path
    assert config.default_session_dir is default_session_dir
    assert WispConfig.__module__ == "wisp.config.runtime"
    for removed in ("config.py", "settings.py"):
        assert not (REPO_ROOT / "src" / "wisp" / removed).exists()

    root_imports = _module_imports(REPO_ROOT / "src" / "wisp" / "config" / "__init__.py")
    assert root_imports == {"wisp.config.runtime"}, sorted(root_imports)

    internal_root_importers = sorted(
        str(path.relative_to(REPO_ROOT))
        for path in (REPO_ROOT / "src").rglob("*.py")
        if path != REPO_ROOT / "src" / "wisp" / "config" / "__init__.py"
        and "wisp.config" in _module_imports(path)
    )
    assert internal_root_importers == [], internal_root_importers


def test_trust_package_has_no_root_surface_and_permission_mode_lives_in_events() -> None:
    """``wisp.trust`` groups trust records, the interactive flow, and permission I/O.

    Nothing here is SDK-public, so the package root re-exports nothing and callers
    import from the defining module. ``PermissionMode`` is RPC vocabulary and is
    defined by ``wisp.events``; the trust package may only reference it for typing,
    because ``wisp.config`` loads ``wisp.trust.permissions`` while ``wisp.events``
    may still be initializing.
    """

    import wisp.events as events

    trust_dir = REPO_ROOT / "src" / "wisp" / "trust"
    assert not (trust_dir / "__init__.py").exists() or not _module_imports(
        trust_dir / "__init__.py"
    )
    for removed in ("trust.py", "trust_flow.py", "permissions.py"):
        assert not (REPO_ROOT / "src" / "wisp" / removed).exists()

    assert "PermissionMode" in events.__all__
    permissions_tree = ast.parse((trust_dir / "permissions.py").read_text(encoding="utf-8"))
    runtime_imports = {
        node.module
        for node in permissions_tree.body
        if isinstance(node, ast.ImportFrom) and node.module is not None
    }
    assert not any(name.startswith("wisp.events") for name in runtime_imports), runtime_imports


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
