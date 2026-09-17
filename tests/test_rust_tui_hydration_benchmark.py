from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from benchmarks import rust_tui_hydration
from benchmarks.rust_tui_hydration import (
    BenchmarkConfig,
    BenchmarkReport,
    ProfileRecord,
    SessionSample,
    read_profile_records,
    run_benchmark,
    summarize_samples,
    total_stages,
    validate_config,
    validate_profile_coverage,
)

pytestmark = pytest.mark.benchmark


def _sample(history_messages: int, run: int, duration: float) -> SessionSample:
    records = (
        ProfileRecord("python.jsonl", "jsonl_read", duration, 123, 4, 100),
        ProfileRecord("python.jsonl", "jsonl_read", duration + 1, 123, 4, 200),
        ProfileRecord("rust.jsonl", "rust_project", duration + 2, 456, 8, 300),
    )
    return SessionSample(
        history_messages=history_messages,
        history_bytes=history_messages * 100,
        run=run,
        launch_to_ready_ms=duration + 10,
        total_ms=duration + 20,
        terminal_output_bytes=100,
        observed_process_tree_cpu_ms=duration + 30,
        observed_simultaneous_rss_peak_bytes=1_000,
        resource_observations=2,
        ready_process_memory=(rust_tui_hydration.ProcessMemory("rust_tui", 456, 512),),
        settled_process_memory=(rust_tui_hydration.ProcessMemory("rust_tui", 456, 500),),
        profile_records=records,
        stage_totals=total_stages(records),
        clean_exit=True,
        terminal_restored=True,
    )


def test_profile_parser_reads_multiple_process_files_and_rejects_missing(
    tmp_path: Path,
) -> None:
    with pytest.raises(RuntimeError, match="no hydration profile records"):
        read_profile_records(tmp_path)

    (tmp_path / "python.jsonl").write_text(
        json.dumps({"stage": "jsonl_read", "duration_ms": 1.25, "count": 4, "pid": 123}) + "\n",
        encoding="utf-8",
    )
    (tmp_path / "rust.jsonl").write_text(
        json.dumps({"stage": "rust_project", "duration_ms": 2, "bytes": 128, "pid": 456}) + "\n",
        encoding="utf-8",
    )

    assert read_profile_records(tmp_path) == (
        ProfileRecord("python.jsonl", "jsonl_read", 1.25, 123, 4, None),
        ProfileRecord("rust.jsonl", "rust_project", 2.0, 456, None, 128),
    )


@pytest.mark.parametrize(
    "bad_record",
    [
        {"stage": "read", "duration_ms": -1, "pid": 1},
        {"stage": "read", "duration_ms": 1, "pid": True},
        {"stage": "read", "duration_ms": 1, "pid": 1, "count": -1},
        {"stage": "read", "duration_ms": 1, "pid": 1, "bytes": "100"},
        {"stage": "", "duration_ms": 1, "pid": 1},
        {"stage": "read", "duration_ms": 1},
    ],
)
def test_profile_parser_identifies_invalid_record(
    tmp_path: Path, bad_record: dict[str, object]
) -> None:
    (tmp_path / "rust.jsonl").write_text(json.dumps(bad_record) + "\n", encoding="utf-8")

    with pytest.raises(RuntimeError, match=r"rust.jsonl:1"):
        read_profile_records(tmp_path)


def test_stage_totals_and_condition_distributions_keep_raw_evidence() -> None:
    samples = (_sample(0, 1, 1), _sample(8, 1, 3), _sample(0, 2, 5))

    fresh, seeded = summarize_samples(samples)

    assert fresh.sessions == 2
    assert fresh.launch_to_ready_ms.median == 13
    assert fresh.stages[0].stage == "jsonl_read"
    assert fresh.stages[0].duration_ms.median == 7
    assert fresh.stages[0].records.median == 2
    assert fresh.stages[0].count.median == 8
    assert fresh.stages[0].bytes.median == 300
    assert seeded.history_messages == 8
    report = BenchmarkReport(BenchmarkConfig(), {}, samples, (fresh, seeded))
    payload = json.loads(report.to_json())
    assert payload["config"]["rust_binary"] is False
    assert payload["samples"][0]["profile_records"][0]["pid"] == 123
    assert payload["samples"][0]["stage_totals"][0]["duration_ms"] == 3
    assert payload["samples"][0]["ready_process_memory"][0]["rss_bytes"] == 512


def test_summary_rejects_missing_stage_in_repeated_condition() -> None:
    first = _sample(8, 1, 1)
    second = _sample(8, 2, 2)
    second = replace(second, stage_totals=second.stage_totals[:1])

    with pytest.raises(ValueError, match="profile stages differ"):
        summarize_samples((first, second))


def test_profile_coverage_requires_both_processes_and_all_saved_messages() -> None:
    stages = (
        "python.page_read",
        "python.session_refresh",
        "python.page_publish",
        "rust.page_projection",
        "rust.page_clone",
        "rust.final_projection",
        "rust.history_install",
    )
    records = tuple(
        ProfileRecord(
            "profile.jsonl",
            stage,
            1,
            123,
            201 if stage in {"rust.page_projection", "rust.history_install"} else 0,
        )
        for stage in stages
    )
    validate_profile_coverage(records, 200)
    with pytest.raises(RuntimeError, match="missing stages"):
        validate_profile_coverage(records[:2], 200)
    with pytest.raises(RuntimeError, match="missing selected-session refresh"):
        validate_profile_coverage(records[0:1] + records[2:], 200)
    with pytest.raises(
        RuntimeError, match="rust.page_projection counted 201 messages; expected 202"
    ):
        validate_profile_coverage(records, 201)


def test_config_rejects_invalid_history_and_missing_binary(tmp_path: Path) -> None:
    binary = tmp_path / "wisp-tui"
    binary.write_text("binary", encoding="utf-8")
    binary.chmod(0o755)

    with pytest.raises(ValueError, match="multiples of four"):
        validate_config(BenchmarkConfig(rust_binary=binary, history_messages=(0, 7)))
    with pytest.raises(ValueError, match="ready hold seconds"):
        validate_config(BenchmarkConfig(rust_binary=binary, ready_hold_seconds=-1))
    with pytest.raises(ValueError, match="required"):
        validate_config(BenchmarkConfig())


def test_run_order_alternates_histories(monkeypatch: pytest.MonkeyPatch) -> None:
    observed: list[tuple[int, int]] = []

    def record_sample(_config: BenchmarkConfig, history_messages: int, run: int) -> SessionSample:
        observed.append((run, history_messages))
        return _sample(history_messages, run, 1)

    monkeypatch.setattr(rust_tui_hydration, "validate_config", lambda _config: None)
    monkeypatch.setattr(rust_tui_hydration, "_run_sample", record_sample)

    run_benchmark(BenchmarkConfig(runs=2, history_messages=(0, 8)))

    assert observed == [(1, 0), (1, 8), (2, 8), (2, 0)]


@pytest.mark.process
def test_post_fork_setup_failure_reaps_child(monkeypatch: pytest.MonkeyPatch) -> None:
    child_pids: list[int] = []
    real_fork = rust_tui_hydration.pty.fork

    def recording_fork() -> tuple[int, int]:
        child_pid, terminal_fd = real_fork()
        if child_pid > 0:
            child_pids.append(child_pid)
        return child_pid, terminal_fd

    def fail_resize(*_args: object) -> None:
        raise OSError("injected resize failure")

    monkeypatch.setattr(rust_tui_hydration.pty, "fork", recording_fork)
    monkeypatch.setattr(rust_tui_hydration.fcntl, "ioctl", fail_resize)

    with pytest.raises(OSError, match="injected resize failure"):
        rust_tui_hydration._run_sample(BenchmarkConfig(rust_binary=Path(sys.executable)), 0, 1)

    assert len(child_pids) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(child_pids[0], os.WNOHANG)


@pytest.mark.process
def test_source_cli_emits_profile_records_for_both_histories() -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)

    report = run_benchmark(BenchmarkConfig(rust_binary=binary, runs=1, history_messages=(0, 8)))

    assert {s.history_messages for s in report.samples} == {0, 8}
    assert all(s.profile_records for s in report.samples)
    assert all(s.clean_exit and s.terminal_restored for s in report.samples)
