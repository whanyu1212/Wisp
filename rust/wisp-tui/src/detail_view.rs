//! Bounded viewport and wrapping cache for one live retained tool detail.

use std::collections::{HashMap, VecDeque};
use std::sync::Arc;

use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::tool_detail::{DetailRow, DetailRowKind, ToolDetailPresentation};
use crate::transcript::TranscriptEntryId;
use crate::transcript_view::{format_structured_detail_row, sanitize_grapheme};

const DETAIL_CACHE_MAX_ROWS: usize = 1_024;
const DETAIL_CACHE_MAX_BYTES: usize = 512 * 1024;
const DETAIL_OVERSCAN_ROWS: usize = 8;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct DetailAnchor {
    pub row_key: u64,
    pub byte_offset: usize,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DetailViewRow {
    pub anchor: DetailAnchor,
    pub kind: DetailRowKind,
    pub text: String,
}

#[derive(Clone, Debug)]
struct CachedDetailRow {
    row: DetailViewRow,
    next: Option<DetailAnchor>,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct DetailCacheKey {
    row_key: u64,
    byte_offset: usize,
    width: usize,
}

#[derive(Debug)]
pub struct DetailView {
    selected_entry: Option<TranscriptEntryId>,
    top: Option<DetailAnchor>,
    width: usize,
    height: usize,
    cache: HashMap<DetailCacheKey, CachedDetailRow>,
    order: VecDeque<DetailCacheKey>,
    logical_rows: HashMap<u64, Arc<str>>,
    predecessors: HashMap<(DetailAnchor, usize), DetailAnchor>,
    retained_bytes: usize,
}

impl Default for DetailView {
    fn default() -> Self {
        Self {
            selected_entry: None,
            top: None,
            width: 1,
            height: 1,
            cache: HashMap::new(),
            order: VecDeque::new(),
            logical_rows: HashMap::new(),
            predecessors: HashMap::new(),
            retained_bytes: 0,
        }
    }
}

impl DetailView {
    pub fn is_open(&self) -> bool {
        self.selected_entry.is_some()
    }

    pub fn selected_entry(&self) -> Option<TranscriptEntryId> {
        self.selected_entry
    }

    pub fn open(&mut self, entry_id: TranscriptEntryId, presentation: &ToolDetailPresentation) {
        self.selected_entry = Some(entry_id);
        self.top = presentation.rows.first().map(|row| DetailAnchor {
            row_key: row.key,
            byte_offset: 0,
        });
        self.clear_cache();
        for row in &presentation.rows {
            let rendered = Arc::<str>::from(format_structured_detail_row(row));
            self.retained_bytes = self.retained_bytes.saturating_add(rendered.len());
            self.logical_rows.insert(row.key, rendered);
        }
        debug_assert!(self.retained_bytes <= DETAIL_CACHE_MAX_BYTES);
    }

    pub fn close(&mut self) {
        self.selected_entry = None;
        self.top = None;
        self.clear_cache();
    }

    pub fn set_geometry(
        &mut self,
        presentation: &ToolDetailPresentation,
        width: usize,
        height: usize,
    ) {
        let width = width.max(1);
        if self.width != width {
            self.top = self
                .top
                .and_then(|anchor| normalize_anchor(presentation, anchor, width));
        }
        self.width = width;
        self.height = height.max(1);
    }

    pub fn visible_rows(&mut self, presentation: &ToolDetailPresentation) -> Vec<DetailViewRow> {
        let Some(mut current) = self.top.or_else(|| {
            presentation.rows.first().map(|row| DetailAnchor {
                row_key: row.key,
                byte_offset: 0,
            })
        }) else {
            return Vec::new();
        };
        self.top = Some(current);
        let mut visible = Vec::with_capacity(self.height);
        for _ in 0..self.height {
            let Some(cached) = self.row_at(presentation, current) else {
                break;
            };
            visible.push(cached.row.clone());
            let Some(next) = cached.next else {
                break;
            };
            current = next;
        }
        let mut after = current;
        for _ in 0..DETAIL_OVERSCAN_ROWS {
            let Some(cached) = self.row_at(presentation, after) else {
                break;
            };
            let Some(next) = cached.next else {
                break;
            };
            after = next;
        }
        visible
    }

    pub fn scroll_lines(&mut self, presentation: &ToolDetailPresentation, lines: i32) {
        if lines > 0 {
            for _ in 0..lines as usize {
                let Some(current) = self.top else {
                    return;
                };
                let Some(next) = self.row_at(presentation, current).and_then(|row| row.next) else {
                    return;
                };
                self.top = Some(next);
            }
        } else {
            for _ in 0..lines.unsigned_abs() as usize {
                let Some(current) = self.top else {
                    return;
                };
                let Some(previous) = self.previous_anchor(presentation, current) else {
                    return;
                };
                self.top = Some(previous);
            }
        }
    }

    pub fn page_up(&mut self, presentation: &ToolDetailPresentation) {
        self.scroll_lines(presentation, -(self.height.saturating_sub(1).max(1) as i32));
    }

    pub fn page_down(&mut self, presentation: &ToolDetailPresentation) {
        self.scroll_lines(presentation, self.height.saturating_sub(1).max(1) as i32);
    }

    pub fn home(&mut self, presentation: &ToolDetailPresentation) {
        self.top = presentation.rows.first().map(|row| DetailAnchor {
            row_key: row.key,
            byte_offset: 0,
        });
    }

    pub fn end(&mut self, presentation: &ToolDetailPresentation) {
        let Some(last) = presentation.rows.last() else {
            self.top = None;
            return;
        };
        let mut current = DetailAnchor {
            row_key: last.key,
            byte_offset: 0,
        };
        while let Some(next) = self.row_at(presentation, current).and_then(|row| row.next) {
            if next.row_key != last.key {
                break;
            }
            current = next;
        }
        let mut top = current;
        for _ in 1..self.height {
            let Some(previous) = self.previous_anchor(presentation, top) else {
                break;
            };
            top = previous;
        }
        self.top = Some(top);
    }

    fn row_at(
        &mut self,
        presentation: &ToolDetailPresentation,
        anchor: DetailAnchor,
    ) -> Option<CachedDetailRow> {
        let key = DetailCacheKey {
            row_key: anchor.row_key,
            byte_offset: anchor.byte_offset,
            width: self.width,
        };
        if let Some(cached) = self.cache.get(&key) {
            return Some(cached.clone());
        }
        let index = presentation
            .rows
            .iter()
            .position(|row| row.key == anchor.row_key)?;
        let logical = &presentation.rows[index];
        let source = self
            .logical_rows
            .get(&logical.key)
            .cloned()
            .unwrap_or_else(|| Arc::<str>::from(format_structured_detail_row(logical)));
        let mut start = anchor.byte_offset.min(source.len());
        while start > 0 && !source.is_char_boundary(start) {
            start -= 1;
        }
        let (text, end) = wrap_one_row(&source, start, self.width);
        let next = if end < source.len() {
            Some(DetailAnchor {
                row_key: anchor.row_key,
                byte_offset: end,
            })
        } else {
            presentation.rows.get(index + 1).map(|row| DetailAnchor {
                row_key: row.key,
                byte_offset: 0,
            })
        };
        let cached = CachedDetailRow {
            row: DetailViewRow {
                anchor: DetailAnchor {
                    row_key: anchor.row_key,
                    byte_offset: start,
                },
                kind: logical.kind,
                text,
            },
            next,
        };
        self.insert(key, cached.clone());
        Some(cached)
    }

    fn previous_anchor(
        &mut self,
        presentation: &ToolDetailPresentation,
        anchor: DetailAnchor,
    ) -> Option<DetailAnchor> {
        let index = presentation
            .rows
            .iter()
            .position(|row| row.key == anchor.row_key)?;
        if let Some(previous) = self.predecessors.get(&(anchor, self.width)).copied() {
            return Some(previous);
        }
        if anchor.byte_offset > 0 {
            return self.last_anchor_before(&presentation.rows[index], anchor.byte_offset);
        }
        let previous = presentation.rows.get(index.checked_sub(1)?)?;
        self.last_anchor(previous)
    }

    fn last_anchor_before(&mut self, row: &DetailRow, target: usize) -> Option<DetailAnchor> {
        let source = self
            .logical_rows
            .get(&row.key)
            .cloned()
            .unwrap_or_else(|| Arc::<str>::from(format_structured_detail_row(row)));
        let mut current = 0usize;
        let mut previous = None;
        while current < target.min(source.len()) {
            previous = Some(DetailAnchor {
                row_key: row.key,
                byte_offset: current,
            });
            let (_, next) = wrap_one_row(&source, current, self.width);
            if next <= current || next >= target {
                break;
            }
            current = next;
        }
        previous
    }

    fn last_anchor(&mut self, row: &DetailRow) -> Option<DetailAnchor> {
        let source = self
            .logical_rows
            .get(&row.key)
            .cloned()
            .unwrap_or_else(|| Arc::<str>::from(format_structured_detail_row(row)));
        let mut current = 0usize;
        let mut last = DetailAnchor {
            row_key: row.key,
            byte_offset: 0,
        };
        loop {
            let (_, next) = wrap_one_row(&source, current, self.width);
            if next >= source.len() || next <= current {
                return Some(last);
            }
            current = next;
            last.byte_offset = current;
        }
    }

    fn insert(&mut self, key: DetailCacheKey, row: CachedDetailRow) {
        if self.cache.contains_key(&key) {
            return;
        }
        self.retained_bytes = self
            .retained_bytes
            .saturating_add(row.row.text.len())
            .saturating_add(std::mem::size_of::<CachedDetailRow>());
        if let Some(next) = row.next {
            self.predecessors.insert((next, key.width), row.row.anchor);
        }
        self.order.push_back(key);
        self.cache.insert(key, row);
        while self.cache.len() > DETAIL_CACHE_MAX_ROWS
            || self.retained_bytes > DETAIL_CACHE_MAX_BYTES
        {
            let Some(oldest) = self.order.pop_front() else {
                break;
            };
            if let Some(removed) = self.cache.remove(&oldest) {
                if let Some(next) = removed.next {
                    let predecessor_key = (next, oldest.width);
                    if self.predecessors.get(&predecessor_key) == Some(&removed.row.anchor) {
                        self.predecessors.remove(&predecessor_key);
                    }
                }
                self.retained_bytes = self
                    .retained_bytes
                    .saturating_sub(removed.row.text.len())
                    .saturating_sub(std::mem::size_of::<CachedDetailRow>());
            }
        }
    }

    fn clear_cache(&mut self) {
        self.cache.clear();
        self.order.clear();
        self.logical_rows.clear();
        self.predecessors.clear();
        self.retained_bytes = 0;
    }
}

fn normalize_anchor(
    presentation: &ToolDetailPresentation,
    anchor: DetailAnchor,
    width: usize,
) -> Option<DetailAnchor> {
    let row = presentation
        .rows
        .iter()
        .find(|row| row.key == anchor.row_key)?;
    let source = format_structured_detail_row(row);
    let target = anchor.byte_offset.min(source.len());
    let mut current = 0usize;
    loop {
        let (_, next) = wrap_one_row(&source, current, width);
        if target <= next || next <= current || next >= source.len() {
            return Some(DetailAnchor {
                row_key: anchor.row_key,
                byte_offset: current,
            });
        }
        current = next;
    }
}

fn wrap_one_row(source: &str, start: usize, width: usize) -> (String, usize) {
    if start >= source.len() {
        return (String::new(), source.len());
    }
    let mut text = String::new();
    let mut column = 0usize;
    let mut end = source.len();
    for (offset, grapheme) in source[start..].grapheme_indices(true) {
        let absolute = start + offset;
        let safe = sanitize_grapheme(grapheme, column);
        let grapheme_width = UnicodeWidthStr::width(safe.as_str());
        if !text.is_empty() && column.saturating_add(grapheme_width) > width {
            end = absolute;
            break;
        }
        text.push_str(&safe);
        column = column.saturating_add(grapheme_width);
        if column >= width {
            end = absolute.saturating_add(grapheme.len());
            break;
        }
    }
    (text, end)
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::tool_detail::{DetailPresentationKind, DetailRow, ToolDetailPresentation};

    fn presentation() -> ToolDetailPresentation {
        ToolDetailPresentation {
            kind: DetailPresentationKind::DiffModify,
            title: "M file.txt".into(),
            summary: "+1 -1".into(),
            additions: 1,
            deletions: 1,
            rows: vec![
                DetailRow {
                    key: 0,
                    kind: DetailRowKind::Deletion,
                    text: "old value".into(),
                    old_line: Some(1),
                    new_line: None,
                    hidden_rows: 0,
                    hidden_bytes: 0,
                },
                DetailRow {
                    key: 1,
                    kind: DetailRowKind::Addition,
                    text: "new value".into(),
                    old_line: None,
                    new_line: Some(1),
                    hidden_rows: 0,
                    hidden_bytes: 0,
                },
            ],
            truncated: false,
        }
    }

    #[test]
    fn detail_view_wraps_and_preserves_semantic_anchor_across_resize() {
        let detail = presentation();
        let mut view = DetailView::default();
        view.open(TranscriptEntryId::from_raw(7), &detail);
        view.set_geometry(&detail, 6, 3);
        let first = view.visible_rows(&detail);
        assert_eq!(first[0].anchor.row_key, 0);
        view.scroll_lines(&detail, 1);
        let anchored = view.top.unwrap();
        assert_eq!(anchored.row_key, 0);
        assert!(anchored.byte_offset > 0);

        view.set_geometry(&detail, 12, 3);
        let resized = view.visible_rows(&detail);
        assert_eq!(resized[0].anchor.row_key, 0);
        assert!(resized[0].anchor.byte_offset <= anchored.byte_offset);
    }

    #[test]
    fn end_navigation_bottom_aligns_the_retained_detail() {
        let mut detail = presentation();
        detail.rows = (0..30)
            .map(|key| DetailRow {
                key,
                kind: DetailRowKind::ReadLine,
                text: format!("line {key}"),
                old_line: Some(key + 1),
                new_line: None,
                hidden_rows: 0,
                hidden_bytes: 0,
            })
            .collect();
        let mut view = DetailView::default();
        view.open(TranscriptEntryId::from_raw(2), &detail);
        view.set_geometry(&detail, 40, 5);
        view.end(&detail);
        let visible = view.visible_rows(&detail);
        assert_eq!(visible.len(), 5);
        assert_eq!(visible.last().unwrap().anchor.row_key, 29);
    }

    #[test]
    fn detail_cache_and_terminal_sanitization_are_bounded() {
        let mut detail = presentation();
        detail.rows[0].text = "\u{1b}]52;clipboard\u{7}".repeat(10_000);
        let mut view = DetailView::default();
        view.open(TranscriptEntryId::from_raw(1), &detail);
        view.set_geometry(&detail, 8, 20);
        for _ in 0..2_000 {
            let rows = view.visible_rows(&detail);
            assert!(rows.iter().all(|row| !row.text.contains('\u{1b}')));
            view.scroll_lines(&detail, 1);
        }
        assert!(view.cache.len() <= DETAIL_CACHE_MAX_ROWS);
        assert!(view.retained_bytes <= DETAIL_CACHE_MAX_BYTES);
    }
}
