//! Presentation-only review of retained changes. No filesystem reads or git policy.

use ratatui::{
    style::Style,
    text::{Line, Span},
};
use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::theme::Palette;
use crate::tool_detail::{
    DetailPresentationKind, DetailRow, DetailRowKind, ToolDetailPresentation,
};
use crate::ui::sanitize_for_terminal;

pub(crate) fn is_diff(detail: &ToolDetailPresentation) -> bool {
    matches!(
        detail.kind,
        DetailPresentationKind::DiffCreate | DetailPresentationKind::DiffModify
    )
}

#[derive(Debug, Default)]
pub(crate) struct DiffReview {
    pub split: bool,
    pub context: bool,
    /// The `/diff` layout: hunk headers drawn as bars naming their definition, the
    /// unchanged lines between hunks folded, and context rows always shown (the
    /// caller widens the context itself). Without it, context rows fold in place.
    pub hunk_bars: bool,
    top: usize,
    height: usize,
    rows: Vec<ReviewLine>,
    signature: Option<(usize, bool, bool, Palette)>,
    /// The detail row to put at the top once the next presentation is laid out,
    /// when the caller swapped presentations whose row keys differ.
    reopen_key: Option<u64>,
    /// Whether the last layout set the sides side by side, which needs the width.
    split_shown: bool,
}

#[derive(Debug)]
struct ReviewLine {
    key: u64,
    offset: usize,
    paired: Option<(u64, usize)>,
    hunk: bool,
    line: Line<'static>,
}

impl DiffReview {
    /// The `/diff` review: hunk bars, and split where there is room, as the wide
    /// mock reads; `s` switches to unified.
    pub fn working_tree() -> Self {
        Self {
            split: true,
            hunk_bars: true,
            ..Self::default()
        }
    }

    /// The detail row at the top of the view.
    pub fn top_key(&self) -> Option<u64> {
        self.rows.get(self.top).map(|row| row.key)
    }

    /// Lay out a different presentation next, starting at its row `key`.
    pub fn reopen_at(&mut self, key: u64) {
        self.reopen_key = Some(key);
        self.signature = None;
    }

    /// Whether the diff on screen is split, which a narrow pane cannot be.
    pub fn split_shown(&self) -> bool {
        self.split_shown
    }

    pub fn reset(&mut self) {
        self.top = 0;
        self.signature = None;
        self.rows.clear();
    }

    pub fn scroll(&mut self, delta: i32) {
        self.top = self
            .top
            .saturating_add_signed(delta as isize)
            .min(self.rows.len().saturating_sub(1));
    }

    pub fn home(&mut self) {
        self.top = 0;
    }
    pub fn end(&mut self) {
        self.top = self.rows.len().saturating_sub(self.height);
    }
    pub fn page(&mut self, forward: bool) {
        self.scroll(self.height.saturating_sub(1).max(1) as i32 * if forward { 1 } else { -1 });
    }

    pub fn jump_hunk(&mut self, forward: bool) {
        let target = if forward {
            self.rows
                .iter()
                .enumerate()
                .find(|(i, row)| *i > self.top && row.hunk)
                .map(|(i, _)| i)
        } else {
            self.rows
                .iter()
                .enumerate()
                .rev()
                .find(|(i, row)| *i < self.top && row.hunk)
                .map(|(i, _)| i)
        };
        if let Some(target) = target {
            self.top = target;
        }
    }

    /// The hunk the top row is in, 1-based, and how many hunks there are; `None`
    /// before the first frame or for a diff without hunks.
    pub fn hunk_position(&self) -> Option<(usize, usize)> {
        let total = self.rows.iter().filter(|row| row.hunk).count();
        let current = self
            .rows
            .iter()
            .take(self.top + 1)
            .filter(|row| row.hunk)
            .count();
        (total > 0).then_some((current.max(1), total))
    }

    #[cfg(feature = "transcript-benchmark")]
    pub fn visible_key(&self, key: u64) -> bool {
        self.rows
            .iter()
            .skip(self.top)
            .take(self.height)
            .any(|row| row.key == key || row.paired.is_some_and(|(paired, _)| paired == key))
    }

    pub fn lines(
        &mut self,
        detail: &ToolDetailPresentation,
        width: usize,
        height: usize,
        palette: Palette,
    ) -> Vec<Line<'static>> {
        let signature = (width, self.split, self.context, palette);
        if self.signature != Some(signature) {
            let anchor = match self.reopen_key.take() {
                Some(key) => Some((key, 0)),
                None => self.rows.get(self.top).map(|row| (row.key, row.offset)),
            };
            self.split_shown = self.split && width >= 100;
            self.rows = render_rows(
                detail,
                width,
                self.split_shown,
                self.context || self.hunk_bars,
                self.hunk_bars,
                palette,
            );
            self.top = anchor
                .and_then(|(key, offset)| {
                    self.rows
                        .iter()
                        .rposition(|row| {
                            (row.key == key && row.offset <= offset)
                                || row
                                    .paired
                                    .is_some_and(|(paired, start)| paired == key && start <= offset)
                        })
                        .or_else(|| self.rows.iter().rposition(|row| row.key <= key))
                })
                .unwrap_or(0);
            self.signature = Some(signature);
        }
        self.height = height.max(1);
        self.rows
            .iter()
            .skip(self.top)
            .take(self.height)
            .map(|row| row.line.clone())
            .collect()
    }
}

fn chunks(text: &str, width: usize) -> Vec<String> {
    let text = sanitize_for_terminal(text);
    let mut chunks = vec![String::new()];
    let mut columns = 0;
    for grapheme in text.graphemes(true) {
        let size = grapheme.width();
        if columns > 0 && columns + size > width.max(1) {
            chunks.push(String::new());
            columns = 0;
        }
        chunks.last_mut().unwrap().push_str(grapheme);
        columns += size;
    }
    chunks
}

#[allow(clippy::too_many_arguments)]
fn cell(
    row: Option<&DetailRow>,
    old: bool,
    width: usize,
    palette: Palette,
    emphasis: &[std::ops::Range<usize>],
    language: &str,
    dual: bool,
    hunk_bars: bool,
) -> Vec<(Line<'static>, usize)> {
    let Some(row) = row else {
        let filler = if hunk_bars {
            Style::default().bg(palette.diff_filler())
        } else {
            Style::default()
        };
        return vec![(Line::styled(" ".repeat(width), filler), 0)];
    };
    let (sign, color, background) = match row.kind {
        DetailRowKind::Addition => ("+", palette.success, palette.addition_background),
        DetailRowKind::Deletion => ("−", palette.error, palette.deletion_background),
        _ => (" ", palette.muted, palette.background),
    };
    let number = if old { row.old_line } else { row.new_line };
    let prefix = if dual {
        format!(
            "{:>4} {:>4} {sign} ",
            row.old_line.map_or(String::new(), |n| n.to_string()),
            row.new_line.map_or(String::new(), |n| n.to_string())
        )
    } else {
        format!(
            "{:>4} {sign} ",
            number.map_or(String::new(), |n| n.to_string())
        )
    };
    let gutter = prefix.width();
    // Tiny terminals still get readable source; the full gutter returns on resize.
    let gutter = if width > gutter + 4 { gutter } else { 0 };
    let source = chunks(&row.text, width.saturating_sub(gutter));
    let base = Style::default().fg(palette.foreground).bg(background);
    let safe_offsets = sanitize_for_terminal(&row.text) == row.text;
    let tokens = crate::syntax::highlight_fence(language, &row.text)
        .map(|highlight| highlight.tokens)
        .unwrap_or_default();
    let mut offset = 0;
    source
        .into_iter()
        .enumerate()
        .map(|(index, text)| {
            let mut spans: Vec<Span<'static>> = Vec::new();
            if gutter > 0 {
                if index == 0 {
                    spans.push(Span::styled(
                        prefix.clone(),
                        Style::default().fg(color).bg(background),
                    ));
                } else if hunk_bars {
                    // A wrapped line continues under its sign: `↳`.
                    spans.push(Span::styled(
                        format!("{}↳ ", " ".repeat(gutter.saturating_sub(2))),
                        Style::default().fg(palette.dim()).bg(background),
                    ));
                } else {
                    spans.push(Span::styled(
                        format!("{}↪ ", " ".repeat(gutter.saturating_sub(2))),
                        Style::default().fg(color).bg(background),
                    ));
                }
            }
            for (position, grapheme) in text.grapheme_indices(true) {
                let position = offset + position;
                let class = if safe_offsets {
                    tokens
                        .iter()
                        .find(|token| token.range.contains(&position))
                        .map_or(crate::syntax::SyntaxClass::Plain, |token| token.class)
                } else {
                    crate::syntax::SyntaxClass::Plain
                };
                let mut style = palette.syntax(base, class);
                if safe_offsets && emphasis.iter().any(|range| range.contains(&position)) {
                    style = palette.diff_change(style);
                }
                if let Some(last) = spans.last_mut().filter(|span| span.style == style) {
                    last.content.to_mut().push_str(grapheme);
                } else {
                    spans.push(Span::styled(grapheme.to_owned(), style));
                }
            }
            let start = offset;
            offset += text.len();
            let used = gutter + text.width();
            spans.push(Span::styled(" ".repeat(width.saturating_sub(used)), base));
            (Line::from(spans), start)
        })
        .collect()
}

fn render_rows(
    detail: &ToolDetailPresentation,
    width: usize,
    split: bool,
    context: bool,
    hunk_bars: bool,
    palette: Palette,
) -> Vec<ReviewLine> {
    let emphasis = crate::diff_card::row_emphasis(&detail.rows);
    let language = detail.title.rsplit('.').next().unwrap_or("");
    let mut output = Vec::new();
    let mut index = 0;
    let hunks: Vec<&DetailRow> = detail
        .rows
        .iter()
        .filter(|row| row.kind == DetailRowKind::Hunk)
        .collect();
    let mut hunk_index = 0;
    while index < detail.rows.len() {
        let row = &detail.rows[index];
        if hunk_bars && row.kind == DetailRowKind::Hunk {
            if hunk_index > 0 {
                let previous = hunks[hunk_index - 1];
                if let Some(gap) = unchanged_between(previous, row) {
                    output.push(ReviewLine {
                        key: row.key,
                        offset: 0,
                        paired: None,
                        hunk: false,
                        line: fold_line(gap, palette),
                    });
                }
                output.push(ReviewLine {
                    key: row.key,
                    offset: 0,
                    paired: None,
                    hunk: false,
                    line: Line::default(),
                });
            }
            let label = match hunk_index {
                0 if split => "split · s unified".to_owned(),
                0 => "unified · s split".to_owned(),
                index => format!("{} of {}", index + 1, hunks.len()),
            };
            output.push(ReviewLine {
                key: row.key,
                offset: 0,
                paired: None,
                hunk: true,
                line: hunk_bar(&row.text, &label, language, width, palette),
            });
            hunk_index += 1;
            index += 1;
            continue;
        }
        if row.kind == DetailRowKind::Context && !context {
            let start = index;
            while index < detail.rows.len() && detail.rows[index].kind == DetailRowKind::Context {
                index += 1;
            }
            let label = format!("  ⋯ {} unchanged lines · c to show context", index - start);
            for text in chunks(&label, width) {
                output.push(ReviewLine {
                    key: row.key,
                    offset: 0,
                    paired: None,
                    hunk: false,
                    line: Line::styled(text, Style::default().fg(palette.muted)),
                });
            }
            continue;
        }
        if !matches!(
            row.kind,
            DetailRowKind::Addition | DetailRowKind::Deletion | DetailRowKind::Context
        ) {
            let mut offset = 0;
            for (part, text) in chunks(&row.text, width).into_iter().enumerate() {
                let start = offset;
                offset += text.len();
                output.push(ReviewLine {
                    key: row.key,
                    offset: start,
                    paired: None,
                    hunk: part == 0 && row.kind == DetailRowKind::Hunk,
                    line: Line::styled(text, Style::default().fg(palette.muted)),
                });
            }
            index += 1;
            continue;
        }
        if split {
            // `/diff` sets the sides a column apart, as its mock does; elsewhere a
            // rule divides them.
            let separator = if hunk_bars { " " } else { " │ " };
            let half = width.saturating_sub(separator.width()) / 2;
            let mut pairs = Vec::new();
            if row.kind == DetailRowKind::Deletion {
                let deleted = detail.rows[index..]
                    .iter()
                    .take_while(|r| r.kind == DetailRowKind::Deletion)
                    .count();
                let added = detail.rows[index + deleted..]
                    .iter()
                    .take_while(|r| r.kind == DetailRowKind::Addition)
                    .count();
                for n in 0..deleted.max(added) {
                    pairs.push((
                        (n < deleted).then_some(index + n),
                        (n < added).then_some(index + deleted + n),
                    ));
                }
                index += deleted + added;
            } else {
                pairs.push((
                    if row.kind == DetailRowKind::Context {
                        Some(index)
                    } else {
                        None
                    },
                    Some(index),
                ));
                index += 1;
            }
            for (left, right) in pairs {
                let lhs = cell(
                    left.map(|i| &detail.rows[i]),
                    true,
                    half,
                    palette,
                    left.map_or(&[], |i| &emphasis[i]),
                    language,
                    false,
                    hunk_bars,
                );
                let rhs = cell(
                    right.map(|i| &detail.rows[i]),
                    false,
                    width - half - separator.width(),
                    palette,
                    right.map_or(&[], |i| &emphasis[i]),
                    language,
                    false,
                    hunk_bars,
                );
                for n in 0..lhs.len().max(rhs.len()) {
                    let mut spans = lhs.get(n).map_or_else(
                        || vec![Span::raw(" ".repeat(half))],
                        |(line, _)| line.spans.clone(),
                    );
                    spans.push(Span::styled(separator, Style::default().fg(palette.muted)));
                    if let Some((line, _)) = rhs.get(n) {
                        spans.extend(line.spans.clone());
                    }
                    output.push(ReviewLine {
                        key: detail.rows[if n < lhs.len() {
                            left.or(right)
                        } else {
                            right.or(left)
                        }
                        .unwrap()]
                        .key,
                        offset: if n < lhs.len() { lhs[n].1 } else { rhs[n].1 },
                        paired: right.and_then(|i| {
                            rhs.get(n).map(|(_, offset)| (detail.rows[i].key, *offset))
                        }),
                        hunk: false,
                        line: Line::from(spans),
                    });
                }
            }
        } else {
            for (line, offset) in cell(
                Some(row),
                row.kind == DetailRowKind::Deletion,
                width,
                palette,
                &emphasis[index],
                language,
                true,
                hunk_bars,
            ) {
                output.push(ReviewLine {
                    key: row.key,
                    offset,
                    paired: None,
                    hunk: false,
                    line,
                });
            }
            index += 1;
        }
    }
    output
}

/// How many unchanged old-file lines lie between two hunks, read from their headers.
fn unchanged_between(previous: &DetailRow, next: &DetailRow) -> Option<usize> {
    let range = |header: &str| -> Option<(usize, usize)> {
        let old = header.strip_prefix("@@ -")?.split(' ').next()?;
        let (start, length) = old.split_once(',').unwrap_or((old, "1"));
        Some((start.parse().ok()?, length.parse().ok()?))
    };
    let (previous_start, previous_length) = range(&previous.text)?;
    let (next_start, _) = range(&next.text)?;
    let gap = next_start.checked_sub(previous_start + previous_length)?;
    (gap > 0).then_some(gap)
}

/// `⋯ 46 unchanged lines · c show context`, set in under the line numbers.
fn fold_line(gap: usize, palette: Palette) -> Line<'static> {
    let dim = Style::default().fg(palette.dim());
    Line::from(vec![
        Span::styled(
            format!(
                "       ⋯ {gap} unchanged line{} · ",
                if gap == 1 { "" } else { "s" }
            ),
            dim,
        ),
        Span::styled("c", palette.keycap()),
        Span::styled(" show context", dim),
    ])
}

/// A hunk header as a bar across the pane: the range and the definition it falls
/// in on the left, `label` on the right.
fn hunk_bar(
    header: &str,
    label: &str,
    language: &str,
    width: usize,
    palette: Palette,
) -> Line<'static> {
    let bar = Style::default().fg(palette.muted).bg(palette.surface);
    let header = sanitize_for_terminal(header);
    let (range, definition) = match header.match_indices("@@").nth(1) {
        Some((end, _)) => header.split_at(end + 2),
        None => (header.as_str(), ""),
    };
    let mut left = vec![Span::styled(format!(" {range}"), bar)];
    let definition = definition.trim_start();
    if !definition.is_empty() {
        left.push(Span::styled(" ", bar));
        let tokens = crate::syntax::highlight_fence(language, definition)
            .map(|highlight| highlight.tokens)
            .unwrap_or_default();
        for (position, grapheme) in definition.grapheme_indices(true) {
            let class = tokens
                .iter()
                .find(|token| token.range.contains(&position))
                .map_or(crate::syntax::SyntaxClass::Plain, |token| token.class);
            let style = palette.syntax(bar.fg(palette.foreground), class);
            if let Some(last) = left.last_mut().filter(|span| span.style == style) {
                last.content.to_mut().push_str(grapheme);
            } else {
                left.push(Span::styled(grapheme.to_owned(), style));
            }
        }
    }
    let right = format!("{label} ");
    let right_width = right.width();
    let mut spans = crate::side_panel::clip_spans(left, width.saturating_sub(right_width + 1));
    let used: usize = spans.iter().map(|span| span.content.width()).sum();
    spans.push(Span::styled(
        " ".repeat(width.saturating_sub(used + right_width)),
        bar,
    ));
    spans.push(Span::styled(right, bar.fg(palette.dim())));
    Line::from(spans)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn presentation() -> ToolDetailPresentation {
        let rows = [
            (DetailRowKind::Hunk, "@@ first @@"),
            (DetailRowKind::Context, "context"),
            (DetailRowKind::Deletion, "wait 30 seconds"),
            (DetailRowKind::Addition, "wait 60 seconds"),
            (DetailRowKind::Hunk, "@@ second @@"),
            (DetailRowKind::Addition, "新しい行"),
        ]
        .into_iter()
        .enumerate()
        .map(|(index, (kind, text))| DetailRow {
            key: index as u64,
            kind,
            text: text.into(),
            old_line: (kind != DetailRowKind::Addition).then_some(index as u64),
            new_line: (kind != DetailRowKind::Deletion).then_some(index as u64),
            hidden_rows: 0,
            hidden_bytes: 0,
        })
        .collect();
        ToolDetailPresentation {
            kind: DetailPresentationKind::DiffModify,
            title: "M file.txt".into(),
            summary: "+2 -1".into(),
            additions: 2,
            deletions: 1,
            rows,
            truncated: false,
        }
    }

    #[test]
    fn split_pairs_replacements_and_narrow_review_preserves_both_sides() {
        let detail = presentation();
        let split = render_rows(&detail, 120, true, true, false, Palette::default());
        assert!(split.iter().any(|row| {
            let text = row.line.to_string();
            text.contains("wait 30") && text.contains("wait 60")
        }));
        let mut review = DiffReview {
            split: true,
            ..Default::default()
        };
        let narrow = review.lines(&detail, 36, 20, Palette::default());
        assert!(narrow.iter().any(|row| row.to_string().contains("wait 30")));
        assert!(narrow.iter().any(|row| row.to_string().contains("wait 60")));
        assert!(narrow.iter().all(|line| line.width() <= 36));
    }

    #[test]
    fn hunk_navigation_context_toggle_and_resize_keep_source_position() {
        let detail = presentation();
        let mut review = DiffReview::default();
        let lines = review.lines(&detail, 120, 3, Palette::default());
        assert!(lines[1].to_string().contains("unchanged lines"));
        review.jump_hunk(true);
        assert_eq!(
            review.lines(&detail, 120, 3, Palette::default())[0].to_string(),
            "@@ second @@"
        );
        review.split = true;
        review.context = true;
        assert_eq!(
            review.lines(&detail, 80, 3, Palette::default())[0].to_string(),
            "@@ second @@"
        );
        review.jump_hunk(false);
        let lines = review.lines(&detail, 80, 3, Palette::default());
        assert!(lines[0].to_string().contains("first"));
        assert!(lines[1].to_string().contains("context"));
    }

    #[test]
    fn resize_and_split_keep_the_wrapped_source_segment() {
        let mut detail = presentation();
        detail.rows[2].text = "old_value ".repeat(100);
        detail.rows[3].text = "new_value ".repeat(100);
        let mut review = DiffReview::default();
        review.lines(&detail, 80, 3, Palette::default());
        review.top = review
            .rows
            .iter()
            .position(|row| row.key == 3 && row.offset > 300)
            .unwrap();
        let offset = review.rows[review.top].offset;
        review.lines(&detail, 60, 3, Palette::default());
        assert_eq!(review.rows[review.top].key, 3);
        assert!(review.rows[review.top].offset <= offset);
        assert!(offset - review.rows[review.top].offset < 60);
        review.split = true;
        review.lines(&detail, 120, 3, Palette::default());
        let (_, paired_offset) = review.rows[review.top].paired.unwrap();
        assert!(paired_offset <= offset);
        assert!(offset - paired_offset < 120);
    }

    #[test]
    fn wrapping_keeps_unicode_and_neutralizes_terminal_controls() {
        assert_eq!(chunks("αβ世界", 4), ["αβ世", "界"]);
        assert!(
            chunks("a\u{1b}]52;bad\u{7}", 100)
                .iter()
                .all(|s| !s.contains('\u{1b}'))
        );
    }
}
