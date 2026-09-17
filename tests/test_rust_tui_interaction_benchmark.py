from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from benchmarks import rust_tui_interaction
from benchmarks.rust_tui_e2e import Renderer
from benchmarks.rust_tui_interaction import (
    BenchmarkConfig,
    BenchmarkReport,
    NavigationSample,
    ProbeSample,
    SessionSample,
    run_benchmark,
    summarize_samples,
    validate_config,
)

pytestmark = pytest.mark.benchmark


def _sample(renderer: Renderer, history_messages: int, latency: float) -> SessionSample:
    return SessionSample(
        renderer=renderer,
        history_messages=history_messages,
        history_bytes=history_messages * 100,
        run=1,
        order=1,
        launch_to_ready_ms=latency + 1,
        submit_to_first_response_ms=latency + 2,
        submit_to_final_response_ms=latency + 3,
        total_ms=latency + 4,
        terminal_output_bytes=100,
        observed_process_tree_cpu_ms=latency + 5,
        observed_simultaneous_rss_peak_bytes=1000,
        resource_observations=2,
        probes=(
            ProbeSample("idle", 0, latency),
            ProbeSample("streaming", 0, latency + 10),
        ),
        navigation=(
            NavigationSample("page_up", latency + 20),
            NavigationSample("page_down", latency + 30),
        ),
        clean_exit=True,
        terminal_restored=True,
    )


def test_summary_keeps_conditions_and_raw_probe_phases() -> None:
    samples = (
        _sample("rust", 0, 10),
        _sample("textual", 0, 40),
        _sample("rust", 0, 30),
        _sample("rust", 8, 50),
    )

    fresh_rust, fresh_textual, long_rust = summarize_samples(samples)

    assert fresh_rust.sessions == 2
    assert fresh_rust.idle_input_visible_ms.median == 20
    assert fresh_rust.streaming_input_visible_ms.median == 30
    assert fresh_rust.page_up_output_activity_ms.median == 40
    assert fresh_rust.page_down_output_activity_ms.median == 50
    assert fresh_textual.streaming_input_visible_ms.median == 50
    assert long_rust.history_messages == 8
    report = BenchmarkReport(BenchmarkConfig(renderers=("rust",)), {}, samples, (fresh_rust,))
    payload = json.loads(report.to_json())
    assert payload["config"]["rust_binary"] is False
    assert payload["samples"][0]["probes"][1]["phase"] == "streaming"


def test_config_rejects_unusable_stream_and_history() -> None:
    with pytest.raises(ValueError, match="multiples of four"):
        validate_config(BenchmarkConfig(renderers=("textual",), history_messages=(0, 7)))
    with pytest.raises(ValueError, match="at least five seconds"):
        validate_config(
            BenchmarkConfig(renderers=("textual",), response_words=8, stream_interval_ms=20)
        )


def test_probe_markers_do_not_reuse_a_predictable_phase_prefix(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    tokens = iter(("A1B2C3D4E5F60718", "F8E7D6C5B4A39201"))
    writes: list[bytes] = []
    monkeypatch.setattr(rust_tui_interaction.secrets, "token_hex", lambda _n: next(tokens))
    monkeypatch.setattr(rust_tui_interaction.termios, "tcgetattr", lambda _fd: [0, 0, 0, 0])
    monkeypatch.setattr(rust_tui_interaction.os, "write", lambda _fd, data: writes.append(data))

    first = rust_tui_interaction._send_probe(9, "idle", 0)[2]
    second = rust_tui_interaction._send_probe(9, "idle", 1)[2]

    assert writes == [first.encode(), second.encode()]
    assert len(first) == len(second) == 16
    assert first != second
    assert not rust_tui_interaction._marker_painted(f"{first} 1 {first}", second)
    assert rust_tui_interaction._marker_painted(f"{second[:1]} redraw {second[1:]}", second)
    assert rust_tui_interaction._marker_painted(f"status {second}", second)


def test_incomplete_repaints_cannot_form_a_visible_marker() -> None:
    marker = "ABCDEFABCDEFABCD"
    partial_repaints = " status ".join(marker[:length] for length in range(1, 9))

    assert not rust_tui_interaction._marker_painted(partial_repaints, marker)


def test_run_order_alternates_both_histories_and_renderers(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    observed: list[tuple[int, int, Renderer]] = []

    def record_sample(
        _config: BenchmarkConfig,
        *,
        renderer: Renderer,
        history_messages: int,
        run: int,
        order: int,
    ) -> SessionSample:
        observed.append((run, history_messages, renderer))
        return _sample(renderer, history_messages, 10.0)

    monkeypatch.setattr(rust_tui_interaction, "validate_config", lambda _config: None)
    monkeypatch.setattr(rust_tui_interaction, "_run_sample", record_sample)

    run_benchmark(BenchmarkConfig(runs=2, history_messages=(0, 8)))

    assert observed == [
        (1, 0, "rust"),
        (1, 0, "textual"),
        (1, 8, "rust"),
        (1, 8, "textual"),
        (2, 8, "textual"),
        (2, 8, "rust"),
        (2, 0, "textual"),
        (2, 0, "rust"),
    ]


@pytest.mark.process
def test_post_fork_setup_failure_reaps_the_child(monkeypatch: pytest.MonkeyPatch) -> None:
    child_pids: list[int] = []
    real_fork = rust_tui_interaction.pty.fork

    def recording_fork() -> tuple[int, int]:
        child_pid, terminal_fd = real_fork()
        if child_pid > 0:
            child_pids.append(child_pid)
        return child_pid, terminal_fd

    def fail_resize(*_args: object) -> None:
        raise OSError("injected resize failure")

    monkeypatch.setattr(rust_tui_interaction.pty, "fork", recording_fork)
    monkeypatch.setattr(rust_tui_interaction.fcntl, "ioctl", fail_resize)

    with pytest.raises(OSError, match="injected resize failure"):
        rust_tui_interaction._run_sample(
            BenchmarkConfig(renderers=("textual",), runs=1),
            renderer="textual",
            history_messages=0,
            run=1,
            order=1,
        )

    assert len(child_pids) == 1
    with pytest.raises(ChildProcessError):
        os.waitpid(child_pids[0], os.WNOHANG)


@pytest.mark.process
def test_source_cli_interaction_completes_both_renderers_and_histories() -> None:
    binary_value = os.environ.get("RUST_TUI_BINARY_UNDER_TEST")
    if binary_value is None:
        pytest.skip("set RUST_TUI_BINARY_UNDER_TEST to a built wisp-tui binary")
    binary = Path(binary_value).resolve(strict=True)

    report = run_benchmark(
        BenchmarkConfig(
            rust_binary=binary,
            runs=1,
            history_messages=(0, 8),
            response_words=300,
            stream_interval_ms=20,
            input_probes=2,
            timeout_seconds=45,
        )
    )

    assert {(s.renderer, s.history_messages) for s in report.samples} == {
        ("rust", 0),
        ("textual", 0),
        ("rust", 8),
        ("textual", 8),
    }
    for sample in report.samples:
        if sample.history_messages:
            assert sample.history_bytes > 0
        else:
            assert sample.history_bytes == 0
        assert sample.submit_to_final_response_ms > sample.submit_to_first_response_ms
        assert sample.submit_to_final_response_ms >= 5_000
        assert len([p for p in sample.probes if p.phase == "idle"]) == 2
        assert len([p for p in sample.probes if p.phase == "streaming"]) == 2
        assert {n.direction for n in sample.navigation} == {"page_up", "page_down"}
        assert sample.resource_observations > 0
        assert sample.observed_simultaneous_rss_peak_bytes > 0
        assert sample.clean_exit
        assert sample.terminal_restored
