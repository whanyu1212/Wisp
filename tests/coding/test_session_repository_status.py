from __future__ import annotations

import subprocess
import threading
from pathlib import Path

import anyio
import pytest

import wisp.coding.session as session_module
from wisp.coding.session import CodingSession, _prompt_cache_key
from wisp.providers.events import (
    ProviderEvent,
    ProviderResponseCompleted,
    ProviderResponseStarted,
    ProviderUsage,
)
from wisp.providers.fake import FakeProvider, ScriptedProvider
from wisp.sessions.entries import (
    MessageSessionEntry,
)
from wisp.sessions.jsonl import JsonlSessionStore
from wisp.tools.context import ToolContext


def test_reuses_one_git_status_across_its_runs(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    repo.mkdir()

    def git(*args: str) -> None:
        subprocess.run(
            [
                "git",
                "-C",
                str(repo),
                "-c",
                "user.name=Wisp",
                "-c",
                "user.email=wisp@example.com",
                *args,
            ],
            check=True,
            capture_output=True,
        )

    git("init", "-q", "-b", "main")
    (repo / "app.py").write_text("print('v1')\n")
    git("add", "app.py")
    git("commit", "-q", "-m", "initial")

    def reply() -> list[ProviderEvent]:
        return [ProviderResponseStarted(model="test"), ProviderResponseCompleted(content="ok")]

    provider = ScriptedProvider([reply(), reply(), reply()])
    store = JsonlSessionStore(tmp_path / "sessions")
    agent = CodingSession(
        provider=provider,
        sessions=store,
        tool_context=ToolContext(cwd=repo),
        trusted=True,
    )

    def system_prompt(call_index: int) -> tuple[str, ...]:
        return tuple(
            message.content
            for message in provider.calls[call_index].messages
            if message.role == "system"
        )

    async def run() -> None:
        session = store.create()
        _ = [event async for event in agent.run("first", session=session)]
        (repo / "app.py").write_text("print('v2')\n")
        _ = [event async for event in agent.run("second", session=session)]
        _ = [event async for event in agent.run("fresh", session=store.create())]

    anyio.run(run)

    # The edit between runs must not change the prompt prefix the provider caches.
    assert system_prompt(1) == system_prompt(0)
    assert any("branch main; status clean" in content for content in system_prompt(0))
    # A different session takes its own snapshot of the current working tree.
    assert any("branch main; 1 changed file(s)" in content for content in system_prompt(2))


def _init_git_repo(repo: Path) -> None:
    repo.mkdir()
    for args in (
        ("init", "-q", "-b", "main"),
        ("add", "."),
        ("commit", "-q", "--allow-empty", "-m", "initial"),
    ):
        subprocess.run(
            ["git", "-C", str(repo), "-c", "user.name=Wisp", "-c", "user.email=w@x", *args],
            check=True,
            capture_output=True,
        )


def _reply() -> list[ProviderEvent]:
    # Real providers report usage; the loop only records a context observation
    # (and so the request's prompt-cache key) for responses that carry it.
    usage = ProviderUsage(input_tokens=100, output_tokens=5, total_tokens=105)
    return [
        ProviderResponseStarted(model="test"),
        ProviderResponseCompleted(content="ok", usage=usage),
    ]


def _system_prompt(provider: ScriptedProvider, call_index: int) -> tuple[str, ...]:
    return tuple(
        message.content
        for message in provider.calls[call_index].messages
        if message.role == "system"
    )


def test_resumed_session_reuses_saved_git_status(
    tmp_path: Path,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()

    def process(provider: ScriptedProvider) -> CodingSession:
        # A fresh CodingSession stands in for a new process: no in-memory snapshot.
        return CodingSession(
            provider=provider,
            sessions=JsonlSessionStore(tmp_path / "sessions"),
            tool_context=ToolContext(cwd=repo),
            trusted=True,
        )

    before, after = ScriptedProvider([_reply()]), ScriptedProvider([_reply()])

    async def run() -> None:
        _ = [event async for event in process(before).run("first", session=session)]
        (repo / "edit.txt").write_text("changed while Wisp was closed\n")
        resumed = JsonlSessionStore(tmp_path / "sessions").load(session.session_id)
        _ = [event async for event in process(after).run("second", session=resumed)]

    anyio.run(run)

    # The resumed first request sends the same instructions as the last request
    # before the restart, so the provider's cached prefix still applies.
    assert _system_prompt(after, 0) == _system_prompt(before, 0)
    assert any("branch main; status clean" in content for content in _system_prompt(after, 0))


def test_resumed_session_rereads_git_after_cache_expiry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    before, after = ScriptedProvider([_reply()]), ScriptedProvider([_reply()])

    async def run() -> None:
        first = CodingSession(
            provider=before, sessions=store, tool_context=ToolContext(cwd=repo), trusted=True
        )
        _ = [event async for event in first.run("first", session=session)]
        (repo / "edit.txt").write_text("changed\n")
        # Resume after the maximum age: the provider cache has expired anyway.
        late = session_module.utc_now() + session_module.RESUMED_REPOSITORY_STATUS_MAX_AGE
        monkeypatch.setattr(
            session_module, "utc_now", lambda: late + session_module.timedelta(seconds=1)
        )
        second = CodingSession(
            provider=after, sessions=store, tool_context=ToolContext(cwd=repo), trusted=True
        )
        _ = [event async for event in second.run("second", session=session)]

    anyio.run(run)

    assert any("branch main; status clean" in content for content in _system_prompt(before, 0))
    assert any("branch main; 1 changed file(s)" in content for content in _system_prompt(after, 0))


def test_resumed_session_elsewhere_reads_its_own_git_status(tmp_path: Path) -> None:
    first_repo, second_repo = tmp_path / "first", tmp_path / "second"
    _init_git_repo(first_repo)
    _init_git_repo(second_repo)
    (second_repo / "edit.txt").write_text("changed\n")
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    before, after = ScriptedProvider([_reply()]), ScriptedProvider([_reply()])

    async def run() -> None:
        for provider, cwd, prompt in ((before, first_repo, "first"), (after, second_repo, "two")):
            agent = CodingSession(
                provider=provider, sessions=store, tool_context=ToolContext(cwd=cwd), trusted=True
            )
            _ = [event async for event in agent.run(prompt, session=session)]

    anyio.run(run)

    assert any("branch main; 1 changed file(s)" in content for content in _system_prompt(after, 0))


class _KeyedProvider(ScriptedProvider):
    """A scripted adapter that accepts a prompt-cache key, like the OpenAI adapters."""

    supports_prompt_cache_key = True


class _OtherKeyedProvider(_KeyedProvider):
    name = "other-keyed"


@pytest.mark.parametrize("change", ["clone", "fork", "provider", "model"])
def test_resumed_session_rereads_git_when_cache_cannot_continue(
    tmp_path: Path,
    change: str,
) -> None:
    # Reusing an older snapshot only helps while the provider cache still
    # applies. A clone or fork gets a new cache key, and caches are not shared
    # across providers or models, so these read the current status instead.
    # The clone and fork happen immediately, within the second of the source's
    # last response: the recorded key tells them apart, not the file's name.
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    store = JsonlSessionStore(tmp_path / "sessions")
    source = store.create()
    after: ScriptedProvider = (
        _OtherKeyedProvider([_reply()]) if change == "provider" else _KeyedProvider([_reply()])
    )

    async def run() -> None:
        first = CodingSession(
            provider=_KeyedProvider([_reply(), _reply()]),
            sessions=store,
            tool_context=ToolContext(cwd=repo),
            trusted=True,
        )
        _ = [event async for event in first.run("first", session=source)]
        _ = [event async for event in first.run("second", session=source)]
        (repo / "edit.txt").write_text("changed\n")
        target = source
        if change == "clone":
            target = await store.clone(source, expected_active_leaf_id=source.read_active_leaf_id())
        elif change == "fork":
            second_user = [
                entry.id
                for entry in source.read_entries()
                if isinstance(entry, MessageSessionEntry) and entry.message.role == "user"
            ][-1]
            forked = await store.fork_from_user_message(
                source, second_user, expected_active_leaf_id=source.read_active_leaf_id()
            )
            target = forked.session
        resumed = CodingSession(
            provider=after,
            sessions=store,
            tool_context=ToolContext(cwd=repo),
            trusted=True,
            model="another-model" if change == "model" else None,
        )
        _ = [event async for event in resumed.run("resumed", session=target)]

    anyio.run(run)

    assert any("branch main; 1 changed file(s)" in content for content in _system_prompt(after, 0))


def test_responses_record_their_request_cache_key(
    tmp_path: Path,
) -> None:
    store = JsonlSessionStore(tmp_path / "sessions")
    keyed, keyless = store.create(), store.create()

    async def run() -> None:
        for provider, session in (
            (_KeyedProvider([_reply()]), keyed),
            (ScriptedProvider([_reply()]), keyless),
        ):
            agent = CodingSession(provider=provider, sessions=store, trusted=False)
            _ = [event async for event in agent.run("hi", session=session)]

    anyio.run(run)

    def recorded_key(session_id: str) -> str | None:
        session = JsonlSessionStore(tmp_path / "sessions").load(session_id)
        [response] = [m for m in session.read_messages() if m.role == "assistant"]
        assert response.context_observation is not None
        return response.context_observation.prompt_cache_key

    assert recorded_key(keyed.session_id) == _prompt_cache_key(keyed.session_id)
    # A keyless adapter is opened without the key, so none is recorded.
    assert recorded_key(keyless.session_id) is None


def test_resume_with_a_keyed_provider_recovers_the_snapshot(tmp_path: Path) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    before, after = _KeyedProvider([_reply()]), _KeyedProvider([_reply()])

    async def run() -> None:
        for provider, prompt in ((before, "first"), (after, "second")):
            agent = CodingSession(
                provider=provider, sessions=store, tool_context=ToolContext(cwd=repo), trusted=True
            )
            _ = [event async for event in agent.run(prompt, session=session)]
            (repo / "edit.txt").write_text("changed\n")

    anyio.run(run)

    assert _system_prompt(after, 0) == _system_prompt(before, 0)


def test_keyless_clone_in_a_later_second_still_reads_git_again(tmp_path: Path) -> None:
    # Without keys (a keyless provider, or records written before keys were
    # stored) the clone check falls back to the file's creation second.
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    store = JsonlSessionStore(tmp_path / "sessions")
    source = store.create()
    after = ScriptedProvider([_reply()])

    async def run() -> None:
        first = CodingSession(
            provider=ScriptedProvider([_reply()]),
            sessions=store,
            tool_context=ToolContext(cwd=repo),
            trusted=True,
        )
        _ = [event async for event in first.run("first", session=source)]
        (repo / "edit.txt").write_text("changed\n")
        await anyio.sleep(1.1)
        target = await store.clone(source, expected_active_leaf_id=source.read_active_leaf_id())
        resumed = CodingSession(
            provider=after, sessions=store, tool_context=ToolContext(cwd=repo), trusted=True
        )
        _ = [event async for event in resumed.run("resumed", session=target)]

    anyio.run(run)

    assert any("branch main; 1 changed file(s)" in content for content in _system_prompt(after, 0))


def test_untrusted_resumed_session_never_reads_or_recovers_git(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    repo = tmp_path / "repo"
    _init_git_repo(repo)
    store = JsonlSessionStore(tmp_path / "sessions")
    session = store.create()
    git_uses: list[str] = []
    monkeypatch.setattr(
        session_module, "read_repository_status", lambda _cwd: git_uses.append("read") or ""
    )
    monkeypatch.setattr(
        session_module,
        "_persisted_repository_status",
        lambda _session, _cwd, **_cache: git_uses.append("recover") or None,
    )

    async def run() -> None:
        for prompt in ("first", "second"):
            agent = CodingSession(
                provider=ScriptedProvider([_reply()]),
                sessions=store,
                tool_context=ToolContext(cwd=repo),
                trusted=False,
            )
            _ = [event async for event in agent.run(prompt, session=session)]

    anyio.run(run)

    assert git_uses == []


def test_abandoned_git_read_cannot_replace_stored_snapshot(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    agent = CodingSession(
        provider=FakeProvider(),
        sessions=JsonlSessionStore(tmp_path),
        tool_context=ToolContext(cwd=tmp_path),
        trusted=True,
    )
    reads = ["old", "new"]
    first_read_started = threading.Event()
    release_first_read = threading.Event()

    def fake_read_repository_status(_cwd: Path) -> str:
        value = reads.pop(0)
        if value == "old":
            # Stands in for a cancelled run whose abandoned worker is still reading Git.
            first_read_started.set()
            release_first_read.wait(5)
        return value

    monkeypatch.setattr(session_module, "read_repository_status", fake_read_repository_status)
    abandoned_result: list[str] = []
    abandoned = threading.Thread(
        target=lambda: abandoned_result.append(
            agent._repository_status_snapshot("session-1", tmp_path)
        )
    )
    abandoned.start()
    assert first_read_started.wait(5)

    # The retry stores its snapshot while the abandoned read is still running.
    assert agent._repository_status_snapshot("session-1", tmp_path) == "new"
    release_first_read.set()
    abandoned.join(5)

    assert agent._repository_status_snapshot("session-1", tmp_path) == "new"
    assert abandoned_result == ["new"]
