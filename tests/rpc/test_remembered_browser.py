"""Check browser cookie persistence through independent Wisp RPC hosts."""

from __future__ import annotations

import os
import sys
import threading
from collections.abc import AsyncIterator
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

import anyio
import pytest

from tests.support.live_browser import keychain_safe_chrome_path
from wisp.events import KnownWispEvent, RpcCommandFinished
from wisp.rpc.client import JsonlSubprocessRpcTransport
from wisp.rpc.commands import BrowserInteractCommand


@pytest.mark.process
@pytest.mark.skipif(
    os.environ.get("WISP_TEST_LIVE_BROWSER") != "1",
    reason="set WISP_TEST_LIVE_BROWSER=1 on a host with Chrome or Chromium",
)
def test_cookie_survives_until_cleared(tmp_path: Path) -> None:
    env = dict(os.environ, HOME=str(tmp_path), USERPROFILE=str(tmp_path), WISP_TRUST="1")
    env["PATH"] = keychain_safe_chrome_path(tmp_path, env["PATH"])
    project = tmp_path / "project"
    project.mkdir()
    observed: list[tuple[str, bool]] = []

    class Page(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            self.send_response(200)
            if self.path == "/set":
                self.send_header("Set-Cookie", "remembered=1; Max-Age=3600; Path=/; HttpOnly")
            if self.path in {"/set", "/reopened", "/remembered", "/cleared"}:
                observed.append((self.path, self.headers.get("Cookie") == "remembered=1"))
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"<!doctype html><title>Profile test</title>")

        def log_message(self, format: str, *args: object) -> None:
            pass

    clear = [sys.executable, "-m", "wisp", "browser", "clear", "--yes"]

    async def run_command(
        transport: JsonlSubprocessRpcTransport,
        events: AsyncIterator[KnownWispEvent],
        command: BrowserInteractCommand,
    ) -> None:
        await transport.send(command)
        async for event in events:
            if isinstance(event, RpcCommandFinished) and event.command_id == command.id:
                assert event.ok, event.error
                return
        pytest.fail("The RPC host exited before completing the browser command")

    async def open_in_new_host(url: str, *, reopen: str | None = None) -> None:
        transport = await JsonlSubprocessRpcTransport.start(
            [sys.executable, "-m", "wisp", "--mode", "rpc", "--provider", "fake"],
            env=env,
            cwd=project,
        )
        events = transport.events()
        try:
            await run_command(
                transport, events, BrowserInteractCommand(id="open", action="live_open", url=url)
            )
            if reopen:
                result = await anyio.run_process(clear, env=env, cwd=project, check=False)
                assert result.returncode != 0 and b"in use" in result.stderr.lower()
                await run_command(
                    transport, events, BrowserInteractCommand(id="close", action="live_close")
                )
                await run_command(
                    transport,
                    events,
                    BrowserInteractCommand(id="reopen", action="live_open", url=reopen),
                )
        finally:
            await transport.close()

    async def check(base: str) -> None:
        await open_in_new_host(base + "/set", reopen=base + "/reopened")
        await open_in_new_host(base + "/remembered")
        assert observed == [("/set", False), ("/reopened", True), ("/remembered", True)]
        await anyio.run_process(clear, env=env, cwd=project)
        await open_in_new_host(base + "/cleared")
        assert observed[-1] == ("/cleared", False)

    with ThreadingHTTPServer(("127.0.0.1", 0), Page) as server:
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            anyio.run(check, f"http://127.0.0.1:{server.server_port}")
        finally:
            server.shutdown()
            thread.join(timeout=2)
