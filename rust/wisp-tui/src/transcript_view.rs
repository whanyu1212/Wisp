//! Bounded plain-text projection and viewport navigation for transcripts.

use std::collections::{HashMap, VecDeque};

use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::transcript::{
    Transcript, TranscriptEntry, TranscriptEntryId, TranscriptEntryState, TranscriptRole,
};

const CACHE_MAX_ROWS: usize = 4_096;
const CACHE_MAX_BYTES: usize = 2 * 1024 * 1024;
const ENTRY_PRESENTATION_MAX_BYTES: usize = 64 * 1024;
const ENTRY_PRESENTATION_CHUNK_BYTES: usize = 4 * 1024;
const OVERSCAN_ROWS: usize = 8;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum RowPosition {
    Header,
    Omission,
    Content(usize),
    Spacer,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct RowAnchor {
    pub entry_id: TranscriptEntryId,
    pub position: RowPosition,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TranscriptRowKind {
    Header,
    Content,
    Placeholder,
    Omission,
    Spacer,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TranscriptRow {
    pub anchor: RowAnchor,
    pub role: TranscriptRole,
    pub kind: TranscriptRowKind,
    pub text: String,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct LayoutWork {
    pub bytes_scanned: usize,
    pub graphemes_scanned: usize,
    pub rows_built: usize,
    pub cache_hits: usize,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct RowKey {
    entry_id: TranscriptEntryId,
    layout_epoch: u64,
    presentation_start: usize,
    width: usize,
    position: RowPosition,
}

#[derive(Clone, Debug)]
struct CachedRow {
    row: TranscriptRow,
    next: Option<RowAnchor>,
    entry_revision: u64,
    content_len: usize,
}

struct ProjectedRow {
    row: TranscriptRow,
    next: Option<RowAnchor>,
}

#[derive(Debug, Default)]
pub struct TranscriptRowCache {
    rows: HashMap<RowKey, CachedRow>,
    predecessors: HashMap<RowKey, RowAnchor>,
    furthest_content: HashMap<(TranscriptEntryId, u64, usize, usize), RowAnchor>,
    insertion_order: VecDeque<RowKey>,
    retained_bytes: usize,
    work: LayoutWork,
}

impl TranscriptRowCache {
    #[cfg(test)]
    pub fn work(&self) -> LayoutWork {
        self.work
    }

    #[cfg(test)]
    pub fn reset_work(&mut self) {
        self.work = LayoutWork::default();
    }

    fn row_at(
        &mut self,
        transcript: &Transcript,
        anchor: RowAnchor,
        width: usize,
    ) -> Option<CachedRow> {
        let entry = transcript.entry(anchor.entry_id)?;
        let key = RowKey {
            entry_id: entry.id,
            layout_epoch: entry.layout_epoch(),
            presentation_start: presentation_start(entry),
            width: width.max(1),
            position: anchor.position,
        };
        if let Some(cached) = self.rows.get(&key).cloned() {
            if cached_row_valid(&cached, entry) {
                self.work.cache_hits = self.work.cache_hits.saturating_add(1);
                return Some(cached);
            }
            self.retained_bytes = self.retained_bytes.saturating_sub(cached.row.text.len());
            self.remove_indexes(key, &cached);
            self.rows.remove(&key);
            self.insertion_order.retain(|candidate| candidate != &key);
        }

        let cached = self.build_row(transcript, entry, anchor, width.max(1))?;
        self.work.rows_built = self.work.rows_built.saturating_add(1);
        self.insert(key, cached.clone());
        Some(cached)
    }

    fn build_row(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        anchor: RowAnchor,
        width: usize,
    ) -> Option<CachedRow> {
        let next_entry = || {
            transcript.entry_after(entry.id).map(|next| RowAnchor {
                entry_id: next.id,
                position: RowPosition::Header,
            })
        };
        let projected = match anchor.position {
            RowPosition::Header => ProjectedRow {
                row: TranscriptRow {
                    anchor,
                    role: entry.role,
                    kind: TranscriptRowKind::Header,
                    text: match entry.role {
                        TranscriptRole::User => "you",
                        TranscriptRole::Assistant => "wisp",
                    }
                    .into(),
                },
                next: Some(RowAnchor {
                    entry_id: entry.id,
                    position: if presentation_start(entry) > 0 {
                        RowPosition::Omission
                    } else {
                        RowPosition::Content(0)
                    },
                }),
            },
            RowPosition::Omission if presentation_start(entry) > 0 => {
                let omitted = presentation_start(entry);
                ProjectedRow {
                    row: TranscriptRow {
                        anchor,
                        role: entry.role,
                        kind: TranscriptRowKind::Omission,
                        text: format!("… {omitted} earlier bytes omitted …"),
                    },
                    next: Some(RowAnchor {
                        entry_id: entry.id,
                        position: RowPosition::Content(omitted),
                    }),
                }
            }
            RowPosition::Content(start)
                if start <= entry.content.len() && entry.content.is_char_boundary(start) =>
            {
                self.build_content_row(transcript, entry, anchor, start, width)
            }
            RowPosition::Spacer if next_entry().is_some() => ProjectedRow {
                row: TranscriptRow {
                    anchor,
                    role: entry.role,
                    kind: TranscriptRowKind::Spacer,
                    text: String::new(),
                },
                next: next_entry(),
            },
            _ => return None,
        };
        Some(CachedRow {
            row: projected.row,
            next: projected.next,
            entry_revision: entry.revision(),
            content_len: entry.content.len(),
        })
    }

    fn build_content_row(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        anchor: RowAnchor,
        start: usize,
        width: usize,
    ) -> ProjectedRow {
        if entry.content.is_empty() {
            let pending = entry.state != TranscriptEntryState::Complete;
            return ProjectedRow {
                row: TranscriptRow {
                    anchor,
                    role: entry.role,
                    kind: if pending {
                        TranscriptRowKind::Placeholder
                    } else {
                        TranscriptRowKind::Content
                    },
                    text: if pending {
                        "working…".into()
                    } else {
                        String::new()
                    },
                },
                next: separator_after(transcript, entry),
            };
        }
        if start == entry.content.len() {
            return ProjectedRow {
                row: TranscriptRow {
                    anchor,
                    role: entry.role,
                    kind: TranscriptRowKind::Content,
                    text: String::new(),
                },
                next: separator_after(transcript, entry),
            };
        }

        let mut text = String::new();
        let mut column = 0_usize;
        let mut next_offset = None;
        let mut ended_with_break = false;
        for (relative_offset, grapheme) in entry.content[start..].grapheme_indices(true) {
            self.work.graphemes_scanned = self.work.graphemes_scanned.saturating_add(1);
            self.work.bytes_scanned = self.work.bytes_scanned.saturating_add(grapheme.len());
            let absolute_offset = start + relative_offset;
            if is_line_break(grapheme) {
                next_offset = Some(absolute_offset + grapheme.len());
                ended_with_break = true;
                break;
            }
            let safe = sanitize_grapheme(grapheme, column);
            let grapheme_width = UnicodeWidthStr::width(safe.as_str());
            if !text.is_empty() && column.saturating_add(grapheme_width) > width {
                next_offset = Some(absolute_offset);
                break;
            }
            text.push_str(&safe);
            column = column.saturating_add(grapheme_width);
            if column >= width {
                next_offset = Some(absolute_offset + grapheme.len());
                break;
            }
        }

        let next = match next_offset {
            Some(offset) if offset < entry.content.len() => Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Content(offset),
            }),
            Some(offset) if ended_with_break && offset == entry.content.len() => Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Content(offset),
            }),
            _ => separator_after(transcript, entry),
        };
        ProjectedRow {
            row: TranscriptRow {
                anchor,
                role: entry.role,
                kind: TranscriptRowKind::Content,
                text,
            },
            next,
        }
    }

    fn previous_anchor(
        &mut self,
        transcript: &Transcript,
        anchor: RowAnchor,
        width: usize,
    ) -> Option<RowAnchor> {
        let entry = transcript.entry(anchor.entry_id)?;
        match anchor.position {
            RowPosition::Header => transcript.entry_before(entry.id).map(|previous| RowAnchor {
                entry_id: previous.id,
                position: RowPosition::Spacer,
            }),
            RowPosition::Omission => Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Header,
            }),
            RowPosition::Content(offset) => {
                let start = presentation_start(entry);
                if offset <= start {
                    return Some(RowAnchor {
                        entry_id: entry.id,
                        position: if start > 0 {
                            RowPosition::Omission
                        } else {
                            RowPosition::Header
                        },
                    });
                }
                self.content_anchor_before(transcript, entry, offset, width)
            }
            RowPosition::Spacer => Some(self.last_anchor(transcript, entry, width)),
        }
    }

    fn content_anchor_before(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        target: usize,
        width: usize,
    ) -> Option<RowAnchor> {
        let presentation_start = presentation_start(entry);
        let width = width.max(1);
        let target_key = RowKey {
            entry_id: entry.id,
            layout_epoch: entry.layout_epoch(),
            presentation_start,
            width,
            position: RowPosition::Content(target),
        };
        if let Some(previous) = self.predecessors.get(&target_key).copied() {
            return Some(previous);
        }
        let furthest_key = (entry.id, entry.layout_epoch(), presentation_start, width);
        let candidate_start = self
            .furthest_content
            .get(&furthest_key)
            .and_then(|anchor| match anchor.position {
                RowPosition::Content(offset) if (presentation_start..target).contains(&offset) => {
                    Some(offset)
                }
                _ => None,
            })
            .unwrap_or(presentation_start);
        let mut anchor = RowAnchor {
            entry_id: entry.id,
            position: RowPosition::Content(candidate_start),
        };
        let mut previous = anchor;
        loop {
            let cached = self.row_at(transcript, anchor, width)?;
            let Some(next) = cached.next else {
                return Some(previous);
            };
            match next.position {
                RowPosition::Content(offset) if offset < target => {
                    previous = next;
                    anchor = next;
                }
                RowPosition::Content(offset) if offset == target => return Some(anchor),
                RowPosition::Content(_) | RowPosition::Spacer => return Some(anchor),
                _ => return Some(previous),
            }
        }
    }

    fn last_anchor(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        width: usize,
    ) -> RowAnchor {
        if entry.content.is_empty() || ends_with_line_break(&entry.content) {
            return RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Content(entry.content.len()),
            };
        }
        self.content_anchor_before(transcript, entry, entry.content.len(), width)
            .unwrap_or(RowAnchor {
                entry_id: entry.id,
                position: if presentation_start(entry) > 0 {
                    RowPosition::Omission
                } else {
                    RowPosition::Content(0)
                },
            })
    }

    fn insert(&mut self, key: RowKey, value: CachedRow) {
        if self.rows.contains_key(&key) {
            return;
        }
        self.retained_bytes = self.retained_bytes.saturating_add(value.row.text.len());
        if let RowPosition::Content(offset) = value.row.anchor.position {
            let furthest_key = (
                key.entry_id,
                key.layout_epoch,
                key.presentation_start,
                key.width,
            );
            let should_replace = self.furthest_content.get(&furthest_key).is_none_or(|anchor| {
                matches!(anchor.position, RowPosition::Content(current) if offset > current)
            });
            if should_replace {
                self.furthest_content.insert(furthest_key, value.row.anchor);
            }
        }
        if let Some(next) = value.next.filter(|next| next.entry_id == key.entry_id) {
            self.predecessors.insert(
                RowKey {
                    position: next.position,
                    ..key
                },
                value.row.anchor,
            );
        }
        self.insertion_order.push_back(key);
        self.rows.insert(key, value);
        while self.rows.len() > CACHE_MAX_ROWS || self.retained_bytes > CACHE_MAX_BYTES {
            let Some(oldest) = self.insertion_order.pop_front() else {
                break;
            };
            if let Some(removed) = self.rows.remove(&oldest) {
                self.retained_bytes = self.retained_bytes.saturating_sub(removed.row.text.len());
                self.remove_indexes(oldest, &removed);
            }
        }
    }

    fn remove_indexes(&mut self, key: RowKey, cached: &CachedRow) {
        if let Some(next) = cached.next.filter(|next| next.entry_id == key.entry_id) {
            let predecessor_key = RowKey {
                position: next.position,
                ..key
            };
            if self.predecessors.get(&predecessor_key) == Some(&cached.row.anchor) {
                self.predecessors.remove(&predecessor_key);
            }
        }
        let furthest_key = (
            key.entry_id,
            key.layout_epoch,
            key.presentation_start,
            key.width,
        );
        if self.furthest_content.get(&furthest_key) == Some(&cached.row.anchor) {
            self.furthest_content.remove(&furthest_key);
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TranscriptViewAction {
    ScrollLines(i32),
    PageUp,
    PageDown,
    FollowTail,
    OutputChanged,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TranscriptViewport {
    top: Option<RowAnchor>,
    follow_tail: bool,
    unseen_output: bool,
    width: usize,
    height: usize,
}

impl Default for TranscriptViewport {
    fn default() -> Self {
        Self {
            top: None,
            follow_tail: true,
            unseen_output: false,
            width: 1,
            height: 1,
        }
    }
}

impl TranscriptViewport {
    pub fn follows_tail(&self) -> bool {
        self.follow_tail
    }

    pub fn has_unseen_output(&self) -> bool {
        self.unseen_output
    }

    pub fn set_geometry(
        &mut self,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
        width: usize,
        height: usize,
    ) {
        let width = width.max(1);
        let height = height.max(1);
        if self.width == width && self.height == height {
            return;
        }
        self.width = width;
        self.height = height;
        if !self.follow_tail {
            self.top = self
                .top
                .and_then(|anchor| normalize_anchor(transcript, cache, anchor, self.width));
            if self.top.is_none() {
                self.follow_tail = true;
            }
        }
    }

    pub fn reduce(
        &mut self,
        action: TranscriptViewAction,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
    ) {
        match action {
            TranscriptViewAction::OutputChanged => {
                if !self.follow_tail {
                    self.top = self
                        .top
                        .and_then(|anchor| normalize_anchor(transcript, cache, anchor, self.width));
                    if self.top.is_none() {
                        self.follow_tail = true;
                    } else {
                        self.unseen_output = true;
                    }
                }
            }
            TranscriptViewAction::FollowTail => {
                self.follow_tail = true;
                self.unseen_output = false;
                self.top = None;
            }
            TranscriptViewAction::PageUp => {
                let amount = self.height.saturating_sub(1).max(1);
                self.scroll_up(transcript, cache, amount);
            }
            TranscriptViewAction::PageDown => {
                let amount = self.height.saturating_sub(1).max(1);
                self.scroll_down(transcript, cache, amount);
            }
            TranscriptViewAction::ScrollLines(lines) if lines < 0 => {
                self.scroll_up(transcript, cache, lines.unsigned_abs() as usize);
            }
            TranscriptViewAction::ScrollLines(lines) if lines > 0 => {
                self.scroll_down(transcript, cache, lines as usize);
            }
            TranscriptViewAction::ScrollLines(_) => {}
        }
    }

    pub fn visible_rows(
        &mut self,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
    ) -> Vec<TranscriptRow> {
        if transcript.entries().is_empty() {
            self.top = None;
            return Vec::new();
        }
        let top = if self.follow_tail || self.top.is_none() {
            let top = self.tail_top(transcript, cache);
            self.top = top;
            top
        } else {
            self.top
        };
        let Some(top) = top else {
            return Vec::new();
        };
        let rows = collect_rows(transcript, cache, top, self.width, self.height);
        self.populate_overscan(transcript, cache, top, rows.last().map(|row| row.anchor));
        rows
    }

    fn scroll_up(
        &mut self,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
        amount: usize,
    ) {
        let _ = self.visible_rows(transcript, cache);
        let Some(mut top) = self.top else {
            return;
        };
        let mut moved = false;
        for _ in 0..amount {
            let Some(previous) = cache.previous_anchor(transcript, top, self.width) else {
                break;
            };
            top = previous;
            moved = true;
        }
        if moved {
            self.top = Some(top);
            self.follow_tail = false;
        }
    }

    fn scroll_down(
        &mut self,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
        amount: usize,
    ) {
        if self.follow_tail {
            return;
        }
        let Some(mut top) = self.top else {
            self.follow_tail = true;
            self.unseen_output = false;
            return;
        };
        for _ in 0..amount {
            let Some(cached) = cache.row_at(transcript, top, self.width) else {
                break;
            };
            let Some(next) = cached.next else {
                self.follow_tail = true;
                self.unseen_output = false;
                self.top = None;
                return;
            };
            top = next;
        }
        let remaining = collect_rows(transcript, cache, top, self.width, self.height);
        let reaches_tail = remaining.last().is_some_and(|row| {
            cache
                .row_at(transcript, row.anchor, self.width)
                .is_some_and(|cached| cached.next.is_none())
        });
        if remaining.len() < self.height || reaches_tail {
            self.follow_tail = true;
            self.unseen_output = false;
            self.top = None;
        } else {
            self.top = Some(top);
        }
    }

    fn tail_top(
        &self,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
    ) -> Option<RowAnchor> {
        let entry = transcript.entries().last()?;
        let mut top = cache.last_anchor(transcript, entry, self.width);
        for _ in 1..self.height {
            let Some(previous) = cache.previous_anchor(transcript, top, self.width) else {
                break;
            };
            top = previous;
        }
        Some(top)
    }

    fn populate_overscan(
        &self,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
        top: RowAnchor,
        last_visible: Option<RowAnchor>,
    ) {
        let mut before = top;
        for _ in 0..OVERSCAN_ROWS {
            let Some(previous) = cache.previous_anchor(transcript, before, self.width) else {
                break;
            };
            let _ = cache.row_at(transcript, previous, self.width);
            before = previous;
        }
        let Some(mut after) = last_visible else {
            return;
        };
        for _ in 0..OVERSCAN_ROWS {
            let Some(cached) = cache.row_at(transcript, after, self.width) else {
                break;
            };
            let Some(next) = cached.next else {
                break;
            };
            let _ = cache.row_at(transcript, next, self.width);
            after = next;
        }
    }
}

fn cached_row_valid(cached: &CachedRow, entry: &TranscriptEntry) -> bool {
    if cached.entry_revision == entry.revision() {
        return true;
    }
    match cached.row.kind {
        TranscriptRowKind::Header => {
            let expected = if presentation_start(entry) > 0 {
                RowPosition::Omission
            } else {
                RowPosition::Content(0)
            };
            cached
                .next
                .is_some_and(|next| next.entry_id == entry.id && next.position == expected)
        }
        TranscriptRowKind::Spacer => true,
        TranscriptRowKind::Content => matches!(
            cached.next,
            Some(RowAnchor {
                entry_id,
                position: RowPosition::Content(offset),
            }) if entry_id == entry.id
                && offset <= cached.content_len
                && entry.content.len() >= cached.content_len
        ),
        TranscriptRowKind::Placeholder | TranscriptRowKind::Omission => false,
    }
}

fn collect_rows(
    transcript: &Transcript,
    cache: &mut TranscriptRowCache,
    top: RowAnchor,
    width: usize,
    height: usize,
) -> Vec<TranscriptRow> {
    let mut rows = Vec::with_capacity(height);
    let mut anchor = Some(top);
    while rows.len() < height {
        let Some(current) = anchor else {
            break;
        };
        let Some(cached) = cache.row_at(transcript, current, width) else {
            break;
        };
        anchor = cached.next;
        rows.push(cached.row);
    }
    rows
}

fn normalize_anchor(
    transcript: &Transcript,
    cache: &mut TranscriptRowCache,
    anchor: RowAnchor,
    width: usize,
) -> Option<RowAnchor> {
    let entry = transcript.entry(anchor.entry_id)?;
    match anchor.position {
        RowPosition::Content(offset) => {
            let presentation_start = presentation_start(entry);
            let mut clamped = offset.clamp(presentation_start, entry.content.len());
            while clamped > presentation_start && !entry.content.is_char_boundary(clamped) {
                clamped -= 1;
            }
            let target = entry.content[clamped..]
                .chars()
                .next()
                .map_or(clamped, |character| clamped + character.len_utf8());
            cache.content_anchor_before(transcript, entry, target, width)
        }
        _ => Some(anchor),
    }
}

fn ends_with_line_break(content: &str) -> bool {
    content.ends_with('\n') || content.ends_with('\r')
}

fn presentation_start(entry: &TranscriptEntry) -> usize {
    if entry.content.len() <= ENTRY_PRESENTATION_MAX_BYTES {
        return 0;
    }
    let overflow = entry.content.len() - ENTRY_PRESENTATION_MAX_BYTES;
    let mut start = overflow - (overflow % ENTRY_PRESENTATION_CHUNK_BYTES);
    while start < entry.content.len() && !entry.content.is_char_boundary(start) {
        start += 1;
    }
    start
}

fn separator_after(transcript: &Transcript, entry: &TranscriptEntry) -> Option<RowAnchor> {
    transcript.entry_after(entry.id).map(|_| RowAnchor {
        entry_id: entry.id,
        position: RowPosition::Spacer,
    })
}

fn is_line_break(grapheme: &str) -> bool {
    matches!(grapheme, "\n" | "\r\n" | "\r")
}

fn sanitize_grapheme(grapheme: &str, column: usize) -> String {
    if grapheme == "\t" {
        return " ".repeat(4 - (column % 4));
    }
    grapheme
        .chars()
        .map(|character| {
            if terminal_control_character(character) {
                '\u{fffd}'
            } else {
                character
            }
        })
        .collect()
}

fn terminal_control_character(character: char) -> bool {
    character.is_control() || crate::is_bidi_control(character)
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fmt::Write as _;

    fn numbered_lines(prefix: &str, count: usize) -> String {
        let mut content = String::new();
        for line in 0..count {
            writeln!(content, "{prefix}-{line}").unwrap();
        }
        content
    }

    fn transcript_with(lines: usize) -> Transcript {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.start_message(1);
        transcript.complete_message(1, numbered_lines("line", lines));
        transcript
    }

    #[test]
    fn follows_tail_then_preserves_anchor_and_marks_unseen_output() {
        let mut transcript = transcript_with(40);
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 20, 6);
        let tail = viewport.visible_rows(&transcript, &mut cache);
        assert!(tail.iter().any(|row| row.text == "line-39"));

        viewport.reduce(TranscriptViewAction::PageUp, &transcript, &mut cache);
        let anchored = viewport.visible_rows(&transcript, &mut cache);
        assert!(!viewport.follows_tail());

        transcript.append_message_delta(2, "new output");
        viewport.reduce(TranscriptViewAction::OutputChanged, &transcript, &mut cache);
        assert_eq!(viewport.visible_rows(&transcript, &mut cache), anchored);
        assert!(viewport.has_unseen_output());

        viewport.reduce(TranscriptViewAction::FollowTail, &transcript, &mut cache);
        assert!(viewport.follows_tail());
        assert!(!viewport.has_unseen_output());
    }

    #[test]
    fn page_down_to_the_bottom_restores_follow_tail() {
        let transcript = transcript_with(40);
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 20, 6);
        viewport.reduce(TranscriptViewAction::PageUp, &transcript, &mut cache);
        assert!(!viewport.follows_tail());

        for _ in 0..20 {
            viewport.reduce(TranscriptViewAction::PageDown, &transcript, &mut cache);
            if viewport.follows_tail() {
                break;
            }
        }

        assert!(viewport.follows_tail());
        assert!(!viewport.has_unseen_output());
    }

    #[test]
    fn resize_keeps_the_anchored_entry_and_content_offset() {
        let transcript = transcript_with(30);
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 20, 5);
        viewport.reduce(TranscriptViewAction::PageUp, &transcript, &mut cache);
        let before = viewport.top.unwrap();

        viewport.set_geometry(&transcript, &mut cache, 9, 5);
        let after = viewport.top.unwrap();

        assert_eq!(after.entry_id, before.entry_id);
        match (before.position, after.position) {
            (RowPosition::Content(before), RowPosition::Content(after)) => {
                assert!(after <= before);
            }
            _ => assert_eq!(after.position, before.position),
        }
    }

    #[test]
    fn anchored_viewport_survives_authoritative_replacement() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.start_message(1);
        transcript.append_message_delta(1, &numbered_lines("draft", 30));
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 20, 5);
        viewport.reduce(TranscriptViewAction::PageUp, &transcript, &mut cache);
        assert!(!viewport.follows_tail());

        transcript.complete_message(1, "ééé final".into());
        viewport.reduce(TranscriptViewAction::OutputChanged, &transcript, &mut cache);
        let rows = viewport.visible_rows(&transcript, &mut cache);

        assert!(!rows.is_empty());
        assert!(rows.iter().any(|row| row.text == "ééé final"));
        assert!(viewport.has_unseen_output());
    }

    #[test]
    fn repeated_render_reuses_cached_rows() {
        let transcript = transcript_with(10_000);
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 12);
        let first = viewport.visible_rows(&transcript, &mut cache);
        cache.reset_work();
        let second = viewport.visible_rows(&transcript, &mut cache);
        let work = cache.work();

        assert_eq!(second, first);
        assert_eq!(work.rows_built, 0);
        assert!(work.cache_hits >= second.len());
    }

    #[test]
    fn row_cache_enforces_row_and_byte_caps() {
        let mut transcript = Transcript::default();
        for index in 0..5_000 {
            transcript.append_exchange(format!("{index}:{}", "x".repeat(1_024)));
        }
        let mut cache = TranscriptRowCache::default();
        for entry in transcript.entries() {
            let _ = cache.row_at(
                &transcript,
                RowAnchor {
                    entry_id: entry.id,
                    position: RowPosition::Header,
                },
                2_048,
            );
            let _ = cache.row_at(
                &transcript,
                RowAnchor {
                    entry_id: entry.id,
                    position: RowPosition::Content(0),
                },
                2_048,
            );
        }

        assert!(cache.rows.len() <= CACHE_MAX_ROWS);
        assert!(cache.predecessors.len() <= CACHE_MAX_ROWS);
        assert!(cache.furthest_content.len() <= CACHE_MAX_ROWS);
        assert!(cache.retained_bytes <= CACHE_MAX_BYTES);
    }

    #[test]
    fn streaming_append_reuses_the_stable_wrapped_prefix() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.start_message(1);
        transcript.append_message_delta(1, &numbered_lines("stable", 10_000));
        transcript.append_message_delta(1, "growing");
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 12);
        let _ = viewport.visible_rows(&transcript, &mut cache);
        cache.reset_work();

        transcript.append_message_delta(1, " tail");
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let work = cache.work();

        assert!(rows.iter().any(|row| row.text == "growing tail"));
        assert!(work.rows_built <= 2, "stable rows were rebuilt: {work:?}");
        assert!(
            work.bytes_scanned <= 80 * (12 + OVERSCAN_ROWS),
            "work exceeded the visible window and overscan: {work:?}"
        );
    }

    #[test]
    fn appending_an_entry_invalidates_the_previous_tail_link() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("first".into());
        transcript.complete_message(1, "answer".into());
        let first_assistant = transcript.latest_assistant_entry().unwrap().id;
        let mut cache = TranscriptRowCache::default();
        let tail = cache.last_anchor(&transcript, transcript.entry(first_assistant).unwrap(), 80);
        assert!(cache.row_at(&transcript, tail, 80).unwrap().next.is_none());

        transcript.append_exchange("second".into());

        assert!(matches!(
            cache.row_at(&transcript, tail, 80).unwrap().next,
            Some(RowAnchor {
                position: RowPosition::Spacer,
                ..
            })
        ));
    }

    #[test]
    fn crossing_the_presentation_limit_invalidates_the_cached_header_link() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        let assistant = transcript.start_message(1);
        transcript.append_message_delta(1, &"x".repeat(ENTRY_PRESENTATION_MAX_BYTES - 1));
        let mut cache = TranscriptRowCache::default();
        let header = RowAnchor {
            entry_id: assistant,
            position: RowPosition::Header,
        };
        assert!(matches!(
            cache.row_at(&transcript, header, 80).unwrap().next,
            Some(RowAnchor {
                position: RowPosition::Content(0),
                ..
            })
        ));

        transcript.append_message_delta(1, &"x".repeat(ENTRY_PRESENTATION_CHUNK_BYTES + 1));

        assert!(matches!(
            cache.row_at(&transcript, header, 80).unwrap().next,
            Some(RowAnchor {
                position: RowPosition::Omission,
                ..
            })
        ));
    }

    #[test]
    fn authoritative_completion_invalidates_cached_content() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.append_message_delta(1, "streaming draft");
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 6);
        assert!(
            viewport
                .visible_rows(&transcript, &mut cache)
                .iter()
                .any(|row| row.text == "streaming draft")
        );

        transcript.complete_message(1, "authoritative answer".into());
        let rows = viewport.visible_rows(&transcript, &mut cache);

        assert!(rows.iter().any(|row| row.text == "authoritative answer"));
        assert!(rows.iter().all(|row| row.text != "streaming draft"));
    }

    #[test]
    fn presentation_of_huge_content_is_bounded_and_safe() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.complete_message(
            1,
            format!("HEAD{}\u{1b}]0;owned\u{7}TAIL", "x".repeat(1024 * 1024)),
        );
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 8);
        let rows = viewport.visible_rows(&transcript, &mut cache);

        assert!(rows.iter().any(|row| row.text.contains("TAIL")));
        assert!(rows.iter().all(|row| {
            row.text
                .chars()
                .all(|character| !terminal_control_character(character))
        }));
        let work = cache.work();
        assert!(
            work.bytes_scanned <= ENTRY_PRESENTATION_MAX_BYTES + ENTRY_PRESENTATION_CHUNK_BYTES,
            "unbroken content exceeded the presentation budget: {work:?}"
        );
    }

    #[test]
    fn tail_projection_preserves_forward_wrap_boundaries() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.complete_message(1, "abc界d".into());
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 4, 1);

        let rows = viewport.visible_rows(&transcript, &mut cache);

        assert_eq!(rows.len(), 1);
        assert_eq!(rows[0].text, "界d");

        transcript.complete_message(2, "abc\td".into());
        let tab_rows = viewport.visible_rows(&transcript, &mut cache);
        assert_eq!(tab_rows.len(), 1);
        assert_eq!(tab_rows[0].text, "d");
    }

    #[test]
    fn tabs_and_wide_graphemes_wrap_by_terminal_width() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("a\tb👨‍👩‍👧‍👦c".into());
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 4, 8);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let content = rows
            .iter()
            .filter(|row| row.kind == TranscriptRowKind::Content)
            .map(|row| row.text.as_str())
            .collect::<Vec<_>>();

        assert!(content.contains(&"a   "));
        assert!(content.iter().all(|row| UnicodeWidthStr::width(*row) <= 4));
    }
}
