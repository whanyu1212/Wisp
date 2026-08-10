from __future__ import annotations

import json
from pathlib import Path
from subprocess import CompletedProcess
from typing import Any

import anyio
import httpx
import pytest

from wisp import update_check as update_check_module
from wisp.update_check import (
    PYPI_URL,
    UpdateAvailable,
    UpdateCheckError,
    UpdateInstallError,
    UpdateStatus,
    check_for_update,
    get_update_status,
    install_update,
    is_local_install,
)


def _pypi_response(releases: dict[str, list[dict[str, object]]]) -> dict[str, object]:
    return {"releases": releases}


def _available(version: str, *, yanked: bool = False) -> list[dict[str, object]]:
    return [{"yanked": yanked, "filename": f"wisp_ai-{version}.whl"}]


def _check(
    *,
    current_version: str = "1.0.0",
    home_dir: Path,
    now: float = 10_000.0,
    enabled: bool = True,
    local: bool = False,
    python_version: str = "3.12.0",
    transport: httpx.AsyncBaseTransport | None = None,
) -> UpdateAvailable | None:
    async def run() -> UpdateAvailable | None:
        return await check_for_update(
            enabled=enabled,
            current_version=current_version,
            home_dir=home_dir,
            now=now,
            python_version=python_version,
            local_install_detector=lambda: local,
            transport=transport,
        )

    return anyio.run(run)


def test_update_check_uses_pep_440_ordering_and_ignores_yanked_only_releases(
    tmp_path: Path,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        assert str(request.url) == PYPI_URL
        return httpx.Response(
            200,
            json=_pypi_response(
                {
                    "1.2.0": _available("1.2.0"),
                    "1.10.0": _available("1.10.0"),
                    "2.0.0": _available("2.0.0", yanked=True),
                }
            ),
        )

    result = _check(home_dir=tmp_path, transport=httpx.MockTransport(handler))

    assert result == UpdateAvailable(
        current_version="1.0.0",
        latest_version="1.10.0",
        update_command="wisp update",
    )
    cache_path = tmp_path / ".wisp" / "update-check.json"
    assert cache_path.is_file()
    assert list(cache_path.parent.glob("*.tmp")) == []


def test_stable_current_version_ignores_prereleases(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_pypi_response(
                {
                    "1.1.0": _available("1.1.0"),
                    "2.0.0rc1": _available("2.0.0rc1"),
                }
            ),
            request=request,
        )

    result = _check(home_dir=tmp_path, transport=httpx.MockTransport(handler))

    assert result is not None
    assert result.latest_version == "1.1.0"


def test_update_check_ignores_releases_for_newer_python(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_pypi_response(
                {
                    "1.5.0": _available("1.5.0"),
                    "2.0.0": [
                        {
                            "yanked": False,
                            "filename": "wisp_ai-2.0.0.whl",
                            "requires_python": ">=3.13",
                        }
                    ],
                }
            ),
            request=request,
        )

    result = _check(
        home_dir=tmp_path,
        python_version="3.12.0",
        transport=httpx.MockTransport(handler),
    )

    assert result is not None
    assert result.latest_version == "1.5.0"


@pytest.mark.parametrize(
    ("releases", "expected"),
    [
        (["2.0.0b1", "1.9.0"], "2.0.0b1"),
        (["2.0.0b1", "2.0.0"], "2.0.0"),
    ],
)
def test_prerelease_current_considers_prereleases_and_stable_versions(
    tmp_path: Path,
    releases: list[str],
    expected: str,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_pypi_response({version: _available(version) for version in releases}),
            request=request,
        )

    result = _check(
        current_version="2.0.0a1",
        home_dir=tmp_path,
        transport=httpx.MockTransport(handler),
    )

    assert result is not None
    assert result.latest_version == expected


def test_fresh_cache_avoids_network(tmp_path: Path) -> None:
    cache_path = tmp_path / ".wisp" / "update-check.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(
            {
                "checked_at": 10_000.0 - (6 * 60 * 60) + 1,
                "python_version": "3.12.0",
                "releases": ["1.0.0", "1.3.0"],
            }
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    result = _check(home_dir=tmp_path, transport=httpx.MockTransport(handler))

    assert result is not None
    assert result.latest_version == "1.3.0"


def test_cache_from_another_python_version_is_refreshed(tmp_path: Path) -> None:
    cache_path = tmp_path / ".wisp" / "update-check.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(
            {
                "checked_at": 9_999.0,
                "python_version": "3.13.0",
                "releases": ["2.0.0"],
            }
        ),
        encoding="utf-8",
    )
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json=_pypi_response({"1.5.0": _available("1.5.0")}),
            request=request,
        )

    result = _check(
        home_dir=tmp_path,
        python_version="3.12.0",
        transport=httpx.MockTransport(handler),
    )

    assert requests == 1
    assert result is not None
    assert result.latest_version == "1.5.0"
    assert json.loads(cache_path.read_text(encoding="utf-8"))["python_version"] == "3.12.0"


def test_stale_cache_is_refreshed(tmp_path: Path) -> None:
    cache_path = tmp_path / ".wisp" / "update-check.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(
            {
                "checked_at": 10_000.0 - (6 * 60 * 60),
                "python_version": "3.12.0",
                "releases": ["1.1.0"],
            }
        ),
        encoding="utf-8",
    )
    requests = 0

    def handler(request: httpx.Request) -> httpx.Response:
        nonlocal requests
        requests += 1
        return httpx.Response(
            200,
            json=_pypi_response({"1.4.0": _available("1.4.0")}),
            request=request,
        )

    result = _check(home_dir=tmp_path, transport=httpx.MockTransport(handler))

    assert requests == 1
    assert result is not None
    assert result.latest_version == "1.4.0"
    assert json.loads(cache_path.read_text(encoding="utf-8"))["releases"] == ["1.4.0"]


def test_malformed_cache_is_refreshed(tmp_path: Path) -> None:
    cache_path = tmp_path / ".wisp" / "update-check.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text("{not-json", encoding="utf-8")

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_pypi_response({"1.2.0": _available("1.2.0")}),
            request=request,
        )

    result = _check(home_dir=tmp_path, transport=httpx.MockTransport(handler))

    assert result is not None
    assert result.latest_version == "1.2.0"
    assert json.loads(cache_path.read_text(encoding="utf-8"))["releases"] == ["1.2.0"]


def test_cache_io_error_does_not_suppress_network_result(tmp_path: Path) -> None:
    cache_path = tmp_path / ".wisp" / "update-check.json"
    cache_path.mkdir(parents=True)

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_pypi_response({"1.2.0": _available("1.2.0")}),
            request=request,
        )

    result = _check(home_dir=tmp_path, transport=httpx.MockTransport(handler))

    assert result is not None
    assert result.latest_version == "1.2.0"


@pytest.mark.parametrize(("enabled", "local"), [(False, False), (True, True)])
def test_disabled_or_local_install_skips_cache_and_network(
    tmp_path: Path,
    enabled: bool,
    local: bool,
) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.url}")

    result = _check(
        home_dir=tmp_path,
        enabled=enabled,
        local=local,
        transport=httpx.MockTransport(handler),
    )

    assert result is None
    assert not (tmp_path / ".wisp" / "update-check.json").exists()


def test_network_failure_returns_no_result(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    assert _check(home_dir=tmp_path, transport=httpx.MockTransport(handler)) is None


@pytest.mark.parametrize(
    "payload",
    [None, [], {"releases": []}, {"other": {}}],
)
def test_malformed_response_returns_no_result(tmp_path: Path, payload: Any) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json=payload, request=request)

    assert _check(home_dir=tmp_path, transport=httpx.MockTransport(handler)) is None


@pytest.mark.parametrize(
    ("direct_url", "expected"),
    [
        ('{"url": "file:///tmp/Wisp", "dir_info": {"editable": true}}', True),
        ('{"url": "file:///tmp/wisp.whl", "archive_info": {}}', True),
        ('{"url": "https://files.pythonhosted.org/wisp.whl", "archive_info": {}}', False),
        ("{not-json", False),
        (None, False),
    ],
)
def test_local_install_detection_from_direct_url(direct_url: str | None, expected: bool) -> None:
    assert is_local_install(direct_url_reader=lambda: direct_url) is expected


def test_explicit_status_bypasses_fresh_cache(tmp_path: Path) -> None:
    cache_path = tmp_path / ".wisp" / "update-check.json"
    cache_path.parent.mkdir(parents=True)
    cache_path.write_text(
        json.dumps(
            {
                "checked_at": 9_999.0,
                "python_version": "3.12.0",
                "releases": ["1.1.0"],
            }
        ),
        encoding="utf-8",
    )

    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(
            200,
            json=_pypi_response({"1.4.0": _available("1.4.0")}),
            request=request,
        )

    async def run() -> UpdateStatus:
        return await get_update_status(
            current_version="1.0.0",
            home_dir=tmp_path,
            now=10_000.0,
            python_version="3.12.0",
            local_install_detector=lambda: False,
            transport=httpx.MockTransport(handler),
        )

    status = anyio.run(run)

    assert status == UpdateStatus(current_version="1.0.0", latest_version="1.4.0")
    assert status.available is not None
    assert status.available.latest_version == "1.4.0"


def test_explicit_status_reports_network_and_local_install_errors(tmp_path: Path) -> None:
    def handler(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("offline", request=request)

    async def offline() -> None:
        with pytest.raises(UpdateCheckError, match="Could not check PyPI"):
            await get_update_status(
                home_dir=tmp_path,
                local_install_detector=lambda: False,
                transport=httpx.MockTransport(handler),
            )

    async def local() -> None:
        with pytest.raises(UpdateCheckError, match="installed from a local source"):
            await get_update_status(
                home_dir=tmp_path,
                local_install_detector=lambda: True,
                transport=httpx.MockTransport(handler),
            )

    anyio.run(offline)
    anyio.run(local)


def test_install_update_uses_exact_argv_without_a_shell() -> None:
    commands: list[tuple[str, ...]] = []

    async def verify_install() -> None:
        pass

    async def runner(command: tuple[str, ...]) -> None:
        commands.append(command)

    async def run() -> None:
        await install_update(
            UpdateAvailable(
                current_version="1.0.0",
                latest_version="1.2.0",
                update_command="ignored display value",
            ),
            runner=runner,
            install_verifier=verify_install,
        )

    anyio.run(run)

    assert commands == [
        (
            "uv",
            "tool",
            "install",
            "--force",
            "--no-config",
            "--no-sources",
            "--default-index",
            "https://pypi.org/simple",
            "wisp-ai==1.2.0",
        )
    ]


def test_install_update_rejects_invalid_or_non_newer_versions() -> None:
    async def run(update: UpdateAvailable) -> None:
        with pytest.raises(UpdateInstallError):
            await install_update(update)

    anyio.run(
        run,
        UpdateAvailable("1.0.0", "not-a-version; command", "ignored"),
    )
    anyio.run(
        run,
        UpdateAvailable("1.0.0", "1.0.0", "ignored"),
    )


def test_install_update_rejects_unmanaged_install_before_running_command() -> None:
    commands: list[tuple[str, ...]] = []

    async def reject_install() -> None:
        raise UpdateInstallError("not managed by uv")

    async def runner(command: tuple[str, ...]) -> None:
        commands.append(command)

    async def run() -> None:
        with pytest.raises(UpdateInstallError, match="not managed by uv"):
            await install_update(
                UpdateAvailable("1.0.0", "1.1.0", "ignored"),
                runner=runner,
                install_verifier=reject_install,
            )

    anyio.run(run)

    assert commands == []


@pytest.mark.parametrize(
    ("environment", "allowed"), [("/tools/wisp-ai", True), ("/tools/other", False)]
)
def test_install_update_verifies_the_active_uv_tool_environment(
    monkeypatch: pytest.MonkeyPatch,
    environment: str,
    allowed: bool,
) -> None:
    commands: list[tuple[str, ...]] = []

    async def run_process(
        command: tuple[str, ...],
        *,
        check: bool,
        cwd: Path,
        env: dict[str, str],
    ) -> CompletedProcess[bytes]:
        assert command == ("uv", "tool", "dir", "--no-config")
        assert check is False
        assert cwd == Path.home().resolve()
        assert env == update_check_module._update_environment()
        return CompletedProcess(command, 0, stdout=b"/tools\n", stderr=b"")

    async def runner(command: tuple[str, ...]) -> None:
        commands.append(command)

    monkeypatch.setattr(update_check_module.anyio, "run_process", run_process)
    monkeypatch.setattr(update_check_module.sys, "prefix", environment)

    async def run() -> None:
        update = UpdateAvailable("1.0.0", "1.1.0", "ignored")
        if allowed:
            await install_update(update, runner=runner)
        else:
            with pytest.raises(UpdateInstallError, match="persistent uv tool installation"):
                await install_update(update, runner=runner)

    anyio.run(run)

    assert commands == (
        [
            (
                "uv",
                "tool",
                "install",
                "--force",
                "--no-config",
                "--no-sources",
                "--default-index",
                "https://pypi.org/simple",
                "wisp-ai==1.1.0",
            )
        ]
        if allowed
        else []
    )


def test_install_update_shields_environment_replacement_from_cancellation() -> None:
    started = anyio.Event()
    release = anyio.Event()
    completed = anyio.Event()
    phases: list[str] = []

    async def verify_install() -> None:
        phases.append("verified")

    def install_started() -> None:
        phases.append("installing")

    async def runner(command: tuple[str, ...]) -> None:
        assert phases == ["verified", "installing"]
        started.set()
        await release.wait()
        completed.set()

    async def run_install() -> None:
        await install_update(
            UpdateAvailable("1.0.0", "1.1.0", "ignored"),
            runner=runner,
            install_verifier=verify_install,
            on_install_started=install_started,
        )

    async def run() -> None:
        async with anyio.create_task_group() as task_group:
            task_group.start_soon(run_install)
            await started.wait()
            task_group.cancel_scope.cancel()
            with anyio.CancelScope(shield=True):
                release.set()
                await completed.wait()

        assert completed.is_set()

    anyio.run(run)


def test_install_update_isolates_uv_from_project_configuration(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    project = tmp_path / "untrusted-project"
    project.mkdir()
    (project / "uv.toml").write_text(
        '[[index]]\nurl = "https://attacker.invalid/simple"\n',
        encoding="utf-8",
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("UV_INDEX", "https://attacker.invalid/simple")
    monkeypatch.setenv("UV_FIND_LINKS", str(project))
    monkeypatch.setenv("UV_TOOL_DIR", str(tmp_path / "tools"))
    monkeypatch.setenv("WISP_UPDATE_TEST", "retained")
    calls: list[tuple[tuple[str, ...], Path, dict[str, str]]] = []

    async def verify_install() -> None:
        pass

    async def run_process(
        command: tuple[str, ...],
        *,
        check: bool,
        cwd: Path,
        env: dict[str, str],
    ) -> CompletedProcess[bytes]:
        assert check is False
        calls.append((command, cwd, env))
        return CompletedProcess(command, 0, stdout=b"", stderr=b"")

    monkeypatch.setattr(update_check_module.anyio, "run_process", run_process)

    async def run() -> None:
        await install_update(
            UpdateAvailable("1.0.0", "1.1.0", "ignored"),
            install_verifier=verify_install,
        )

    anyio.run(run)

    assert len(calls) == 1
    command, cwd, environment = calls[0]
    assert "--no-config" in command
    assert "--no-sources" in command
    assert command[command.index("--default-index") + 1] == "https://pypi.org/simple"
    assert cwd == Path.home().resolve()
    assert environment["UV_TOOL_DIR"] == str(tmp_path / "tools")
    assert environment["WISP_UPDATE_TEST"] == "retained"
    assert "UV_INDEX" not in environment
    assert "UV_FIND_LINKS" not in environment
