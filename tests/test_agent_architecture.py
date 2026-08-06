from __future__ import annotations

import ast
import os
import subprocess
import sys
from concurrent.futures import ThreadPoolExecutor
from functools import partial
from pathlib import Path

import pytest

_PURE_AGENT_MODULES = ("loop.py", "execution.py", "harness.py")
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
    "wisp.tui",
)
_CODING_MODULES = ("compaction.py", "session.py", "tool_execution.py")
_CODING_FORBIDDEN_IMPORTS = (
    "wisp.agent.compat",
    "wisp.cli",
    "wisp.config",
    "wisp.rpc",
    "wisp.settings",
    "wisp.trust",
    "wisp.tui",
)
_FRONTEND_MODULES = (Path("cli/__init__.py"), Path("cli/rpc.py"))
_FRESH_IMPORT_MODULES = (
    "wisp.agent.harness",
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
    "wisp.tui",
)
_RPC_TRANSPORT_FORBIDDEN_IMPORTS = (
    "wisp.agent",
    "wisp.coding",
    "wisp.config",
    "wisp.runtime",
    "wisp.sessions",
    "wisp.trust",
    "wisp.tui",
)
_RPC_EXECUTION_FORBIDDEN_IMPORTS = (
    "os",
    "queue",
    "stat",
    "sys",
    "threading",
    "wisp.cli.rpc",
    "wisp.trust",
    "wisp.tui",
)
_TEXTUAL_INPUT_FORBIDDEN_IMPORTS = (
    "wisp.providers",
    "wisp.rpc",
    "wisp.sessions",
    "wisp.tui.shell",
    "wisp.tui.textual_app",
    "wisp.tui.widgets",
)
_TEXTUAL_TRANSCRIPT_FORBIDDEN_IMPORTS = (
    "wisp.providers",
    "wisp.rpc",
    "wisp.sessions",
    "wisp.tui.shell",
    "wisp.tui.stream_buffer",
    "wisp.tui.textual_app",
    "wisp.tui.textual_history",
    "wisp.tui.textual_renderer",
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
    agent_dir = Path(__file__).parents[1] / "src" / "wisp" / "agent"

    violations: list[str] = []
    for filename in _PURE_AGENT_MODULES:
        for imported in sorted(_module_imports(agent_dir / filename)):
            if imported.startswith(_FORBIDDEN_IMPORTS):
                violations.append(f"{filename}: {imported}")

    assert violations == []


def test_coding_modules_do_not_import_frontends_or_trust_resolution() -> None:
    coding_dir = Path(__file__).parents[1] / "src" / "wisp" / "coding"

    violations: list[str] = []
    for filename in _CODING_MODULES:
        for imported in sorted(_module_imports(coding_dir / filename)):
            if imported.startswith(_CODING_FORBIDDEN_IMPORTS):
                violations.append(f"{filename}: {imported}")

    assert violations == []


def test_frontends_import_coding_session_directly() -> None:
    wisp_dir = Path(__file__).parents[1] / "src" / "wisp"

    for module in _FRONTEND_MODULES:
        imports = _module_imports(wisp_dir / module)
        assert "wisp.coding" in imports
        assert "wisp.agent.compat" not in imports


def test_rpc_coordinator_does_not_own_transport_or_runtime_policy() -> None:
    path = Path(__file__).parents[1] / "src" / "wisp" / "cli" / "rpc_coordinator.py"

    violations = [
        imported
        for imported in sorted(_module_imports(path))
        if imported.startswith(_RPC_COORDINATOR_FORBIDDEN_IMPORTS)
    ]

    assert violations == []


@pytest.mark.parametrize(
    ("filename", "forbidden"),
    [
        ("rpc_transport.py", _RPC_TRANSPORT_FORBIDDEN_IMPORTS),
        ("rpc_execution.py", _RPC_EXECUTION_FORBIDDEN_IMPORTS),
    ],
)
def test_rpc_layers_preserve_dependency_direction(
    filename: str,
    forbidden: tuple[str, ...],
) -> None:
    path = Path(__file__).parents[1] / "src" / "wisp" / "cli" / filename

    violations = [
        imported for imported in sorted(_module_imports(path)) if imported.startswith(forbidden)
    ]

    assert violations == []


def test_textual_input_controller_preserves_frontend_import_direction() -> None:
    path = Path(__file__).parents[1] / "src" / "wisp" / "tui" / "textual_input.py"

    violations = [
        imported
        for imported in sorted(_module_imports(path))
        if imported.startswith(_TEXTUAL_INPUT_FORBIDDEN_IMPORTS)
    ]

    assert violations == []


def test_textual_transcript_controller_preserves_frontend_import_direction() -> None:
    path = Path(__file__).parents[1] / "src" / "wisp" / "tui" / "textual_transcript.py"

    violations = [
        imported
        for imported in sorted(_module_imports(path))
        if imported.startswith(_TEXTUAL_TRANSCRIPT_FORBIDDEN_IMPORTS)
    ]

    assert violations == []


def test_legacy_agent_compatibility_exports_are_removed() -> None:
    import wisp.agent.loop as agent_loop

    compat_path = Path(__file__).parents[1] / "src" / "wisp" / "agent" / "compat.py"

    assert not compat_path.exists()
    assert not hasattr(agent_loop, "Agent")
    assert "Agent" not in agent_loop.__all__


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
    root = Path(__file__).parents[1]
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
