"""Background-process exits published to frontends and held for the next prompt."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wisp.events import ProcessEnded, WarningEvent, WispEvent
from wisp.rpc.session.process_exits import MAX_WAKE_RUNS, ProcessExitNotices
from wisp.tools.shell.supervisor import ProcessExit, ProcessSupervisor

_STARTED = datetime(2026, 11, 13, 9, 0, tzinfo=UTC)


class _Reported(ProcessSupervisor):
    """A supervisor whose polls have returned the exits of ``reported``."""

    def __init__(self, reported: set[str]) -> None:
        super().__init__()
        self.reported = reported

    def is_terminal_reported(self, process_id: str) -> bool:
        return process_id in self.reported


def _exit(process_id: str, *, notify: bool, state: str = "completed") -> ProcessExit:
    return ProcessExit(
        process_id=process_id,
        command="gh pr checks 1 --watch",
        state=state,  # type: ignore[arg-type]
        exit_code=0 if state == "completed" else None,
        started_at=_STARTED,
        ended_at=_STARTED + timedelta(seconds=252),
        notify=notify,
    )


@pytest.fixture
def published() -> list[WispEvent]:
    return []


def test_every_exit_published(published: list[WispEvent]) -> None:
    notices = ProcessExitNotices(_Reported(set()), published.append)

    notices.record(_exit("a", notify=False))
    notices.record(_exit("b", notify=True, state="timed_out"))

    assert [(e.process_id, e.state, e.exit_code, e.duration_ms, e.notify) for e in published] == [  # type: ignore[attr-defined]
        ("a", "completed", 0, 252_000, False),
        ("b", "timed_out", None, 252_000, True),
    ]
    assert all(isinstance(event, ProcessEnded) for event in published)


def test_only_asked_for_held(published: list[WispEvent]) -> None:
    notices = ProcessExitNotices(_Reported(set()), published.append)
    notices.record(_exit("quiet", notify=False))
    notices.record(_exit("loud", notify=True))

    assert [notice.process_id for notice in notices.pending()] == ["loud"]


def test_delivered_notices_forgotten(published: list[WispEvent]) -> None:
    """Delivery forgets only what the saved prompt carried, not later exits."""

    notices = ProcessExitNotices(_Reported(set()), published.append)
    notices.record(_exit("first", notify=True))
    carried = notices.pending()
    notices.record(_exit("second", notify=True))

    assert [notice.process_id for notice in notices.pending()] == ["first", "second"]
    notices.mark_delivered(carried)

    assert [notice.process_id for notice in notices.pending()] == ["second"]


def test_polled_exit_dropped(published: list[WispEvent]) -> None:
    """A poll that already returned the exit makes the notice redundant."""

    notices = ProcessExitNotices(_Reported({"seen"}), published.append)
    notices.record(_exit("seen", notify=True))
    notices.record(_exit("unseen", notify=True))

    assert [notice.process_id for notice in notices.pending()] == ["unseen"]


def test_failed_publish_keeps_notice() -> None:
    def publish(_event: WispEvent) -> None:
        raise BrokenPipeError("frontend gone")

    notices = ProcessExitNotices(_Reported(set()), publish)

    with pytest.raises(BrokenPipeError):
        notices.record(_exit("loud", notify=True))

    assert [notice.process_id for notice in notices.pending()] == ["loud"]


def test_clear_forgets_notices(published: list[WispEvent]) -> None:
    notices = ProcessExitNotices(_Reported(set()), published.append)
    notices.record(_exit("old-session", notify=True))

    notices.clear()

    assert notices.pending() == ()
    assert len(published) == 1


def test_long_command_summarized(published: list[WispEvent]) -> None:
    notices = ProcessExitNotices(_Reported(set()), published.append)
    long_exit = ProcessExit(
        process_id="long",
        command="x" * 2000,
        state="completed",
        exit_code=0,
        started_at=_STARTED,
        ended_at=_STARTED,
        notify=True,
    )

    notices.record(long_exit)

    (event,) = published
    assert isinstance(event, ProcessEnded)
    assert len(event.command) == 500
    assert event.command.endswith("…")
    assert notices.pending()[0].command == "x" * 2000


def test_restored_notices_lead(published: list[WispEvent]) -> None:
    """A rolled-back prompt's notices come back ahead of exits held since."""

    notices = ProcessExitNotices(_Reported(set()), published.append)
    notices.record(_exit("first", notify=True))
    carried = notices.pending()
    notices.mark_delivered(carried)
    notices.record(_exit("second", notify=True))

    notices.restore(carried)
    notices.restore(carried)

    assert [notice.process_id for notice in notices.pending()] == ["first", "second"]


def test_wake_due_once(published: list[WispEvent]) -> None:
    """An exit makes one wake due; a restore or a quiet exit makes none."""

    woken: list[None] = []
    notices = ProcessExitNotices(_Reported(set()), published.append)
    notices.set_wake_listener(lambda: woken.append(None))

    notices.record(_exit("quiet", notify=False))
    assert notices.take_wake() is False
    notices.record(_exit("loud", notify=True))

    assert len(woken) == 1
    assert notices.take_wake() is True
    assert notices.take_wake() is False
    carried = notices.pending()
    notices.mark_delivered(carried)
    notices.restore(carried)
    assert notices.take_wake() is False


def test_wake_guard_resets() -> None:
    """At most three wakes start between the user's prompts."""

    notices = ProcessExitNotices(_Reported(set()), lambda _event: None)

    def exit_and_wake(process_id: str) -> bool:
        notices.record(_exit(process_id, notify=True))
        return notices.take_wake()

    assert [exit_and_wake(f"run-{n}") for n in range(4)] == [True, True, True, False]
    notices.user_prompted()
    assert exit_and_wake("after-prompt") is True


def test_polled_exit_wakes_nobody(published: list[WispEvent]) -> None:
    notices = ProcessExitNotices(_Reported({"seen"}), published.append)
    notices.record(_exit("seen", notify=True))

    assert notices.take_wake() is False


def test_clear_drops_wake(published: list[WispEvent]) -> None:
    notices = ProcessExitNotices(_Reported(set()), published.append)
    notices.record(_exit("old-session", notify=True))

    notices.clear()

    assert notices.take_wake() is False


def test_user_stop_told_not_woken(published: list[WispEvent]) -> None:
    """A process the user stopped is published and told, but wakes nobody."""

    woken: list[None] = []
    notices = ProcessExitNotices(_Reported(set()), published.append)
    notices.set_wake_listener(lambda: woken.append(None))

    notices.record(_exit("stopped", notify=True, state="cancelled"))

    (event,) = published
    assert isinstance(event, ProcessEnded)
    assert event.state == "cancelled"
    assert [notice.state for notice in notices.pending()] == ["cancelled"]
    assert notices.take_wake() is False
    assert woken == []


def _use_up_wakes(notices: ProcessExitNotices) -> None:
    for n in range(MAX_WAKE_RUNS):
        notices.record(_exit(f"woke-{n}", notify=True))
        assert notices.take_wake() is True
        notices.mark_delivered(notices.pending())


def test_pause_warns_once(published: list[WispEvent]) -> None:
    """Once the limit stops a wake, the user is warned once and the waiting notices marked."""

    notices = ProcessExitNotices(_Reported(set()), published.append)
    _use_up_wakes(notices)

    for process_id in ("waited-1", "waited-2"):
        notices.record(_exit(process_id, notify=True))
        assert notices.take_wake() is False

    (warning,) = [event for event in published if isinstance(event, WarningEvent)]
    assert warning.message.startswith("Auto-wake paused")
    carried = notices.pending()
    assert [(n.process_id, n.wake_paused) for n in carried] == [
        ("waited-1", True),
        ("waited-2", True),
    ]
    notices.mark_delivered(carried)
    notices.restore(carried)
    assert [n.wake_paused for n in notices.pending()] == [True, True]

    notices.mark_delivered(notices.pending())
    notices.user_prompted()
    notices.record(_exit("after-prompt", notify=True))
    assert notices.take_wake() is True
    assert [n.wake_paused for n in notices.pending()] == [False]
    notices.mark_delivered(notices.pending())
    notices.user_prompted()
    _use_up_wakes(notices)
    notices.record(_exit("paused-again", notify=True))
    assert notices.take_wake() is False
    assert len([event for event in published if isinstance(event, WarningEvent)]) == 2


def test_pause_needs_held_notice(published: list[WispEvent]) -> None:
    """At the limit, an exit a poll already reported pauses nothing and warns nobody."""

    notices = ProcessExitNotices(_Reported({"seen"}), published.append)
    _use_up_wakes(notices)

    notices.record(_exit("seen", notify=True))

    assert notices.take_wake() is False
    assert not any(isinstance(event, WarningEvent) for event in published)
