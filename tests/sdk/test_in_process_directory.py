"""`change_directory` over the in-process controller, and folders across sessions."""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Awaitable, Callable
from pathlib import Path

import anyio
import pytest
from pytest import MonkeyPatch

from tests.support.rpc import guard_rpc_command_serialization
from wisp.coding.session import CodingSession
from wisp.config.runtime import WispConfig
from wisp.events import ProjectConfigApplied, RpcCommandFinished, WispEvent
from wisp.sdk import InProcessOptions, InProcessWisp
from wisp.sessions.jsonl import JsonlSession
from wisp.trust.permissions import save_permission_mode
from wisp.trust.records import record_trust

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


async def _until_finished(
    controller: InProcessWisp,
    stream: AsyncIterator[WispEvent],
    command_id: str,
    *,
    trust_answer: bool | None,
    trust_transient: bool = False,
) -> list[WispEvent]:
    events: list[WispEvent] = []
    with anyio.fail_after(10):
        while True:
            event = await anext(stream)
            events.append(event)
            if event.type == "trust.requested" and trust_answer is not None:
                await controller.trust(
                    event.request_id,  # type: ignore[attr-defined]
                    trusted=trust_answer,
                    transient=trust_transient,
                )
            if isinstance(event, RpcCommandFinished) and event.command_id == command_id:
                return events


def _scenario(
    tmp_path: Path,
    project: Path,
    steps: list[Step],
    *,
    resume: str | None = None,
    trust_answer: bool | None = None,
    trust_transient: bool = False,
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
                results.append(
                    await _until_finished(
                        controller,
                        stream,
                        command_id,
                        trust_answer=trust_answer,
                        trust_transient=trust_transient,
                    )
                )
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
        tmp_path, project, [_cd("src"), _cd(".."), _cd("missing")]
    )

    # No session yet: the folder changes, and the first prompt records it later.
    assert _finished(before_session).ok
    assert _workspace(before_session) == project / "src"
    assert not any(event.type == "session.directory_changed" for event in before_session)
    assert _workspace(back) == project
    assert not _finished(refused).ok
    assert "No such folder" in (_finished(refused).error or "")


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
    assert "it is unchanged" in (_finished(moved).error or "")
    assert _finished(state).ok


def _other_project(tmp_path: Path) -> Path:
    other = tmp_path / "other"
    (other / "lib").mkdir(parents=True)
    (other / "README.md").write_text("other\n", encoding="utf-8")
    return other.resolve()


def _host_agent(controller: InProcessWisp) -> object:
    return controller._in_process_transport._host.agent  # type: ignore[attr-defined]


def _event(events: list[WispEvent], event_type: str) -> WispEvent:
    return next(event for event in events if event.type == event_type)


def test_move_into_trusted_project(tmp_path: Path, project: Path) -> None:
    """A trusted project applies at once: its root, trust, skills and saved mode."""

    other = _other_project(tmp_path)
    record_trust(other, True)
    save_permission_mode(other, "yolo")
    seen: dict[str, object] = {}

    async def inspect(controller: InProcessWisp, _results: Results) -> str:
        agent = _host_agent(controller)
        seen["root"] = agent.project_context_root  # type: ignore[attr-defined]
        seen["trusted"] = agent.trusted  # type: ignore[attr-defined]
        seen["boundary"] = agent.tool_context.boundary()  # type: ignore[attr-defined]
        return await controller.get_state()

    _first, moved, _state = _scenario(
        tmp_path, project, [_prompt("a"), _cd(str(other / "lib")), inspect]
    )

    assert _workspace(moved) == other / "lib"
    assert _event(moved, "session.directory_changed").cwd == other / "lib"  # type: ignore[attr-defined]
    assert _event(moved, "skill.catalog.updated")
    assert _event(moved, "rpc.permissions").permissions.mode == "yolo"  # type: ignore[attr-defined]
    assert seen == {"root": other, "trusted": True, "boundary": other}


@pytest.mark.parametrize("approve", [True, False], ids=["approved", "declined"])
def test_untrusted_project_asks_next_prompt(tmp_path: Path, project: Path, approve: bool) -> None:
    """An undecided project is untrusted until its trust prompt, as at startup."""

    other = _other_project(tmp_path)
    seen: dict[str, object] = {}

    async def inspect(controller: InProcessWisp, _results: Results) -> str:
        agent = _host_agent(controller)
        seen["trusted"] = agent.trusted  # type: ignore[attr-defined]
        seen["boundary"] = agent.tool_context.boundary()  # type: ignore[attr-defined]
        return await controller.get_state()

    moved, before_prompt, prompted, after = _scenario(
        tmp_path,
        project,
        [_cd(str(other / "lib")), inspect, _prompt("b"), inspect],
        trust_answer=approve,
    )

    del moved, before_prompt, after
    request = _event(prompted, "trust.requested")
    assert request.project_path == other  # type: ignore[attr-defined]
    assert seen["trusted"] is approve
    assert seen["boundary"] == (other if approve else other / "lib")


def test_nested_repository_stays(tmp_path: Path, project: Path) -> None:
    """A folder inside the project stays in it, even with a repository of its own."""

    nested = project / "vendor" / "lib"
    nested.mkdir(parents=True)
    (nested.parent / "README.md").write_text("vendored\n", encoding="utf-8")
    seen: dict[str, object] = {}

    async def inspect(controller: InProcessWisp, _results: Results) -> str:
        seen["root"] = _host_agent(controller).project_context_root  # type: ignore[attr-defined]
        return await controller.get_state()

    (moved, _state) = _scenario(tmp_path, project, [_cd("vendor/lib"), inspect])

    assert not any(event.type == "rpc.permissions" for event in moved)
    assert seen["root"] == project


def test_round_trip_restores_project(tmp_path: Path, project: Path) -> None:
    other = _other_project(tmp_path)
    record_trust(other, True)
    seen: dict[str, object] = {}

    async def inspect(controller: InProcessWisp, _results: Results) -> str:
        seen["root"] = _host_agent(controller).project_context_root  # type: ignore[attr-defined]
        return await controller.get_state()

    _scenario(tmp_path, project, [_cd(str(other)), _cd(str(project / "src")), inspect])

    assert seen["root"] == project


def test_select_enters_session_project(tmp_path: Path, project: Path) -> None:
    """Selecting a session that last worked in another project enters that project."""

    other = _other_project(tmp_path)
    record_trust(other, True)
    seen: dict[str, object] = {}

    async def inspect(controller: InProcessWisp, _results: Results) -> str:
        seen["root"] = _host_agent(controller).project_context_root  # type: ignore[attr-defined]
        return await controller.get_state()

    results = _scenario(
        tmp_path,
        project,
        [
            _prompt("a"),  # 0: session A
            _cd(str(other / "lib")),  # 1: A moves into the other project
            lambda controller, _results: controller.new_session(),
            _cd(str(project)),  # 3: back in the launch project
            _prompt("b"),  # 4: session B
            lambda controller, results: controller.select_session(_session_id(results[0])),
            inspect,
        ],
    )

    assert _workspace(results[5]) == other / "lib"
    assert seen["root"] == other


def test_restore_falls_back(tmp_path: Path, project: Path) -> None:
    """A session whose folder is gone works from the launch folder, with a notice."""

    results = _scenario(
        tmp_path,
        project,
        [
            _prompt("a"),
            _cd("src"),
            lambda controller, _results: controller.new_session(),
            _prompt("b"),
        ],
    )
    (project / "src").rmdir()

    (selected,) = _scenario(
        tmp_path,
        project,
        [lambda controller, _results: controller.select_session(_session_id(results[0]))],
    )

    assert _workspace(selected) == project
    assert any(
        event.type == "error" and "No such folder" in event.message  # type: ignore[attr-defined]
        for event in selected
    )


@pytest.mark.parametrize("other_trusted", [True, False], ids=["trusted", "undecided"])
def test_project_settings_follow_move(
    tmp_path: Path, project: Path, monkeypatch: MonkeyPatch, other_trusted: bool
) -> None:
    """The old project's settings never carry over; a trusted new one's apply."""

    monkeypatch.setenv("WISP_PROVIDER", "fake")
    monkeypatch.delenv("WISP_TRUST", raising=False)
    other = _other_project(tmp_path)
    for root, model in ((project, "model-a"), (other, "model-b")):
        (root / ".wisp").mkdir()
        (root / ".wisp" / "settings.json").write_text(json.dumps({"model": model}))
    record_trust(project, True)
    if other_trusted:
        record_trust(other, True)
    models: list[str | None] = []
    applied: list[str | None] = []

    async def scenario() -> None:
        controller = await InProcessWisp.from_environment(
            session_dir=tmp_path / "sessions",
            options=InProcessOptions(project_context_root=project, cwd=project),
        )
        stream = controller.events()
        agent = _host_agent(controller)
        try:
            models.append(agent.model)  # type: ignore[attr-defined]
            for path in (str(other), str(project)):
                command_id = await controller.change_directory(path)
                events = await _until_finished(controller, stream, command_id, trust_answer=None)
                models.append(agent.model)  # type: ignore[attr-defined]
                # Each project's configuration differs, so each move rebuilds the
                # provider adapters and says so.
                (config,) = [e for e in events if isinstance(e, ProjectConfigApplied)]
                applied.append(config.model)
        finally:
            await controller.aclose()

    anyio.run(scenario)

    assert models[0] == "model-a"
    assert models[1] == ("model-b" if other_trusted else None)
    assert models[2] == "model-a"
    assert applied == models[1:]


def _fail_project_entry(monkeypatch: MonkeyPatch) -> None:
    """Make entering another project fail after its move was saved."""

    original = CodingSession.move_into

    def failing(self: CodingSession, target: Path, **kwargs: object) -> None:
        if kwargs.get("configuration") is not None:
            raise RuntimeError("provider construction failed")
        original(self, target, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(CodingSession, "move_into", failing)


def test_failed_entry_records_way_back(
    tmp_path: Path, project: Path, monkeypatch: MonkeyPatch
) -> None:
    """A project that cannot be entered leaves the session, and its file, in place."""

    other = _other_project(tmp_path)
    record_trust(other, True)
    _fail_project_entry(monkeypatch)
    seen: dict[str, object] = {}

    async def inspect(controller: InProcessWisp, _results: Results) -> str:
        host = controller._in_process_transport._host  # type: ignore[attr-defined]
        seen["root"] = host.agent.project_context_root
        seen["recorded"] = await host.agent.recorded_directory(
            host.coordinator.session_state.session
        )
        return await controller.get_state()

    _first, moved, state = _scenario(tmp_path, project, [_prompt("a"), _cd(str(other)), inspect])

    assert not _finished(moved).ok
    assert "it is unchanged" in (_finished(moved).error or "")
    assert _finished(state).ok
    assert seen == {"root": project, "recorded": project}


def test_failed_restore_keeps_host(tmp_path: Path, project: Path, monkeypatch: MonkeyPatch) -> None:
    other = _other_project(tmp_path)
    record_trust(other, True)
    results = _scenario(
        tmp_path,
        project,
        [
            _prompt("a"),
            _cd(str(other)),
            lambda controller, _results: controller.new_session(),
            _cd(str(project)),
            _prompt("b"),
        ],
    )
    _fail_project_entry(monkeypatch)

    selected, state = _scenario(
        tmp_path,
        project,
        [
            lambda controller, _results: controller.select_session(_session_id(results[0])),
            lambda controller, _results: controller.get_state(),
        ],
    )

    assert _workspace(selected) == project
    assert any(
        event.type == "error" and "could not enter" in event.message  # type: ignore[attr-defined]
        for event in selected
    )
    assert _finished(state).ok


def test_decline_not_asked_again(tmp_path: Path, project: Path) -> None:
    """A project declined in this process stays declined when the session returns.

    The decline is transient, so no saved record answers the second time.
    """

    other = _other_project(tmp_path)
    results = _scenario(
        tmp_path,
        project,
        [
            _cd(str(other)),
            _prompt("b"),
            _cd(str(project)),
            _cd(str(other)),
            _prompt("again"),
        ],
        trust_answer=False,
        trust_transient=True,
    )

    requests = [event for events in results for event in events if event.type == "trust.requested"]
    assert len(requests) == 1
