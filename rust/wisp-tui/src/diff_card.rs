//! The collapsed preview of an `edit` or `write` diff inside its transcript card.
//!
//! The retained detail rows follow unified-diff conventions (hunk headers, "no
//! newline" notes) and still back the Enter-details view unchanged. The card shows a
//! quieter projection of the same rows: hunk headers become `⋯` separators, a
//! trailing-newline change becomes one `⏎` row, `write` diffs get a line-number
//! gutter, and the words that differ between a deleted line and its replacement
//! are marked so a rewrap reads as a rewrap.

use std::ops::Range;

use similar::{ChangeTag, TextDiff};

use crate::tool_detail::{
    DETAIL_COLLAPSED_MAX_BYTES, DETAIL_COLLAPSED_MAX_ROWS, DetailRow, DetailRowKind,
    ToolDetailPresentation, select_detail_rows,
};
use crate::transcript_view::TranscriptRowTone;

/// Lines longer than this keep their row band but get no word-level emphasis, which
/// bounds the per-pair diff cost.
const EMPHASIS_MAX_LINE_BYTES: usize = 512;
const HUNK_SEPARATOR: &str = "⋯";

/// Byte ranges of changed words within one line.
type WordRanges = Vec<Range<usize>>;

/// A diff card's preview text, with byte ranges for row tones and changed words.
pub(crate) struct DiffCardPreview {
    pub text: String,
    pub tones: Vec<(Range<usize>, TranscriptRowTone)>,
    pub emphasis: Vec<Range<usize>>,
}

/// Build the collapsed card preview for a diff presentation.
///
/// Rows are filtered before the collapsed row budget is applied, so hunk headers and
/// notes do not crowd out changed lines.
pub(crate) fn preview(detail: &ToolDetailPresentation) -> DiffCardPreview {
    let gutter = detail
        .rows
        .iter()
        .any(|row| row.old_line.is_some() || row.new_line.is_some());
    let rows = select_detail_rows(
        &card_rows(&detail.rows),
        DETAIL_COLLAPSED_MAX_ROWS,
        DETAIL_COLLAPSED_MAX_BYTES,
    );
    let emphasis = row_emphasis(&rows);
    let mut preview = DiffCardPreview {
        text: String::new(),
        tones: Vec::new(),
        emphasis: Vec::new(),
    };
    for (row, emphasis) in rows.iter().zip(emphasis) {
        if !preview.text.is_empty() {
            preview.text.push('\n');
        }
        let start = preview.text.len();
        let (line, content_start) = format_row(row, gutter);
        preview.text.push_str(&line);
        let end = preview.text.len();
        let tone = row_tone(row.kind);
        if tone != TranscriptRowTone::Default && start < end {
            preview.tones.push((start..end, tone));
        }
        let offset = start + content_start;
        preview.emphasis.extend(
            emphasis
                .into_iter()
                .map(|range| offset + range.start..offset + range.end),
        );
    }
    if detail.truncated {
        preview.push_line(
            "⋯ retained detail is incomplete",
            TranscriptRowTone::Warning,
        );
    }
    preview.push_line("F6 browse · Enter details", TranscriptRowTone::Muted);
    preview
}

impl DiffCardPreview {
    fn push_line(&mut self, line: &str, tone: TranscriptRowTone) {
        if !self.text.is_empty() {
            self.text.push('\n');
        }
        let start = self.text.len();
        self.text.push_str(line);
        self.tones.push((start..self.text.len(), tone));
    }
}

/// Rows worth showing in a card: the first hunk header is dropped and later ones
/// become separators; "no newline" notes are dropped unless the trailing newline
/// changed within that hunk, which becomes one `⏎` row.
fn card_rows(rows: &[DetailRow]) -> Vec<DetailRow> {
    fn flush(hunk: &mut Vec<DetailRow>, card: &mut Vec<DetailRow>) {
        let old = hunk.iter().any(|row| is_note(row, "old file"));
        let new = hunk.iter().any(|row| is_note(row, "new file"));
        // Only a replacement can add or remove a final newline. In a pure insertion
        // (a created file) or deletion, a lone note describes one side only.
        let replaced = hunk.iter().any(|row| row.kind == DetailRowKind::Deletion)
            && hunk.iter().any(|row| row.kind == DetailRowKind::Addition);
        let marker = match (old && replaced, new && replaced) {
            (true, false) => Some("⏎ trailing newline added"),
            (false, true) => Some("⏎ trailing newline removed"),
            _ => None,
        };
        let template = hunk
            .iter()
            .find(|row| row.kind == DetailRowKind::Note)
            .cloned();
        card.extend(hunk.drain(..).filter(|row| row.kind != DetailRowKind::Note));
        if let (Some(text), Some(mut row)) = (marker, template) {
            row.text = text.into();
            card.push(row);
        }
    }

    let mut card = Vec::with_capacity(rows.len());
    let mut hunk: Vec<DetailRow> = Vec::new();
    let mut started = false;
    for row in rows {
        if row.kind == DetailRowKind::Hunk {
            flush(&mut hunk, &mut card);
            if started {
                let mut separator = row.clone();
                separator.text = HUNK_SEPARATOR.into();
                card.push(separator);
            }
            started = true;
            continue;
        }
        hunk.push(row.clone());
    }
    flush(&mut hunk, &mut card);
    card
}

fn is_note(row: &DetailRow, side: &str) -> bool {
    row.kind == DetailRowKind::Note && row.text.ends_with(side)
}

/// Format one card row, returning the line and the byte offset where its source
/// text starts (emphasis ranges are relative to that text).
fn format_row(row: &DetailRow, gutter: bool) -> (String, usize) {
    let (sign, number) = match row.kind {
        DetailRowKind::Addition => ("+", row.new_line),
        DetailRowKind::Deletion => ("-", row.old_line),
        DetailRowKind::Context => (" ", row.new_line.or(row.old_line)),
        _ => return (crate::transcript_view::format_structured_detail_row(row), 0),
    };
    let prefix = if gutter {
        format!(
            "{:>4} │ {sign} ",
            number.map_or(String::new(), |line| line.to_string())
        )
    } else {
        format!("{sign} ")
    };
    (format!("{prefix}{}", row.text), prefix.len())
}

fn row_tone(kind: DetailRowKind) -> TranscriptRowTone {
    match kind {
        DetailRowKind::Addition => TranscriptRowTone::DiffAddition,
        DetailRowKind::Deletion => TranscriptRowTone::DiffDeletion,
        DetailRowKind::Hunk | DetailRowKind::Note => TranscriptRowTone::Muted,
        DetailRowKind::Omission => TranscriptRowTone::Warning,
        _ => TranscriptRowTone::Default,
    }
}

/// Changed-word ranges for each row, relative to its text. Each run of deleted
/// lines is paired line by line with the added lines that follow it; a pair with
/// nothing in common gets no emphasis, since marking every word adds nothing to the
/// row band.
fn row_emphasis(rows: &[DetailRow]) -> Vec<Vec<Range<usize>>> {
    let mut emphasis = vec![Vec::new(); rows.len()];
    let mut index = 0;
    while index < rows.len() {
        let deletions = run_length(&rows[index..], DetailRowKind::Deletion);
        if deletions == 0 {
            index += 1;
            continue;
        }
        let additions = run_length(&rows[index + deletions..], DetailRowKind::Addition);
        for pair in 0..deletions.min(additions) {
            let old = index + pair;
            let new = index + deletions + pair;
            if let Some((old_ranges, new_ranges)) = changed_words(&rows[old].text, &rows[new].text)
            {
                emphasis[old] = old_ranges;
                emphasis[new] = new_ranges;
            }
        }
        index += deletions + additions;
    }
    emphasis
}

fn run_length(rows: &[DetailRow], kind: DetailRowKind) -> usize {
    rows.iter().take_while(|row| row.kind == kind).count()
}

/// Word-level differences between two lines, as byte ranges trimmed of
/// surrounding whitespace. `None` when either line is too long to diff, the lines
/// share no word, or nothing differs.
fn changed_words(old: &str, new: &str) -> Option<(WordRanges, WordRanges)> {
    if old.len() > EMPHASIS_MAX_LINE_BYTES || new.len() > EMPHASIS_MAX_LINE_BYTES {
        return None;
    }
    let diff = TextDiff::from_words(old, new);
    let mut old_ranges = Vec::new();
    let mut new_ranges = Vec::new();
    let (mut old_offset, mut new_offset) = (0, 0);
    let mut shared = false;
    for change in diff.iter_all_changes() {
        let value = change.value();
        match change.tag() {
            ChangeTag::Equal => {
                shared |= !value.trim().is_empty();
                old_offset += value.len();
                new_offset += value.len();
            }
            ChangeTag::Delete => {
                push_trimmed(&mut old_ranges, old, old_offset..old_offset + value.len());
                old_offset += value.len();
            }
            ChangeTag::Insert => {
                push_trimmed(&mut new_ranges, new, new_offset..new_offset + value.len());
                new_offset += value.len();
            }
        }
    }
    (shared && !(old_ranges.is_empty() && new_ranges.is_empty()))
        .then_some((old_ranges, new_ranges))
}

/// Add `range` without its surrounding whitespace, merging it into the previous
/// range when only whitespace separates them, so "so they" is one marked span.
fn push_trimmed(ranges: &mut Vec<Range<usize>>, text: &str, range: Range<usize>) {
    let slice = &text[range.clone()];
    let start = range.start + (slice.len() - slice.trim_start().len());
    let end = range.end - (slice.len() - slice.trim_end().len());
    if start >= end {
        return;
    }
    if let Some(last) = ranges.last_mut() {
        if text[last.end..start].trim().is_empty() {
            last.end = end;
            return;
        }
    }
    ranges.push(start..end);
}

#[cfg(test)]
mod tests {
    use super::*;

    fn row(kind: DetailRowKind, text: &str) -> DetailRow {
        DetailRow {
            key: 0,
            kind,
            text: text.into(),
            old_line: None,
            new_line: None,
            hidden_rows: 0,
            hidden_bytes: 0,
        }
    }

    fn marked<'a>(text: &'a str, ranges: &[Range<usize>]) -> Vec<&'a str> {
        ranges.iter().map(|range| &text[range.clone()]).collect()
    }

    #[test]
    fn a_rewrap_marks_only_the_moved_words() {
        let old_first =
            "retain portable text and calls, not the provider-native ``Part`` payload, so they";
        let old_second = "cannot safely rebuild an active tool exchange as fresh context.";
        let new_first = "retain portable text and calls, not the provider-native ``Part`` payload,";
        let new_second = "so they cannot safely rebuild an active tool exchange as fresh context.";
        let rows = [
            row(DetailRowKind::Deletion, old_first),
            row(DetailRowKind::Deletion, old_second),
            row(DetailRowKind::Addition, new_first),
            row(DetailRowKind::Addition, new_second),
        ];
        let emphasis = row_emphasis(&rows);
        assert_eq!(marked(old_first, &emphasis[0]), ["so they"]);
        assert!(emphasis[1].is_empty());
        assert!(emphasis[2].is_empty());
        assert_eq!(marked(new_second, &emphasis[3]), ["so they"]);
    }

    #[test]
    fn a_single_word_change_marks_that_word_on_both_sides() {
        let (old, new) = changed_words("wait 30 seconds", "wait 60 seconds").unwrap();
        assert_eq!(marked("wait 30 seconds", &old), ["30"]);
        assert_eq!(marked("wait 60 seconds", &new), ["60"]);
    }

    #[test]
    fn unrelated_or_oversized_lines_keep_only_their_band() {
        assert!(changed_words("alpha beta", "gamma delta").is_none());
        let long = "word ".repeat(EMPHASIS_MAX_LINE_BYTES);
        assert!(changed_words(&long, &format!("{long}more")).is_none());
        // A pure addition has nothing to pair with.
        let rows = [row(DetailRowKind::Addition, "new line")];
        assert!(row_emphasis(&rows)[0].is_empty());
    }

    #[test]
    fn hunk_headers_become_separators_after_the_first() {
        let rows = [
            row(DetailRowKind::Hunk, "@@ edit 1 @@"),
            row(DetailRowKind::Deletion, "a"),
            row(DetailRowKind::Hunk, "@@ edit 2 @@"),
            row(DetailRowKind::Addition, "b"),
        ];
        let texts: Vec<String> = card_rows(&rows).into_iter().map(|row| row.text).collect();
        assert_eq!(texts, ["a", "⋯", "b"]);
    }

    #[test]
    fn newline_notes_show_only_a_change_in_the_trailing_newline() {
        let note = |side: &str| {
            row(
                DetailRowKind::Note,
                &format!("\\ No newline at end of {side}"),
            )
        };
        let texts = |rows: Vec<DetailRow>| -> Vec<String> {
            card_rows(&rows).into_iter().map(|row| row.text).collect()
        };
        // Both sides lack it (as in most edit fragments): nothing changed.
        assert_eq!(
            texts(vec![
                row(DetailRowKind::Deletion, "old"),
                note("old file"),
                row(DetailRowKind::Addition, "new"),
                note("new file"),
            ]),
            ["old", "new"]
        );
        assert_eq!(
            texts(vec![
                row(DetailRowKind::Context, "same"),
                note("either file")
            ]),
            ["same"]
        );
        // Only the old side lacked it: the change adds one.
        assert_eq!(
            texts(vec![
                row(DetailRowKind::Deletion, "old"),
                note("old file"),
                row(DetailRowKind::Addition, "old"),
            ]),
            ["old", "old", "⏎ trailing newline added"]
        );
        assert_eq!(
            texts(vec![
                row(DetailRowKind::Deletion, "old"),
                row(DetailRowKind::Addition, "old"),
                note("new file"),
            ]),
            ["old", "old", "⏎ trailing newline removed"]
        );
        // A created file without a final newline had no newline to remove.
        assert_eq!(
            texts(vec![
                row(DetailRowKind::Addition, "first"),
                row(DetailRowKind::Addition, "second"),
                note("new file"),
            ]),
            ["first", "second"]
        );
        assert_eq!(
            texts(vec![row(DetailRowKind::Deletion, "gone"), note("old file")]),
            ["gone"]
        );
    }

    #[test]
    fn write_rows_get_a_gutter_and_edit_rows_do_not() {
        let mut addition = row(DetailRowKind::Addition, "value");
        assert_eq!(format_row(&addition, false), ("+ value".into(), 2));
        addition.new_line = Some(118);
        assert_eq!(
            format_row(&addition, true),
            (" 118 │ + value".into(), " 118 │ + ".len())
        );
    }
}
