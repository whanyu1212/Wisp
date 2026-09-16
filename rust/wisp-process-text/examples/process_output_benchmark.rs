use std::env;
use std::hint::black_box;
use std::process;
use std::time::{Duration, Instant};
use wisp_process_text::PendingText;

const DEFAULT_SIZE: usize = 1024 * 1024;
const DEFAULT_CHUNK: usize = 8_192;
const DEFAULT_ITERATIONS: usize = 5;
const MAX_RETAINED_BYTES: usize = 50_000;
const MAX_RETAINED_LINES: usize = 2_000;
const ASCII_LINES_PATTERN: [u8; 128] = ascii_lines_pattern();
const WORKLOADS: [(&str, &[u8]); 6] = [
    ("ascii_lines", &ASCII_LINES_PATTERN),
    ("unicode", "lambda λ emoji 🙂\n".as_bytes()),
    ("short_lines", b"x\n"),
    ("long_line", b"x"),
    ("mixed_newlines", b"alpha\r\nbeta\rgamma\n"),
    ("invalid_utf8", b"\xff\xfevalid\n"),
];

const fn ascii_lines_pattern() -> [u8; 128] {
    let mut pattern = [b'x'; 128];
    pattern[127] = b'\n';
    pattern
}

#[derive(Clone, Copy, Debug)]
struct Config {
    size: usize,
    chunk: usize,
    iterations: usize,
}

fn main() {
    let config = parse_args().unwrap_or_else(|message| {
        eprintln!("{message}");
        eprintln!(
            "usage: process_output_benchmark [--size BYTES] [--chunk BYTES] [--iterations COUNT]"
        );
        process::exit(2);
    });

    println!(
        "workload\tinput_bytes\tchunk_bytes\titerations\ttotal_ms\tthroughput_mib_s\tmax_chunk_ms\tretained_bytes\tdropped_bytes"
    );
    for (name, pattern) in WORKLOADS {
        let source = repeat_to_size(pattern, config.size);
        let mut total = Duration::ZERO;
        let mut max_chunk = Duration::ZERO;
        let mut final_retained = 0;
        let mut final_dropped = 0;

        for _ in 0..config.iterations {
            let mut pending = PendingText::new(MAX_RETAINED_BYTES, MAX_RETAINED_LINES);
            let started = Instant::now();
            for chunk in source.chunks(config.chunk) {
                let chunk_started = Instant::now();
                pending.append_bytes(black_box(chunk), false);
                max_chunk = max_chunk.max(chunk_started.elapsed());
            }
            pending.append_bytes(&[], true);
            let drain = pending.drain();
            total += started.elapsed();
            final_retained = drain.retained_source_bytes;
            final_dropped = drain.dropped_bytes;
            assert_eq!(
                final_retained + final_dropped,
                source.len(),
                "{name} lost source-byte accounting"
            );
            black_box(drain);
        }

        let total_seconds = total.as_secs_f64();
        let throughput =
            source.len() as f64 * config.iterations as f64 / (1024.0 * 1024.0) / total_seconds;
        println!(
            "{name}\t{}\t{}\t{}\t{:.3}\t{throughput:.3}\t{:.3}\t{final_retained}\t{final_dropped}",
            source.len(),
            config.chunk,
            config.iterations,
            total_seconds * 1_000.0,
            max_chunk.as_secs_f64() * 1_000.0,
        );
    }
}

fn parse_args() -> Result<Config, String> {
    let mut config = Config {
        size: DEFAULT_SIZE,
        chunk: DEFAULT_CHUNK,
        iterations: DEFAULT_ITERATIONS,
    };
    let mut arguments = env::args().skip(1);
    while let Some(argument) = arguments.next() {
        let value = arguments
            .next()
            .ok_or_else(|| format!("missing value for {argument}"))?;
        let parsed = value
            .parse::<usize>()
            .map_err(|_| format!("invalid integer for {argument}: {value}"))?;
        if parsed == 0 {
            return Err(format!("{argument} must be positive"));
        }
        match argument.as_str() {
            "--size" => config.size = parsed,
            "--chunk" => config.chunk = parsed,
            "--iterations" => config.iterations = parsed,
            _ => return Err(format!("unknown argument: {argument}")),
        }
    }
    Ok(config)
}

fn repeat_to_size(pattern: &[u8], size: usize) -> Vec<u8> {
    pattern.iter().copied().cycle().take(size).collect()
}
