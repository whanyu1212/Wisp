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
    /// Code fences in the complete lines shown so far, kept as the reply grows so a
    /// frame reads only the lines added since the last one.
    fences: FenceState,
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
            self.pause();
            return false;
        };
        let arrived = shown.len() + waiting.len();
        // A reply with less text than last time, or less shown text than the fence scan
        // has read, is a different one, such as a reply in a replaced transcript that
        // reuses the entry id. Its offsets must not be applied to this reply.
        if self.entry != Some(entry)
            || arrived < self.arrived
            || self.fences.scanned.max(self.fences.searched) > shown.len()
        {
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
            self.pause();
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
        self.fences = self.fences.advance_shown(shown);
        let end = reveal_end(shown, waiting, allowance, release, self.fences);
        if end == 0 {
            return false;
        }
        // A word or closed span may run past the allowance. Its debt is capped, so a long
        // span does not freeze the text after it.
        let spent = waiting[..end].graphemes(true).count() as f64;
        self.budget = (self.budget - spent).max(-(WORD_HOLD_GRAPHEMES as f64));
        transcript.reveal(end);
        if transcript.unrevealed().is_none() {
            self.pause();
        }
        true
    }

    /// Start the next burst from a still pace, rather than showing it at once for the
    /// time spent waiting for it. The reply and its fence scan are kept, so a reply that
    /// catches up between bursts is not scanned again from its start.
    fn pause(&mut self) {
        self.last_tick = None;
        self.budget = 0.0;
        self.last_growth = None;
    }
}

/// How many bytes of `waiting` to show now.
///
/// Args:
///     shown (&str): The reply text already shown.
///     waiting (&str): The reply text that has arrived but is not shown.
///     allowance (usize): Graphemes the pace allows; a word in progress may run past it.
///     release (bool): Whether the stream paused or completed, which lets holds go.
///     fences (FenceState): The code fences in the complete lines of `shown`.
///
/// Returns:
///     usize: A byte length of `waiting` on a grapheme boundary; zero shows nothing.
///
/// Examples:
///     With nothing shown, `reveal_end("", "Hello wor", 3, false, FenceState::default())`
///     is 6: `Hello ` finishes the word that
///     the allowance started, and `wor` waits for the rest of its word.
fn reveal_end(
    shown: &str,
    waiting: &str,
    allowance: usize,
    release: bool,
    fences: FenceState,
) -> usize {
    if allowance == 0 {
        return 0;
    }
    let end = word_end(waiting, allowance, release);
    if release {
        end
    } else {
        hold_open_markup(shown, waiting, end, fences)
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
/// closer. A row or closer that has already arrived just past `end` is shown whole.
fn hold_open_markup(shown: &str, waiting: &str, end: usize, fences: FenceState) -> usize {
    // The pacer has read every complete line of `shown`, so the current line starts at
    // `fences.scanned` unless a line break has arrived in `waiting`.
    let line_start = waiting[..end].rfind('\n').map(|newline| newline + 1);
    let line_starts_unshown = line_start.or((fences.scanned == shown.len()).then_some(0));

    // A table row or fence line shown before its line break flashes as plain text, so
    // it is shown whole once its line break arrives, within a cap on the row's length.
    if let Some(start) = line_starts_unshown {
        let trimmed = waiting[start..end].trim_start();
        let block_line =
            trimmed.starts_with('|') || trimmed.starts_with("```") || trimmed.starts_with("~~~");
        if block_line {
            match waiting[end..].find('\n') {
                Some(newline) if end + newline + 1 - start <= LINE_HOLD_BYTES => {
                    return end + newline + 1;
                }
                None if waiting.len() - start <= LINE_HOLD_BYTES => return start,
                _ => {}
            }
        }
    }

    // Read the text after the shown fence scan, in offsets from the start of `shown`. A
    // shown line longer than the lookback is read only from the lookback on; a line that
    // long is not a fence line.
    let unfinished = shown.len() - fences.scanned;
    let (tail_start, mid_line) = if unfinished > PARAGRAPH_LOOKBACK_BYTES {
        let mut start = shown.len() - PARAGRAPH_LOOKBACK_BYTES;
        while !shown.is_char_boundary(start) {
            start += 1;
        }
        (start, true)
    } else {
        (fences.scanned, false)
    };
    let tail = format!("{}{}", &shown[tail_start..], &waiting[..end]);
    let fences = fences.advance_from(&tail, tail_start, mid_line);
    // Markers inside a fenced code block are literal.
    if fences.open.is_some() {
        return end;
    }
    // An inline span can continue across a soft line break but not a blank line, and
    // only prose after the last fence line counts.
    let combined_len = shown.len() + end;
    let paragraph_start = match after_last_blank_line(&tail, !mid_line) {
        Some(after) => fences.scanned_from + after,
        None => {
            let window = shown.len().saturating_sub(PARAGRAPH_LOOKBACK_BYTES);
            let mut window = window.min(fences.scanned_from);
            while !shown.is_char_boundary(window) {
                window += 1;
            }
            let starts_at_line = window == 0 || shown[..window].ends_with('\n');
            after_last_blank_line(&shown[window..fences.scanned_from], starts_at_line)
                .map_or(window, |after| window + after)
        }
    };
    let mut prose_start = paragraph_start
        .max(fences.boundary_end)
        .max(combined_len.saturating_sub(PARAGRAPH_LOOKBACK_BYTES));
    let prose = if prose_start < shown.len() {
        while !shown.is_char_boundary(prose_start) {
            prose_start += 1;
        }
        format!("{}{}", &shown[prose_start..], &waiting[..end])
    } else {
        waiting[prose_start - shown.len()..end].to_owned()
    };
    let Some((marker, opened_at)) = open_inline_marker(&prose) else {
        return end;
    };
    // The opener is already on screen, so holding the rest of the span gains nothing.
    let Some(opener) = (prose_start + opened_at).checked_sub(shown.len()) else {
        return end;
    };
    let rest_starts_at_line = if end == 0 {
        shown.is_empty() || shown.ends_with('\n')
    } else {
        waiting[..end].ends_with('\n')
    };
    let rest_of_paragraph = before_first_blank_line(&waiting[end..], rest_starts_at_line);
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

/// The code fences in a reply's complete lines: which fence is open, if any, and where
/// the last fence line ends. A fence closes only on its own delimiter, at least as long
/// as the opener, as the renderer reads it.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct FenceState {
    /// Where the first line not yet read starts, in bytes from the start of the reply.
    scanned: usize,
    /// How far the shown text has been searched for a line break, so a long unfinished
    /// line is not searched again each frame.
    searched: usize,
    /// Where the text passed to the last `advance_from` started.
    scanned_from: usize,
    /// The open fence's delimiter and length.
    open: Option<(u8, usize)>,
    /// Where the line after the last opening or closing fence starts.
    boundary_end: usize,
}

impl FenceState {
    /// Read the lines of `shown` completed since the last call. Each byte is searched
    /// for a line break once, however many frames its line takes to finish.
    fn advance_shown(mut self, shown: &str) -> Self {
        let mut from = self.searched.max(self.scanned);
        while let Some(newline) = shown[from..].find('\n') {
            let line_end = from + newline + 1;
            self.read_line(&shown[self.scanned..line_end - 1], line_end);
            self.scanned = line_end;
            from = line_end;
        }
        self.searched = shown.len();
        self
    }

    /// Read the complete lines of `text`, which starts `base` bytes into the reply. When
    /// `mid_line`, `text` starts partway into a line, which is not read as a fence line.
    fn advance_from(mut self, text: &str, base: usize, mid_line: bool) -> Self {
        self.scanned_from = base;
        let mut offset = base;
        for (index, line) in text.split_inclusive('\n').enumerate() {
            let Some(content) = line.strip_suffix('\n') else {
                break;
            };
            if index > 0 || !mid_line {
                self.read_line(content, offset + line.len());
            }
            offset += line.len();
        }
        self.scanned = offset;
        self
    }

    /// Open or close a fence on one complete line, which ends at `line_end`.
    fn read_line(&mut self, content: &str, line_end: usize) {
        match self.open {
            Some((delimiter, length)) => {
                if crate::markdown::is_closing_fence_line(content, delimiter, length) {
                    self.open = None;
                    self.boundary_end = line_end;
                }
            }
            None => {
                if let Some(opened) = opening_fence(content) {
                    self.open = Some(opened);
                    self.boundary_end = line_end;
                }
            }
        }
    }
}

/// Whether `line`, with its line break, is a blank line: only spaces, tabs or a carriage
/// return, as Markdown reads a paragraph break.
fn is_blank_line(line: &str) -> bool {
    line.ends_with('\n') && line.trim_matches([' ', '\t', '\r', '\n']).is_empty()
}

/// Where the text after the last blank line in `text` starts. The first line counts
/// only when `text` starts at the beginning of a line.
fn after_last_blank_line(text: &str, starts_at_line: bool) -> Option<usize> {
    let mut after = None;
    let mut offset = 0;
    for (index, line) in text.split_inclusive('\n').enumerate() {
        offset += line.len();
        if (index > 0 || starts_at_line) && is_blank_line(line) {
            after = Some(offset);
        }
    }
    after
}

/// `text` up to its first blank line. The first line counts only when `text` starts at
/// the beginning of a line.
fn before_first_blank_line(text: &str, starts_at_line: bool) -> &str {
    let mut offset = 0;
    for (index, line) in text.split_inclusive('\n').enumerate() {
        if (index > 0 || starts_at_line) && is_blank_line(line) {
            return &text[..offset];
        }
        offset += line.len();
    }
    text
}

/// The delimiter and length of the code fence that `line` opens, if it opens one. As in
/// CommonMark, a fence is indented at most three spaces (inside any block quote or list
/// item markers),
/// since more makes the line indented code, and a backtick fence's info string has no
/// backtick.
fn opening_fence(line: &str) -> Option<(u8, usize)> {
    let mut rest = line;
    loop {
        let indent = rest.len() - rest.trim_start_matches(' ').len();
        if indent > 3 || rest[indent..].starts_with('\t') {
            return None;
        }
        rest = &rest[indent..];
        if let Some(quoted) = rest.strip_prefix('>') {
            rest = quoted;
        } else if let Some(item) = list_item_content(rest) {
            rest = item;
        } else {
            break;
        }
    }
    let delimiter = *rest.as_bytes().first()?;
    if !matches!(delimiter, b'`' | b'~') {
        return None;
    }
    let length = rest.bytes().take_while(|byte| *byte == delimiter).count();
    if length < 3 || (delimiter == b'`' && rest[length..].contains('`')) {
        return None;
    }
    Some((delimiter, length))
}

/// The content of a list item line after its marker (`-`, `*`, `+`, or up to nine digits
/// and `.` or `)`) and the spaces that follow it, if `line` starts a list item.
fn list_item_content(line: &str) -> Option<&str> {
    let bytes = line.as_bytes();
    let marker = match bytes.first()? {
        b'-' | b'*' | b'+' => 1,
        b'0'..=b'9' => {
            let digits = bytes
                .iter()
                .take_while(|byte| byte.is_ascii_digit())
                .count();
            if digits > 9 || !matches!(bytes.get(digits), Some(b'.' | b')')) {
                return None;
            }
            digits + 1
        }
        _ => return None,
    };
    let after = &line[marker..];
    after
        .starts_with([' ', '\t'])
        .then(|| after.trim_start_matches([' ', '\t']))
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

    /// `reveal_end` with the fence state of `shown`, as the pacer keeps it.
    fn reveal(shown: &str, waiting: &str, allowance: usize, release: bool) -> usize {
        let fences = FenceState::default().advance_shown(shown);
        reveal_end(shown, waiting, allowance, release, fences)
    }

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
        assert_eq!(reveal("", "Hello wor", 3, false), 6);
        assert_eq!(reveal("", "Hello wor", 30, false), 6);
        assert_eq!(reveal("", "Hello wor", 30, true), 9);
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
        assert_eq!(reveal("", "你好世界", 2, false), "你好".len());
    }

    #[test]
    fn long_token_reveals_gradually() {
        let url = "https://example.com/a/very/long/path/that/keeps/going";
        let end = reveal("", url, 30, false);
        assert!(end > WORD_HOLD_GRAPHEMES && end < url.len(), "{end}");
    }

    #[test]
    fn unclosed_bold_waits() {
        let waiting = "Use **bold te";
        assert_eq!(reveal("", waiting, 20, false), "Use ".len());
        // A closed span is shown whole once the allowance reaches into it.
        assert_eq!(
            reveal("", "Use **bold** now", 6, false),
            "Use **bold** ".len()
        );
        // An opener already on screen is not held again.
        assert_eq!(reveal("Use **bo", "ld te", 20, false), "ld ".len());
    }

    #[test]
    fn span_continues_across_soft_break() {
        // A span opened on an earlier line is closed here, not opened.
        assert_eq!(
            reveal("Some **bold\n", "text** here ", 20, false),
            "text** here ".len()
        );
        // An opener before a soft break still waits for its closer.
        assert_eq!(reveal("", "Use **bold\ntext ", 20, false), "Use ".len());
        // A blank line ends the paragraph, and the span with it.
        assert_eq!(
            reveal("", "a **b\n\nc d ", 20, false),
            "a **b\n\nc d ".len()
        );
        // Prose after a closed fence still holds its spans.
        assert_eq!(
            reveal("```\ncode\n```\n", "Use **bold ", 20, false),
            "Use ".len()
        );
        // A shorter or different fence line inside a block does not close it.
        let inner = "a **b c ";
        assert_eq!(reveal("````md\n```\n", inner, 20, false), inner.len());
        assert_eq!(reveal("```\n~~~\n", inner, 20, false), inner.len());
        // A blank line inside an open code block keeps it open.
        assert_eq!(reveal("```\ncode\n\n", inner, 20, false), inner.len());
        assert_eq!(
            reveal("```\n\n", "x\n\na **b c ", 20, false),
            "x\n\na **b c ".len()
        );
        // Indented four spaces, or with a backtick in its info string, a fence line is
        // not a fence, so the prose after it still holds its spans.
        assert_eq!(
            reveal("    ```\n\n", "Use **bold ", 20, false),
            "Use ".len()
        );
        assert_eq!(
            reveal("``` a `b`\n", "Use **bold ", 20, false),
            "Use ".len()
        );
        assert_eq!(reveal("> ```\n", inner, 20, false), inner.len());
        // A fence can open on a list item's line.
        assert_eq!(
            reveal("- ```rust\n", "let x = a ** b; ", 40, false),
            "let x = a ** b; ".len()
        );
        assert_eq!(
            reveal("1. ```\n", "let x = a ** b; ", 40, false),
            "let x = a ** b; ".len()
        );
        // A blank line may hold spaces or a carriage return.
        assert_eq!(
            reveal("", "a **b\n \nc d ", 20, false),
            "a **b\n \nc d ".len()
        );
        assert_eq!(
            reveal("", "a **b\r\n\r\nc d ", 20, false),
            "a **b\r\n\r\nc d ".len()
        );
        // Markers in a fenced code block are literal.
        assert_eq!(
            reveal("```rust\n", "let x = a ** b; ", 40, false),
            "let x = a ** b; ".len()
        );
    }

    #[test]
    fn closer_skips_escapes_and_code() {
        let escaped = "Use **bold \\** still** end ";
        assert_eq!(
            reveal("", escaped, 6, false),
            "Use **bold \\** still**".len()
        );
        let coded = "Use **a `**` b** c ";
        assert_eq!(reveal("", coded, 6, false), "Use **a `**` b**".len());
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
        assert_eq!(reveal("", "Run `cargo te", 20, false), "Run ".len());
        assert_eq!(reveal("", "a `**` b ", 20, false), "a `**` b ".len());
    }

    #[test]
    fn hold_cap_releases_span() {
        let waiting = format!("**{} ", "word ".repeat(40));
        assert_eq!(reveal("", &waiting, 10, false), "**word word ".len());
    }

    #[test]
    fn table_row_waits_for_newline() {
        let waiting = "Intro\n| a | b ";
        assert_eq!(reveal("", waiting, 40, false), "Intro\n".len());
        assert_eq!(reveal("", "| a | b |\n| c", 40, false), "| a | b |\n".len());
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
    fn pause_keeps_fence_scan() {
        let mut transcript = Transcript::default();
        let mut pacer = StreamPacer::default();
        let start = Instant::now();
        transcript.append_paced_message_delta(1, "First line here.\nSecond line here.\n");
        let mut frame = 0;
        while transcript.unrevealed().is_some() {
            pacer.tick(&mut transcript, start + FRAME * frame);
            frame += 1;
        }
        let scanned = pacer.fences.scanned;
        assert!(scanned > 0);
        // A frame with nothing waiting keeps the scan for the next burst.
        assert!(!pacer.tick(&mut transcript, start + FRAME * frame));
        assert_eq!(pacer.fences.scanned, scanned);
        // The next burst continues the scan where it stopped.
        transcript.append_paced_message_delta(1, "Third line arrives later. ");
        pacer.tick(&mut transcript, start + Duration::from_secs(2));
        assert_eq!(
            pacer.fences.scanned,
            "First line here.\nSecond line here.\n".len()
        );
    }

    #[test]
    fn replaced_transcript_resets_scan() {
        let start = Instant::now();
        let mut pacer = StreamPacer::default();
        let mut old = Transcript::default();
        old.append_paced_message_delta(1, "An old reply.\nWith lines.\n");
        let mut frame = 0;
        while old.unrevealed().is_some() {
            pacer.tick(&mut old, start + FRAME * frame);
            frame += 1;
        }
        // A new transcript reuses the entry id, and its first delta is as long as the old
        // reply, so neither the id nor the length tells them apart.
        let mut new = Transcript::default();
        new.append_paced_message_delta(1, "A new reply, just as long text. ");
        for frame in 0..40 {
            pacer.tick(&mut new, start + Duration::from_secs(5) + FRAME * frame);
        }
        assert_eq!(shown(&new), "A new reply, just as long text. ");
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
        assert_eq!(reveal("", line, 200, false), line.len());
    }

    #[test]
    fn arrived_row_shows_whole() {
        // A short row whose line break has arrived is shown whole, however much follows.
        let waiting = format!("| a | b |\n{}", "more words ".repeat(80));
        assert_eq!(reveal("", &waiting, 3, false), "| a | b |\n".len());
        // A row still arriving waits for its line break.
        assert_eq!(reveal("Intro\n", "| a | b ", 40, false), 0);
    }

    #[test]
    fn long_line_scan_is_bounded() {
        let mut fences = FenceState::default();
        let mut shown = String::new();
        for _ in 0..200 {
            shown.push_str("word ");
            fences = fences.advance_shown(&shown);
        }
        // An unfinished line is searched once, not again on every frame.
        assert_eq!((fences.scanned, fences.searched), (0, shown.len()));
        shown.push_str(&"word ".repeat(2_000));
        let fences = fences.advance_shown(&shown);
        let waiting = "and **bold te";
        let end = reveal_end(&shown, waiting, 20, false, fences);
        assert_eq!(end, "and ".len());
    }

    #[test]
    fn long_table_row_is_shown() {
        let row = format!("| {} ", "cell ".repeat(120));
        assert_eq!(reveal("", &row, 1_000, false), row.len());
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
