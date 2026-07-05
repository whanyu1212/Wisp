"""Private credential storage for Wisp providers."""

from __future__ import annotations

import json
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import Literal

PRIVATE_DIR_MODE = 0o700
PRIVATE_FILE_MODE = 0o600


class AuthStorageError(RuntimeError):
    """Raised when Wisp auth credentials cannot be read or written."""


@dataclass(frozen=True)
class ApiKeyCredential:
    """Persisted API key credential."""

    key: str
    type: Literal["api_key"] = "api_key"

    def to_json(self) -> dict[str, object]:
        return {"type": self.type, "key": self.key}


@dataclass(frozen=True)
class OAuthCredential:
    """Persisted OAuth credential.

    ``expires`` is milliseconds since the Unix epoch, matching the credential
    shape used by Codex-compatible auth flows.
    """

    access: str
    refresh: str
    expires: int
    account_id: str | None = None
    type: Literal["oauth"] = "oauth"

    def to_json(self) -> dict[str, object]:
        data: dict[str, object] = {
            "type": self.type,
            "access": self.access,
            "refresh": self.refresh,
            "expires": self.expires,
        }
        if self.account_id is not None:
            data["accountId"] = self.account_id
        return data


type AuthCredential = ApiKeyCredential | OAuthCredential


class JsonAuthStore:
    """JSON-backed credential store with private file permissions."""

    def __init__(self, path: Path) -> None:
        self.path = path.expanduser()

    def get(self, provider: str) -> AuthCredential | None:
        return self._load().get(provider)

    def providers(self) -> tuple[str, ...]:
        return tuple(sorted(self._load()))

    def set(self, provider: str, credential: AuthCredential) -> None:
        credentials = self._load()
        credentials[provider] = credential
        self._save(credentials)

    def delete(self, provider: str) -> bool:
        credentials = self._load()
        existed = provider in credentials
        if existed:
            del credentials[provider]
            self._save(credentials)
        return existed

    def _load(self) -> dict[str, AuthCredential]:
        if not self.path.exists():
            return {}
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise AuthStorageError(f"Invalid auth file JSON: {self.path}") from exc
        except OSError as exc:
            raise AuthStorageError(f"Could not read auth file: {self.path}") from exc
        if not isinstance(raw, dict):
            raise AuthStorageError(f"Auth file must contain a JSON object: {self.path}")
        return {
            str(provider): _credential_from_json(value, provider=str(provider))
            for provider, value in raw.items()
        }

    def _save(self, credentials: dict[str, AuthCredential]) -> None:
        _ensure_private_parent(self.path)
        payload = {
            provider: credential.to_json() for provider, credential in sorted(credentials.items())
        }
        tmp_path = self.path.with_name(f".{self.path.name}.tmp")
        try:
            tmp_path.write_text(
                json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
            )
            if os.name == "posix":
                tmp_path.chmod(PRIVATE_FILE_MODE)
            tmp_path.replace(self.path)
            if os.name == "posix":
                self.path.chmod(PRIVATE_FILE_MODE)
        except OSError as exc:
            raise AuthStorageError(f"Could not write auth file: {self.path}") from exc


def _credential_from_json(value: object, *, provider: str) -> AuthCredential:
    if not isinstance(value, dict):
        raise AuthStorageError(f"Invalid credential for provider {provider}: expected object")
    credential_type = value.get("type")
    if credential_type == "api_key":
        key = value.get("key")
        if not isinstance(key, str) or not key:
            raise AuthStorageError(f"Invalid API key credential for provider {provider}")
        return ApiKeyCredential(key=key)
    if credential_type == "oauth":
        access = value.get("access")
        refresh = value.get("refresh")
        expires = value.get("expires")
        account_id = value.get("accountId", value.get("account_id"))
        if not isinstance(access, str) or not access:
            raise AuthStorageError(f"Invalid OAuth access token for provider {provider}")
        if not isinstance(refresh, str) or not refresh:
            raise AuthStorageError(f"Invalid OAuth refresh token for provider {provider}")
        if not isinstance(expires, int):
            raise AuthStorageError(f"Invalid OAuth expiry for provider {provider}")
        if account_id is not None and not isinstance(account_id, str):
            raise AuthStorageError(f"Invalid OAuth account id for provider {provider}")
        return OAuthCredential(
            access=access,
            refresh=refresh,
            expires=expires,
            account_id=account_id,
        )
    raise AuthStorageError(
        f"Unsupported credential type for provider {provider}: {credential_type}"
    )


def _ensure_private_parent(path: Path) -> None:
    parent = path.parent
    try:
        parent.mkdir(mode=PRIVATE_DIR_MODE, parents=True, exist_ok=True)
    except OSError as exc:
        raise AuthStorageError(f"Could not create auth directory: {parent}") from exc
    if os.name != "posix":
        return
    try:
        info = parent.lstat()
    except OSError as exc:
        raise AuthStorageError(f"Could not inspect auth directory: {parent}") from exc
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
        raise AuthStorageError(f"Auth directory is not a directory: {parent}")
    if stat.S_IMODE(info.st_mode) & 0o077:
        parent.chmod(PRIVATE_DIR_MODE)
        info = parent.lstat()
        if stat.S_IMODE(info.st_mode) & 0o077:
            raise AuthStorageError(f"Auth directory is not private: {parent}")


__all__ = [
    "ApiKeyCredential",
    "AuthCredential",
    "AuthStorageError",
    "JsonAuthStore",
    "OAuthCredential",
]
