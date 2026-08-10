"""Plain-text presentation helpers for MCP runtime status."""

from __future__ import annotations

from wisp.events import RpcMcpStatusSnapshot


def mcp_status_text(status: RpcMcpStatusSnapshot) -> str:
    """Render sanitized MCP status without terminal or Markdown markup."""

    lines = [f"MCP servers ({len(status.servers)})"]
    if not status.servers:
        lines.extend(
            (
                "No MCP servers configured.",
                "Configure servers in ~/.wisp/settings.json and restart Wisp.",
            )
        )
        return "\n".join(lines)

    for server in status.servers:
        if server.status == "unavailable":
            lines.append(f"{server.name}: unavailable - {server.error}")
            continue
        if server.status == "disconnected":
            if not server.tool_names:
                lines.append(f"{server.name}: disconnected (no tools discovered)")
                continue
            count = len(server.tool_names)
            noun = "tool" if count == 1 else "tools"
            lines.append(f"{server.name}: disconnected ({count} registered {noun})")
            lines.extend(f"  {name}" for name in server.tool_names)
            continue
        count = len(server.tool_names)
        noun = "tool" if count == 1 else "tools"
        lines.append(f"{server.name}: connected ({count} {noun})")
        lines.extend(f"  {name}" for name in server.tool_names)
    return "\n".join(lines)


__all__ = ["mcp_status_text"]
