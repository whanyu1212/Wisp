//! Paced reveal of streamed reply text.
//!
//! Providers send a reply in bursts: several words at once, then a pause. Shown as it
//! arrives, the reply jumps forward and stalls, a word split between bursts appears half
//! typed and then moves to the next line, and `**bold` shows its asterisks until the
//! closing `**` arrives. The live terminal instead queues arrived text on the transcript
//! entry, and each frame [`StreamPacer::tick`] shows the next part of it:
//!
//! - fast enough to clear what is waiting in about [`DRAIN_TIME`], so the reply never
//!   falls far behind the stream;
//! - a whole word at a time, where a wide (CJK) character counts as a word;
//! - holding back an unclosed `**`, `~~` or `` ` `` span, and a table row or code fence
//!   line, until it is complete.
//!
//! When the stream pauses for [`IDLE_RELEASE`], or the reply completes, the holds are let
//! go so nothing waits on text that may never come.

use std::time::{Duration, Instant};

use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::transcript::{Transcript, TranscriptEntryId};

/// About how long arrived text waits before it is shown.
const DRAIN_TIME: Duration = Duration::from_millis(120);
/// The slowest pace, so a short wait still clears quickly.
const MIN_GRAPHEMES_PER_SECOND: f64 = 240.0;
/// The longest gap between frames that counts toward the pace, so a late frame does not
/// show a whole burst at once.
const MAX_TICK: Duration = Duration::from_millis(100);
/// How long the stream must pause before held text is shown anyway.
const IDLE_RELEASE: Duration = Duration::from_millis(200);
/// A word longer than this, such as a URL, is shown a character at a time.
const WORD_HOLD_GRAPHEMES: usize = 24;
/// An unclosed inline span longer than this is shown without waiting for its closer.
const MARKER_HOLD_BYTES: usize = 160;
/// A table row or fence line longer than this is shown without waiting for its line break.
const LINE_HOLD_BYTES: usize = 512;
/// Waiting text past this many graphemes all counts as a full backlog.
const BACKLOG_COUNT_LIMIT: usize = 4_096;
/// Waiting text larger than this is a dump rather than a stream, such as a provider that
/// sends a whole reply at once, so it is shown at once instead of falling behind.
const DUMP_BYTES: usize = 16 * 1024;
/// How far back into the shown paragraph inline markers are read, to bound the work per
/// frame in a long paragraph.
const PARAGRAPH_LOOKBACK_BYTES: usize = 4_096;

/// The pace of the reply being revealed, carried from frame to frame.
#[derive(Debug, Default)]
pub(crate) struct StreamPacer {
    entry: Option<TranscriptEntryId>,
    last_tick: Option<Instant>,
    /// Graphemes the pace allows but that have not been shown yet; negative after a
    /// finished word ran past the allowance.
    budget: f64,
    /// Shown plus waiting bytes at the last tick, to notice when more text arrives.
    arrived: usize,
    last_growth: Option<Instant>,
}

impl StreamPacer {
    /// Show the next part of the reply being revealed, if any.
    ///
    /// Args:
    ///     transcript (&mut Transcript): The transcript holding the waiting text.
    ///     now (Instant): The frame time. Time enters pacing only here.
    ///
    /// Returns:
    ///     bool: Whether more of the reply is now shown.
    pub(crate) fn tick(&mut self, transcript: &mut Transcript, now: Instant) -> bool {
        let Some((entry, shown, waiting, complete)) = transcript.unrevealed() else {
            *self = Self::default();
            return false;
        };
        let arrived = shown.len() + waiting.len();
        // A reply with less text than last time is a different one, such as a reply in a
        // replaced transcript that reuses the entry id.
        if self.entry != Some(entry) || arrived < self.arrived {
            *self = Self {
                entry: Some(entry),
                ..Self::default()
            };
        }
        if arrived != self.arrived {
            self.arrived = arrived;
            self.last_growth = Some(now);
        }
        let elapsed = self
            .last_tick
            .map_or(Duration::ZERO, |last| now.saturating_duration_since(last))
            .min(MAX_TICK);
        self.last_tick = Some(now);

        if waiting.len() > DUMP_BYTES {
            let all = waiting.len();
            transcript.reveal(all);
            *self = Self::default();
            return true;
        }
        let backlog = waiting.graphemes(true).take(BACKLOG_COUNT_LIMIT).count() as f64;
        let rate = (backlog / DRAIN_TIME.as_secs_f64()).max(MIN_GRAPHEMES_PER_SECOND);
        self.budget = (self.budget + rate * elapsed.as_secs_f64()).min(backlog);
        let release = complete
            || self
                .last_growth
                .is_some_and(|growth| now.saturating_duration_since(growth) >= IDLE_RELEASE);
        let allowance = self.budget.max(0.0).floor() as usize;
        let end = reveal_end(shown, waiting, allowance, release);
        if end == 0 {
            return false;
        }
        // A word or closed span may run past the allowance. Its debt is capped, so a long
        // span does not freeze the text after it.
        let spent = waiting[..end].graphemes(true).count() as f64;
        self.budget = (self.budget - spent).max(-(WORD_HOLD_GRAPHEMES as f64));
        transcript.reveal(end);
        if transcript.unrevealed().is_none() {
            // The reply caught up. The next burst starts from a still pace rather than being
            // shown at once for the time spent waiting for it.
            *self = Self::default();
        }
        true
    }
}

/// How many bytes of `waiting` to show now.
///
/// Args:
///     shown (&str): The reply text already shown.
///     waiting (&str): The reply text that has arrived but is not shown.
///     allowance (usize): Graphemes the pace allows; a word in progress may run past it.
///     release (bool): Whether the stream paused or completed, which lets holds go.
///
/// Returns:
///     usize: A byte length of `waiting` on a grapheme boundary; zero shows nothing.
///
/// Examples:
///     `reveal_end("", "Hello wor", 3, false)` is 6: `Hello ` finishes the word that
///     the allowance started, and `wor` waits for the rest of its word.
fn reveal_end(shown: &str, waiting: &str, allowance: usize, release: bool) -> usize {
    if allowance == 0 {
        return 0;
    }
    let end = word_end(waiting, allowance, release);
    if release {
        end
    } else {
        hold_open_markup(shown, waiting, end)
    }
}

/// The end of the words that `allowance` graphemes reach into `waiting`.
fn word_end(waiting: &str, allowance: usize, release: bool) -> usize {
    let mut counted = 0;
    let mut end = 0;
    let mut word_graphemes = 0;
    for (offset, grapheme) in waiting.grapheme_indices(true) {
        let long_word = word_graphemes > WORD_HOLD_GRAPHEMES;
        if counted >= allowance && (word_graphemes == 0 || long_word) {
            break;
        }
        let next = offset + grapheme.len();
        if grapheme.chars().all(char::is_whitespace) || UnicodeWidthStr::width(grapheme) >= 2 {
            word_graphemes = 0;
            end = next;
        } else {
            word_graphemes += 1;
            if word_graphemes > WORD_HOLD_GRAPHEMES {
                end = next;
            }
        }
        counted += 1;
    }
    // Text that ends mid-word waits for the rest of the word, unless none is coming.
    if release && counted == waiting.graphemes(true).count() {
        waiting.len()
    } else {
        end
    }
}

/// Pull `end` back so the shown text does not stop inside markup that is not finished:
/// a table row or code fence line without its line break, or an inline span without its
/// closer. A closer that has already arrived just past `end` is shown with its span.
fn hold_open_markup(shown: &str, waiting: &str, end: usize) -> usize {
    let line_start = waiting[..end].rfind('\n').map(|newline| newline + 1);
    let shown_line = match line_start {
        Some(_) => "",
        None => &shown[shown.rfind('\n').map_or(0, |newline| newline + 1)..],
    };
    let waiting_line_start = line_start.unwrap_or(0);
    let line = format!("{shown_line}{}", &waiting[waiting_line_start..end]);

    // A table row or fence line shown before its line break flashes as plain text.
    if let Some(start) = line_start.or((shown_line.is_empty()).then_some(0)) {
        let trimmed = line.trim_start();
        let block_line =
            trimmed.starts_with('|') || trimmed.starts_with("```") || trimmed.starts_with("~~~");
        if block_line && waiting.len() - start <= LINE_HOLD_BYTES {
            return start;
        }
    }

    // An inline span can continue across a soft line break, so look back to the start of
    // the paragraph rather than the line.
    let paragraph_start = waiting[..end].rfind("\n\n").map(|blank| blank + 2);
    let shown_paragraph = match paragraph_start {
        Some(_) => "",
        None => {
            let mut start = shown
                .rfind("\n\n")
                .map_or(0, |blank| blank + 2)
                .max(shown.len().saturating_sub(PARAGRAPH_LOOKBACK_BYTES));
            while !shown.is_char_boundary(start) {
                start += 1;
            }
            &shown[start..]
        }
    };
    let waiting_paragraph_start = paragraph_start.unwrap_or(0);
    let paragraph = format!(
        "{shown_paragraph}{}",
        &waiting[waiting_paragraph_start..end]
    );
    // Markers inside a fenced code block are literal, so read only the prose after the
    // last fence line, and none at all while a fence is still open.
    let mut prose_start = 0;
    let mut in_fence = false;
    let mut offset = 0;
    for line in paragraph.split_inclusive('\n') {
        let trimmed = line.trim_start();
        if trimmed.starts_with("```") || trimmed.starts_with("~~~") {
            in_fence = !in_fence;
            prose_start = offset + line.len();
        }
        offset += line.len();
    }
    if in_fence {
        return end;
    }
    let Some((marker, opened_at)) = open_inline_marker(&paragraph[prose_start..]) else {
        return end;
    };
    let opened_at = prose_start + opened_at;
    // The opener is already on screen, so holding the rest of the span gains nothing.
    let Some(opener) = opened_at.checked_sub(shown_paragraph.len()) else {
        return end;
    };
    let opener = waiting_paragraph_start + opener;
    let rest_of_paragraph = waiting[end..].split("\n\n").next().unwrap_or_default();
    if let Some(close) = find_closer(rest_of_paragraph, marker) {
        let closed = end + close + marker.len();
        if closed - opener <= MARKER_HOLD_BYTES {
            return closed;
        }
    }
    if waiting.len() - opener > MARKER_HOLD_BYTES {
        end
    } else {
        opener
    }
}

/// Where `marker` next closes a span in `text`, skipping escaped markers and, for `**`
/// and `~~`, markers inside a code span, as [`open_inline_marker`] reads them.
fn find_closer(text: &str, marker: &str) -> Option<usize> {
    let bytes = text.as_bytes();
    let marker_byte = marker.as_bytes()[0];
    let mut index = 0;
    while index < bytes.len() {
        let byte = bytes[index];
        if byte == b'\\' && marker != "`" {
            index += 2;
            continue;
        }
        let run = bytes[index..]
            .iter()
            .take_while(|next| **next == byte)
            .count();
        if byte == marker_byte && run == marker.len() {
            return Some(index);
        }
        if byte == b'`' {
            // Skip a code span: everything up to the next run of as many backticks.
            let after = index + run;
            let close = (after..bytes.len()).find(|start| {
                bytes[*start..]
                    .iter()
                    .take_while(|next| **next == b'`')
                    .count()
                    == run
                    && bytes[*start - 1] != b'`'
            });
            if let Some(close) = close {
                index = close + run;
                continue;
            }
        }
        index += run;
    }
    None
}

/// The first inline span opened in `text` and not closed in it, as its marker and byte
/// offset. Only `**`, `~~` and `` ` `` count: a single `*` or `_` is too often literal.
fn open_inline_marker(text: &str) -> Option<(&'static str, usize)> {
    let bytes = text.as_bytes();
    let mut open: Option<(&'static str, usize)> = None;
    let mut index = 0;
    while index < bytes.len() {
        let byte = bytes[index];
        // A backslash escapes a marker, except inside a code span, where it is literal.
        if byte == b'\\' && !matches!(open, Some(("`", _))) {
            index += 2;
            continue;
        }
        let run = bytes[index..]
            .iter()
            .take_while(|next| **next == byte)
            .count();
        let marker = match (byte, run) {
            (b'`', 1) => Some("`"),
            (b'*', 2) => Some("**"),
            (b'~', 2) => Some("~~"),
            _ => None,
        };
        if let Some(marker) = marker {
            match open {
                Some((opened, _)) if opened == marker => open = None,
                // Markers inside a code span are literal.
                Some(("`", _)) => {}
                Some(_) => {}
                None => open = Some((marker, index)),
            }
        }
        index += run.max(1);
    }
    open
}

/// A record of when streamed reply text arrives, for measuring how bursty a provider's
/// stream is. `WISP_TUI_STREAM_TRACE=<path>` turns it on; each text delta appends a
/// tab-separated line of milliseconds since the first delta, milliseconds since the
/// previous one, and its size in bytes.
pub(crate) struct StreamTrace {
    file: std::fs::File,
    first: Option<Instant>,
    previous: Option<Instant>,
}

impl StreamTrace {
    pub(crate) fn from_env() -> Option<Self> {
        let path = std::env::var_os("WISP_TUI_STREAM_TRACE")?;
        let file = std::fs::OpenOptions::new()
            .create(true)
            .append(true)
            .open(path)
            .ok()?;
        Some(Self {
            file,
            first: None,
            previous: None,
        })
    }

    /// Append one delta's line. Returns `false` once the file cannot be written, so the
    /// caller can stop tracing.
    pub(crate) fn record(&mut self, bytes: usize, now: Instant) -> bool {
        use std::io::Write;
        let first = *self.first.get_or_insert(now);
        let gap = self.previous.map_or(Duration::ZERO, |previous| {
            now.saturating_duration_since(previous)
        });
        self.previous = Some(now);
        writeln!(
            self.file,
            "{}\t{}\t{bytes}",
            now.saturating_duration_since(first).as_millis(),
            gap.as_millis()
        )
        .is_ok()
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    const FRAME: Duration = Duration::from_millis(16);

    fn shown(transcript: &Transcript) -> String {
        transcript.entries().last().unwrap().content.clone()
    }

    /// Tick at frame pace until nothing more is shown, returning every shown text.
    fn run_frames(
        transcript: &mut Transcript,
        pacer: &mut StreamPacer,
        start: Instant,
        frames: u32,
    ) -> Vec<String> {
        (0..frames)
            .map(|frame| {
                pacer.tick(transcript, start + FRAME * frame);
                shown(transcript)
            })
            .collect()
    }

    #[test]
    fn words_reveal_over_frames() {
        let mut transcript = Transcript::default();
        let text = "The quick brown fox jumps over the lazy dog and keeps running far away. ";
        transcript.append_paced_message_delta(1, text);
        let mut pacer = StreamPacer::default();
        let frames = run_frames(&mut transcript, &mut pacer, Instant::now(), 40);
        assert_eq!(frames[0], "", "the first frame only sets the pace");
        assert!(frames[1].len() < text.len() / 2, "{frames:?}");
        // Every frame stops at a word boundary.
        assert!(
            frames
                .iter()
                .all(|shown| shown.is_empty() || shown.ends_with(' '))
        );
        assert_eq!(frames.last().unwrap(), text);
    }

    #[test]
    fn partial_word_waits() {
        assert_eq!(reveal_end("", "Hello wor", 3, false), 6);
        assert_eq!(reveal_end("", "Hello wor", 30, false), 6);
        assert_eq!(reveal_end("", "Hello wor", 30, true), 9);
    }

    #[test]
    fn idle_stream_releases_word() {
        let mut transcript = Transcript::default();
        transcript.append_paced_message_delta(1, "Hello wor");
        let mut pacer = StreamPacer::default();
        let start = Instant::now();
        run_frames(&mut transcript, &mut pacer, start, 4);
        assert_eq!(shown(&transcript), "Hello ");
        pacer.tick(&mut transcript, start + IDLE_RELEASE + FRAME * 2);
        assert_eq!(shown(&transcript), "Hello wor");
    }

    #[test]
    fn cjk_reveals_without_spaces() {
        assert_eq!(reveal_end("", "你好世界", 2, false), "你好".len());
    }

    #[test]
    fn long_token_reveals_gradually() {
        let url = "https://example.com/a/very/long/path/that/keeps/going";
        let end = reveal_end("", url, 30, false);
        assert!(end > WORD_HOLD_GRAPHEMES && end < url.len(), "{end}");
    }

    #[test]
    fn unclosed_bold_waits() {
        let waiting = "Use **bold te";
        assert_eq!(reveal_end("", waiting, 20, false), "Use ".len());
        // A closed span is shown whole once the allowance reaches into it.
        assert_eq!(
            reveal_end("", "Use **bold** now", 6, false),
            "Use **bold** ".len()
        );
        // An opener already on screen is not held again.
        assert_eq!(reveal_end("Use **bo", "ld te", 20, false), "ld ".len());
    }

    #[test]
    fn span_continues_across_soft_break() {
        // A span opened on an earlier line is closed here, not opened.
        assert_eq!(
            reveal_end("Some **bold\n", "text** here ", 20, false),
            "text** here ".len()
        );
        // An opener before a soft break still waits for its closer.
        assert_eq!(reveal_end("", "Use **bold\ntext ", 20, false), "Use ".len());
        // A blank line ends the paragraph, and the span with it.
        assert_eq!(
            reveal_end("", "a **b\n\nc d ", 20, false),
            "a **b\n\nc d ".len()
        );
        // Prose after a closed fence still holds its spans.
        assert_eq!(
            reveal_end("```\ncode\n```\n", "Use **bold ", 20, false),
            "Use ".len()
        );
        // Markers in a fenced code block are literal.
        assert_eq!(
            reveal_end("```rust\n", "let x = a ** b; ", 40, false),
            "let x = a ** b; ".len()
        );
    }

    #[test]
    fn closer_skips_escapes_and_code() {
        let escaped = "Use **bold \\** still** end ";
        assert_eq!(
            reveal_end("", escaped, 6, false),
            "Use **bold \\** still**".len()
        );
        let coded = "Use **a `**` b** c ";
        assert_eq!(reveal_end("", coded, 6, false), "Use **a `**` b**".len());
    }

    #[test]
    fn dump_shows_at_once() {
        let mut transcript = Transcript::default();
        let dump = "word ".repeat(DUMP_BYTES / 4);
        transcript.append_paced_message_delta(1, &dump);
        assert!(StreamPacer::default().tick(&mut transcript, Instant::now()));
        assert_eq!(shown(&transcript), dump);
        assert!(transcript.unrevealed().is_none());
    }

    #[test]
    fn unclosed_code_waits() {
        assert_eq!(reveal_end("", "Run `cargo te", 20, false), "Run ".len());
        assert_eq!(reveal_end("", "a `**` b ", 20, false), "a `**` b ".len());
    }

    #[test]
    fn hold_cap_releases_span() {
        let waiting = format!("**{} ", "word ".repeat(40));
        assert_eq!(reveal_end("", &waiting, 10, false), "**word word ".len());
    }

    #[test]
    fn table_row_waits_for_newline() {
        let waiting = "Intro\n| a | b ";
        assert_eq!(reveal_end("", waiting, 40, false), "Intro\n".len());
        assert_eq!(
            reveal_end("", "| a | b |\n| c", 40, false),
            "| a | b |\n".len()
        );
    }

    #[test]
    fn completion_drains_then_completes() {
        let mut transcript = Transcript::default();
        transcript.append_paced_message_delta(1, "First words ");
        transcript.complete_message(1, "First words and the rest of it.".into());
        let entry = transcript.entries().last().unwrap();
        assert_eq!(
            entry.state,
            crate::transcript::TranscriptEntryState::Streaming
        );
        let mut pacer = StreamPacer::default();
        run_frames(&mut transcript, &mut pacer, Instant::now(), 20);
        let entry = transcript.entries().last().unwrap();
        assert_eq!(entry.content, "First words and the rest of it.");
        assert_eq!(
            entry.state,
            crate::transcript::TranscriptEntryState::Complete
        );
        assert!(transcript.unrevealed().is_none());
    }

    #[test]
    fn finished_run_shows_tail() {
        let mut transcript = Transcript::default();
        transcript.append_paced_message_delta(1, "The last reply of the run ");
        transcript.finish_active_response();
        let entry = &transcript.entries()[0];
        assert_eq!(entry.content, "The last reply of the run ");
        assert_eq!(
            entry.state,
            crate::transcript::TranscriptEntryState::Complete
        );
        assert!(transcript.unrevealed().is_none());
    }

    #[test]
    fn new_entry_flushes_backlog() {
        let mut transcript = Transcript::default();
        transcript.append_paced_message_delta(1, "Looking at the file now ");
        transcript.append_prompt("next".into());
        assert_eq!(transcript.entries()[0].content, "Looking at the file now ");
        assert!(transcript.unrevealed().is_none());
    }

    #[test]
    fn cost_waits_for_drain() {
        let mut transcript = Transcript::default();
        transcript.append_paced_message_delta(1, "Done ");
        transcript.complete_message_with_cost(1, "Done here.".into(), Some("$0.01".into()));
        assert!(!transcript.entries()[0].display_content().contains("$0.01"));
        transcript.flush_reveal();
        let entry = &transcript.entries()[0];
        assert!(entry.display_content().ends_with("$0.01"), "{entry:?}");
    }

    #[test]
    fn burst_after_pause_starts_slow() {
        let mut transcript = Transcript::default();
        let mut pacer = StreamPacer::default();
        let start = Instant::now();
        transcript.append_paced_message_delta(1, "First burst of words here. ");
        // Tick only while text waits, as the event loop does.
        let mut frame = 0;
        while transcript.unrevealed().is_some() {
            pacer.tick(&mut transcript, start + FRAME * frame);
            frame += 1;
        }
        let later = start + Duration::from_secs(2);
        transcript.append_paced_message_delta(1, "and then a second burst arrives after a pause. ");
        let before = shown(&transcript).len();
        pacer.tick(&mut transcript, later);
        assert_eq!(shown(&transcript).len(), before);
        pacer.tick(&mut transcript, later + FRAME);
        assert!(
            shown(&transcript).len() - before < 20,
            "{}",
            shown(&transcript)
        );
    }

    #[test]
    fn long_span_debt_is_capped() {
        let mut transcript = Transcript::default();
        let mut pacer = StreamPacer::default();
        let start = Instant::now();
        let span = format!("Hi **{}** and a few more words. ", "word ".repeat(28));
        transcript.append_paced_message_delta(1, &span);
        let frames = run_frames(&mut transcript, &mut pacer, start, 14);
        let after_span = frames
            .iter()
            .position(|shown| shown.contains("**") && shown.matches("**").count() == 2)
            .unwrap();
        assert!(
            frames[after_span..]
                .iter()
                .any(|shown| shown.ends_with("and ")),
            "{frames:?}"
        );
    }

    #[test]
    fn code_span_backslash_is_literal() {
        let line = "Use `C:\\` as the root and then more ";
        assert_eq!(reveal_end("", line, 200, false), line.len());
    }

    #[test]
    fn long_table_row_is_shown() {
        let row = format!("| {} ", "cell ".repeat(120));
        assert_eq!(reveal_end("", &row, 1_000, false), row.len());
    }

    #[test]
    fn trace_records_gaps() {
        let path = std::env::temp_dir().join(format!("wisp-stream-trace-{}", std::process::id()));
        let file = std::fs::File::create(&path).unwrap();
        let mut trace = StreamTrace {
            file,
            first: None,
            previous: None,
        };
        let start = Instant::now();
        assert!(trace.record(12, start));
        assert!(trace.record(40, start + Duration::from_millis(85)));
        let written = std::fs::read_to_string(&path).unwrap();
        std::fs::remove_file(&path).unwrap();
        assert_eq!(written, "0\t0\t12\n85\t85\t40\n");
    }

    #[test]
    fn rewritten_final_snaps() {
        let mut transcript = Transcript::default();
        transcript.append_paced_message_delta(1, "draft text ");
        transcript.reveal(6);
        transcript.complete_message(1, "Final answer.".into());
        let entry = &transcript.entries()[0];
        assert_eq!(entry.content, "Final answer.");
        assert_eq!(
            entry.state,
            crate::transcript::TranscriptEntryState::Complete
        );
        assert!(transcript.unrevealed().is_none());
    }
}
