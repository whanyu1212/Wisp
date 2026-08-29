use clap::Parser;
use std::path::PathBuf;
use wisp_tui::transcript_benchmark::{BenchmarkConfig, run};

#[derive(Debug, Parser)]
#[command(about = "Measure bounded Rust TUI transcript work and frame cost")]
struct Arguments {
    #[arg(long, value_delimiter = ',', default_values_t = [1_000, 10_000, 100_000])]
    entries: Vec<usize>,
    #[arg(long, default_value_t = 5)]
    runs: usize,
    #[arg(long, default_value_t = 100)]
    stream_updates: usize,
    #[arg(long)]
    output: Option<PathBuf>,
}

fn main() -> Result<(), Box<dyn std::error::Error>> {
    let arguments = Arguments::parse();
    let report = run(BenchmarkConfig {
        entry_counts: arguments.entries,
        runs: arguments.runs,
        stream_updates: arguments.stream_updates,
        ..BenchmarkConfig::default()
    })?;
    let payload = serde_json::to_string_pretty(&report)?;
    if let Some(path) = arguments.output {
        std::fs::write(path, format!("{payload}\n"))?;
    }
    println!("{payload}");
    Ok(())
}
