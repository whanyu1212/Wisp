"""Cached release checks and explicit Wisp updates through PyPI."""

from __future__ import annotations

import json
import os
import platform
import sys
import tempfile
import time
from collections.abc import Awaitable, Callable, Mapping
from dataclasses import dataclass
from importlib import metadata
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

import anyio
import httpx
from packaging.specifiers import InvalidSpecifier, SpecifierSet
from packaging.utils import canonicalize_name
from packaging.version import InvalidVersion, Version

from wisp import __version__

PYPI_URL = "https://pypi.org/pypi/wisp-ai/json"
PYPI_INDEX_URL = "https://pypi.org/simple"
UPDATE_COMMAND_TEMPLATE = "wisp update"
CACHE_TTL_SECONDS = 6 * 60 * 60
HTTP_TIMEOUT_SECONDS = 2.0

_DISTRIBUTION_NAME = "wisp-ai"
_CACHE_FILENAME = "update-check.json"

type UpdateCommandRunner = Callable[[tuple[str, ...]], Awaitable[None]]
type UpdateInstallVerifier = Callable[[], Awaitable[None]]


@dataclass(frozen=True, slots=True)
class UpdateAvailable:
    """A newer compatible Wisp release available from PyPI."""

    current_version: str
    latest_version: str
    update_command: str


@dataclass(frozen=True, slots=True)
class UpdateStatus:
    """The installed and latest compatible Wisp releases."""

    current_version: str
    latest_version: str

    @property
    def available(self) -> UpdateAvailable | None:
        installed = Version(self.current_version)
        latest = Version(self.latest_version)
        if latest <= installed:
            return None
        return UpdateAvailable(
            current_version=str(installed),
            latest_version=str(latest),
            update_command=UPDATE_COMMAND_TEMPLATE.format(version=latest),
        )


class UpdateCheckError(RuntimeError):
    """An explicit update check could not produce a trustworthy result."""


class UpdateInstallError(RuntimeError):
    """A requested Wisp update could not be installed."""


async def check_for_update(
    *,
    enabled: bool = True,
    current_version: str = __version__,
    home_dir: Path | None = None,
    now: float | None = None,
    python_version: str | None = None,
    local_install_detector: Callable[[], bool] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
) -> UpdateAvailable | None:
    """Return an available update, or ``None`` when no notification should be shown.

    This function is deliberately best-effort: local metadata, cache, network, and
    response errors are all suppressed so an update check can never block startup.
    """

    if not enabled:
        return None

    try:
        status = await get_update_status(
            current_version=current_version,
            home_dir=home_dir,
            now=now,
            python_version=python_version,
            local_install_detector=local_install_detector,
            transport=transport,
            use_cache=True,
        )
        return status.available
    except Exception:
        return None


async def get_update_status(
    *,
    current_version: str = __version__,
    home_dir: Path | None = None,
    now: float | None = None,
    python_version: str | None = None,
    local_install_detector: Callable[[], bool] | None = None,
    transport: httpx.AsyncBaseTransport | None = None,
    use_cache: bool = False,
) -> UpdateStatus:
    """Return explicit release status, bypassing the six-hour cache by default."""

    try:
        detector = local_install_detector or is_local_install
        if await anyio.to_thread.run_sync(detector):
            raise UpdateCheckError(
                "Wisp is installed from a local source; update it through that source instead."
            )

        installed = Version(current_version)
        interpreter = Version(python_version or platform.python_version())
        checked_at = time.time() if now is None else now
        cache_path = _cache_path(home_dir=home_dir)
        releases: tuple[str, ...] | None = None
        if use_cache:
            try:
                releases = await anyio.to_thread.run_sync(
                    _read_cache,
                    cache_path,
                    checked_at,
                    interpreter,
                )
            except Exception:
                releases = None
        if releases is None:
            releases = await _fetch_releases(
                transport=transport,
                python_version=interpreter,
            )
            try:
                await anyio.to_thread.run_sync(
                    _write_cache,
                    cache_path,
                    checked_at,
                    interpreter,
                    releases,
                )
            except Exception:
                pass

        latest = _latest_compatible_version(installed, releases)
        if latest is None:
            raise UpdateCheckError("PyPI returned no compatible Wisp releases.")
        return UpdateStatus(
            current_version=str(installed),
            latest_version=str(latest),
        )
    except UpdateCheckError:
        raise
    except Exception as exc:
        raise UpdateCheckError("Could not check PyPI for Wisp updates.") from exc


async def install_update(
    update: UpdateAvailable,
    *,
    runner: UpdateCommandRunner | None = None,
    install_verifier: UpdateInstallVerifier | None = None,
) -> None:
    """Install one exact newer release through Wisp's supported uv-tool path."""

    try:
        current = Version(update.current_version)
        latest = Version(update.latest_version)
    except InvalidVersion as exc:
        raise UpdateInstallError("The requested Wisp update version is invalid.") from exc
    if latest <= current:
        raise UpdateInstallError("The requested Wisp version is not newer than this installation.")
    await (install_verifier or _require_uv_tool_install)()
    command = (
        "uv",
        "tool",
        "install",
        "--force",
        "--no-config",
        "--no-sources",
        "--default-index",
        PYPI_INDEX_URL,
        f"wisp-ai=={latest}",
    )
    # Once uv starts replacing the active tool environment, interruption risks
    # leaving the installation incomplete. Checks and verification remain cancellable.
    with anyio.CancelScope(shield=True):
        await (runner or _run_update_command)(command)


async def _require_uv_tool_install() -> None:
    try:
        result = await anyio.run_process(
            ("uv", "tool", "dir", "--no-config"),
            check=False,
            cwd=_safe_update_cwd(),
            env=_update_environment(),
        )
    except FileNotFoundError:
        raise UpdateInstallError("uv is not installed or is not available on PATH.") from None
    except OSError:
        raise UpdateInstallError("Could not verify this Wisp installation with uv.") from None
    if result.returncode != 0:
        raise UpdateInstallError("Could not verify this Wisp installation with uv.")
    raw_tools_dir = result.stdout.decode(errors="replace").strip()
    if not raw_tools_dir:
        raise UpdateInstallError("uv returned an invalid tools directory.")
    tools_dir = Path(raw_tools_dir).expanduser().resolve(strict=False)
    environment = Path(sys.prefix).resolve(strict=False)
    try:
        relative_environment = environment.relative_to(tools_dir)
    except ValueError:
        relative_environment = None
    if (
        relative_environment is None
        or len(relative_environment.parts) != 1
        or canonicalize_name(relative_environment.name) != canonicalize_name(_DISTRIBUTION_NAME)
    ):
        raise UpdateInstallError(
            "Automatic updates require a persistent uv tool installation; "
            "update this installation with its package manager instead."
        )


async def _run_update_command(command: tuple[str, ...]) -> None:
    try:
        result = await anyio.run_process(
            command,
            check=False,
            cwd=_safe_update_cwd(),
            env=_update_environment(),
        )
    except FileNotFoundError:
        raise UpdateInstallError("uv is not installed or is not available on PATH.") from None
    except OSError:
        raise UpdateInstallError("Could not start uv to update Wisp.") from None
    if result.returncode == 0:
        return
    detail = result.stderr.decode(errors="replace").strip()
    if detail:
        detail = detail.splitlines()[-1][:500]
        raise UpdateInstallError(f"uv failed to update Wisp (exit {result.returncode}): {detail}")
    raise UpdateInstallError(f"uv failed to update Wisp (exit {result.returncode}).")


def _safe_update_cwd() -> Path:
    return Path.home().expanduser().resolve(strict=False)


def _update_environment() -> dict[str, str]:
    # Keep custom persistent tool locations, but prevent project/shell-scoped uv
    # resolver settings from adding indexes, local links, sources, or constraints.
    retained_uv_names = {"UV_TOOL_BIN_DIR", "UV_TOOL_DIR"}
    return {
        name: value
        for name, value in os.environ.items()
        if not name.startswith("UV_") or name in retained_uv_names
    }


def is_local_install(
    *,
    direct_url_reader: Callable[[], str | None] | None = None,
) -> bool:
    """Return whether Wisp was installed from an editable or local direct URL."""

    reader = direct_url_reader or _read_direct_url
    try:
        raw = reader()
        if raw is None:
            return False
        payload = json.loads(raw)
        if not isinstance(payload, Mapping):
            return False

        directory_info = payload.get("dir_info")
        if isinstance(directory_info, Mapping) and directory_info.get("editable") is True:
            return True

        url = payload.get("url")
        return isinstance(url, str) and urlsplit(url).scheme.casefold() == "file"
    except Exception:
        return False


def _read_direct_url() -> str | None:
    try:
        return metadata.distribution(_DISTRIBUTION_NAME).read_text("direct_url.json")
    except (metadata.PackageNotFoundError, OSError):
        return None


def _cache_path(*, home_dir: Path | None) -> Path:
    home = Path.home() if home_dir is None else home_dir
    return home.expanduser() / ".wisp" / _CACHE_FILENAME


def _read_cache(path: Path, now: float, python_version: Version) -> tuple[str, ...] | None:
    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None

    payload = json.loads(raw)
    if not isinstance(payload, Mapping):
        raise ValueError("update cache must be a JSON object")

    checked_at = payload.get("checked_at")
    cached_python_version = payload.get("python_version")
    releases = payload.get("releases")
    if isinstance(checked_at, bool) or not isinstance(checked_at, (int, float)):
        raise ValueError("update cache has an invalid timestamp")
    if not isinstance(releases, list) or not all(isinstance(item, str) for item in releases):
        raise ValueError("update cache has invalid releases")
    _parse_versions(releases)
    if cached_python_version != str(python_version):
        return None

    age = now - checked_at
    if age < 0 or age >= CACHE_TTL_SECONDS:
        return None
    return tuple(releases)


async def _fetch_releases(
    *,
    transport: httpx.AsyncBaseTransport | None,
    python_version: Version,
) -> tuple[str, ...]:
    async with httpx.AsyncClient(timeout=HTTP_TIMEOUT_SECONDS, transport=transport) as client:
        response = await client.get(PYPI_URL)
        response.raise_for_status()
        payload: Any = response.json()

    if not isinstance(payload, Mapping):
        raise ValueError("PyPI response must be a JSON object")
    release_data = payload.get("releases")
    if not isinstance(release_data, Mapping):
        raise ValueError("PyPI response has no releases object")

    releases: list[str] = []
    for release, files in release_data.items():
        if not isinstance(release, str) or not isinstance(files, list):
            continue
        if not any(_file_supports_python(file, python_version) for file in files):
            continue
        try:
            Version(release)
        except InvalidVersion:
            continue
        releases.append(release)
    return tuple(releases)


def _file_supports_python(file: object, python_version: Version) -> bool:
    if not isinstance(file, Mapping) or file.get("yanked") is True:
        return False
    requires_python = file.get("requires_python")
    if requires_python is None:
        return True
    if not isinstance(requires_python, str):
        return False
    try:
        return python_version in SpecifierSet(requires_python)
    except InvalidSpecifier:
        return False


def _write_cache(
    path: Path,
    checked_at: float,
    python_version: Version,
    releases: tuple[str, ...],
) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(
        {
            "checked_at": checked_at,
            "python_version": str(python_version),
            "releases": list(releases),
        },
        indent=2,
        sort_keys=True,
    )
    temporary_path: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            "w",
            encoding="utf-8",
            dir=path.parent,
            prefix=f".{path.name}.",
            suffix=".tmp",
            delete=False,
        ) as temporary:
            temporary.write(payload + "\n")
            temporary.flush()
            os.fsync(temporary.fileno())
            temporary_path = Path(temporary.name)
        temporary_path.replace(path)
    finally:
        if temporary_path is not None:
            temporary_path.unlink(missing_ok=True)


def _latest_compatible_version(installed: Version, releases: tuple[str, ...]) -> Version | None:
    versions = _parse_versions(releases)
    if not installed.is_prerelease:
        versions = tuple(version for version in versions if not version.is_prerelease)
    return max(versions, default=None)


def _parse_versions(releases: list[str] | tuple[str, ...]) -> tuple[Version, ...]:
    try:
        return tuple(Version(release) for release in releases)
    except InvalidVersion as exc:
        raise ValueError("invalid release version") from exc
