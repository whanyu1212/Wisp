"""Moving a session's working directory, and what the model and resume see of it."""

from __future__ import annotations

from pathlib import Path

import anyio
import pytest

from wisp.coding.session import CodingSession, DirectoryChangeError
from wisp.events import WispEvent
from wisp.messages import Message
from wisp.providers.events import ProviderResponseCompleted, ProviderResponseStarted
from wisp.providers.fake import ProviderRequest, ScriptedProvider
from wisp.runtime.event_bus import EventBus
from wisp.sessions.jsonl import JsonlSession, JsonlSessionStore
from wisp.tools.context import ToolContext

NOTE = "[The working directory has been changed to {}.]"


def _answers(count: int) -> ScriptedProvider:
    return ScriptedProvider(
        [
            [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content=f"a{i}")]
            for i in range(count)
        ]
    )


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "project"
    (root / "src").mkdir(parents=True)
    (root / "README.md").write_text("project\n", encoding="utf-8")
    return root.resolve()


def _agent(
    project: Path,
    provider: ScriptedProvider,
    store: JsonlSessionStore,
    *,
    cwd: Path | None = None,
    events: EventBus | None = None,
) -> CodingSession:
    return CodingSession(
        provider=provider,
        sessions=store,
        events=events,
        tool_context=ToolContext(cwd=cwd or project),
        project_context_root=project,
    )


def _run(agent: CodingSession, prompt: str, session: JsonlSession) -> None:
    async def run() -> None:
        async for _ in agent.run(prompt, session=session):
            pass

    anyio.run(run)


def _last_prompt(request: ProviderRequest) -> Message:
    return [message for message in request.messages if message.role == "user"][-1]


def _moves(session: JsonlSession) -> list[dict[str, object]]:
    return [
        event for event in session.read_events() if event["type"] == "session.directory_changed"
    ]


def test_move_notes_next_prompt(project: Path, tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    provider = _answers(2)
    agent = _agent(project, provider, store)
    _run(agent, "first", session)

    change = anyio.run(lambda: agent.change_directory("src", session=session))
    _run(agent, "fix it", session)

    assert change is not None and change.cwd == project / "src"
    assert agent.tool_context.cwd == project / "src"
    sent = _last_prompt(provider.calls[1])
    assert sent.content == f"{NOTE.format(project / 'src')}\n\nfix it"
    # The saved prompt stays as typed; replay rebuilds exactly what was sent.
    saved = [m for m in session.read_messages() if m.role == "user"][-1]
    assert saved.content == "fix it"
    assert session.read_context_messages()[-2] == sent


@pytest.mark.parametrize(
    ("path", "error"),
    [
        ("..", "outside the project"),
        ("/", "outside the project"),
        ("missing", "No such folder"),
        ("README.md", "Not a folder"),
        ("escape", "outside the project"),
        ("src\x00/../..", "NUL character"),
    ],
    ids=["parent", "absolute", "missing", "file", "link-out", "nul"],
)
def test_bad_folder_refused(project: Path, tmp_path: Path, path: str, error: str) -> None:
    (project / "escape").symlink_to(tmp_path)
    agent = _agent(project, _answers(0), JsonlSessionStore(tmp_path / "sessions"))

    with pytest.raises(DirectoryChangeError, match=error):
        anyio.run(lambda: agent.change_directory(path, session=None))

    assert agent.tool_context.cwd == project


def test_no_project_root_refused(tmp_path: Path) -> None:
    agent = CodingSession(
        provider=_answers(0),
        sessions=JsonlSessionStore(tmp_path / "sessions"),
        tool_context=ToolContext(cwd=tmp_path),
    )

    with pytest.raises(DirectoryChangeError, match="no project root"):
        anyio.run(lambda: agent.change_directory(".", session=None))


def test_move_refused_during_run(project: Path, tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    bus = EventBus()
    agent = _agent(project, _answers(1), store, events=bus)
    refusals: list[str] = []

    async def move_now(event: WispEvent) -> None:
        try:
            await agent.change_directory("src", session=None)
        except RuntimeError as exc:
            refusals.append(str(exc))

    bus.on("agent.started", move_now)
    _run(agent, "first", store.create())

    assert refusals == ["CodingSession is busy"]
    assert agent.tool_context.cwd == project


def test_new_session_records_start(project: Path, tmp_path: Path) -> None:
    """A session first used after a move records its folder, with no note."""

    store = JsonlSessionStore(tmp_path / "sessions")
    provider = _answers(1)
    agent = _agent(project, provider, store)
    session = store.create()

    assert anyio.run(lambda: agent.change_directory("src", session=session)) is None
    _run(agent, "first", session)

    assert _last_prompt(provider.calls[0]).content == "first"
    assert [(m["previous_cwd"], m["cwd"]) for m in _moves(session)] == [
        (None, str(project / "src"))
    ]


def test_unmoved_session_records_nothing(project: Path, tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    _run(_agent(project, _answers(1), store), "first", session)

    assert _moves(session) == []


def test_run_elsewhere_records_move(project: Path, tmp_path: Path) -> None:
    """Resuming a session from another folder records the move and tells the model."""

    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    first = _agent(project, _answers(1), store)
    anyio.run(lambda: first.change_directory("src", session=session))
    _run(first, "first", session)

    provider = _answers(1)
    _run(_agent(project, provider, store), "again", session)

    assert _last_prompt(provider.calls[0]).content == f"{NOTE.format(project)}\n\nagain"
    assert _moves(session)[-1]["cwd"] == str(project)


@pytest.mark.parametrize("folder_kept", [True, False], ids=["restored", "folder-gone"])
def test_restore_recorded_folder(project: Path, tmp_path: Path, folder_kept: bool) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    first = _agent(project, _answers(1), store)
    _run(first, "first", session)
    anyio.run(lambda: first.change_directory("src", session=session))
    if not folder_kept:
        (project / "src").rmdir()

    later = _agent(project, _answers(0), store)
    notice = anyio.run(lambda: later.restore_directory(session))

    if folder_kept:
        assert (later.tool_context.cwd, notice) == (project / "src", None)
    else:
        assert later.tool_context.cwd == project
        assert notice is not None and "No such folder" in notice


def test_restore_without_record_uses_launch(project: Path, tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    _run(_agent(project, _answers(1), store), "first", session)
    agent = _agent(project, _answers(0), store)
    anyio.run(lambda: agent.change_directory("src", session=None))

    assert anyio.run(lambda: agent.restore_directory(session)) is None
    assert agent.tool_context.cwd == project


def test_failed_save_keeps_folder(
    project: Path, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A move that cannot be saved leaves the session in its folder."""

    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    agent = _agent(project, _answers(1), store)
    _run(agent, "first", session)

    async def full_disk(*_args: object, **_kwargs: object) -> object:
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(session, "append_event", full_disk)

    with pytest.raises(OSError, match="No space left"):
        anyio.run(lambda: agent.change_directory("src", session=session))

    assert agent.tool_context.cwd == project


def test_unreadable_session_restores_launch(project: Path, tmp_path: Path) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    first = _agent(project, _answers(1), store)
    _run(first, "first", session)
    anyio.run(lambda: first.change_directory("src", session=session))
    # A move whose payload no longer validates makes the session unreadable here.
    text = session.path.read_text(encoding="utf-8")
    session.path.write_text(text.replace('"cwd":', '"cwd_was":'), encoding="utf-8")

    later = _agent(project, _answers(0), store)
    anyio.run(lambda: later.change_directory("src", session=None))
    notice = anyio.run(lambda: later.restore_directory(session))

    assert later.tool_context.cwd == project
    assert notice is not None and "could not read" in notice


def test_link_to_project_accepted(project: Path, tmp_path: Path) -> None:
    """A path through a link to the project names a folder inside it."""

    (tmp_path / "alias").symlink_to(project)
    agent = _agent(project, _answers(0), JsonlSessionStore(tmp_path / "sessions"))

    anyio.run(lambda: agent.change_directory(str(tmp_path / "alias" / "src"), session=None))

    assert agent.tool_context.cwd == project / "src"


def test_same_folder_other_case_no_move(project: Path, tmp_path: Path) -> None:
    if not (project / "SRC").exists():
        pytest.skip("the file system is case-sensitive")
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    agent = _agent(project, _answers(1), store, cwd=project / "src")
    _run(agent, "first", session)
    moves_before = len(_moves(session))

    assert anyio.run(lambda: agent.change_directory("../SRC", session=session)) is None
    assert len(_moves(session)) == moves_before
