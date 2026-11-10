"""`change_directory` over the in-process controller, and folders across sessions."""

from __future__ import annotations

from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.support.rpc import guard_rpc_command_serialization
from wisp.config.runtime import WispConfig
from wisp.events import RpcCommandFinished, WispEvent
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.sessions.jsonl import JsonlSession

Results = list[list[WispEvent]]
Step = Callable[[InProcessWisp, Results], Awaitable[str]]


@pytest.fixture(autouse=True)
def _guard_typed_command_execution(monkeypatch: MonkeyPatch) -> None:
    guard_rpc_command_serialization(monkeypatch)


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("project\n", encoding="utf-8")
    return root.resolve()


async def _until_finished(stream: AsyncIterator[WispEvent], command_id: str) -> list[WispEvent]:
    events: list[WispEvent] = []
    with anyio.fail_after(10):
        while True:
            event = await anext(stream)
            events.append(event)
            if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                return events


def _scenario(
    tmp_path: Path, project: Path, steps: list[Step], *, resume: str | None = None
) -> Results:
    """Run each step's command to completion; return the events each one produced.

    Events the controller sends at startup are part of the first step's result.
    """

    results: Results = []

    async def scenario() -> None:
        controller = await InProcessWisp.start(
            WispConfig(provider="fake", session_dir=tmp_path / "sessions"),
            options=InProcessOptions(
                startup_trusted=True, project_context_root=project, cwd=project, resume=resume
            ),
        )
        # The controller's event stream can be claimed once, so every step reads on.
        stream = controller.events()
        try:
            for step in steps:
                command_id = await step(controller, results)
                results.append(await _until_finished(stream, command_id))
        finally:
            await controller.aclose()

    anyio.run(scenario)
    return results


def _cd(path: str) -> Step:
    return lambda controller, _results: controller.change_directory(path)


def _prompt(text: str) -> Step:
    return lambda controller, _results: controller.prompt(text)


def _workspace(events: list[WispEvent]) -> Path:
    return [event for event in events if event.type == "rpc.workspace"][-1].cwd  # type: ignore[attr-defined]


def _finished(events: list[WispEvent]) -> RpcCommandFinished:
    return [event for event in events if isinstance(event, RpcCommandFinished)][-1]


def _session_id(events: list[WispEvent]) -> str:
    return next(event.session_id for event in events if event.type == "session.saved")  # type: ignore[attr-defined]


def test_change_directory_replies(tmp_path: Path, project: Path) -> None:
    before_session, back, refused = _scenario(
        tmp_path, project, [_cd("src"), _cd(".."), _cd("../..")]
    )

    # No session yet: the folder changes, and the first prompt records it later.
    assert _finished(before_session).ok
    assert _workspace(before_session) == project / "src"
    assert not any(event.type == "session.directory_changed" for event in before_session)
    assert _workspace(back) == project
    assert not _finished(refused).ok
    assert "outside the project" in (_finished(refused).error or "")


def test_move_saved_after_prompt(tmp_path: Path, project: Path) -> None:
    _first, moved = _scenario(tmp_path, project, [_prompt("hello"), _cd("src")])

    types = [event.type for event in moved]
    assert types.index("session.directory_changed") < types.index("rpc.workspace")
    change = next(event for event in moved if event.type == "session.directory_changed")
    assert (change.previous_cwd, change.cwd) == (project, project / "src")  # type: ignore[attr-defined]


def test_move_waits_for_prompt(tmp_path: Path, project: Path) -> None:
    async def prompt_then_cd(controller: InProcessWisp, _results: Results) -> str:
        await controller.prompt("hello")
        return await controller.change_directory("src")

    (events,) = _scenario(tmp_path, project, [prompt_then_cd])

    types = [event.type for event in events]
    assert types.index("agent.completed") < types.index("session.directory_changed")
    assert _workspace(events) == project / "src"


def test_select_restores_folder(tmp_path: Path, project: Path) -> None:
    """Each session works from the folder it last recorded, or the launch folder."""

    def select(index: int) -> Step:
        return lambda controller, results: controller.select_session(_session_id(results[index]))

    results = _scenario(
        tmp_path,
        project,
        [
            _prompt("a"),  # 0: session A, in the launch folder
            _cd("src"),  # 1: A moves to src
            lambda controller, _results: controller.new_session(),
            _cd(".."),  # 3: back in the launch folder, nothing saved yet
            _prompt("b"),  # 4: session B, records nothing
            select(0),  # 5
            select(4),  # 6
        ],
    )

    assert _workspace(results[5]) == project / "src"
    assert _workspace(results[6]) == project


def test_resume_restores_folder(tmp_path: Path, project: Path) -> None:
    first, _moved = _scenario(tmp_path, project, [_prompt("a"), _cd("src")])

    (resumed,) = _scenario(
        tmp_path,
        project,
        [lambda controller, _results: controller.get_state()],
        resume=_session_id(first),
    )

    assert _workspace(resumed) == project / "src"


def test_file_index_follows_move(tmp_path: Path, project: Path) -> None:
    (project / "src" / "app.py").write_text("x = 1\n", encoding="utf-8")
    (project / "top.py").write_text("y = 2\n", encoding="utf-8")

    _moved, listed = _scenario(
        tmp_path,
        project,
        [_cd("src"), lambda controller, _results: controller.get_project_files()],
    )

    report = next(event for event in listed if event.type == "rpc.project_files")
    paths = {entry.path for entry in report.entries}  # type: ignore[attr-defined]
    assert "app.py" in paths
    assert "top.py" not in paths


def test_failed_save_keeps_host(tmp_path: Path, project: Path, monkeypatch: MonkeyPatch) -> None:
    """A move that cannot be saved fails its command; the host keeps serving."""

    original = JsonlSession.append_event

    async def failing(self: JsonlSession, event: WispEvent, **kwargs: object) -> object:
        if event.type == "session.directory_changed":
            raise OSError(28, "No space left on device")
        return await original(self, event, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(JsonlSession, "append_event", failing)

    _first, moved, state = _scenario(
        tmp_path,
        project,
        [_prompt("a"), _cd("src"), lambda controller, _results: controller.get_state()],
    )

    assert not _finished(moved).ok
    assert "folder is unchanged" in (_finished(moved).error or "")
    assert _finished(state).ok
