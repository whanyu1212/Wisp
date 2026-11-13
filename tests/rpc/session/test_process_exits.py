"""Background-process exits published to frontends and held for the next prompt."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta

import pytest

from wisp.events import ProcessEnded, WispEvent
from wisp.rpc.session.process_exits import ProcessExitNotices
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
