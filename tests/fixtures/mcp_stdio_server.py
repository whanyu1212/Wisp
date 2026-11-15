from __future__ import annotations

import atexit
import json
import os
import sys
from pathlib import Path

from mcp.server.mcpserver import MCPServer
from mcp.types import ToolAnnotations

server = MCPServer("wisp-stdio-test")
# Tool name to the `readOnlyHint` the server sends for it, as JSON; unlisted tools
# send no annotations.
_READ_ONLY_HINTS: dict[str, bool | None] = json.loads(
    os.environ.get("WISP_MCP_TEST_READ_ONLY_HINTS", "{}")
)


def _annotations(name: str) -> ToolAnnotations | None:
    if name not in _READ_ONLY_HINTS:
        return None
    return ToolAnnotations(read_only_hint=_READ_ONLY_HINTS[name])


@server.tool(annotations=_annotations("echo"))
def echo(value: str) -> str:
    return f"echo={value}"


@server.tool(annotations=_annotations("runtime_environment"))
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


# Optional tools whose names Wisp cannot use as they are, so their Wisp names are
# hashed, for tests that map names back to the server's own.
if os.environ.get("WISP_MCP_TEST_ODD_NAMES"):

    @server.tool(name="docs.search", annotations=_annotations("docs.search"))
    def docs_search() -> str:
        return "docs"

    @server.tool(name="x" * 129, annotations=ToolAnnotations(read_only_hint=True))
    def overlong() -> str:
        return "overlong"


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
