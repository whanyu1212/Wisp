"""Saved OAuth credentials for HTTP MCP servers.

Kept apart from :mod:`wisp.mcp.oauth` so settings code can forget a server's
credential without importing the MCP SDK.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from pydantic import ValidationError

from wisp.auth.storage import PrivateJsonFile
from wisp.mcp.config import McpServerConfig

if TYPE_CHECKING:
    from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

MCP_AUTH_PATH = Path("~/.wisp/mcp-auth.json")


def mcp_auth_path(*, home_dir: Path | None = None) -> Path:
    """Return the MCP credential file under ``home_dir`` (default: the user's home)."""

    if home_dir is None:
        return MCP_AUTH_PATH.expanduser()
    return home_dir / ".wisp" / "mcp-auth.json"


@dataclass(frozen=True, slots=True)
class McpCredential:
    """OAuth tokens and client registration for one server URL.

    Attributes:
        server_url (str): The URL the tokens were issued for.
        tokens (OAuthToken): Access and refresh tokens.
        expires_at (float | None): When the access token expires, in seconds
            since the epoch, or None when the server gave no lifetime.
        client_info (OAuthClientInformationFull): The client registration a
            refresh must present.
        authorization_server (OAuthMetadata | None): The authorization server's
            metadata. A refresh posts to its token endpoint, which is often on
            another host than the MCP server.
    """

    server_url: str
    tokens: OAuthToken
    expires_at: float | None
    client_info: OAuthClientInformationFull
    authorization_server: OAuthMetadata | None = None


class McpTokenStore:
    """MCP OAuth credentials, by server name, in a private file.

    The file is separate from ``auth.json`` so a Wisp release that does not know
    these entries still reads provider credentials.
    """

    def __init__(self, path: Path | None = None) -> None:
        self._file = PrivateJsonFile(path or mcp_auth_path(), description="MCP auth file")

    @property
    def path(self) -> Path:
        """The credential file."""

        return self._file.path

    def load(self, name: str, server_url: str) -> McpCredential | None:
        """Return the credential saved for ``name``, if it was issued for ``server_url``.

        Args:
            name (str): Server name.
            server_url (str): The server's configured URL.

        Returns:
            McpCredential | None: The credential, or None when none is saved, it was
            issued for another URL, or the entry is unreadable.

        Raises:
            AuthStorageError: The file itself cannot be read.
        """

        from mcp.shared.auth import OAuthClientInformationFull, OAuthMetadata, OAuthToken

        entry = self._file.read().get(name)
        if not isinstance(entry, dict) or entry.get("server_url") != server_url:
            return None
        try:
            expires_at = entry.get("expires_at")
            metadata = entry.get("authorization_server")
            return McpCredential(
                server_url=server_url,
                tokens=OAuthToken.model_validate(entry.get("tokens")),
                expires_at=float(expires_at) if isinstance(expires_at, int | float) else None,
                client_info=OAuthClientInformationFull.model_validate(entry.get("client_info")),
                authorization_server=(
                    OAuthMetadata.model_validate(metadata) if metadata is not None else None
                ),
            )
        except ValidationError:
            return None

    def save(self, name: str, credential: McpCredential) -> None:
        """Save ``credential`` for ``name``, replacing any earlier one."""

        with self._file.lock():
            credentials = self._file.read_unlocked()
            credentials[name] = _entry(credential)
            self._file.write_unlocked(credentials)

    def replace(self, name: str, *, expected: McpCredential, replacement: McpCredential) -> bool:
        """Save a refreshed credential only if ``expected`` is still the saved one.

        A refresh can finish after the user signed out, removed the server, or
        signed in again in another Wisp process; it must not undo that.

        Returns:
            bool: Whether the credential was replaced.
        """

        with self._file.lock():
            credentials = self._file.read_unlocked()
            if credentials.get(name) != _entry(expected):
                return False
            credentials[name] = _entry(replacement)
            self._file.write_unlocked(credentials)
            return True

    def delete(self, name: str) -> bool:
        """Forget ``name``'s credential.

        Returns:
            bool: Whether a credential was saved.
        """

        with self._file.lock():
            credentials = self._file.read_unlocked()
            if name not in credentials:
                return False
            del credentials[name]
            self._file.write_unlocked(credentials)
            return True


def _entry(credential: McpCredential) -> dict[str, object]:
    return {
        "server_url": credential.server_url,
        "tokens": credential.tokens.model_dump(mode="json", exclude_none=True),
        "expires_at": credential.expires_at,
        "client_info": credential.client_info.model_dump(mode="json", exclude_none=True),
        "authorization_server": (
            credential.authorization_server.model_dump(mode="json", exclude_none=True)
            if credential.authorization_server is not None
            else None
        ),
    }


def uses_header_authorization(server: McpServerConfig) -> bool:
    """Return whether ``server`` already sends an Authorization header itself."""

    names = (name for name, _ in (*server.headers, *server.headers_from))
    return any(name.casefold() == "authorization" for name in names)


__all__ = [
    "MCP_AUTH_PATH",
    "McpCredential",
    "McpTokenStore",
    "mcp_auth_path",
    "uses_header_authorization",
]
