from __future__ import annotations

from pathlib import Path

from pytest import MonkeyPatch

from wisp.cli import _restart_current_process


def test_restart_current_process_preserves_invocation_cwd_and_environment(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    changed_directories: list[Path] = []
    executions: list[tuple[str, tuple[str, ...], dict[str, str]]] = []

    monkeypatch.setattr("wisp.cli.os.chdir", changed_directories.append)
    monkeypatch.setattr(
        "wisp.cli.os.execvpe",
        lambda executable, argv, environment: executions.append(
            (executable, tuple(argv), environment)
        ),
    )

    argv = ("/tools/wisp/bin/python", "/tools/wisp/bin/wisp", "--continue")
    environment = {"PATH": "/tools/wisp/bin", "WISP_PROVIDER": "openai"}
    _restart_current_process(argv, cwd=tmp_path, environment=environment)

    assert changed_directories == [tmp_path]
    assert executions == [(argv[0], argv, environment)]
