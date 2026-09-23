from __future__ import annotations

import json
import os
import signal
import sys
from dataclasses import replace
from pathlib import Path

import pytest

from benchmarks import rust_tui_e2e
from benchmarks.rust_tui_e2e import (
    BenchmarkConfig,
    BenchmarkReport,
    BenchmarkSample,
    Renderer,
    run_benchmark,
    summarize_samples,
    validate_config,
)

pytestmark = pytest.mark.benchmark


def _sample(renderer: Renderer, run: int, value: float) -> BenchmarkSample:
    return BenchmarkSample(
        renderer=renderer,
        run=run,
        order=1,
        launch_to_ready_ms=value,
        submit_to_prompt_echo_ms=value + 1,
        submit_to_first_response_ms=value + 2,
        submit_to_final_response_ms=value + 3,
        submit_to_settled_ms=value + 4,
        total_ms=value + 5,
        terminal_output_bytes=int(value * 100),
        child_user_cpu_ms=value + 6,
        child_system_cpu_ms=value + 7,
        child_max_rss_bytes=int(value * 1_000),
        exact_markers_visible=True,
        clean_exit=True,
        terminal_restored=True,
    )


def test_summarize_samples_retains_raw_distribution_extremes() -> None:
    samples = (
        _sample("rust", 1, 10),
        _sample("rust", 2, 30),
    )

    (rust,) = summarize_samples(samples)

    assert rust.renderer == "rust"
    assert rust.samples == 2
    assert rust.launch_to_ready_ms.median == 20
    assert rust.launch_to_ready_ms.p95 == 29
    assert rust.launch_to_ready_ms.max == 30
    assert rust.terminal_output_bytes.median == 2_000
    assert rust.child_max_rss_bytes is not None
    assert rust.child_max_rss_bytes.max == 30_000


def test_report_json_includes_samples_and_summaries() -> None:
    samples = (_sample("rust", 1, 10),)
    report = BenchmarkReport(
        config=BenchmarkConfig(runs=1),
        environment={"platform": "test", "python": "3.12"},
        samples=samples,
        summaries=summarize_samples(samples),
    )

    payload = json.loads(report.to_json())

    assert payload["config"]["rust_binary"] is None
    assert payload["samples"][0]["renderer"] == "rust"
    assert payload["summaries"][0]["launch_to_ready_ms"]["median"] == 10


def test_validate_config_rejects_invalid_dimensions_and_missing_binary() -> None:
    with pytest.raises(ValueError, match="at least 60 columns"):
        validate_config(BenchmarkConfig(width=59))
    with pytest.raises(ValueError, match="unknown renderers"):
        validate_config(BenchmarkConfig(renderers=("textual",)))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="rust-binary is required"):
        validate_config(BenchmarkConfig())
    with pytest.raises(ValueError, match="absolute path"):
        validate_config(BenchmarkConfig(rust_binary=Path("wisp-tui")))


def test_response_markers_survive_blank_cells_skipped_by_incremental_repaint() -> None:
    # Only changed cells are written, so a response painted over blank rows can
    # reach the PTY as words separated by cursor moves instead of spaces.
    echo = b"you\x1b[3;14HWISP_E2E_START_7F3A payload WISP_E2E_FINAL_9C2D"
    response = (
        b"\x1b[6;14Hfake\x1b[6;19Hresponse\x1b[6;28Hto:"
        b"\x1b[6;32HWISP_E2E_START_7F3A\x1b[6;52Hpayload\x1b[7;14HWISP_E2E_FINAL_9C2D"
    )

    assert rust_tui_e2e._response_tail(rust_tui_e2e._plain_text(echo)) is None
    assert rust_tui_e2e._response_tail(rust_tui_e2e._plain_text(echo + response)) == (
        "payloadWISP_E2E_FINAL_9C2D"
    )


@pytest.mark.parametrize("foreground", [0, os.getpgrp()])
def test_timeout_cleanup_never_signals_the_benchmark_process_group(
    monkeypatch: pytest.MonkeyPatch,
    foreground: int,
) -> None:
    signaled_groups: list[tuple[int, signal.Signals]] = []
    monkeypatch.setattr(os, "tcgetpgrp", lambda _fd: foreground)
    monkeypatch.setattr(os, "killpg", lambda group, sig: signaled_groups.append((group, sig)))

    rust_tui_e2e._kill_process_group(123_456, 9)

    assert signaled_groups == [(123_456, signal.SIGKILL)]


def test_terminal_drain_collects_every_buffered_chunk(monkeypatch: pytest.MonkeyPatch) -> None:
    chunks = iter((b"restore-", b"terminal", b""))
    monkeypatch.setattr(rust_tui_e2e, "_read_available", lambda _fd: next(chunks))

    assert rust_tui_e2e._drain_available(9) == b"restore-terminal"


def test_terminal_drain_rejects_a_writer_that_outlives_the_cli(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    timestamps = iter((1.0, 1.2))
    monkeypatch.setattr(rust_tui_e2e.time, "monotonic", lambda: next(timestamps))
    monkeypatch.setattr(rust_tui_e2e, "_read_available", lambda _fd: b"still-writing")

    with pytest.raises(RuntimeError, match="did not quiesce"):
        rust_tui_e2e._drain_available(9, timeout_seconds=0.1)


@pytest.mark.process
def test_post_fork_setup_failure_reaps_the_blocked_child(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    child_pids: list[int] = []
    real_fork = rust_tui_e2e.pty.fork

    def recording_fork() -> tuple[int, int]:
        child_pid, terminal_fd = real_fork()
        if child_pid > 0:
            child_pids.append(child_pid)
        return child_pid, terminal_fd

    def fail_resize(*_args: object) -> None:
        raise OSError("injected resize failure")

    monkeypatch.setattr(rust_tui_e2e.pty, "fork", recording_fork)
    monkeypatch.setattr(rust_tui_e2e.fcntl, "ioctl", fail_resize)

    with pytest.raises(OSError, match="injected resize failure"):
        rust_tui_e2e._run_sample(
            BenchmarkConfig(rust_binary=Path(sys.executable), runs=1),
            renderer="rust",
            run=1,
            order=1,
        )

    assert len(child_pids) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(child_pids[0], os.WNOHANG)


@pytest.mark.process
def test_source_cli_benchmark_completes_rust_renderer() -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)

    report = run_benchmark(
        replace(
            BenchmarkConfig(),
            rust_binary=binary,
            runs=1,
            prompt_words=8,
        )
    )

    assert [sample.renderer for sample in report.samples] == ["rust"]
    assert [summary.renderer for summary in report.summaries] == ["rust"]
    for sample in report.samples:
        assert sample.launch_to_ready_ms >= 0
        assert sample.submit_to_prompt_echo_ms >= 0
        assert sample.submit_to_first_response_ms >= sample.submit_to_prompt_echo_ms
        assert sample.submit_to_final_response_ms >= sample.submit_to_first_response_ms
        assert sample.submit_to_settled_ms >= sample.submit_to_final_response_ms
        assert sample.total_ms >= sample.launch_to_ready_ms
        assert sample.terminal_output_bytes > 0
        assert sample.exact_markers_visible
        assert sample.clean_exit
        assert sample.terminal_restored
