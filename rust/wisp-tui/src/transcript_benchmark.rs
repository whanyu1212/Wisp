//! Feature-gated, terminal-independent performance evidence for the Rust transcript.

use crate::LiveUi;
use crate::detail_view::DetailView;
use crate::reducer::{UiState, ViewStatus};
use crate::tool_cards::{ToolCallInput, ToolResultInput, bounded_tool_arguments};
use crate::tool_detail::{
    DETAIL_EXPANDED_MAX_ROWS, DetailAvailability, project_tool_detail_source,
};
use crate::transcript::{Transcript, TranscriptEntryId};
use crate::transcript_view::{LayoutWork, TranscriptViewAction};
use crate::ui::ConnectionInfo;
use nix::sys::resource::{UsageWho, getrusage};
use nix::sys::time::TimeValLike;
use ratatui::Terminal;
use ratatui::backend::TestBackend;
use ratatui::layout::Rect;
use serde::Serialize;
use serde_json::json;
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::process::Command;
use std::time::Instant;
use thiserror::Error;

const FORMAT_VERSION: u32 = 1;
const RICH_SUFFIX_ENTRIES: usize = 512;
const RICH_SUFFIX_TURNS: usize = 253;
const DEFAULT_ENTRY_COUNTS: [usize; 3] = [1_000, 10_000, 100_000];

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct BenchmarkConfig {
    pub entry_counts: Vec<usize>,
    pub runs: usize,
    pub width: u16,
    pub height: u16,
    pub warm_frames: usize,
    pub navigation_cycles: usize,
    pub resize_cycles: usize,
    pub stream_updates: usize,
    pub detail_frames: usize,
}

impl Default for BenchmarkConfig {
    fn default() -> Self {
        Self {
            entry_counts: DEFAULT_ENTRY_COUNTS.into(),
            runs: 5,
            width: 100,
            height: 24,
            warm_frames: 50,
            navigation_cycles: 20,
            resize_cycles: 10,
            stream_updates: 100,
            detail_frames: DETAIL_EXPANDED_MAX_ROWS,
        }
    }
}

impl BenchmarkConfig {
    fn validate(&self) -> Result<(), BenchmarkError> {
        if self.entry_counts.is_empty()
            || self
                .entry_counts
                .iter()
                .any(|count| *count < RICH_SUFFIX_ENTRIES)
        {
            return Err(BenchmarkError::InvalidConfig(format!(
                "entry counts must be at least {RICH_SUFFIX_ENTRIES}"
            )));
        }
        if self.runs == 0
            || self.warm_frames == 0
            || self.navigation_cycles == 0
            || self.resize_cycles == 0
            || self.stream_updates == 0
            || self.detail_frames == 0
        {
            return Err(BenchmarkError::InvalidConfig(
                "runs and workload counts must be positive".into(),
            ));
        }
        if self.width < 50 || self.height < 12 {
            return Err(BenchmarkError::InvalidConfig(
                "benchmark viewport must be at least 50x12".into(),
            ));
        }
        let mut sorted = self.entry_counts.clone();
        sorted.sort_unstable();
        sorted.dedup();
        if sorted.len() != self.entry_counts.len() {
            return Err(BenchmarkError::InvalidConfig(
                "entry counts must be unique".into(),
            ));
        }
        Ok(())
    }
}

#[derive(Debug, Error)]
pub enum BenchmarkError {
    #[error("invalid benchmark configuration: {0}")]
    InvalidConfig(String),
    #[error("benchmark terminal failed: {0}")]
    Terminal(#[from] std::io::Error),
    #[error("benchmark renderer failed: {0}")]
    Renderer(#[from] crate::Error),
    #[error("process CPU measurement failed: {0}")]
    ProcessCpu(String),
    #[error("benchmark fixture invariant failed: {0}")]
    Fixture(String),
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct TimingDistribution {
    pub sample_count: usize,
    pub total_ms: f64,
    pub p50_ms: f64,
    pub p95_ms: f64,
    pub max_ms: f64,
}

impl TimingDistribution {
    fn from_samples(samples: &[f64]) -> Self {
        let mut ordered = samples.to_vec();
        ordered.sort_by(f64::total_cmp);
        if ordered.is_empty() {
            return Self {
                sample_count: 0,
                total_ms: 0.0,
                p50_ms: 0.0,
                p95_ms: 0.0,
                max_ms: 0.0,
            };
        }
        Self {
            sample_count: ordered.len(),
            total_ms: ordered.iter().sum(),
            p50_ms: nearest_rank(&ordered, 0.50),
            p95_ms: nearest_rank(&ordered, 0.95),
            max_ms: ordered.last().copied().unwrap_or(0.0),
        }
    }
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct Correctness {
    pub source_complete: bool,
    pub stream_entry_visible: bool,
    pub follows_tail: bool,
    pub unseen_output_clear: bool,
    pub visible_rows_bounded: bool,
    pub screen_rendered: bool,
    pub detail_rendered: bool,
    pub detail_row_budget_exercised: bool,
    pub detail_tail_rendered: bool,
    pub warm_cache_reused: bool,
    pub stream_work_bounded: bool,
}

impl Correctness {
    fn all_passed(&self) -> bool {
        self.source_complete
            && self.stream_entry_visible
            && self.follows_tail
            && self.unseen_output_clear
            && self.visible_rows_bounded
            && self.screen_rendered
            && self.detail_rendered
            && self.detail_row_budget_exercised
            && self.detail_tail_rendered
            && self.warm_cache_reused
            && self.stream_work_bounded
    }
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct BenchmarkSample {
    pub run: usize,
    pub order: usize,
    pub entry_count: usize,
    pub cold_frame_ms: f64,
    pub warm_frames: TimingDistribution,
    pub navigation_frames: TimingDistribution,
    pub resize_frames: TimingDistribution,
    pub stream_update_ms: TimingDistribution,
    pub stream_draw_ms: TimingDistribution,
    pub stream_stall_ms: TimingDistribution,
    pub detail_open_ms: f64,
    pub detail_frames: TimingDistribution,
    pub stream_process_cpu_ms: f64,
    pub max_synchronous_stall_ms: f64,
    pub stream_source_bytes: usize,
    pub cold_work: LayoutWork,
    pub warm_work: LayoutWork,
    pub navigation_work: LayoutWork,
    pub resize_work: LayoutWork,
    pub stream_work: LayoutWork,
    pub correctness: Correctness,
}

#[derive(Clone, Debug, Eq, PartialEq, Serialize)]
pub struct BenchmarkEnvironment {
    pub os: String,
    pub architecture: String,
    pub kernel: String,
    pub cpu: String,
    pub machine_model: String,
    pub crate_version: String,
    pub rustc: String,
    pub commit: String,
    pub worktree_dirty: bool,
}

#[derive(Clone, Debug, PartialEq, Serialize)]
pub struct BenchmarkReport {
    pub format_version: u32,
    pub config: BenchmarkConfig,
    pub environment: BenchmarkEnvironment,
    pub first_syntax_highlight_ms: f64,
    pub scaling_work_independent: bool,
    pub all_correctness_checks_passed: bool,
    pub samples: Vec<BenchmarkSample>,
}

pub fn run(config: BenchmarkConfig) -> Result<BenchmarkReport, BenchmarkError> {
    config.validate()?;
    let syntax_started = Instant::now();
    crate::syntax::highlight_fence("rust", "fn benchmark_warmup() {}\n")
        .map_err(|failure| BenchmarkError::Fixture(format!("syntax warmup failed: {failure:?}")))?;
    let first_syntax_highlight_ms = elapsed_ms(syntax_started);
    let mut samples = Vec::with_capacity(config.runs.saturating_mul(config.entry_counts.len()));
    for run in 1..=config.runs {
        let order = rotated_counts(&config.entry_counts, run);
        for (index, entry_count) in order.into_iter().enumerate() {
            samples.push(run_sample(&config, run, index + 1, entry_count)?);
        }
    }
    let scaling_work_independent = work_is_independent(&samples, config.entry_counts.len());
    let all_correctness_checks_passed =
        samples.iter().all(|sample| sample.correctness.all_passed());
    Ok(BenchmarkReport {
        format_version: FORMAT_VERSION,
        config,
        environment: environment(),
        first_syntax_highlight_ms,
        scaling_work_independent,
        all_correctness_checks_passed,
        samples,
    })
}

fn run_sample(
    config: &BenchmarkConfig,
    run: usize,
    order: usize,
    entry_count: usize,
) -> Result<BenchmarkSample, BenchmarkError> {
    let Fixture {
        mut ui,
        stream_turn,
        stream_entry,
        detail_entry,
    } = fixture(entry_count)?;
    let connection = benchmark_connection();
    let mut terminal = Terminal::new(TestBackend::new(config.width, config.height))?;

    ui.transcript_row_cache.reset_work();
    let cold_started = Instant::now();
    ui.draw(&mut terminal, &connection)?;
    let cold_frame_ms = elapsed_ms(cold_started);
    let cold_work = ui.transcript_row_cache.work();

    ui.transcript_row_cache.reset_work();
    let mut warm_ms = Vec::with_capacity(config.warm_frames);
    for _ in 0..config.warm_frames {
        warm_ms.push(timed_draw(&mut ui, &mut terminal, &connection)?);
    }
    let warm_work = ui.transcript_row_cache.work();

    ui.transcript_row_cache.reset_work();
    let mut navigation_ms = Vec::with_capacity(config.navigation_cycles.saturating_mul(2));
    for _ in 0..config.navigation_cycles {
        navigation_ms.push(timed_view_action(
            &mut ui,
            &mut terminal,
            &connection,
            TranscriptViewAction::PageUp,
        )?);
        navigation_ms.push(timed_view_action(
            &mut ui,
            &mut terminal,
            &connection,
            TranscriptViewAction::PageDown,
        )?);
    }
    let navigation_work = ui.transcript_row_cache.work();

    ui.transcript_row_cache.reset_work();
    let mut resize_ms = Vec::with_capacity(config.resize_cycles.saturating_mul(2));
    let narrow_width = config.width.saturating_sub(20);
    for _ in 0..config.resize_cycles {
        resize_ms.push(timed_resize(
            &mut ui,
            &mut terminal,
            &connection,
            narrow_width,
            config.height,
        )?);
        resize_ms.push(timed_resize(
            &mut ui,
            &mut terminal,
            &connection,
            config.width,
            config.height,
        )?);
    }
    let resize_work = ui.transcript_row_cache.work();

    ui.transcript_viewport.reduce(
        TranscriptViewAction::FollowTail,
        &ui.state.transcript,
        &mut ui.transcript_row_cache,
    );
    ui.transcript_row_cache.reset_work();
    let cpu_started = process_cpu_microseconds()?;
    let mut stream_update_ms = Vec::with_capacity(config.stream_updates);
    let mut stream_draw_ms = Vec::with_capacity(config.stream_updates);
    let mut stream_stall_ms = Vec::with_capacity(config.stream_updates);
    let mut stream_source = String::new();
    let mut closed_fences = 0_usize;
    for update in 0..config.stream_updates {
        let chunk = stream_chunk(update);
        if update % 10 == 0 {
            closed_fences = closed_fences.saturating_add(1);
        }
        stream_source.push_str(&chunk);
        let stall_started = Instant::now();
        let update_started = Instant::now();
        ui.state
            .transcript
            .append_message_delta(stream_turn, &chunk);
        ui.transcript_viewport.reduce(
            TranscriptViewAction::OutputChanged,
            &ui.state.transcript,
            &mut ui.transcript_row_cache,
        );
        stream_update_ms.push(elapsed_ms(update_started));
        stream_draw_ms.push(timed_draw(&mut ui, &mut terminal, &connection)?);
        stream_stall_ms.push(elapsed_ms(stall_started));
    }
    let stream_process_cpu_ms =
        (process_cpu_microseconds()?.saturating_sub(cpu_started)) as f64 / 1_000.0;
    let stream_work = ui.transcript_row_cache.work();

    let stream_entry_source = ui
        .state
        .transcript
        .entry(stream_entry)
        .map(|entry| entry.content.as_str());
    let source_complete = stream_entry_source == Some(stream_source.as_str());
    let visible_rows = ui
        .transcript_viewport
        .visible_rows(&ui.state.transcript, &mut ui.transcript_row_cache);
    let visible_rows_bounded = visible_rows.len() <= usize::from(config.height);
    let stream_entry_visible = visible_rows
        .iter()
        .any(|row| row.anchor.entry_id == stream_entry);
    let rendered_stream = terminal.backend().to_string();
    let screen_rendered = rendered_stream.contains("WISP")
        && rendered_stream.contains(&stream_marker(config.stream_updates - 1));

    let detail = ui
        .state
        .transcript
        .entry(detail_entry)
        .and_then(|entry| entry.tool_card())
        .and_then(|card| match &card.structured_detail {
            DetailAvailability::LiveRetained(detail) => Some(detail.clone()),
            DetailAvailability::None | DetailAvailability::Unavailable(_) => None,
        })
        .ok_or_else(|| BenchmarkError::Fixture("structured detail was not retained".into()))?;
    let detail_row_budget_exercised =
        detail.rows.len() == crate::tool_detail::DETAIL_EXPANDED_MAX_ROWS;
    let detail_open_started = Instant::now();
    ui.detail_view = DetailView::default();
    ui.detail_view.open(detail_entry, &detail);
    ui.draw(&mut terminal, &connection)?;
    let detail_open_ms = elapsed_ms(detail_open_started);
    let rendered_detail = terminal.backend().to_string();
    let detail_rendered = rendered_detail.contains("live retained detail")
        && rendered_detail.contains("old value 0")
        && rendered_detail.contains("new value 0");
    let last_detail_row_key = detail.rows.last().map(|row| row.key);
    let mut detail_tail_rendered = false;
    let mut detail_ms = Vec::with_capacity(config.detail_frames);
    for _ in 0..config.detail_frames {
        let started = Instant::now();
        ui.detail_view.page_down(&detail);
        ui.draw(&mut terminal, &connection)?;
        detail_ms.push(elapsed_ms(started));
        detail_tail_rendered = ui
            .detail_view
            .visible_rows(&detail)
            .iter()
            .any(|row| Some(row.anchor.row_key) == last_detail_row_key);
        if detail_tail_rendered {
            break;
        }
    }
    let warm_frames = TimingDistribution::from_samples(&warm_ms);
    let navigation_frames = TimingDistribution::from_samples(&navigation_ms);
    let resize_frames = TimingDistribution::from_samples(&resize_ms);
    let stream_update_ms = TimingDistribution::from_samples(&stream_update_ms);
    let stream_draw_ms = TimingDistribution::from_samples(&stream_draw_ms);
    let stream_stall_ms = TimingDistribution::from_samples(&stream_stall_ms);
    let detail_frames = TimingDistribution::from_samples(&detail_ms);
    let max_synchronous_stall_ms = maximum_synchronous_stall(
        cold_frame_ms,
        warm_frames.max_ms,
        navigation_frames.max_ms,
        resize_frames.max_ms,
        stream_stall_ms.max_ms,
        detail_open_ms,
        detail_frames.max_ms,
    );
    let warm_cache_reused = warm_work.rows_built == 0
        && warm_work.bytes_scanned == 0
        && warm_work.markdown_source_bytes_parsed == 0
        && warm_work.markdown_blocks_built == 0
        && warm_work.syntax_source_bytes == 0;
    let stream_work_bounded = stream_work.markdown_full_reparses <= 1
        && stream_work.markdown_source_bytes_parsed <= stream_source.len().saturating_mul(2)
        && stream_work.syntax_fences_highlighted <= closed_fences.saturating_mul(2);

    Ok(BenchmarkSample {
        run,
        order,
        entry_count,
        cold_frame_ms,
        warm_frames,
        navigation_frames,
        resize_frames,
        stream_update_ms,
        stream_draw_ms,
        stream_stall_ms,
        detail_open_ms,
        detail_frames,
        stream_process_cpu_ms,
        max_synchronous_stall_ms,
        stream_source_bytes: stream_source.len(),
        cold_work,
        warm_work,
        navigation_work,
        resize_work,
        stream_work,
        correctness: Correctness {
            source_complete,
            stream_entry_visible,
            follows_tail: ui.transcript_viewport.follows_tail(),
            unseen_output_clear: !ui.transcript_viewport.has_unseen_output(),
            visible_rows_bounded,
            screen_rendered,
            detail_rendered,
            detail_row_budget_exercised,
            detail_tail_rendered,
            warm_cache_reused,
            stream_work_bounded,
        },
    })
}

struct Fixture {
    ui: LiveUi,
    stream_turn: u64,
    stream_entry: TranscriptEntryId,
    detail_entry: TranscriptEntryId,
}

fn fixture(entry_count: usize) -> Result<Fixture, BenchmarkError> {
    let mut ui = LiveUi {
        state: UiState::new("benchmark".into(), Some("rust-tui".into()), None),
        ..LiveUi::default()
    };
    ui.state.view_status = ViewStatus::Running;
    let transcript = &mut *ui.state.transcript;
    let prefix_entries = entry_count.saturating_sub(RICH_SUFFIX_ENTRIES);
    let mut turn = 1_u64;
    if prefix_entries % 2 == 1 {
        transcript.append_prompt("history marker".into());
    }
    while transcript.entries().len() < prefix_entries {
        transcript.append_exchange("historical prompt".into());
        transcript.complete_message(turn, "historical response".into());
        turn = turn.saturating_add(1);
    }

    transcript.append_prompt("rich suffix marker".into());
    for _ in 0..RICH_SUFFIX_TURNS {
        transcript.append_exchange("repeat prompt".into());
        transcript.complete_message(
            turn,
            "## Result\n\n- stable item\n- second item\n\n```rust\nfn stable() {}\n```".into(),
        );
        turn = turn.saturating_add(1);
    }
    add_pending_read_card(transcript);
    add_running_process_card(transcript);
    let detail_entry = add_diff_card(transcript);
    transcript.append_prompt("stream prompt".into());
    let stream_turn = turn;
    let stream_entry = transcript.start_message(stream_turn);

    if transcript.entries().len() != entry_count {
        return Err(BenchmarkError::Fixture(format!(
            "built {} entries instead of {entry_count}",
            transcript.entries().len()
        )));
    }
    Ok(Fixture {
        ui,
        stream_turn,
        stream_entry,
        detail_entry,
    })
}

fn add_pending_read_card(transcript: &mut Transcript) {
    let arguments = json!({"path": "benchmark.txt"});
    transcript.observe_tool_call(ToolCallInput {
        call_id: "benchmark-read".into(),
        name: "read".into(),
        arguments: bounded_tool_arguments("read", &arguments),
        detail_source: project_tool_detail_source("read", arguments.as_object().unwrap()),
    });
}

fn add_running_process_card(transcript: &mut Transcript) {
    let arguments = json!({"operation": "poll", "process_id": "benchmark-process"});
    transcript.observe_tool_call(ToolCallInput {
        call_id: "benchmark-process-poll".into(),
        name: "bash".into(),
        arguments: bounded_tool_arguments("bash", &arguments),
        detail_source: project_tool_detail_source("bash", arguments.as_object().unwrap()),
    });
    transcript.observe_tool_result(ToolResultInput {
        process_id: Some("benchmark-process".into()),
        process_state: Some("running".into()),
        stdout: Some("bounded process output\n".repeat(32)),
        stdout_source_bytes: 736,
        ..tool_result("benchmark-process-poll", "bash", "Process is still running")
    });
}

fn add_diff_card(transcript: &mut Transcript) -> TranscriptEntryId {
    let mut before = String::new();
    let mut after = String::new();
    for index in 0..250 {
        writeln!(before, "stable line {index}").expect("writing to String cannot fail");
        writeln!(before, "old value {index}").expect("writing to String cannot fail");
        writeln!(after, "stable line {index}").expect("writing to String cannot fail");
        writeln!(after, "new value {index}").expect("writing to String cannot fail");
    }
    let arguments = json!({"path": "benchmark.txt", "content": after});
    let entry = transcript.observe_tool_call(ToolCallInput {
        call_id: "benchmark-write".into(),
        name: "write".into(),
        arguments: bounded_tool_arguments("write", &arguments),
        detail_source: project_tool_detail_source("write", arguments.as_object().unwrap()),
    });
    transcript.observe_tool_result(ToolResultInput {
        before_text: Some(before),
        summary: Some("write: 500 lines".into()),
        ..tool_result("benchmark-write", "write", "Wrote benchmark.txt")
    });
    entry
}

fn tool_result(call_id: &str, name: &str, output: &str) -> ToolResultInput {
    ToolResultInput {
        call_id: call_id.into(),
        name: name.into(),
        output: output.into(),
        output_tail: None,
        output_source_bytes: output.len() as u64,
        output_source_lines: 1,
        output_projection_cut_mid_line: false,
        is_error: false,
        failure_code: None,
        retryable: false,
        recovery_hint: None,
        exit_code: None,
        output_has_exit_status: false,
        before_text: None,
        created: false,
        summary: None,
        truncated: false,
        process_id: None,
        process_state: None,
        process_error: None,
        stdout: None,
        stdout_source_bytes: 0,
        stderr: None,
        stderr_source_bytes: 0,
        stdout_truncated: false,
        stderr_truncated: false,
        stdout_dropped_bytes: 0,
        stderr_dropped_bytes: 0,
    }
}

fn stream_marker(update: usize) -> String {
    if update % 10 == 0 {
        format!("update_{update}")
    } else {
        format!("Stream paragraph {update}")
    }
}

fn stream_chunk(update: usize) -> String {
    if update % 10 == 0 {
        format!("## Stream section {update}\n\n```rust\nfn update_{update}() {{}}\n```\n\n")
    } else {
        format!("Stream paragraph {update} with **bounded Markdown**.\n\n")
    }
}

fn timed_draw(
    ui: &mut LiveUi,
    terminal: &mut Terminal<TestBackend>,
    connection: &ConnectionInfo,
) -> Result<f64, BenchmarkError> {
    let started = Instant::now();
    ui.draw(terminal, connection)?;
    Ok(elapsed_ms(started))
}

fn timed_view_action(
    ui: &mut LiveUi,
    terminal: &mut Terminal<TestBackend>,
    connection: &ConnectionInfo,
    action: TranscriptViewAction,
) -> Result<f64, BenchmarkError> {
    let started = Instant::now();
    ui.transcript_viewport
        .reduce(action, &ui.state.transcript, &mut ui.transcript_row_cache);
    ui.draw(terminal, connection)?;
    Ok(elapsed_ms(started))
}

fn timed_resize(
    ui: &mut LiveUi,
    terminal: &mut Terminal<TestBackend>,
    connection: &ConnectionInfo,
    width: u16,
    height: u16,
) -> Result<f64, BenchmarkError> {
    terminal.backend_mut().resize(width, height);
    let started = Instant::now();
    terminal.resize(Rect::new(0, 0, width, height))?;
    ui.draw(terminal, connection)?;
    Ok(elapsed_ms(started))
}

fn process_cpu_microseconds() -> Result<i64, BenchmarkError> {
    let usage = getrusage(UsageWho::RUSAGE_SELF)
        .map_err(|error| BenchmarkError::ProcessCpu(error.to_string()))?;
    Ok(usage
        .user_time()
        .num_microseconds()
        .saturating_add(usage.system_time().num_microseconds()))
}

fn benchmark_connection() -> ConnectionInfo {
    ConnectionInfo {
        backend_version: env!("CARGO_PKG_VERSION").into(),
        protocol_version: wisp_protocol::LIVE_RPC_PROTOCOL_VERSION,
        event_schema_version: wisp_protocol::EVENT_SCHEMA_VERSION,
    }
}

fn work_is_independent(samples: &[BenchmarkSample], condition_count: usize) -> bool {
    if condition_count < 2 {
        return false;
    }
    let mut by_run: BTreeMap<
        usize,
        (
            &LayoutWork,
            &LayoutWork,
            &LayoutWork,
            &LayoutWork,
            &LayoutWork,
        ),
    > = BTreeMap::new();
    for sample in samples {
        let signature = (
            &sample.cold_work,
            &sample.warm_work,
            &sample.navigation_work,
            &sample.resize_work,
            &sample.stream_work,
        );
        if let Some(expected) = by_run.get(&sample.run) {
            if *expected != signature {
                return false;
            }
        } else {
            by_run.insert(sample.run, signature);
        }
    }
    true
}

fn rotated_counts(values: &[usize], run: usize) -> Vec<usize> {
    let offset = (run - 1) % values.len();
    values[offset..]
        .iter()
        .chain(&values[..offset])
        .copied()
        .collect()
}

fn environment() -> BenchmarkEnvironment {
    let status = command_output("git", &["status", "--porcelain"]);
    BenchmarkEnvironment {
        os: std::env::consts::OS.into(),
        architecture: std::env::consts::ARCH.into(),
        kernel: command_output("uname", &["-sr"]).unwrap_or_else(|| "unknown".into()),
        cpu: cpu_name(),
        machine_model: machine_model(),
        crate_version: env!("CARGO_PKG_VERSION").into(),
        rustc: command_output("rustc", &["--version"]).unwrap_or_else(|| "unknown".into()),
        commit: command_output("git", &["rev-parse", "HEAD"]).unwrap_or_else(|| "unknown".into()),
        worktree_dirty: worktree_is_dirty(status.as_deref()),
    }
}

fn cpu_name() -> String {
    command_output("sysctl", &["-n", "machdep.cpu.brand_string"])
        .and_then(|value| normalized_metadata(&value))
        .or_else(|| {
            std::fs::read_to_string("/proc/cpuinfo")
                .ok()
                .and_then(|cpuinfo| cpu_name_from_proc(&cpuinfo))
        })
        .unwrap_or_else(|| "unknown".into())
}

fn machine_model() -> String {
    command_output("sysctl", &["-n", "hw.model"])
        .and_then(|value| normalized_metadata(&value))
        .or_else(|| {
            [
                "/sys/devices/virtual/dmi/id/product_name",
                "/proc/device-tree/model",
            ]
            .into_iter()
            .find_map(|path| {
                std::fs::read_to_string(path)
                    .ok()
                    .and_then(|value| normalized_metadata(&value))
            })
        })
        .unwrap_or_else(|| "unknown".into())
}

fn cpu_name_from_proc(cpuinfo: &str) -> Option<String> {
    cpuinfo.lines().find_map(|line| {
        let (key, value) = line.split_once(':')?;
        matches!(key.trim(), "model name" | "Hardware" | "Processor")
            .then(|| normalized_metadata(value))
            .flatten()
    })
}

fn normalized_metadata(value: &str) -> Option<String> {
    let value =
        value.trim_matches(|character: char| character.is_whitespace() || character == '\0');
    (!value.is_empty()).then(|| value.to_owned())
}

fn worktree_is_dirty(status: Option<&str>) -> bool {
    status.is_none_or(|output| !output.is_empty())
}

fn command_output(program: &str, arguments: &[&str]) -> Option<String> {
    let output = Command::new(program).args(arguments).output().ok()?;
    output
        .status
        .success()
        .then(|| String::from_utf8_lossy(&output.stdout).trim().to_owned())
}

fn nearest_rank(ordered: &[f64], percentile: f64) -> f64 {
    let index = ((percentile * ordered.len() as f64).ceil() as usize)
        .saturating_sub(1)
        .min(ordered.len().saturating_sub(1));
    ordered[index]
}

fn maximum_synchronous_stall(
    cold_frame_ms: f64,
    warm_max_ms: f64,
    navigation_max_ms: f64,
    resize_max_ms: f64,
    stream_max_ms: f64,
    detail_open_ms: f64,
    detail_max_ms: f64,
) -> f64 {
    [
        warm_max_ms,
        navigation_max_ms,
        resize_max_ms,
        stream_max_ms,
        detail_open_ms,
        detail_max_ms,
    ]
    .into_iter()
    .fold(cold_frame_ms, f64::max)
}

fn elapsed_ms(started: Instant) -> f64 {
    started.elapsed().as_secs_f64() * 1_000.0
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn linux_metadata_fallbacks_are_nonempty_and_normalized() {
        assert_eq!(
            cpu_name_from_proc("processor: 0\nmodel name: Unit Test CPU\n"),
            Some("Unit Test CPU".into())
        );
        assert_eq!(
            normalized_metadata(" Unit Test Model\0\n"),
            Some("Unit Test Model".into())
        );
        assert_eq!(cpu_name_from_proc("processor: 0\n"), None);
        assert_eq!(normalized_metadata("\0\n"), None);
        assert!(!worktree_is_dirty(Some("")));
        assert!(worktree_is_dirty(Some("M tracked-file")));
        assert!(worktree_is_dirty(None));
    }

    #[test]
    fn detail_open_contributes_to_maximum_synchronous_stall() {
        assert_eq!(
            maximum_synchronous_stall(1.0, 2.0, 3.0, 4.0, 5.0, 7.0, 6.0),
            7.0
        );
    }

    #[test]
    fn timing_distribution_uses_nearest_rank() {
        let distribution =
            TimingDistribution::from_samples(&(1..=20).map(f64::from).collect::<Vec<_>>());
        assert_eq!(distribution.sample_count, 20);
        assert_eq!(distribution.p50_ms, 10.0);
        assert_eq!(distribution.p95_ms, 19.0);
        assert_eq!(distribution.max_ms, 20.0);
    }

    #[test]
    fn benchmark_smoke_preserves_bounded_work_and_correctness() {
        let report = run(BenchmarkConfig {
            entry_counts: vec![512, 1_024],
            runs: 1,
            width: 80,
            height: 16,
            warm_frames: 2,
            navigation_cycles: 1,
            resize_cycles: 1,
            stream_updates: 5,
            detail_frames: DETAIL_EXPANDED_MAX_ROWS,
        })
        .unwrap();

        assert_eq!(report.samples.len(), 2);
        assert!(report.scaling_work_independent);
        assert!(report.samples.iter().all(|sample| {
            sample.detail_open_ms.is_finite() && sample.correctness.detail_tail_rendered
        }));
        assert!(
            report.all_correctness_checks_passed,
            "correctness failed: {:#?}",
            report.samples
        );
        assert!(
            report
                .samples
                .iter()
                .all(|sample| sample.correctness.all_passed())
        );
    }

    #[test]
    fn single_condition_does_not_claim_scaling_work_independence() {
        let report = run(BenchmarkConfig {
            entry_counts: vec![RICH_SUFFIX_ENTRIES],
            runs: 1,
            width: 80,
            height: 16,
            warm_frames: 1,
            navigation_cycles: 1,
            resize_cycles: 1,
            stream_updates: 1,
            detail_frames: DETAIL_EXPANDED_MAX_ROWS,
        })
        .unwrap();

        assert_eq!(report.samples.len(), 1);
        assert!(!report.scaling_work_independent);
        assert!(report.all_correctness_checks_passed);
    }

    #[test]
    fn benchmark_config_rejects_unrepresentative_or_empty_workloads() {
        let too_short = BenchmarkConfig {
            entry_counts: vec![RICH_SUFFIX_ENTRIES - 1],
            ..BenchmarkConfig::default()
        };
        assert!(matches!(
            run(too_short),
            Err(BenchmarkError::InvalidConfig(_))
        ));
        let no_runs = BenchmarkConfig {
            runs: 0,
            ..BenchmarkConfig::default()
        };
        assert!(matches!(
            run(no_runs),
            Err(BenchmarkError::InvalidConfig(_))
        ));
    }
}
