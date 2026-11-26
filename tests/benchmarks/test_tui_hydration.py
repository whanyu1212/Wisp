from __future__ import annotations

import contextlib
import json
import os
import sys
from dataclasses import dataclass, replace
from pathlib import Path
from types import SimpleNamespace

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


def _frame(body: str) -> bytes:
    return f"\x1b[?2026h{body}\x1b[?2026l".encode()


@pytest.mark.parametrize("history_messages, expected", [(0, None), (8, 30)])
def test_first_visible_history(history_messages: int, expected: int | None) -> None:
    paint = rust_tui_hydration._HydrationPaint(history_messages, first_reply=False)
    # Printable bytes can spell the sentinel across unrelated screen rows.
    paint.observe(_frame("\x1b[1;1HRC2 saved \x1b[2;1Hhistory ready"), 10)
    assert paint.history_visible_ns is None

    # A cursor move places "history" beside the first half. The resize frame
    # erases the marker in the very read that completes the preview frame.
    preview = _frame("\x1b[1;11Hhistory ready")
    paint.observe(preview[:-1], 20)
    assert paint.history_visible_ns is None
    paint.observe(preview[-1:] + _frame("\x1b[2J\x1b[1;1HAsk Wisp anything"), 30)
    paint.observe(_frame("\x1b[2;1HRC2 saved history ready"), 40)
    assert paint.history_visible_ns == expected


@pytest.mark.parametrize("reading", ["ctx ▰▱ ~35%", "ctx ~2.6k", "ctx 950"])
def test_single_accepted_probe(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path, reading: str
) -> None:
    paint = rust_tui_hydration._HydrationPaint(8, first_reply=True)
    writes: list[bytes] = []

    def write(_fd: int, data: bytes) -> int:
        writes.append(data)
        return len(data)

    monkeypatch.setattr(rust_tui_hydration.os, "write", write)
    monkeypatch.setattr(rust_tui_hydration.time, "perf_counter_ns", lambda: 35)
    profile = tmp_path / "python.jsonl"
    line = '{"stage":"python.page_read","duration_ms":1,"pid":123}\n'
    profile.write_text(line, encoding="utf-8")

    paint.observe(
        _frame(
            "\x1b[1;1Hctx 42%\x1b[2;1HRC2 saved history ready"
            "\x1b[20;1HAsk Wisp anything\x1b[23;1Hidle\x1b[24;1Hctx ~…"
        ),
        10,
    )
    paint.submit_if_ready(1, tmp_path)
    assert paint.history_visible_ns == 10
    assert paint.submitted_ns is None
    assert writes == []

    budget = _frame(f"\x1b[24;1H\x1b[K{reading}")
    paint.observe(budget[:-1], 20)
    paint.submit_if_ready(1, tmp_path)
    assert writes == []
    paint.observe(budget[-1:], 30)
    paint.submit_if_ready(1, tmp_path)
    assert paint.submitted_ns == 35
    assert writes == [b"WispProbe42\r"]

    profile.write_text(line * 2, encoding="utf-8")  # post-prompt refresh
    paint.observe(_frame("\x1b[2;1H▎WispProbe42\x1b[23;1Hworking"), 40)
    paint.submit_if_ready(1, tmp_path)
    assert paint.reply_ns is None
    assert not paint.reply_idle

    paint.observe(_frame("\x1b[4;1Hfake response to:") + _frame("\x1b[4;19HWispProbe42"), 50)
    assert paint.reply_ns == 50
    assert not paint.reply_idle
    paint.observe(_frame("\x1b[23;1H\x1b[Kidle"), 60)
    paint.submit_if_ready(1, tmp_path)
    assert paint.reply_idle
    assert paint.reply_ns == 50
    assert writes == [b"WispProbe42\r"]
    assert paint.startup_records == (ProfileRecord("python.jsonl", "python.page_read", 1, 123),)


@pytest.mark.parametrize("first_reply", [False, True])
def test_cli_reports_milestones(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], first_reply: bool
) -> None:
    def report(config: BenchmarkConfig) -> BenchmarkReport:
        fresh = _sample(0, 1, 1)
        saved = replace(_sample(8, 1, 2), launch_to_history_visible_ms=5)
        samples: tuple[SessionSample, ...] = (fresh, saved)
        if config.first_reply:
            samples = tuple(
                replace(s, launch_to_submit_ms=15, submit_to_reply_ms=10, launch_to_reply_ms=25)
                for s in samples
            )
        return BenchmarkReport(config, {}, samples, summarize_samples(samples))

    monkeypatch.setattr(rust_tui_hydration, "run_benchmark", report)
    flags = ["--first-reply"] if first_reply else []
    rust_tui_hydration.main(["--rust-binary", "/example/wisp-tui", *flags])
    output = json.loads(capsys.readouterr().out)
    assert output["config"]["first_reply"] is first_reply
    assert output["config"]["profile"] is True
    fresh, saved = output["samples"]
    assert fresh["launch_to_history_visible_ms"] is None
    assert saved["launch_to_history_visible_ms"] == 5
    assert output["summaries"][0]["launch_to_history_visible_ms"] is None
    assert output["summaries"][1]["launch_to_history_visible_ms"]["median"] == 5
    for field, elapsed in (
        ("launch_to_submit_ms", 15),
        ("submit_to_reply_ms", 10),
        ("launch_to_reply_ms", 25),
    ):
        assert saved[field] == (elapsed if first_reply else None)
        distribution = output["summaries"][1][field]
        assert distribution == (
            {"median": elapsed, "p95": elapsed, "max": elapsed} if first_reply else None
        )


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


@dataclass(frozen=True)
class _FakeProcess:
    pid: int
    parent_pid: int
    process_name: str
    arguments: tuple[str, ...]
    descendants: tuple[_FakeProcess, ...] = ()

    def oneshot(self) -> contextlib.nullcontext[None]:
        return contextlib.nullcontext()

    def cmdline(self) -> list[str]:
        return list(self.arguments)

    def name(self) -> str:
        return self.process_name

    def ppid(self) -> int:
        return self.parent_pid

    def memory_info(self) -> SimpleNamespace:
        return SimpleNamespace(rss=self.pid * 100)

    def children(self, *, recursive: bool) -> tuple[_FakeProcess, ...]:
        assert recursive
        return self.descendants


def test_process_snapshot_attributes_backend_helpers_but_keeps_strays(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    # The RPC backend runs Git while building project context; a checkpoint can
    # land while that helper is still alive.
    tui = _FakeProcess(2, 1, "wisp-tui", ("wisp-tui", "--", "python"))
    backend = _FakeProcess(3, 2, "python3.12", ("python", "-m", "wisp", "--mode", "rpc"))
    git = _FakeProcess(4, 3, "git", ("git", "rev-parse", "--show-toplevel"))
    stray = _FakeProcess(5, 1, "python3.12", ("python", "-c", "pass"))
    launcher = _FakeProcess(
        1, 0, "python3.12", ("python", "-m", "wisp", "tui"), (tui, backend, git, stray)
    )
    monkeypatch.setattr(rust_tui_hydration.psutil, "Process", lambda _pid: launcher)

    samples = rust_tui_hydration.snapshot_process_memory(1)

    assert [(sample.role, sample.pid) for sample in samples] == [
        ("backend_helper", 4),
        ("launcher", 1),
        ("other", 5),
        ("rpc_backend", 3),
        ("rust_tui", 2),
    ]


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
@pytest.mark.parametrize("first_reply", [False, True])
def test_source_cli_emits_profile_records_for_both_histories(first_reply: bool) -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)

    report = run_benchmark(
        BenchmarkConfig(
            rust_binary=binary, runs=1, history_messages=(0, 8), first_reply=first_reply
        )
    )

    assert {s.history_messages for s in report.samples} == {0, 8}
    assert all(s.profile_records for s in report.samples)
    assert all(s.clean_exit and s.terminal_restored for s in report.samples)
    fresh, saved = report.samples
    assert fresh.launch_to_history_visible_ms is None
    assert saved.launch_to_history_visible_ms is not None
    assert 0 < saved.launch_to_history_visible_ms <= saved.launch_to_ready_ms
    for sample in report.samples:
        if first_reply:
            assert sample.launch_to_submit_ms is not None and sample.submit_to_reply_ms is not None
            assert sample.launch_to_reply_ms is not None
            assert (
                sample.launch_to_ready_ms <= sample.launch_to_submit_ms < sample.launch_to_reply_ms
            )
            assert sample.launch_to_reply_ms == pytest.approx(
                sample.launch_to_submit_ms + sample.submit_to_reply_ms
            )
        else:
            assert sample.launch_to_submit_ms is None
            assert sample.submit_to_reply_ms is None
            assert sample.launch_to_reply_ms is None
