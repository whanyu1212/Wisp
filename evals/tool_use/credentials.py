"""Find provider credentials for eval runs without letting settings leak in.

Wisp itself does not read `.env` files. The runner does, but passes on only the
provider key names below: a `.env` may also set `WISP_*` variables, such as a
model or a session folder, that would silently change the setup under test.
Values are never logged or written to results.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Literal

from wisp.auth.storage import AuthStorageError, JsonAuthStore

REPO_ROOT = Path(__file__).resolve().parents[2]
DEFAULT_ENV_FILE = REPO_ROOT / ".env"
DEFAULT_AUTH_FILE = Path.home() / ".wisp" / "auth.json"

# The environment variable each provider reads its API key from. Providers not
# listed here, such as `openai-codex`, sign in through `auth.json` only.
PROVIDER_KEY_VARIABLES: Mapping[str, str] = {"openai": "OPENAI_API_KEY"}
# Providers that need no credential; the runner's own tests use `fake`.
CREDENTIAL_FREE_PROVIDERS = frozenset({"fake"})

type CredentialSource = Literal["environment", "stored", "none"]


class CredentialError(RuntimeError):
    """A requested provider has no credential, or a credential file is unreadable."""


@dataclass(frozen=True, slots=True)
class Credentials:
    """What each eval run needs to sign in.

    Attributes:
        auth_file (Path): Wisp's credential file, passed as `WISP_AUTH_FILE`.
        environment (Mapping[str, str]): Provider key variables to set in each run.
        sources (Mapping[str, CredentialSource]): Where each provider's credential
            comes from, recorded in results instead of the value.
    """

    auth_file: Path
    environment: Mapping[str, str] = field(default_factory=dict)
    sources: Mapping[str, CredentialSource] = field(default_factory=dict)


def read_provider_keys(path: Path) -> dict[str, str]:
    """Read only the provider key variables from a `.env` file.

    Lines are `NAME=value`, optionally prefixed with `export`. A value may be
    quoted with single or double quotes; an unquoted value ends at ` #`, which
    starts a comment. Blank lines and `#` comment lines are skipped.

    Args:
        path (Path): The `.env` file; a missing file yields no keys.

    Returns:
        dict[str, str]: Allowlisted names with non-empty values.

    Raises:
        CredentialError: When the file is not UTF-8 text.
    """

    if not path.is_file():
        return {}
    try:
        text = path.read_text(encoding="utf-8-sig")
    except UnicodeDecodeError as err:
        raise CredentialError(f"{path} is not UTF-8 text") from err
    allowed = set(PROVIDER_KEY_VARIABLES.values())
    keys: dict[str, str] = {}
    for raw_line in text.splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        name, _, value = line.partition("=")
        words = name.split()
        if len(words) == 2 and words[0] == "export":
            words = words[1:]
        if len(words) != 1 or words[0] not in allowed:
            continue
        parsed = _dotenv_value(value.strip())
        if parsed:
            keys[words[0]] = parsed
    return keys


def _dotenv_value(value: str) -> str:
    if value[:1] in {"'", '"'}:
        closing = value.find(value[0], 1)
        return value[1:closing] if closing > 0 else ""
    comment = value.find(" #")
    return (value[:comment] if comment >= 0 else value).strip()


def resolve_credentials(
    providers: Iterable[str],
    *,
    env_file: Path = DEFAULT_ENV_FILE,
    auth_file: Path = DEFAULT_AUTH_FILE,
    environ: Mapping[str, str],
) -> Credentials:
    """Decide where each provider's credential comes from, the way Wisp would.

    A provider key variable already set in ``environ`` wins over ``env_file``,
    and either wins over a key stored in ``auth_file``, matching the OpenAI
    provider's own order.

    Args:
        providers (Iterable[str]): Providers the runs will use.
        env_file (Path): Optional `.env` file to read provider keys from.
        auth_file (Path): Wisp's credential file.
        environ (Mapping[str, str]): The runner's own environment.

    Returns:
        Credentials: The auth file, the key variables to pass, and each source.

    Raises:
        CredentialError: When a provider has no credential or `auth_file` is
            unreadable.
    """

    file_keys = read_provider_keys(env_file)
    environment: dict[str, str] = {}
    sources: dict[str, CredentialSource] = {}
    stored = _stored_providers(auth_file)
    missing: list[str] = []
    for provider in sorted(set(providers)):
        variable = PROVIDER_KEY_VARIABLES.get(provider)
        value = (environ.get(variable) or file_keys.get(variable)) if variable else None
        if provider in CREDENTIAL_FREE_PROVIDERS:
            sources[provider] = "none"
        elif variable is not None and value:
            environment[variable] = value
            sources[provider] = "environment"
        elif provider in stored:
            sources[provider] = "stored"
        else:
            missing.append(provider)
    if missing:
        raise CredentialError(
            f"no credential for {', '.join(missing)}: sign in with `wisp auth`"
            + (
                " or set its API key in .env"
                if any(p in PROVIDER_KEY_VARIABLES for p in missing)
                else ""
            )
        )
    return Credentials(auth_file=auth_file, environment=environment, sources=sources)


def _stored_providers(auth_file: Path) -> frozenset[str]:
    if not auth_file.is_file():
        return frozenset()
    try:
        return frozenset(JsonAuthStore(auth_file).providers())
    except AuthStorageError as err:
        raise CredentialError(f"cannot read credentials from {auth_file}: {err}") from err
