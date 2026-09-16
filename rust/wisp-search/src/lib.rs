//! Bounded literal scanning for securely opened files.

#![forbid(unsafe_code)]

use std::collections::VecDeque;
use std::fs::File;
use std::io::{self, Read};
use std::sync::atomic::{AtomicBool, Ordering};

const READ_BUFFER_BYTES: usize = 64 * 1024;

/// Configuration for scanning one file.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScanConfig {
    /// Case-sensitive literal to find. Must not be empty.
    pub pattern: String,
    /// Display path used in emitted records.
    pub display_path: String,
    /// Number of lines retained before and after a match.
    pub context_lines: usize,
    /// Number of matches that may still be retained across the whole search.
    pub remaining_matches: usize,
    /// Lines already retained for earlier files.
    pub prior_lines: usize,
    /// UTF-8 bytes already retained for earlier files, including their separators.
    pub prior_bytes: usize,
    /// Whether a `--` context-group separator precedes this file's first record.
    pub prefix_separator: bool,
    /// Maximum retained lines across the whole search.
    pub max_output_lines: usize,
    /// Maximum retained UTF-8 bytes across the whole search.
    pub max_output_bytes: usize,
    /// Maximum Unicode scalar values accepted in one source line.
    pub max_line_chars: usize,
}

impl ScanConfig {
    fn validate(&self) -> Result<(), ScanError> {
        if self.pattern.is_empty() {
            return Err(ScanError::InvalidConfig(
                "literal grep pattern must not be empty".to_owned(),
            ));
        }
        if self.max_line_chars == 0 {
            return Err(ScanError::InvalidConfig(
                "max_line_chars must be greater than zero".to_owned(),
            ));
        }
        Ok(())
    }
}

/// Terminal status of a scan.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ScanStatus {
    /// Scanning completed or stopped at an output or match bound.
    Complete,
    /// The file contained NUL or invalid UTF-8; all results were discarded.
    Binary,
    /// Cancellation was requested; all results were discarded.
    Cancelled,
}

impl ScanStatus {
    /// Stable lowercase value used by language bindings.
    #[must_use]
    pub const fn as_str(self) -> &'static str {
        match self {
            Self::Complete => "complete",
            Self::Binary => "binary",
            Self::Cancelled => "cancelled",
        }
    }
}

/// Bounded records produced while scanning one file.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScanResult {
    /// Formatted match and context records, excluding a leading `--` separator.
    pub lines: Vec<String>,
    /// UTF-8 bytes contributed by `lines`, including line joiners but excluding `\n--`.
    pub byte_count: usize,
    /// Retained matching records.
    pub match_count: usize,
    /// Whether one additional match was observed after the match budget was spent.
    pub had_extra_match: bool,
    /// Whether a line or byte bound was reached.
    pub exhausted: bool,
    /// Terminal scan status.
    pub status: ScanStatus,
}

impl ScanResult {
    fn discarded(status: ScanStatus) -> Self {
        Self {
            lines: Vec::new(),
            byte_count: 0,
            match_count: 0,
            had_extra_match: false,
            exhausted: false,
            status,
        }
    }
}

/// Failure to configure, open, or read a native scan.
#[derive(Debug)]
pub enum ScanError {
    /// The caller supplied an invalid scan configuration.
    InvalidConfig(String),
    /// Opening or reading the duplicated descriptor failed.
    Io(io::Error),
    /// A source line exceeded the configured character limit.
    LineTooLong { max_chars: usize },
    /// Descriptor duplication is not implemented on this target.
    UnsupportedTarget,
}

impl std::fmt::Display for ScanError {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::InvalidConfig(message) => formatter.write_str(message),
            Self::Io(error) => write!(formatter, "native grep I/O error: {error}"),
            Self::LineTooLong { max_chars } => {
                write!(
                    formatter,
                    "grep encountered a line longer than {max_chars} characters"
                )
            }
            Self::UnsupportedTarget => formatter
                .write_str("native descriptor scanning is supported only on Linux and macOS"),
        }
    }
}

impl std::error::Error for ScanError {
    fn source(&self) -> Option<&(dyn std::error::Error + 'static)> {
        match self {
            Self::Io(error) => Some(error),
            _ => None,
        }
    }
}

impl From<io::Error> for ScanError {
    fn from(error: io::Error) -> Self {
        Self::Io(error)
    }
}

/// Duplicates an already authorized descriptor and scans its current file.
///
/// The filesystem pathname originally used to authorize and open the file is never reopened.
pub fn scan_literal_fd(
    descriptor: i64,
    config: &ScanConfig,
    cancellation: &AtomicBool,
) -> Result<ScanResult, ScanError> {
    let mut file = open_descriptor(descriptor)?;
    scan_literal(&mut file, config, cancellation)
}

/// Scans valid UTF-8 from `reader` for a bounded, case-sensitive literal.
pub fn scan_literal(
    reader: &mut impl Read,
    config: &ScanConfig,
    cancellation: &AtomicBool,
) -> Result<ScanResult, ScanError> {
    config.validate()?;
    let mut scanner = Scanner::new(config, cancellation);
    let mut buffer = [0_u8; READ_BUFFER_BYTES];
    let mut pending_utf8 = Vec::with_capacity(4);

    loop {
        if scanner.cancelled() {
            return Ok(ScanResult::discarded(ScanStatus::Cancelled));
        }
        let read = reader.read(&mut buffer)?;
        if read == 0 {
            break;
        }
        if buffer[..read].contains(&0) {
            return Ok(ScanResult::discarded(ScanStatus::Binary));
        }

        if pending_utf8.is_empty() {
            match process_utf8_chunk(&buffer[..read], &mut scanner)? {
                ChunkOutcome::Continue => {}
                ChunkOutcome::Incomplete(suffix) => pending_utf8.extend_from_slice(suffix),
                ChunkOutcome::Binary => {
                    return Ok(ScanResult::discarded(ScanStatus::Binary));
                }
                ChunkOutcome::Stopped => return Ok(scanner.finish()),
            }
        } else {
            pending_utf8.extend_from_slice(&buffer[..read]);
            let combined = std::mem::take(&mut pending_utf8);
            match process_utf8_chunk(&combined, &mut scanner)? {
                ChunkOutcome::Continue => {}
                ChunkOutcome::Incomplete(suffix) => pending_utf8.extend_from_slice(suffix),
                ChunkOutcome::Binary => {
                    return Ok(ScanResult::discarded(ScanStatus::Binary));
                }
                ChunkOutcome::Stopped => return Ok(scanner.finish()),
            }
        }
    }

    if !pending_utf8.is_empty() {
        return Ok(ScanResult::discarded(ScanStatus::Binary));
    }
    if scanner.cancelled() {
        return Ok(ScanResult::discarded(ScanStatus::Cancelled));
    }
    scanner.end_of_file()?;
    Ok(scanner.finish())
}

#[cfg(any(target_os = "linux", target_os = "macos"))]
fn open_descriptor(descriptor: i64) -> Result<File, ScanError> {
    if descriptor < 0 {
        return Err(ScanError::InvalidConfig(
            "file descriptor must be non-negative".to_owned(),
        ));
    }
    #[cfg(target_os = "linux")]
    let path = format!("/proc/self/fd/{descriptor}");
    #[cfg(target_os = "macos")]
    let path = format!("/dev/fd/{descriptor}");
    Ok(File::open(path)?)
}

#[cfg(not(any(target_os = "linux", target_os = "macos")))]
fn open_descriptor(_descriptor: i64) -> Result<File, ScanError> {
    Err(ScanError::UnsupportedTarget)
}

enum ChunkOutcome<'a> {
    Continue,
    Incomplete(&'a [u8]),
    Binary,
    Stopped,
}

fn process_utf8_chunk<'a>(
    bytes: &'a [u8],
    scanner: &mut Scanner<'_>,
) -> Result<ChunkOutcome<'a>, ScanError> {
    match std::str::from_utf8(bytes) {
        Ok(text) => {
            if scanner.push_text(text)? {
                Ok(ChunkOutcome::Stopped)
            } else {
                Ok(ChunkOutcome::Continue)
            }
        }
        Err(error) => {
            if error.error_len().is_some() {
                return Ok(ChunkOutcome::Binary);
            }
            let valid = &bytes[..error.valid_up_to()];
            // SAFETY is unnecessary: `valid_up_to` identifies a valid UTF-8 prefix.
            let valid = std::str::from_utf8(valid).map_err(|_| {
                ScanError::InvalidConfig("UTF-8 validator returned an invalid prefix".to_owned())
            })?;
            if scanner.push_text(valid)? {
                return Ok(ChunkOutcome::Stopped);
            }
            Ok(ChunkOutcome::Incomplete(&bytes[error.valid_up_to()..]))
        }
    }
}

struct Scanner<'a> {
    config: &'a ScanConfig,
    cancellation: &'a AtomicBool,
    output: BoundedOutput,
    preceding: VecDeque<(usize, String)>,
    current_line: String,
    current_line_chars: usize,
    pending_carriage_return: bool,
    line_number: usize,
    group_end: usize,
    last_emitted_line: usize,
    match_count: usize,
    had_extra_match: bool,
}

impl<'a> Scanner<'a> {
    fn new(config: &'a ScanConfig, cancellation: &'a AtomicBool) -> Self {
        Self {
            config,
            cancellation,
            output: BoundedOutput::new(config),
            preceding: VecDeque::new(),
            current_line: String::new(),
            current_line_chars: 0,
            pending_carriage_return: false,
            line_number: 0,
            group_end: 0,
            last_emitted_line: 0,
            match_count: 0,
            had_extra_match: false,
        }
    }

    fn cancelled(&self) -> bool {
        self.cancellation.load(Ordering::Relaxed)
    }

    fn push_text(&mut self, text: &str) -> Result<bool, ScanError> {
        if self.cancelled() {
            return Ok(true);
        }
        for character in text.chars() {
            if self.pending_carriage_return {
                if self.emit_current_line()? {
                    return Ok(true);
                }
                self.pending_carriage_return = false;
                if character == '\n' {
                    continue;
                }
            }

            match character {
                '\r' => self.pending_carriage_return = true,
                '\n' | '\u{000b}' | '\u{000c}' | '\u{001c}' | '\u{001d}' | '\u{001e}'
                | '\u{0085}' | '\u{2028}' | '\u{2029}' => {
                    if self.emit_current_line()? {
                        return Ok(true);
                    }
                }
                _ => {
                    self.current_line.push(character);
                    self.current_line_chars = self.current_line_chars.saturating_add(1);
                    if self.current_line_chars > self.config.max_line_chars {
                        return Err(ScanError::LineTooLong {
                            max_chars: self.config.max_line_chars,
                        });
                    }
                }
            }
        }
        Ok(false)
    }

    fn end_of_file(&mut self) -> Result<(), ScanError> {
        if self.pending_carriage_return {
            self.emit_current_line()?;
            self.pending_carriage_return = false;
        } else if !self.current_line.is_empty() {
            self.emit_current_line()?;
        }
        Ok(())
    }

    /// Returns true once scanning can stop without looking at more input.
    fn emit_current_line(&mut self) -> Result<bool, ScanError> {
        if self.cancelled() {
            return Ok(true);
        }
        self.line_number = self.line_number.saturating_add(1);
        let line = std::mem::take(&mut self.current_line);
        self.current_line_chars = 0;
        let is_match = line.contains(&self.config.pattern);

        if is_match {
            if self.match_count >= self.config.remaining_matches {
                self.had_extra_match = true;
            } else {
                self.match_count += 1;
                if self.line_number > self.group_end {
                    for (number, text) in &self.preceding {
                        if *number > self.last_emitted_line {
                            self.output.append(
                                format_record(&self.config.display_path, *number, text, false),
                                false,
                            );
                            self.last_emitted_line = *number;
                            if self.output.exhausted {
                                break;
                            }
                        }
                    }
                }
                self.output.append(
                    format_record(&self.config.display_path, self.line_number, &line, true),
                    true,
                );
                self.last_emitted_line = self.line_number;
                self.group_end = self
                    .group_end
                    .max(self.line_number.saturating_add(self.config.context_lines));
            }
        } else if self.line_number <= self.group_end {
            self.output.append(
                format_record(&self.config.display_path, self.line_number, &line, false),
                false,
            );
            self.last_emitted_line = self.line_number;
        }

        if self.config.context_lines > 0 {
            if self.preceding.len() == self.config.context_lines {
                self.preceding.pop_front();
            }
            self.preceding.push_back((self.line_number, line));
        }
        Ok(self.output.exhausted || self.had_extra_match || self.cancelled())
    }

    fn finish(self) -> ScanResult {
        if self.cancelled() {
            return ScanResult::discarded(ScanStatus::Cancelled);
        }
        ScanResult {
            lines: self.output.lines,
            byte_count: self.output.byte_count,
            match_count: self.match_count,
            had_extra_match: self.had_extra_match,
            exhausted: self.output.exhausted,
            status: ScanStatus::Complete,
        }
    }
}

struct BoundedOutput {
    prior_lines: usize,
    prior_bytes: usize,
    prefix_separator: bool,
    max_lines: usize,
    max_bytes: usize,
    lines: Vec<String>,
    byte_count: usize,
    exhausted: bool,
}

impl BoundedOutput {
    fn new(config: &ScanConfig) -> Self {
        Self {
            prior_lines: config.prior_lines,
            prior_bytes: config.prior_bytes,
            prefix_separator: config.prefix_separator,
            max_lines: config.max_output_lines,
            max_bytes: config.max_output_bytes,
            lines: Vec::new(),
            byte_count: 0,
            exhausted: false,
        }
    }

    fn append(&mut self, record: String, preserve_match: bool) {
        let separator_lines = usize::from(self.prefix_separator && self.lines.is_empty());
        let separator_bytes = if separator_lines == 1 { 3 } else { 0 };
        let newline_bytes =
            usize::from(self.prior_lines > 0 || !self.lines.is_empty() || separator_lines > 0);
        let total_lines = self
            .prior_lines
            .saturating_add(separator_lines)
            .saturating_add(self.lines.len())
            .saturating_add(1);
        let total_bytes = self
            .prior_bytes
            .saturating_add(separator_bytes)
            .saturating_add(self.byte_count)
            .saturating_add(record.len())
            .saturating_add(newline_bytes);
        let would_exceed = total_lines > self.max_lines || total_bytes > self.max_bytes;
        if would_exceed && !preserve_match {
            self.exhausted = true;
            return;
        }
        self.byte_count = self
            .byte_count
            .saturating_add(record.len())
            .saturating_add(newline_bytes);
        self.lines.push(record);
        self.exhausted = would_exceed;
    }
}

fn format_record(display_path: &str, line_number: usize, line: &str, is_match: bool) -> String {
    let separator = if is_match { ':' } else { '-' };
    format!("{display_path}{separator}{line_number}{separator}{line}")
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::io::{Cursor, Read};

    fn config(pattern: &str) -> ScanConfig {
        ScanConfig {
            pattern: pattern.to_owned(),
            display_path: "src/data.txt".to_owned(),
            context_lines: 0,
            remaining_matches: 100,
            prior_lines: 0,
            prior_bytes: 0,
            prefix_separator: false,
            max_output_lines: 2_000,
            max_output_bytes: 50_000,
            max_line_chars: 1_000_000,
        }
    }

    fn scan(bytes: &[u8], config: &ScanConfig) -> Result<ScanResult, ScanError> {
        scan_literal(&mut Cursor::new(bytes), config, &AtomicBool::new(false))
    }

    #[test]
    fn scans_literal_unicode_and_all_splitlines_boundaries() {
        let text = "β needle\nβ needle\rβ needle\r\nβ needle\u{000b}β needle\u{000c}β needle\u{001c}β needle\u{001d}β needle\u{001e}β needle\u{0085}β needle\u{2028}β needle\u{2029}β needle";
        let result = scan(text.as_bytes(), &config("β needle")).unwrap();

        assert_eq!(result.lines.len(), 12);
        assert_eq!(result.lines[0], "src/data.txt:1:β needle");
        assert_eq!(result.lines[11], "src/data.txt:12:β needle");
        assert_eq!(result.match_count, 12);
        assert_eq!(result.status, ScanStatus::Complete);
    }

    #[test]
    fn preserves_context_groups_and_record_separators() {
        let mut config = config("hit");
        config.context_lines = 1;
        let result = scan(b"before\nhit\nafter\ngap\nhit again\ntail\n", &config).unwrap();

        assert_eq!(
            result.lines,
            [
                "src/data.txt-1-before",
                "src/data.txt:2:hit",
                "src/data.txt-3-after",
                "src/data.txt-4-gap",
                "src/data.txt:5:hit again",
                "src/data.txt-6-tail",
            ]
        );
        assert_eq!(result.match_count, 2);
    }

    #[test]
    fn match_budget_uses_one_match_lookahead() {
        let mut config = config("hit");
        config.remaining_matches = 1;
        let result = scan(b"hit one\nnope\nhit two\nnever read", &config).unwrap();

        assert_eq!(result.lines, ["src/data.txt:1:hit one"]);
        assert_eq!(result.match_count, 1);
        assert!(result.had_extra_match);
    }

    #[test]
    fn bounds_include_prefix_separator_and_preserve_oversized_match() {
        let mut config = config("hit");
        config.prior_lines = 1;
        config.prior_bytes = 8;
        config.prefix_separator = true;
        config.max_output_lines = 2;
        config.max_output_bytes = 10;
        let result = scan(b"hit with a long record\n", &config).unwrap();

        assert_eq!(result.lines, ["src/data.txt:1:hit with a long record"]);
        assert_eq!(result.byte_count, result.lines[0].len() + 1);
        assert_eq!(result.match_count, 1);
        assert!(result.exhausted);
    }

    #[test]
    fn context_over_byte_bound_is_omitted_before_preserved_match() {
        let mut config = config("hit");
        config.context_lines = 1;
        config.max_output_bytes = 0;
        let result = scan(b"before\nhit\n", &config).unwrap();

        assert_eq!(result.lines, ["src/data.txt:2:hit"]);
        assert_eq!(result.byte_count, result.lines[0].len());
        assert_eq!(result.match_count, 1);
        assert!(result.exhausted);
    }

    #[test]
    fn exact_output_byte_bound_is_not_exhausted() {
        let expected = "src/data.txt:1:hit";
        let mut config = config("hit");
        config.max_output_bytes = expected.len();
        let result = scan(b"hit\n", &config).unwrap();

        assert_eq!(result.lines, [expected]);
        assert_eq!(result.byte_count, expected.len());
        assert!(!result.exhausted);
    }

    #[test]
    fn context_record_exhaustion_stops_before_unneeded_input() {
        let mut config = config("hit");
        config.context_lines = 1;
        config.max_output_lines = 1;
        let result = scan(b"hit\nafter\nhit again\n", &config).unwrap();

        assert_eq!(result.lines, ["src/data.txt:1:hit"]);
        assert_eq!(result.match_count, 1);
        assert!(result.exhausted);
        assert!(!result.had_extra_match);
    }

    #[test]
    fn binary_and_invalid_utf8_discard_pending_results() {
        for bytes in [
            b"hit\nthen\0binary".as_slice(),
            b"hit\nthen\xffinvalid".as_slice(),
        ] {
            let result = scan(bytes, &config("hit")).unwrap();
            assert_eq!(result, ScanResult::discarded(ScanStatus::Binary));
        }
    }

    #[test]
    fn rejects_lines_over_character_limit() {
        let mut config = config("x");
        config.max_line_chars = 3;
        let error = scan("éééé\n".as_bytes(), &config).unwrap_err();
        assert!(matches!(error, ScanError::LineTooLong { max_chars: 3 }));
    }

    #[test]
    fn rejects_invalid_configuration() {
        let mut empty_pattern = config("");
        assert!(matches!(
            scan(b"text", &empty_pattern).unwrap_err(),
            ScanError::InvalidConfig(_)
        ));

        empty_pattern.pattern = "text".to_owned();
        empty_pattern.max_line_chars = 0;
        assert!(matches!(
            scan(b"text", &empty_pattern).unwrap_err(),
            ScanError::InvalidConfig(_)
        ));
    }

    struct ChunkedReader {
        bytes: Cursor<Vec<u8>>,
        chunk_size: usize,
    }

    impl Read for ChunkedReader {
        fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
            let limit = buffer.len().min(self.chunk_size);
            self.bytes.read(&mut buffer[..limit])
        }
    }

    #[test]
    fn preserves_crlf_and_utf8_across_read_chunks() {
        let mut reader = ChunkedReader {
            bytes: Cursor::new("one\r\nβ needle\rthree".as_bytes().to_vec()),
            chunk_size: 1,
        };
        let result =
            scan_literal(&mut reader, &config("β needle"), &AtomicBool::new(false)).unwrap();

        assert_eq!(result.lines, ["src/data.txt:2:β needle"]);
    }

    #[test]
    fn consecutive_and_trailing_boundaries_preserve_line_numbers() {
        let result = scan(b"\n\r\n\rhit\n", &config("hit")).unwrap();

        assert_eq!(result.lines, ["src/data.txt:4:hit"]);
    }

    #[test]
    fn incomplete_utf8_at_end_discards_pending_results() {
        let result = scan(b"hit\ntruncated \xe2\x82", &config("hit")).unwrap();

        assert_eq!(result, ScanResult::discarded(ScanStatus::Binary));
    }

    #[test]
    fn cancellation_discards_pending_results() {
        struct CancellingReader<'a> {
            inner: Cursor<&'a [u8]>,
            cancellation: &'a AtomicBool,
            reads: usize,
        }

        impl Read for CancellingReader<'_> {
            fn read(&mut self, buffer: &mut [u8]) -> io::Result<usize> {
                self.reads += 1;
                if self.reads > 1 {
                    self.cancellation.store(true, Ordering::Relaxed);
                }
                let read_limit = buffer.len().min(4);
                self.inner.read(&mut buffer[..read_limit])
            }
        }

        let cancellation = AtomicBool::new(false);
        let mut reader = CancellingReader {
            inner: Cursor::new(b"hit\nmore\n".as_slice()),
            cancellation: &cancellation,
            reads: 0,
        };
        let result = scan_literal(&mut reader, &config("hit"), &cancellation).unwrap();

        assert_eq!(result, ScanResult::discarded(ScanStatus::Cancelled));
    }

    #[test]
    fn output_byte_count_uses_utf8_bytes_and_existing_output_newline() {
        let mut config = config("é");
        config.prior_lines = 1;
        config.prior_bytes = 4;
        let result = scan("é\n".as_bytes(), &config).unwrap();

        assert_eq!(result.lines, ["src/data.txt:1:é"]);
        assert_eq!(result.byte_count, result.lines[0].len() + 1);
    }

    #[cfg(any(target_os = "linux", target_os = "macos"))]
    #[test]
    fn scans_through_live_descriptor_path() {
        use std::os::fd::AsRawFd;

        let path = std::env::temp_dir().join(format!(
            "wisp-search-descriptor-{}-{}",
            std::process::id(),
            std::thread::current().name().unwrap_or("test")
        ));
        std::fs::write(&path, b"before\nhit\nafter\n").unwrap();
        let file = File::open(&path).unwrap();
        let result = scan_literal_fd(
            i64::from(file.as_raw_fd()),
            &config("hit"),
            &AtomicBool::new(false),
        )
        .unwrap();
        std::fs::remove_file(path).unwrap();

        assert_eq!(result.lines, ["src/data.txt:2:hit"]);
    }
}
