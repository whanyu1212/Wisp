from __future__ import annotations

import atexit
import json
import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer

server = MCPServer("wisp-stdio-test")


@server.tool()
def echo(value: str) -> str:
    return f"echo={value}"


@server.tool()
def runtime_environment() -> str:
    return json.dumps(
        {
            "cwd": os.getcwd(),
            "literal": os.environ.get("WISP_MCP_TEST_LITERAL"),
            "forwarded": os.environ.get("WISP_MCP_TEST_FORWARDED"),
            "trap": os.environ.get("WISP_MCP_TEST_TRAP"),
        },
        sort_keys=True,
    )


def _record_close() -> None:
    if path := os.environ.get("WISP_MCP_TEST_CLOSED_FILE"):
        Path(path).write_text("closed", encoding="utf-8")


if __name__ == "__main__":
    atexit.register(_record_close)
    sys.stderr.write(os.environ.get("WISP_MCP_TEST_STDERR", "stdio fixture started"))
    sys.stderr.flush()
    mode = sys.argv[1] if len(sys.argv) > 1 else "server"
    if mode == "invalid-frame":
        sys.stdout.write(
            json.dumps({"credential": os.environ.get("WISP_MCP_TEST_FRAME_SECRET")}) + "\n"
        )
        sys.stdout.flush()
    elif mode == "oversized-frame":
        frame_bytes = int(os.environ["WISP_MCP_TEST_FRAME_BYTES"])
        sys.stdout.buffer.write(b"x" * frame_bytes)
        sys.stdout.buffer.flush()
    else:
        server.run()
