from __future__ import annotations

import errno
import json
import multiprocessing
import os
import stat
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from typing import Any

import anyio
import pytest
from pytest import MonkeyPatch

from wisp.auth import storage as auth_storage_module
from wisp.auth.storage import (
    ApiKeyCredential,
    AuthStorageError,
    JsonAuthStore,
    OAuthCredential,
)
from wisp.providers.auth import StoredProviderAuthResolver


def _set_auth_credential_in_process(path: str, provider: str, start_event: Any) -> None:
    start_event.wait()
    JsonAuthStore(Path(path)).set(provider, ApiKeyCredential(key=f"key-{provider}"))


def test_json_auth_store_writes_private_oauth_credentials(tmp_path: Path) -> None:
    path = tmp_path / "wisp" / "auth.json"
    store = JsonAuthStore(path)

    store.set(
        "openai-codex",
        OAuthCredential(
            access="access-token",
            refresh="refresh-token",
            expires=4_102_444_800_000,
            account_id="account-id",
        ),
    )

    assert store.providers() == ("openai-codex",)
    assert store.get("openai-codex") == OAuthCredential(
        access="access-token",
        refresh="refresh-token",
        expires=4_102_444_800_000,
        account_id="account-id",
    )
    raw = json.loads(path.read_text(encoding="utf-8"))
    assert raw["openai-codex"]["type"] == "oauth"
    assert raw["openai-codex"]["accountId"] == "account-id"
    if os.name == "posix":
        assert stat.S_IMODE(path.parent.stat().st_mode) == 0o700
        assert stat.S_IMODE(path.stat().st_mode) == 0o600


def test_json_auth_store_deletes_provider(tmp_path: Path) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("openai", ApiKeyCredential(key="sk-test"))

    assert store.delete("openai") is True
    assert store.delete("openai") is False
    assert store.get("openai") is None


def test_json_auth_store_retries_read_racing_with_atomic_replacement(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    credential = ApiKeyCredential(key="sk-test")
    store.set("openai", credential)
    real_read = auth_storage_module._read_auth_file  # noqa: SLF001
    calls = 0

    def race_once(path: Path) -> str | None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise auth_storage_module._AuthFileChangedError(  # noqa: SLF001
                f"Auth file changed while being opened: {path}"
            )
        return real_read(path)

    monkeypatch.setattr(auth_storage_module, "_read_auth_file", race_once)

    assert store.get("openai") == credential
    assert calls == 2


def test_json_auth_store_bounds_retries_during_sustained_replacement(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("openai", ApiKeyCredential(key="sk-test"))
    calls = 0

    def always_racing(path: Path) -> str | None:
        nonlocal calls
        calls += 1
        raise auth_storage_module._AuthFileChangedError(  # noqa: SLF001
            f"Auth file changed while being opened: {path}"
        )

    monkeypatch.setattr(auth_storage_module, "_read_auth_file", always_racing)

    with pytest.raises(AuthStorageError, match="changed while being opened"):
        store.get("openai")

    assert calls == 3


def test_stored_provider_auth_resolver_refreshes_expired_oauth(tmp_path: Path) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set(
        "openai-codex",
        OAuthCredential(
            access="old-access",
            refresh="old-refresh",
            expires=0,
            account_id="old-account",
        ),
    )
    resolver = StoredProviderAuthResolver(store)

    async def refresh(credential: OAuthCredential) -> OAuthCredential:
        assert credential.access == "old-access"
        return OAuthCredential(
            access="new-access",
            refresh="new-refresh",
            expires=4_102_444_800_000,
            account_id="new-account",
        )

    async def run() -> None:
        auth = await resolver.bearer_token("openai-codex", refresh=refresh)
        assert auth is not None
        assert auth.token == "new-access"
        assert auth.account_id == "new-account"

    anyio.run(run)
    assert store.get("openai-codex") == OAuthCredential(
        access="new-access",
        refresh="new-refresh",
        expires=4_102_444_800_000,
        account_id="new-account",
    )


def test_json_auth_store_serializes_concurrent_provider_updates(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"

    def update(provider: str) -> None:
        JsonAuthStore(path).set(provider, ApiKeyCredential(key=f"key-{provider}"))

    with ThreadPoolExecutor(max_workers=2) as executor:
        futures = [executor.submit(update, provider) for provider in ("openai", "anthropic")]
        for future in futures:
            future.result()

    store = JsonAuthStore(path)
    assert store.get("openai") == ApiKeyCredential(key="key-openai")
    assert store.get("anthropic") == ApiKeyCredential(key="key-anthropic")


def test_json_auth_store_serializes_cross_process_provider_updates(tmp_path: Path) -> None:
    path = tmp_path / "auth.json"
    context = multiprocessing.get_context("spawn")
    start_event = context.Event()
    processes = [
        context.Process(
            target=_set_auth_credential_in_process,
            args=(str(path), provider, start_event),
        )
        for provider in ("openai", "anthropic")
    ]
    for process in processes:
        process.start()
    start_event.set()
    for process in processes:
        process.join(timeout=10)
        assert process.exitcode == 0

    store = JsonAuthStore(path)
    assert store.get("openai") == ApiKeyCredential(key="key-openai")
    assert store.get("anthropic") == ApiKeyCredential(key="key-anthropic")


def test_json_auth_store_rejects_symlink_auth_file(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported")
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    if os.name == "posix":
        target.chmod(0o600)
    path = tmp_path / "auth.json"
    path.symlink_to(target)

    with pytest.raises(AuthStorageError, match="not a regular file"):
        JsonAuthStore(path).providers()


def test_json_auth_store_rejects_symlink_lock_file(tmp_path: Path) -> None:
    if not hasattr(os, "symlink"):
        pytest.skip("symlinks are not supported")
    path = tmp_path / "auth.json"
    target = tmp_path / "lock-target"
    target.write_bytes(b"")
    path.with_suffix(".json.lock").symlink_to(target)

    with pytest.raises(AuthStorageError, match="Auth lock is not a private regular file"):
        JsonAuthStore(path).set("openai", ApiKeyCredential(key="key"))

    assert target.read_bytes() == b""


def test_json_auth_store_rejects_hard_linked_auth_file(tmp_path: Path) -> None:
    target = tmp_path / "target.json"
    target.write_text("{}\n", encoding="utf-8")
    if os.name == "posix":
        target.chmod(0o600)
    path = tmp_path / "auth.json"
    try:
        os.link(target, path)
    except OSError as exc:
        pytest.skip(f"hard links are not supported: {exc}")

    with pytest.raises(AuthStorageError, match="multiple hard links"):
        JsonAuthStore(path).providers()


def test_json_auth_store_rejects_non_private_existing_file(tmp_path: Path) -> None:
    if os.name != "posix":
        pytest.skip("POSIX permissions are required")
    path = tmp_path / "auth.json"
    path.write_text("{}\n", encoding="utf-8")
    path.chmod(0o644)

    with pytest.raises(AuthStorageError, match="not private"):
        JsonAuthStore(path).providers()


@pytest.mark.production_fault
def test_json_auth_store_write_failure_preserves_existing_credentials(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "auth.json"
    store = JsonAuthStore(path)
    store.set("openai", ApiKeyCredential(key="original"))
    original = path.read_bytes()
    real_write = os.write
    calls = 0

    def fail_after_short_write(fd: int, data: bytes | memoryview) -> int:
        nonlocal calls
        calls += 1
        if calls == 1:
            return real_write(fd, data[:7])
        raise OSError(errno.ENOSPC, "disk full")

    monkeypatch.setattr(os, "write", fail_after_short_write)

    with pytest.raises(AuthStorageError, match="Could not write auth file"):
        store.set("anthropic", ApiKeyCredential(key="new"))

    assert path.read_bytes() == original
    assert tuple(tmp_path.glob(".auth.json.*.tmp")) == ()


@pytest.mark.production_fault
def test_json_auth_store_sync_failure_preserves_existing_credentials(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    path = tmp_path / "auth.json"
    store = JsonAuthStore(path)
    store.set("openai", ApiKeyCredential(key="original"))
    original = path.read_bytes()
    real_fsync = os.fsync
    calls = 0

    def fail_first_sync(fd: int) -> None:
        nonlocal calls
        calls += 1
        if calls == 1:
            raise OSError(errno.EIO, "sync failed")
        real_fsync(fd)

    monkeypatch.setattr(os, "fsync", fail_first_sync)

    with pytest.raises(AuthStorageError, match="Could not write auth file"):
        store.set("anthropic", ApiKeyCredential(key="new"))

    assert path.read_bytes() == original
    assert tuple(tmp_path.glob(".auth.json.*.tmp")) == ()


def test_json_auth_store_compare_and_set_preserves_newer_credential(tmp_path: Path) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    original = OAuthCredential(access="old", refresh="old-refresh", expires=0)
    newer = OAuthCredential(access="newer", refresh="newer-refresh", expires=20)
    stale = OAuthCredential(access="stale", refresh="stale-refresh", expires=10)
    store.set("openai-codex", original)
    store.set("openai-codex", newer)

    assert (
        store.compare_and_set(
            "openai-codex",
            expected=original,
            replacement=stale,
        )
        is False
    )
    assert store.get("openai-codex") == newer


def test_stored_provider_auth_resolver_uses_refresh_winner(tmp_path: Path) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    original = OAuthCredential(access="old", refresh="old-refresh", expires=0)
    winner = OAuthCredential(
        access="winner",
        refresh="winner-refresh",
        expires=4_102_444_800_000,
    )
    stale = OAuthCredential(
        access="stale",
        refresh="stale-refresh",
        expires=4_102_444_800_000,
    )
    store.set("openai-codex", original)
    resolver = StoredProviderAuthResolver(store)

    async def refresh(credential: OAuthCredential) -> OAuthCredential:
        assert credential == original
        store.set("openai-codex", winner)
        return stale

    async def run() -> None:
        auth = await resolver.bearer_token("openai-codex", refresh=refresh)
        assert auth is not None
        assert auth.token == "winner"

    anyio.run(run)
    assert store.get("openai-codex") == winner


def test_json_auth_store_syncs_file_and_parent_directory(
    tmp_path: Path,
    monkeypatch: MonkeyPatch,
) -> None:
    file_syncs = 0
    directories: list[Path] = []
    real_fsync = os.fsync
    real_directory_sync = auth_storage_module._sync_directory  # noqa: SLF001

    def track_fsync(fd: int) -> None:
        nonlocal file_syncs
        file_syncs += 1
        real_fsync(fd)

    def track_directory(path: Path) -> None:
        directories.append(path)
        real_directory_sync(path)

    monkeypatch.setattr(os, "fsync", track_fsync)
    monkeypatch.setattr(auth_storage_module, "_sync_directory", track_directory)

    JsonAuthStore(tmp_path / "auth.json").set("openai", ApiKeyCredential(key="key"))

    assert file_syncs >= 2  # temporary credential file and containing directory
    assert directories == [tmp_path]


def test_stored_provider_auth_resolver_resolves_only_api_keys(tmp_path: Path) -> None:
    store = JsonAuthStore(tmp_path / "auth.json")
    store.set("openai", ApiKeyCredential(key="stored-key"))
    store.set(
        "openai-codex",
        OAuthCredential(access="access", refresh="refresh", expires=4_102_444_800_000),
    )
    resolver = StoredProviderAuthResolver(store)

    async def run() -> None:
        assert await resolver.api_key("openai") == "stored-key"
        assert await resolver.api_key("openai-codex") is None
        assert await resolver.api_key("missing") is None

    anyio.run(run)
