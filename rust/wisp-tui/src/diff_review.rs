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
    top: usize,
    height: usize,
    rows: Vec<ReviewLine>,
    signature: Option<(usize, bool, bool, Palette)>,
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
            let anchor = self.rows.get(self.top).map(|row| (row.key, row.offset));
            self.rows = render_rows(
                detail,
                width,
                self.split && width >= 100,
                self.context,
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

fn cell(
    row: Option<&DetailRow>,
    old: bool,
    width: usize,
    palette: Palette,
    emphasis: &[std::ops::Range<usize>],
    language: &str,
    dual: bool,
) -> Vec<(Line<'static>, usize)> {
    let Some(row) = row else {
        return vec![(Line::raw(" ".repeat(width)), 0)];
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
                let label = if index == 0 {
                    prefix.clone()
                } else {
                    format!("{}↪ ", " ".repeat(gutter.saturating_sub(2)))
                };
                spans.push(Span::styled(
                    label,
                    Style::default().fg(color).bg(background),
                ));
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
    palette: Palette,
) -> Vec<ReviewLine> {
    let emphasis = crate::diff_card::row_emphasis(&detail.rows);
    let language = detail.title.rsplit('.').next().unwrap_or("");
    let mut output = Vec::new();
    let mut index = 0;
    while index < detail.rows.len() {
        let row = &detail.rows[index];
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
            let half = width.saturating_sub(3) / 2;
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
                );
                let rhs = cell(
                    right.map(|i| &detail.rows[i]),
                    false,
                    width - half - 3,
                    palette,
                    right.map_or(&[], |i| &emphasis[i]),
                    language,
                    false,
                );
                for n in 0..lhs.len().max(rhs.len()) {
                    let mut spans = lhs.get(n).map_or_else(
                        || vec![Span::raw(" ".repeat(half))],
                        |(line, _)| line.spans.clone(),
                    );
                    spans.push(Span::styled(" │ ", Style::default().fg(palette.muted)));
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
        let split = render_rows(&detail, 120, true, true, Palette::default());
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
