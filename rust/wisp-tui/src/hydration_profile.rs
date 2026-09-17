//! Opt-in timings for the saved-history handoff benchmark.

use std::fs::OpenOptions;
use std::io::Write;
use std::time::Duration;

pub(crate) fn record(stage: &str, elapsed: Duration, count: usize) {
    let Some(directory) = std::env::var_os("WISP_HYDRATION_PROFILE_DIR") else {
        return;
    };
    let path =
        std::path::PathBuf::from(directory).join(format!("rust-{}.jsonl", std::process::id()));
    let Ok(mut output) = OpenOptions::new().create(true).append(true).open(path) else {
        return;
    };
    let record = serde_json::json!({
        "stage": stage,
        "duration_ms": elapsed.as_secs_f64() * 1_000.0,
        "count": count,
        "pid": std::process::id(),
    });
    let _ = writeln!(output, "{record}");
}
