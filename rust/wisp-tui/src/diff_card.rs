//! The collapsed preview of an `edit` or `write` diff inside its transcript card.
//!
//! The retained detail rows follow unified-diff conventions (hunk headers, "no
//! newline" notes) and still back the Enter-details view unchanged. The card shows a
//! quieter projection of the same rows: hunk headers become `⋯` separators, a
//! trailing-newline change becomes one `⏎` row, changes get old/new line-number
//! gutters (fragment-relative for edits), and the words that differ between a deleted line and its replacement
//! are marked so a rewrap reads as a rewrap.

use std::ops::Range;

use similar::{ChangeTag, TextDiff};

use crate::tool_detail::{
    DETAIL_COLLAPSED_MAX_BYTES, DetailRow, DetailRowKind, ToolDetailPresentation,
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
    pub syntax: Vec<crate::syntax::SyntaxToken>,
    pub gutters: Vec<Range<usize>>,
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
    let rows = coherent_preview_rows(
        &card_rows(&detail.rows),
        detail.additions.saturating_add(detail.deletions),
    );
    let emphasis = row_emphasis(&rows);
    let mut preview = DiffCardPreview {
        text: String::new(),
        tones: Vec::new(),
        emphasis: Vec::new(),
        syntax: Vec::new(),
        gutters: Vec::new(),
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
        if content_start > 0 {
            preview.gutters.push(start..offset);
        }
        if matches!(
            row.kind,
            DetailRowKind::Addition | DetailRowKind::Deletion | DetailRowKind::Context
        ) {
            let language = detail.title.rsplit('.').next().unwrap_or("");
            if let Ok(highlight) = crate::syntax::highlight_fence(language, &row.text) {
                preview
                    .syntax
                    .extend(highlight.tokens.into_iter().map(|mut token| {
                        token.range = offset + token.range.start..offset + token.range.end;
                        token
                    }));
            }
        }

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
    preview
}

/// Keep short hunks intact. Larger previews end at a hunk boundary when possible;
/// never splice the tail of a function onto its beginning across hidden changes.
fn coherent_preview_rows(rows: &[DetailRow], total_changes: u64) -> Vec<DetailRow> {
    const MAX_ROWS: usize = 16;
    let mut bytes = 0usize;
    let limit = rows
        .iter()
        .take(MAX_ROWS)
        .take_while(|row| {
            bytes = bytes.saturating_add(row.text.len());
            bytes <= DETAIL_COLLAPSED_MAX_BYTES
        })
        .count();
    if limit == rows.len() {
        return rows.to_vec();
    }
    let boundary = rows[..limit]
        .iter()
        .rposition(|row| row.kind == DetailRowKind::Hunk);
    let mut end = boundary.filter(|index| *index > 0).unwrap_or(limit);
    while end > 0 && rows[end - 1].text.trim().is_empty() {
        end -= 1;
    }
    let hidden = &rows[end..];
    // Retained rows may already contain an omission from the detail budget.
    // The presentation totals still count those source changes.
    let displayed_changes = rows[..end]
        .iter()
        .filter(|row| matches!(row.kind, DetailRowKind::Addition | DetailRowKind::Deletion))
        .count() as u64;
    let changed = total_changes.saturating_sub(displayed_changes);
    let mut selected = rows[..end].to_vec();
    selected.push(DetailRow {
        key: hidden.first().map_or(0, |row| row.key),
        kind: DetailRowKind::Omission,
        text: format!("▸ {changed} more changed lines · Enter to review"),
        old_line: None,
        new_line: None,
        hidden_rows: hidden.len() as u64,
        hidden_bytes: hidden.iter().map(|row| row.text.len() as u64).sum(),
    });
    selected
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
///
/// A note marks the final line of one side as unterminated ("either" marks both),
/// so a hunk changes the trailing newline when exactly one side is unterminated,
/// whether that side's final line was changed or kept as context. An empty side
/// (a created file, or a file emptied completely) has no final line to compare.
fn card_rows(rows: &[DetailRow]) -> Vec<DetailRow> {
    fn flush(hunk: &mut Vec<DetailRow>, card: &mut Vec<DetailRow>, compare: bool) {
        let either = hunk.iter().any(|row| is_note(row, "either file"));
        let old = either || hunk.iter().any(|row| is_note(row, "old file"));
        let new = either || hunk.iter().any(|row| is_note(row, "new file"));
        let marker = match (compare, old, new) {
            (true, true, false) => Some("⏎ trailing newline added"),
            (true, false, true) => Some("⏎ trailing newline removed"),
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

    let has = |kinds: [DetailRowKind; 2]| rows.iter().any(|row| kinds.contains(&row.kind));
    let compare = has([DetailRowKind::Deletion, DetailRowKind::Context])
        && has([DetailRowKind::Addition, DetailRowKind::Context]);
    let mut card = Vec::with_capacity(rows.len());
    let mut hunk: Vec<DetailRow> = Vec::new();
    let mut started = false;
    for row in rows {
        if row.kind == DetailRowKind::Hunk {
            flush(&mut hunk, &mut card, compare);
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
    flush(&mut hunk, &mut card, compare);
    card
}

fn is_note(row: &DetailRow, side: &str) -> bool {
    row.kind == DetailRowKind::Note && row.text.ends_with(side)
}

/// Format one card row, returning the line and the byte offset where its source
/// text starts (emphasis ranges are relative to that text).
fn format_row(row: &DetailRow, gutter: bool) -> (String, usize) {
    let (sign, _number) = match row.kind {
        DetailRowKind::Addition => ("+", row.new_line),
        // A card-only sign; the details view keeps the unified-diff `-`.
        DetailRowKind::Deletion => ("−", row.old_line),
        DetailRowKind::Context => (" ", row.new_line.or(row.old_line)),
        _ => return (crate::transcript_view::format_structured_detail_row(row), 0),
    };
    let prefix = if gutter {
        format!(
            "{:>4} {:>4} {sign} ",
            row.old_line.map_or(String::new(), |line| line.to_string()),
            row.new_line.map_or(String::new(), |line| line.to_string()),
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
        DetailRowKind::Hunk | DetailRowKind::Note | DetailRowKind::Omission => {
            TranscriptRowTone::SurfaceNote
        }
        _ => TranscriptRowTone::Surface,
    }
}

/// Changed-word ranges for each row, relative to its text. Each run of deleted
/// lines is paired line by line with the added lines that follow it; a pair with
/// nothing in common gets no emphasis, since marking every word adds nothing to the
/// row band.
pub(crate) fn row_emphasis(rows: &[DetailRow]) -> Vec<Vec<Range<usize>>> {
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
    fn small_functions_stay_complete_and_large_hunks_do_not_splice_tails() {
        let short = (0..12)
            .map(|n| row(DetailRowKind::Addition, &format!("line {n}")))
            .collect::<Vec<_>>();
        assert_eq!(coherent_preview_rows(&short, 12), short);
        let long = (0..30)
            .map(|n| row(DetailRowKind::Addition, &format!("line {n}")))
            .collect::<Vec<_>>();
        let preview = coherent_preview_rows(&long, 30);
        assert_eq!(preview[15].text, "line 15");
        assert_eq!(
            preview[16].text,
            "▸ 14 more changed lines · Enter to review"
        );
        assert!(!preview.iter().any(|row| row.text == "line 29"));
    }

    #[test]
    fn preview_omission_includes_changes_evicted_by_the_retained_detail_budget() {
        use crate::tool_detail::{
            DETAIL_EXPANDED_MAX_BYTES, DETAIL_EXPANDED_MAX_ROWS, DetailPresentationKind,
            select_detail_rows,
        };
        for kind in [DetailRowKind::Addition, DetailRowKind::Deletion] {
            let source = (0..500)
                .map(|n| row(kind, &format!("line {n}")))
                .collect::<Vec<_>>();
            let rows =
                select_detail_rows(&source, DETAIL_EXPANDED_MAX_ROWS, DETAIL_EXPANDED_MAX_BYTES);
            assert!(rows.iter().any(|row| row.kind == DetailRowKind::Omission));
            let detail = ToolDetailPresentation {
                kind: DetailPresentationKind::DiffModify,
                title: "M file.txt".into(),
                summary: String::new(),
                additions: if kind == DetailRowKind::Addition {
                    500
                } else {
                    0
                },
                deletions: if kind == DetailRowKind::Deletion {
                    500
                } else {
                    0
                },
                rows,
                truncated: false,
            };
            let preview = preview(&detail);
            assert!(
                preview.text.contains("484 more changed lines"),
                "{}",
                preview.text
            );
        }
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
        // Only one side changed, with the other side's final line kept as context:
        // writing "a\nb" over "a\n" removes it; dropping an unterminated "b" adds it.
        assert_eq!(
            texts(vec![
                row(DetailRowKind::Context, "a"),
                row(DetailRowKind::Addition, "b"),
                note("new file"),
            ]),
            ["a", "b", "⏎ trailing newline removed"]
        );
        assert_eq!(
            texts(vec![
                row(DetailRowKind::Context, "a"),
                row(DetailRowKind::Deletion, "b"),
                note("old file"),
            ]),
            ["a", "b", "⏎ trailing newline added"]
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
            ("      118 + value".into(), "      118 + ".len())
        );
        let deletion = row(DetailRowKind::Deletion, "value");
        assert_eq!(format_row(&deletion, false), ("− value".into(), "− ".len()));
    }
}
