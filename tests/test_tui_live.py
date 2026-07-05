# ruff: noqa: F403,F405

from __future__ import annotations

from tests.tui_support import *


def test_live_fullscreen_tui_refreshes_streaming_token_deltas() -> None:
    class FakeApplication:
        is_done = False

        def __init__(self) -> None:
            self.invalidations = 0

        def invalidate(self) -> None:
            self.invalidations += 1

    renderer = LiveFullscreenTui(run_application=False)
    app = FakeApplication()
    renderer._application = app

    renderer.token_delta("hel")
    renderer.token_delta("lo")

    assert renderer.state.streaming_text == "hello"
    assert app.invalidations == 2


def test_live_fullscreen_tui_registers_transcript_scroll_keybindings() -> None:
    from prompt_toolkit.keys import Keys

    renderer = LiveFullscreenTui(run_application=False)

    assert (Keys.PageUp,) in {binding.keys for binding in renderer._key_bindings.bindings}
    assert (Keys.PageDown,) in {binding.keys for binding in renderer._key_bindings.bindings}


def test_live_fullscreen_tui_scrolls_visible_transcript_and_refreshes() -> None:
    class FakeApplication:
        is_done = False

        def __init__(self) -> None:
            self.invalidations = 0

        def invalidate(self) -> None:
            self.invalidations += 1

    renderer = LiveFullscreenTui(run_application=False)
    renderer.state.transcript_view_entries = 2
    renderer._application = FakeApplication()
    for index in range(4):
        renderer.event(AssistantMessage(content=f"message {index}"))

    renderer.scroll_transcript_top()

    rendered = "".join(fragment for _style, fragment in renderer._transcript_fragments())
    assert "message 0" in rendered
    assert "message 1" in rendered
    assert "message 3" not in rendered
    assert renderer._application.invalidations == 5


def test_live_fullscreen_tui_sizes_latest_view_to_terminal_rows() -> None:
    class FakeSize:
        rows = 13

    class FakeOutput:
        def get_size(self) -> FakeSize:
            return FakeSize()

    class FakeApplication:
        is_done = False
        output = FakeOutput()

        def invalidate(self) -> None:
            pass

    renderer = LiveFullscreenTui(run_application=False)
    renderer._application = FakeApplication()
    for index in range(6):
        renderer.event(AssistantMessage(content=f"message {index}"))

    assert renderer._transcript_view_entries() == 3
    rendered = "".join(fragment for _style, fragment in renderer._transcript_fragments())
    assert "message 0" not in rendered
    assert "message 2" not in rendered
    assert "message 3" in rendered
    assert "message 4" in rendered
    assert "message 5" in rendered


def test_live_fullscreen_tui_paginates_multiline_wrapped_entry_by_rows() -> None:
    class FakeSize:
        rows = 13
        columns = 16

    class FakeOutput:
        def get_size(self) -> FakeSize:
            return FakeSize()

    class FakeApplication:
        is_done = False
        output = FakeOutput()

        def invalidate(self) -> None:
            pass

    renderer = LiveFullscreenTui(run_application=False)
    renderer._application = FakeApplication()

    renderer.event(AssistantMessage(content="abcdef\nghijklmnopqrstuv"))

    assert renderer._transcript_view_entries() == 3
    assert renderer._max_transcript_scroll_offset() == 1
    rendered = "".join(fragment for _style, fragment in renderer._transcript_fragments())
    assert "abc" not in rendered
    assert "def" in rendered
    assert "ghijklmnopqrst" in rendered
    assert "uv" in rendered

    renderer.scroll_transcript_up(1)

    rendered = "".join(fragment for _style, fragment in renderer._transcript_fragments())
    assert "abc" in rendered
    assert "def" in rendered
    assert "ghijklmnopqrst" in rendered
    assert "uv" not in rendered


def test_live_fullscreen_tui_preserves_scrolled_view_during_streaming() -> None:
    renderer = LiveFullscreenTui(run_application=False)
    renderer.state.transcript_view_entries = 2
    for index in range(4):
        renderer.event(AssistantMessage(content=f"message {index}"))
    renderer.scroll_transcript_up(1)

    renderer.token_delta("stream")

    rendered = "".join(fragment for _style, fragment in renderer._transcript_fragments())
    assert "message 1" in rendered
    assert "message 2" in rendered
    assert "message 3" not in rendered
    assert "stream" not in rendered

    renderer.scroll_transcript_bottom()

    rendered = "".join(fragment for _style, fragment in renderer._transcript_fragments())
    assert "message 3" in rendered
    assert "stream" in rendered


def test_live_fullscreen_tui_accepts_submitted_input() -> None:
    async def run() -> None:
        renderer = LiveFullscreenTui(run_application=False)
        read_task = asyncio.create_task(renderer.read_prompt("wisp> "))
        await anyio.sleep(0)

        renderer._buffer.insert_text("hello")
        renderer._accept_input()

        assert await read_task == "hello"

    anyio.run(run)


def test_live_fullscreen_tui_queues_submission_accepted_between_reads() -> None:
    async def run() -> None:
        renderer = LiveFullscreenTui(run_application=False)
        renderer.view_updated(
            TuiViewSnapshot(
                status="running",
                input_hint="wisp(running)> ",
                input_mode="running",
            )
        )
        first_read = asyncio.create_task(renderer.read_prompt("wisp(running)> "))
        await anyio.sleep(0)

        renderer._buffer.insert_text("first")
        renderer._accept_input()
        renderer._buffer.insert_text("second")
        renderer._accept_input()

        assert await first_read == "first"
        assert renderer.consume_submitted_input_mode("idle") == "running"
        assert await renderer.read_prompt("wisp(running)> ") == "second"
        assert renderer.consume_submitted_input_mode("idle") == "running"

    anyio.run(run)


def test_live_fullscreen_tui_splits_bracketed_paste_into_submissions() -> None:
    async def run() -> None:
        renderer = LiveFullscreenTui(run_application=False)
        renderer.view_updated(
            TuiViewSnapshot(
                status="running",
                input_hint="wisp(running)> ",
                input_mode="running",
            )
        )
        first_read = asyncio.create_task(renderer.read_prompt("wisp(running)> "))
        await anyio.sleep(0)

        renderer._paste_input("first\nsecond\nthird")

        assert await first_read == "first"
        assert renderer.consume_submitted_input_mode("idle") == "running"
        assert await renderer.read_prompt("wisp(running)> ") == "second"
        assert renderer.consume_submitted_input_mode("idle") == "running"
        second_read = asyncio.create_task(renderer.read_prompt("wisp(running)> "))
        await anyio.sleep(0)
        assert renderer._buffer.text == "third"

        renderer._accept_input()

        assert await second_read == "third"
        assert renderer.consume_submitted_input_mode("idle") == "running"

    anyio.run(run)


def test_tui_shell_reads_live_submission_queued_between_reads() -> None:
    async def run() -> None:
        renderer = LiveFullscreenTui(run_application=False)
        shell = TuiShell(
            ScriptedController(), renderer=renderer, prompt_reader=renderer.read_prompt
        )
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.running
        shell._sync_view()
        send, receive = anyio.create_memory_object_stream[object](10)

        async with anyio.create_task_group() as task_group, send, receive:
            task_group.start_soon(shell._read_inputs, send.clone())
            await anyio.sleep(0)

            renderer._buffer.insert_text("first")
            renderer._accept_input()
            renderer._buffer.insert_text("second")
            renderer._accept_input()

            first = await receive.receive()
            second = await receive.receive()
            task_group.cancel_scope.cancel()

        assert isinstance(first, _InputLine)
        assert first.text == "first"
        assert first.mode is _InputMode.running
        assert isinstance(second, _InputLine)
        assert second.text == "second"
        assert second.mode is _InputMode.running

    anyio.run(run)


def test_live_fullscreen_tui_preserves_typed_buffer_between_reads() -> None:
    async def run() -> None:
        renderer = LiveFullscreenTui(run_application=False)
        renderer.view_updated(
            TuiViewSnapshot(
                status="running",
                input_hint="wisp(running)> ",
                input_mode="running",
            )
        )
        first_read = asyncio.create_task(renderer.read_prompt("wisp(running)> "))
        await anyio.sleep(0)

        renderer._buffer.insert_text("first")
        renderer._accept_input()
        renderer._buffer.insert_text("sec")

        assert await first_read == "first"
        assert renderer.consume_submitted_input_mode("idle") == "running"
        second_read = asyncio.create_task(renderer.read_prompt("wisp(running)> "))
        await anyio.sleep(0)
        assert renderer._buffer.text == "sec"

        renderer._buffer.insert_text("ond")
        renderer._accept_input()

        assert await second_read == "second"
        assert renderer.consume_submitted_input_mode("idle") == "running"

    anyio.run(run)


def test_live_fullscreen_tui_interrupts_input() -> None:
    async def run() -> None:
        renderer = LiveFullscreenTui(run_application=False)
        read_task = asyncio.create_task(renderer.read_prompt("wisp> "))
        await anyio.sleep(0)

        renderer._interrupt_input()

        try:
            await read_task
        except LiveFullscreenInputInterrupted:
            pass
        else:  # pragma: no cover - defensive assertion branch
            raise AssertionError("expected KeyboardInterrupt")

    anyio.run(run)


def test_live_fullscreen_tui_closes_input() -> None:
    async def run() -> None:
        renderer = LiveFullscreenTui(run_application=False)
        read_task = asyncio.create_task(renderer.read_prompt("wisp> "))
        await anyio.sleep(0)

        renderer._close_input()

        try:
            await read_task
        except EOFError:
            pass
        else:  # pragma: no cover - defensive assertion branch
            raise AssertionError("expected EOFError")

    anyio.run(run)


def test_live_fullscreen_tui_close_ends_pending_input() -> None:
    async def run() -> None:
        renderer = LiveFullscreenTui(run_application=False)
        read_task = asyncio.create_task(renderer.read_prompt("wisp> "))
        await anyio.sleep(0)

        await renderer.close()

        try:
            await read_task
        except EOFError:
            pass
        else:  # pragma: no cover - defensive assertion branch
            raise AssertionError("expected EOFError")

    anyio.run(run)


def test_live_fullscreen_tui_close_cancels_stuck_application() -> None:
    class StuckApplication:
        is_done = False

        def exit(self) -> None:
            raise RuntimeError("not running yet")

    async def run() -> None:
        renderer = LiveFullscreenTui(run_application=False)
        renderer._application = StuckApplication()
        renderer._application_task = asyncio.create_task(anyio.sleep(10))

        await renderer.close()

        assert renderer._application_task.done()

    anyio.run(run)


def test_live_fullscreen_tui_retags_empty_submission_after_mode_change() -> None:
    async def run() -> None:
        renderer = LiveFullscreenTui(run_application=False)
        shell = TuiShell(
            ScriptedController(), renderer=renderer, prompt_reader=renderer.read_prompt
        )
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.running
        shell._sync_view()
        send, receive = anyio.create_memory_object_stream[object](10)

        async with anyio.create_task_group() as task_group, send, receive:
            task_group.start_soon(shell._read_inputs, send.clone())
            await anyio.sleep(0)

            shell.state.status = TuiStatus.waiting_for_approval
            shell.state.pending_approval = ToolApprovalRequested(
                call_id="call-1",
                name="bash",
                arguments={"command": "echo hi"},
                safety="command",
            )
            shell._sync_view()
            renderer._buffer.insert_text("y")
            renderer._accept_input()

            signal = await receive.receive()
            task_group.cancel_scope.cancel()

        assert isinstance(signal, _InputLine)
        assert signal.text == "y"
        assert signal.mode is _InputMode.approval

    anyio.run(run)


def test_live_fullscreen_tui_keeps_preexisting_text_mode_when_approval_arrives() -> None:
    async def run() -> None:
        controller = ScriptedController()
        renderer = LiveFullscreenTui(run_application=False)
        shell = TuiShell(controller, renderer=renderer, prompt_reader=renderer.read_prompt)
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.running
        shell._sync_view()
        send, receive = anyio.create_memory_object_stream[object](10)

        async with anyio.create_task_group() as task_group, send, receive:
            task_group.start_soon(shell._read_inputs, send.clone())
            await anyio.sleep(0)

            renderer._buffer.insert_text("run tests")
            shell.state.status = TuiStatus.waiting_for_approval
            shell.state.pending_approval = ToolApprovalRequested(
                call_id="call-1",
                name="bash",
                arguments={"command": "echo hi"},
                safety="command",
            )
            shell._sync_view()
            renderer._accept_input()

            signal = await receive.receive()
            task_group.cancel_scope.cancel()

        assert isinstance(signal, _InputLine)
        assert signal.text == "run tests"
        assert signal.mode is _InputMode.running

        should_exit = await shell._handle_input_line(signal)

        assert should_exit is False
        assert controller.approvals == []
        assert list(shell.state.queued_prompts) == ["run tests"]

    anyio.run(run)


def test_live_fullscreen_tui_retags_after_preexisting_text_is_cleared() -> None:
    async def run() -> None:
        renderer = LiveFullscreenTui(run_application=False)
        shell = TuiShell(
            ScriptedController(), renderer=renderer, prompt_reader=renderer.read_prompt
        )
        shell.state.current_command_id = "prompt-1"
        shell.state.status = TuiStatus.running
        shell._sync_view()
        send, receive = anyio.create_memory_object_stream[object](10)

        async with anyio.create_task_group() as task_group, send, receive:
            task_group.start_soon(shell._read_inputs, send.clone())
            await anyio.sleep(0)

            renderer._buffer.insert_text("run tests")
            shell.state.status = TuiStatus.waiting_for_approval
            shell.state.pending_approval = ToolApprovalRequested(
                call_id="call-1",
                name="bash",
                arguments={"command": "echo hi"},
                safety="command",
            )
            shell._sync_view()
            renderer._buffer.delete_before_cursor(count=len("run tests"))
            renderer._buffer.insert_text("y")
            renderer._accept_input()

            signal = await receive.receive()
            task_group.cancel_scope.cancel()

        assert isinstance(signal, _InputLine)
        assert signal.text == "y"
        assert signal.mode is _InputMode.approval

    anyio.run(run)


def test_live_fullscreen_tui_captures_mode_at_accept_time() -> None:
    async def run() -> None:
        renderer = LiveFullscreenTui(run_application=False)
        renderer.view_updated(
            TuiViewSnapshot(
                status="waiting for approval",
                input_hint="approve? [y/N] ",
                input_mode="approval",
            )
        )
        read_task = asyncio.create_task(renderer.read_prompt("approve? [y/N] "))
        await anyio.sleep(0)

        renderer._buffer.insert_text("y")
        renderer._accept_input()
        renderer.view_updated(
            TuiViewSnapshot(
                status="running",
                input_hint="wisp(running)> ",
                input_mode="running",
            )
        )

        assert await read_task == "y"
        assert renderer.consume_submitted_input_mode("running") == "approval"

    anyio.run(run)


def test_live_fullscreen_tui_captures_mode_for_interrupt_and_close() -> None:
    async def captured_mode_for(action: str) -> str:
        renderer = LiveFullscreenTui(run_application=False)
        renderer.view_updated(
            TuiViewSnapshot(
                status="running",
                input_hint="wisp(running)> ",
                input_mode="running",
            )
        )
        read_task = asyncio.create_task(renderer.read_prompt("wisp(running)> "))
        await anyio.sleep(0)

        renderer._buffer.insert_text("run tests")
        renderer.view_updated(
            TuiViewSnapshot(
                status="waiting for approval",
                input_hint="approve? [y/N] ",
                input_mode="approval",
            )
        )
        if action == "interrupt":
            renderer._interrupt_input()
            expected_exception: type[BaseException] = LiveFullscreenInputInterrupted
        else:
            renderer._close_input()
            expected_exception = EOFError

        try:
            await read_task
        except expected_exception:
            pass
        else:  # pragma: no cover - defensive assertion branch
            raise AssertionError(f"expected {expected_exception.__name__}")
        return renderer.consume_submitted_input_mode("approval")

    async def run() -> None:
        assert await captured_mode_for("interrupt") == "running"
        assert await captured_mode_for("close") == "running"

    anyio.run(run)
