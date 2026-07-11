from __future__ import annotations

import ast
import os
import subprocess
import sys
from pathlib import Path

import pytest

_PURE_AGENT_MODULES = ("loop.py", "execution.py")
_FORBIDDEN_IMPORTS = (
    "wisp.agent.compat",
    "wisp.agent.prompt",
    "wisp.cli",
    "wisp.config",
    "wisp.rpc",
    "wisp.runtime",
    "wisp.sessions",
    "wisp.settings",
    "wisp.trust",
    "wisp.tui",
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


@pytest.mark.parametrize("module", ["wisp.providers.base", "wisp.runtime.api"])
def test_provider_facing_modules_import_cleanly_in_fresh_process(module: str) -> None:
    root = Path(__file__).parents[1]
    existing_pythonpath = os.environ.get("PYTHONPATH")
    pythonpath = str(root / "src")
    if existing_pythonpath:
        pythonpath = f"{pythonpath}{os.pathsep}{existing_pythonpath}"

    result = subprocess.run(
        [sys.executable, "-c", f"import {module}"],
        cwd=root,
        env={**os.environ, "PYTHONPATH": pythonpath},
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
