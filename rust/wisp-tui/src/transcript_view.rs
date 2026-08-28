//! Bounded styled projection and viewport navigation for transcripts.

use std::collections::{HashMap, VecDeque, hash_map::Entry};
use std::sync::Arc;

use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::markdown::{
    IncrementalMarkdownState, MarkdownDocument, MarkdownWork, SourceAffinity, TranscriptSpan,
    TranscriptSpanStyle,
};
use crate::tool_cards::{ProcessDisplayState, ToolStatus};
use crate::transcript::{
    Transcript, TranscriptEntry, TranscriptEntryId, TranscriptEntryKind, TranscriptEntryState,
    TranscriptRole,
};

const CACHE_MAX_ROWS: usize = 4_096;
const CACHE_MAX_BYTES: usize = 2 * 1024 * 1024;
const MARKDOWN_CACHE_MAX_ENTRIES: usize = 128;
const MARKDOWN_CACHE_MAX_BYTES: usize = 2 * 1024 * 1024;
const CARD_CACHE_MAX_ENTRIES: usize = 128;
const CARD_CACHE_MAX_BYTES: usize = 1024 * 1024;
const ENTRY_PRESENTATION_MAX_BYTES: usize = 64 * 1024;
const ENTRY_PRESENTATION_CHUNK_BYTES: usize = 4 * 1024;
const OVERSCAN_ROWS: usize = 8;

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum RowPosition {
    Header,
    Omission,
    Content(usize),
    Markdown(MarkdownPosition),
    Card(CardPosition),
    Spacer,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum CardSection {
    Action,
    Detail,
    Omission,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct CardPosition {
    pub section: CardSection,
    pub absolute_byte_offset: usize,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub struct MarkdownPosition {
    pub source_offset: usize,
    pub output_offset: usize,
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
    CardAction,
    CardDetail,
    CardOmission,
    Spacer,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TranscriptRowTone {
    Default,
    User,
    Assistant,
    Muted,
    Pending,
    Success,
    Warning,
    Error,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TranscriptRow {
    pub anchor: RowAnchor,
    pub role: TranscriptRole,
    pub kind: TranscriptRowKind,
    pub tone: TranscriptRowTone,
    pub spans: Vec<TranscriptSpan>,
}

impl TranscriptRow {
    #[cfg(test)]
    pub fn plain_text(&self) -> String {
        self.spans.iter().map(|span| span.text.as_str()).collect()
    }

    fn retained_bytes(&self) -> usize {
        self.spans
            .iter()
            .map(|span| span.text.len())
            .sum::<usize>()
            .saturating_add(
                self.spans
                    .len()
                    .saturating_mul(std::mem::size_of::<TranscriptSpan>()),
            )
    }

    fn plain(
        anchor: RowAnchor,
        role: TranscriptRole,
        kind: TranscriptRowKind,
        text: String,
        source_offset: usize,
    ) -> Self {
        let spans = if text.is_empty() {
            Vec::new()
        } else {
            let source_end = source_offset.saturating_add(text.len());
            vec![TranscriptSpan {
                text,
                style: TranscriptSpanStyle::default(),
                affinity: SourceAffinity {
                    source_offset,
                    source_end,
                    output_offset: 0,
                },
            }]
        };
        Self {
            anchor,
            role,
            kind,
            tone: default_row_tone(role, kind),
            spans,
        }
    }
}

fn default_row_tone(role: TranscriptRole, kind: TranscriptRowKind) -> TranscriptRowTone {
    match kind {
        TranscriptRowKind::Placeholder | TranscriptRowKind::Omission => TranscriptRowTone::Muted,
        TranscriptRowKind::Header if role == TranscriptRole::User => TranscriptRowTone::User,
        TranscriptRowKind::Header if role == TranscriptRole::Assistant => {
            TranscriptRowTone::Assistant
        }
        TranscriptRowKind::Content if role == TranscriptRole::Assistant => {
            TranscriptRowTone::Assistant
        }
        _ => TranscriptRowTone::Default,
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct LayoutWork {
    pub bytes_scanned: usize,
    pub graphemes_scanned: usize,
    pub rows_built: usize,
    pub cache_hits: usize,
    pub markdown_source_bytes_parsed: usize,
    pub markdown_source_bytes_reused: usize,
    pub markdown_blocks_built: usize,
    pub markdown_full_reparses: usize,
    pub markdown_incremental_builds: usize,
    pub markdown_fragments_emitted: usize,
    pub syntax_fences_considered: usize,
    pub syntax_fences_highlighted: usize,
    pub syntax_fallbacks: usize,
    pub syntax_source_bytes: usize,
    pub syntax_lines: usize,
    pub syntax_fragments: usize,
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
    markdown_presentation_identity: Option<u64>,
}

struct ProjectedRow {
    row: TranscriptRow,
    next: Option<RowAnchor>,
}

#[derive(Debug)]
struct CardProjection {
    action: String,
    detail: String,
    detail_base: usize,
    omission: Option<String>,
    omission_before_detail: bool,
    action_tone: TranscriptRowTone,
}

impl CardProjection {
    fn retained_bytes(&self) -> usize {
        self.action
            .len()
            .saturating_add(self.detail.len())
            .saturating_add(self.omission.as_ref().map_or(0, String::len))
            .saturating_add(std::mem::size_of::<Self>())
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct MarkdownKey {
    entry_id: TranscriptEntryId,
    layout_epoch: u64,
    presentation_start: usize,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct CardKey {
    entry_id: TranscriptEntryId,
    layout_epoch: u64,
}

#[derive(Clone, Debug, Default)]
struct MarkdownSnapshot {
    document: MarkdownDocument,
    block_starts: Vec<usize>,
    output_len: usize,
    stable_output_len: usize,
    presentation_identity: u64,
}

impl MarkdownSnapshot {
    fn new(document: MarkdownDocument, stable_blocks: usize, presentation_identity: u64) -> Self {
        let mut block_starts = Vec::with_capacity(document.blocks.len());
        let mut output_len = 0_usize;
        for (index, block) in document.blocks.iter().enumerate() {
            if index > 0 {
                output_len = output_len.saturating_add(1);
            }
            block_starts.push(output_len);
            output_len = output_len.saturating_add(
                block
                    .spans
                    .iter()
                    .map(|span| span.text.len())
                    .sum::<usize>(),
            );
        }
        let stable_output_len = if stable_blocks >= document.blocks.len() {
            output_len
        } else {
            block_starts.get(stable_blocks).copied().unwrap_or(0)
        };
        Self {
            document,
            block_starts,
            output_len,
            stable_output_len,
            presentation_identity,
        }
    }

    fn retained_bytes(&self) -> usize {
        self.document.retained_bytes()
    }

    fn ends_with_line_break(&self) -> bool {
        self.document
            .blocks
            .last()
            .and_then(|block| block.spans.last())
            .is_some_and(|span| ends_with_line_break(&span.text))
    }

    fn position_at(&self, offset: usize) -> MarkdownPosition {
        let output_offset = self.normalize_offset(offset);
        if output_offset == self.output_len {
            let source_offset = self
                .document
                .blocks
                .last()
                .map_or(0, |block| block.source.end);
            return MarkdownPosition {
                source_offset,
                output_offset,
            };
        }
        let block_index = self
            .block_starts
            .partition_point(|block_start| *block_start <= output_offset)
            .saturating_sub(1);
        let block = &self.document.blocks[block_index];
        let block_start = self.block_starts[block_index];
        let local = output_offset.saturating_sub(block_start);
        let mut span_start = 0_usize;
        for span in &block.spans {
            let span_end = span_start + span.text.len();
            if local < span_end {
                let output_within_span = local - span_start;
                let source_len = span
                    .affinity
                    .source_end
                    .saturating_sub(span.affinity.source_offset);
                return MarkdownPosition {
                    source_offset: span
                        .affinity
                        .source_offset
                        .saturating_add(output_within_span.min(source_len)),
                    output_offset,
                };
            }
            span_start = span_end;
        }
        MarkdownPosition {
            source_offset: block.source.end,
            output_offset,
        }
    }

    fn output_for_source(&self, source_offset: usize, fallback: usize) -> usize {
        let mut best = None::<(usize, usize)>;
        for (block_index, block) in self.document.blocks.iter().enumerate() {
            let mut span_start = self.block_starts[block_index];
            for span in &block.spans {
                let (distance, output) = if (span.affinity.source_offset..=span.affinity.source_end)
                    .contains(&source_offset)
                {
                    let source_within_span =
                        source_offset.saturating_sub(span.affinity.source_offset);
                    (
                        0,
                        span_start.saturating_add(source_within_span.min(span.text.len())),
                    )
                } else {
                    (
                        source_offset
                            .abs_diff(span.affinity.source_offset)
                            .min(source_offset.abs_diff(span.affinity.source_end)),
                        span_start,
                    )
                };
                if best.is_none_or(|(best_distance, _)| distance < best_distance) {
                    best = Some((distance, output));
                }
                span_start = span_start.saturating_add(span.text.len());
            }
        }
        best.map_or_else(|| self.normalize_offset(fallback), |(_, output)| output)
    }

    fn line_break_end_at(&self, offset: usize) -> Option<usize> {
        let offset = self.normalize_offset(offset);
        if offset >= self.output_len || self.document.blocks.is_empty() {
            return None;
        }
        let block_index = self
            .block_starts
            .partition_point(|block_start| *block_start <= offset)
            .saturating_sub(1);
        let block = &self.document.blocks[block_index];
        let block_start = self.block_starts[block_index];
        let local = offset.saturating_sub(block_start);
        let mut span_start = 0_usize;
        for span in &block.spans {
            let span_end = span_start + span.text.len();
            if local < span_end {
                let relative = local - span_start;
                let grapheme = span.text[relative..].graphemes(true).next()?;
                return is_line_break(grapheme).then(|| offset + grapheme.len());
            }
            span_start = span_end;
        }
        None
    }

    fn next_boundary(&self, offset: usize) -> usize {
        let offset = self.normalize_offset(offset);
        if offset >= self.output_len || self.document.blocks.is_empty() {
            return offset;
        }
        let block_index = self
            .block_starts
            .partition_point(|block_start| *block_start <= offset)
            .saturating_sub(1);
        let block = &self.document.blocks[block_index];
        let block_start = self.block_starts[block_index];
        let block_len = block
            .spans
            .iter()
            .map(|span| span.text.len())
            .sum::<usize>();
        if offset >= block_start + block_len {
            return offset.saturating_add(1).min(self.output_len);
        }
        let local = offset - block_start;
        let mut span_start = 0_usize;
        for span in &block.spans {
            let span_end = span_start + span.text.len();
            if local < span_end {
                let relative = local - span_start;
                let grapheme_len = span.text[relative..]
                    .graphemes(true)
                    .next()
                    .map_or(0, str::len);
                return offset.saturating_add(grapheme_len).min(self.output_len);
            }
            span_start = span_end;
        }
        offset
    }

    fn normalize_offset(&self, offset: usize) -> usize {
        let offset = offset.min(self.output_len);
        if offset == self.output_len || self.document.blocks.is_empty() {
            return offset;
        }
        let block_index = self
            .block_starts
            .partition_point(|block_start| *block_start <= offset)
            .saturating_sub(1);
        let block = &self.document.blocks[block_index];
        let block_start = self.block_starts[block_index];
        let block_len = block
            .spans
            .iter()
            .map(|span| span.text.len())
            .sum::<usize>();
        if offset >= block_start + block_len {
            return offset;
        }
        let local = offset - block_start;
        let mut span_start = 0_usize;
        for span in &block.spans {
            let span_end = span_start + span.text.len();
            if local <= span_end {
                let mut relative = local.saturating_sub(span_start).min(span.text.len());
                while relative > 0 && !span.text.is_char_boundary(relative) {
                    relative -= 1;
                }
                return block_start + span_start + relative;
            }
            span_start = span_end;
        }
        offset
    }
}

#[derive(Debug, Default)]
struct MarkdownCacheEntry {
    state: IncrementalMarkdownState,
    snapshot: Arc<MarkdownSnapshot>,
    entry_revision: Option<u64>,
    state_presentation_epoch: Option<u64>,
    presentation_identity: u64,
    retained_bytes: usize,
}

#[derive(Debug, Default)]
pub struct TranscriptRowCache {
    rows: HashMap<RowKey, CachedRow>,
    predecessors: HashMap<RowKey, RowAnchor>,
    furthest_content: HashMap<(TranscriptEntryId, u64, usize, usize), RowAnchor>,
    insertion_order: VecDeque<RowKey>,
    retained_bytes: usize,
    markdown: HashMap<MarkdownKey, MarkdownCacheEntry>,
    markdown_order: VecDeque<MarkdownKey>,
    markdown_retained_bytes: usize,
    next_markdown_presentation_identity: u64,
    cards: HashMap<CardKey, Arc<CardProjection>>,
    card_order: VecDeque<CardKey>,
    card_retained_bytes: usize,
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
        let markdown_snapshot = (entry.role == TranscriptRole::Assistant
            && !entry.content.is_empty())
        .then(|| self.markdown_snapshot(entry));
        if let Some(cached) = self.rows.get(&key).cloned() {
            if cached_row_valid(&cached, entry, markdown_snapshot.as_deref()) {
                self.work.cache_hits = self.work.cache_hits.saturating_add(1);
                return Some(cached);
            }
            self.retained_bytes = self
                .retained_bytes
                .saturating_sub(cached.row.retained_bytes());
            self.remove_indexes(key, &cached);
            self.rows.remove(&key);
            self.insertion_order.retain(|candidate| candidate != &key);
        }

        let cached = self.build_row(
            transcript,
            entry,
            anchor,
            width.max(1),
            markdown_snapshot
                .as_ref()
                .map(|snapshot| snapshot.presentation_identity),
        )?;
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
        markdown_presentation_identity: Option<u64>,
    ) -> Option<CachedRow> {
        let next_entry = || {
            transcript.entry_after(entry.id).map(|next| RowAnchor {
                entry_id: next.id,
                position: RowPosition::Header,
            })
        };
        let projected = match anchor.position {
            RowPosition::Header if !matches!(entry.kind, TranscriptEntryKind::Message) => {
                self.build_card_row(transcript, entry, anchor, CardSection::Action, 0, width)?
            }
            RowPosition::Header => ProjectedRow {
                row: TranscriptRow::plain(
                    anchor,
                    entry.role,
                    TranscriptRowKind::Header,
                    match entry.role {
                        TranscriptRole::User => "you",
                        TranscriptRole::Assistant => "wisp",
                        TranscriptRole::Tool => unreachable!("cards use card action rows"),
                    }
                    .into(),
                    0,
                ),
                next: Some(RowAnchor {
                    entry_id: entry.id,
                    position: if presentation_start(entry) > 0 {
                        RowPosition::Omission
                    } else {
                        self.position_for_offset(entry, content_start(entry))
                    },
                }),
            },
            RowPosition::Omission if presentation_start(entry) > 0 => {
                let omitted = presentation_start(entry);
                ProjectedRow {
                    row: TranscriptRow::plain(
                        anchor,
                        entry.role,
                        TranscriptRowKind::Omission,
                        format!("… {omitted} earlier bytes omitted …"),
                        omitted,
                    ),
                    next: Some(RowAnchor {
                        entry_id: entry.id,
                        position: self.position_for_offset(entry, content_start(entry)),
                    }),
                }
            }
            RowPosition::Content(start)
                if entry.role != TranscriptRole::Assistant
                    && start <= entry.content.len()
                    && entry.content.is_char_boundary(start) =>
            {
                self.build_content_row(transcript, entry, anchor, start, width)
            }
            RowPosition::Markdown(position) if entry.role == TranscriptRole::Assistant => {
                self.build_markdown_row(transcript, entry, anchor, position.output_offset, width)
            }
            RowPosition::Card(position) if entry.role == TranscriptRole::Tool => self
                .build_card_row(
                    transcript,
                    entry,
                    anchor,
                    position.section,
                    position.absolute_byte_offset,
                    width,
                )?,
            RowPosition::Spacer if next_entry().is_some() => ProjectedRow {
                row: TranscriptRow::plain(
                    anchor,
                    entry.role,
                    TranscriptRowKind::Spacer,
                    String::new(),
                    entry.content.len(),
                ),
                next: next_entry(),
            },
            _ => return None,
        };
        Some(CachedRow {
            row: projected.row,
            next: projected.next,
            entry_revision: entry.revision(),
            content_len: entry.content.len(),
            markdown_presentation_identity,
        })
    }

    fn position_for_offset(&mut self, entry: &TranscriptEntry, offset: usize) -> RowPosition {
        if entry.role == TranscriptRole::Assistant && !entry.content.is_empty() {
            RowPosition::Markdown(self.markdown_snapshot(entry).position_at(offset))
        } else {
            RowPosition::Content(offset)
        }
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
                row: TranscriptRow::plain(
                    anchor,
                    entry.role,
                    if pending {
                        TranscriptRowKind::Placeholder
                    } else {
                        TranscriptRowKind::Content
                    },
                    if pending {
                        "working…".into()
                    } else {
                        String::new()
                    },
                    0,
                ),
                next: separator_after(transcript, entry),
            };
        }
        if start == entry.content.len() {
            return ProjectedRow {
                row: TranscriptRow::plain(
                    anchor,
                    entry.role,
                    TranscriptRowKind::Content,
                    String::new(),
                    start,
                ),
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
            row: TranscriptRow::plain(anchor, entry.role, TranscriptRowKind::Content, text, start),
            next,
        }
    }

    fn card_snapshot(&mut self, entry: &TranscriptEntry) -> Option<Arc<CardProjection>> {
        let key = CardKey {
            entry_id: entry.id,
            layout_epoch: entry.layout_epoch(),
        };
        if let Some(cached) = self.cards.get(&key) {
            let snapshot = Arc::clone(cached);
            self.card_order.retain(|candidate| candidate != &key);
            self.card_order.push_back(key);
            return Some(snapshot);
        }
        let snapshot = Arc::new(card_projection(entry)?);
        self.card_retained_bytes = self
            .card_retained_bytes
            .saturating_add(snapshot.retained_bytes());
        self.cards.insert(key, Arc::clone(&snapshot));
        self.card_order.push_back(key);
        while self.cards.len() > CARD_CACHE_MAX_ENTRIES
            || self.card_retained_bytes > CARD_CACHE_MAX_BYTES
        {
            let Some(oldest) = self.card_order.pop_front() else {
                break;
            };
            if let Some(removed) = self.cards.remove(&oldest) {
                self.card_retained_bytes = self
                    .card_retained_bytes
                    .saturating_sub(removed.retained_bytes());
            }
        }
        Some(snapshot)
    }

    fn build_card_row(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        anchor: RowAnchor,
        section: CardSection,
        absolute_offset: usize,
        width: usize,
    ) -> Option<ProjectedRow> {
        let projection = self.card_snapshot(entry)?;
        match section {
            CardSection::Action => {
                let next = card_position_after_action(entry, &projection);
                Some(self.build_card_text_row(
                    entry,
                    anchor,
                    section,
                    &projection.action,
                    0,
                    absolute_offset,
                    width,
                    TranscriptRowKind::CardAction,
                    projection.action_tone,
                    next.or_else(|| separator_after(transcript, entry)),
                ))
            }
            CardSection::Detail if !projection.detail.is_empty() => {
                let next = if projection.omission_before_detail {
                    separator_after(transcript, entry)
                } else {
                    projection
                        .omission
                        .as_ref()
                        .map(|_| RowAnchor {
                            entry_id: entry.id,
                            position: RowPosition::Card(CardPosition {
                                section: CardSection::Omission,
                                absolute_byte_offset: 0,
                            }),
                        })
                        .or_else(|| separator_after(transcript, entry))
                };
                Some(self.build_card_text_row(
                    entry,
                    anchor,
                    section,
                    &projection.detail,
                    projection.detail_base,
                    absolute_offset,
                    width,
                    TranscriptRowKind::CardDetail,
                    TranscriptRowTone::Default,
                    next,
                ))
            }
            CardSection::Omission => {
                let omission = projection.omission.as_deref()?;
                let next = if projection.omission_before_detail && !projection.detail.is_empty() {
                    Some(RowAnchor {
                        entry_id: entry.id,
                        position: RowPosition::Card(CardPosition {
                            section: CardSection::Detail,
                            absolute_byte_offset: projection.detail_base,
                        }),
                    })
                } else {
                    separator_after(transcript, entry)
                };
                Some(self.build_card_text_row(
                    entry,
                    anchor,
                    section,
                    omission,
                    0,
                    absolute_offset,
                    width,
                    TranscriptRowKind::CardOmission,
                    TranscriptRowTone::Warning,
                    next,
                ))
            }
            CardSection::Detail => None,
        }
    }

    #[allow(clippy::too_many_arguments)]
    fn build_card_text_row(
        &mut self,
        entry: &TranscriptEntry,
        anchor: RowAnchor,
        section: CardSection,
        source: &str,
        base_offset: usize,
        absolute_start: usize,
        width: usize,
        kind: TranscriptRowKind,
        tone: TranscriptRowTone,
        after_section: Option<RowAnchor>,
    ) -> ProjectedRow {
        let relative_start = absolute_start.saturating_sub(base_offset).min(source.len());
        let mut relative_start = relative_start;
        while relative_start > 0 && !source.is_char_boundary(relative_start) {
            relative_start -= 1;
        }
        if relative_start == source.len() {
            let mut row =
                TranscriptRow::plain(anchor, entry.role, kind, String::new(), absolute_start);
            row.tone = tone;
            return ProjectedRow {
                row,
                next: after_section,
            };
        }

        let mut text = String::new();
        let mut column = 0_usize;
        let mut next_offset = None;
        let mut preserve_terminal_break = false;
        for (offset, grapheme) in source[relative_start..].grapheme_indices(true) {
            self.work.graphemes_scanned = self.work.graphemes_scanned.saturating_add(1);
            self.work.bytes_scanned = self.work.bytes_scanned.saturating_add(grapheme.len());
            let relative = relative_start + offset;
            if is_line_break(grapheme) {
                next_offset = Some(relative + grapheme.len());
                preserve_terminal_break = true;
                break;
            }
            let safe = sanitize_grapheme(grapheme, column);
            let grapheme_width = UnicodeWidthStr::width(safe.as_str());
            if !text.is_empty() && column.saturating_add(grapheme_width) > width {
                next_offset = Some(relative);
                break;
            }
            text.push_str(&safe);
            column = column.saturating_add(grapheme_width);
            if column >= width {
                let mut boundary = relative + grapheme.len();
                if let Some(line_break) = line_break_prefix_len(&source[boundary..]) {
                    boundary = boundary.saturating_add(line_break);
                }
                next_offset = Some(boundary);
                break;
            }
        }
        let consumed_end = next_offset.unwrap_or(source.len());
        let next = match next_offset {
            Some(offset) if offset < source.len() => Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Card(CardPosition {
                    section,
                    absolute_byte_offset: base_offset.saturating_add(offset),
                }),
            }),
            Some(offset) if preserve_terminal_break && offset == source.len() => Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Card(CardPosition {
                    section,
                    absolute_byte_offset: base_offset.saturating_add(offset),
                }),
            }),
            _ => after_section,
        };
        let mut row = TranscriptRow::plain(anchor, entry.role, kind, text, absolute_start);
        if let Some(span) = row.spans.first_mut() {
            span.affinity.source_end = base_offset.saturating_add(consumed_end);
        }
        row.tone = tone;
        ProjectedRow { row, next }
    }

    fn markdown_snapshot(&mut self, entry: &TranscriptEntry) -> Arc<MarkdownSnapshot> {
        let start = presentation_start(entry);
        let key = MarkdownKey {
            entry_id: entry.id,
            layout_epoch: entry.layout_epoch(),
            presentation_start: start,
        };
        if let Some(cached) = self.markdown.get(&key) {
            if cached.entry_revision == Some(entry.revision()) {
                let snapshot = Arc::clone(&cached.snapshot);
                self.markdown_order.retain(|candidate| candidate != &key);
                self.markdown_order.push_back(key);
                return snapshot;
            }
        }
        if let Entry::Vacant(vacant) = self.markdown.entry(key) {
            let presentation_identity = self.next_markdown_presentation_identity;
            self.next_markdown_presentation_identity = self
                .next_markdown_presentation_identity
                .checked_add(1)
                .expect("Markdown presentation identity exhausted");
            vacant.insert(MarkdownCacheEntry {
                presentation_identity,
                ..MarkdownCacheEntry::default()
            });
        }
        let (old_bytes, build, presentation_changed) = {
            let cached = self
                .markdown
                .get_mut(&key)
                .expect("Markdown cache entry must exist");
            let old_bytes = cached.retained_bytes;
            let build = cached.state.build(
                &entry.content[start..],
                start,
                entry.layout_epoch(),
                entry.state == TranscriptEntryState::Complete,
            );
            let presentation_changed = cached
                .state_presentation_epoch
                .is_some_and(|epoch| epoch != build.presentation_epoch);
            cached.state_presentation_epoch = Some(build.presentation_epoch);
            (old_bytes, build, presentation_changed)
        };
        if presentation_changed {
            let presentation_identity = self.allocate_markdown_presentation_identity();
            self.markdown
                .get_mut(&key)
                .expect("Markdown cache entry must exist")
                .presentation_identity = presentation_identity;
        }
        let (snapshot, new_bytes, work) = {
            let cached = self
                .markdown
                .get_mut(&key)
                .expect("Markdown cache entry must exist");
            let work = build.work;
            cached.snapshot = Arc::new(MarkdownSnapshot::new(
                build.document,
                build.stable_blocks,
                cached.presentation_identity,
            ));
            cached.entry_revision = Some(entry.revision());
            cached.retained_bytes = cached.snapshot.retained_bytes();
            (Arc::clone(&cached.snapshot), cached.retained_bytes, work)
        };
        self.markdown_retained_bytes = self
            .markdown_retained_bytes
            .saturating_sub(old_bytes)
            .saturating_add(new_bytes);
        self.record_markdown_work(work);
        self.markdown_order.retain(|candidate| candidate != &key);
        self.markdown_order.push_back(key);
        self.evict_markdown(key);
        snapshot
    }

    fn allocate_markdown_presentation_identity(&mut self) -> u64 {
        let identity = self.next_markdown_presentation_identity;
        self.next_markdown_presentation_identity = self
            .next_markdown_presentation_identity
            .checked_add(1)
            .expect("Markdown presentation identity exhausted");
        identity
    }

    fn record_markdown_work(&mut self, work: MarkdownWork) {
        self.work.markdown_source_bytes_parsed = self
            .work
            .markdown_source_bytes_parsed
            .saturating_add(work.source_bytes_parsed);
        self.work.markdown_source_bytes_reused = self
            .work
            .markdown_source_bytes_reused
            .saturating_add(work.source_bytes_reused);
        self.work.markdown_blocks_built = self
            .work
            .markdown_blocks_built
            .saturating_add(work.blocks_built);
        self.work.markdown_full_reparses = self
            .work
            .markdown_full_reparses
            .saturating_add(work.full_reparses);
        self.work.markdown_incremental_builds = self
            .work
            .markdown_incremental_builds
            .saturating_add(work.incremental_builds);
        self.work.markdown_fragments_emitted = self
            .work
            .markdown_fragments_emitted
            .saturating_add(work.fragments_emitted);
        self.work.syntax_fences_considered = self
            .work
            .syntax_fences_considered
            .saturating_add(work.syntax_fences_considered);
        self.work.syntax_fences_highlighted = self
            .work
            .syntax_fences_highlighted
            .saturating_add(work.syntax_fences_highlighted);
        self.work.syntax_fallbacks = self
            .work
            .syntax_fallbacks
            .saturating_add(work.syntax_fallbacks);
        self.work.syntax_source_bytes = self
            .work
            .syntax_source_bytes
            .saturating_add(work.syntax_source_bytes);
        self.work.syntax_lines = self.work.syntax_lines.saturating_add(work.syntax_lines);
        self.work.syntax_fragments = self
            .work
            .syntax_fragments
            .saturating_add(work.syntax_fragments);
    }

    fn evict_markdown(&mut self, protected: MarkdownKey) {
        if self
            .markdown
            .get(&protected)
            .is_some_and(|entry| entry.retained_bytes > MARKDOWN_CACHE_MAX_BYTES)
        {
            if let Some(removed) = self.markdown.remove(&protected) {
                self.markdown_retained_bytes = self
                    .markdown_retained_bytes
                    .saturating_sub(removed.retained_bytes);
            }
            self.markdown_order
                .retain(|candidate| candidate != &protected);
            return;
        }
        while self.markdown.len() > MARKDOWN_CACHE_MAX_ENTRIES
            || self.markdown_retained_bytes > MARKDOWN_CACHE_MAX_BYTES
        {
            let Some(oldest) = self.markdown_order.pop_front() else {
                break;
            };
            if oldest == protected {
                self.markdown_order.push_back(oldest);
                break;
            }
            if let Some(removed) = self.markdown.remove(&oldest) {
                self.markdown_retained_bytes = self
                    .markdown_retained_bytes
                    .saturating_sub(removed.retained_bytes);
            }
        }
    }

    fn build_markdown_row(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        anchor: RowAnchor,
        start: usize,
        width: usize,
    ) -> ProjectedRow {
        let snapshot = self.markdown_snapshot(entry);
        let start = snapshot.normalize_offset(start);
        if start >= snapshot.output_len {
            return ProjectedRow {
                row: TranscriptRow::plain(
                    anchor,
                    entry.role,
                    TranscriptRowKind::Content,
                    String::new(),
                    entry.content.len(),
                ),
                next: separator_after(transcript, entry),
            };
        }

        let mut spans = Vec::new();
        let mut column = 0_usize;
        let mut next_offset = None;
        let mut ended_with_break = false;
        let mut cursor = start;
        while cursor < snapshot.output_len {
            let block_index = snapshot
                .block_starts
                .partition_point(|block_start| *block_start <= cursor)
                .saturating_sub(1);
            let block = &snapshot.document.blocks[block_index];
            let block_start = snapshot.block_starts[block_index];
            let block_len = block
                .spans
                .iter()
                .map(|span| span.text.len())
                .sum::<usize>();
            let block_end = block_start + block_len;
            if cursor == block_end && block_index + 1 < snapshot.document.blocks.len() {
                if spans.is_empty() {
                    cursor = cursor.saturating_add(1);
                    continue;
                }
                next_offset = Some(cursor + 1);
                ended_with_break = true;
                break;
            }

            let local = cursor.saturating_sub(block_start);
            let mut span_start = 0_usize;
            let mut advanced = false;
            for span in &block.spans {
                let span_end = span_start + span.text.len();
                if local >= span_end {
                    span_start = span_end;
                    continue;
                }
                let relative_start = local.saturating_sub(span_start);
                for (relative_offset, grapheme) in
                    span.text[relative_start..].grapheme_indices(true)
                {
                    self.work.graphemes_scanned = self.work.graphemes_scanned.saturating_add(1);
                    self.work.bytes_scanned =
                        self.work.bytes_scanned.saturating_add(grapheme.len());
                    let absolute_offset =
                        block_start + span_start + relative_start + relative_offset;
                    if is_line_break(grapheme) {
                        cursor = absolute_offset + grapheme.len();
                        next_offset = Some(cursor);
                        ended_with_break = true;
                        break;
                    }
                    let safe = sanitize_grapheme(grapheme, column);
                    let grapheme_width = UnicodeWidthStr::width(safe.as_str());
                    if !spans.is_empty() && column.saturating_add(grapheme_width) > width {
                        next_offset = Some(absolute_offset);
                        break;
                    }
                    push_styled_span(
                        &mut spans,
                        safe,
                        span.style,
                        SourceAffinity {
                            source_offset: span.affinity.source_offset,
                            source_end: span.affinity.source_end,
                            output_offset: u32::try_from(absolute_offset).unwrap_or(u32::MAX),
                        },
                    );
                    column = column.saturating_add(grapheme_width);
                    cursor = absolute_offset + grapheme.len();
                    advanced = true;
                    if column >= width {
                        if let Some(after_break) = snapshot.line_break_end_at(cursor) {
                            cursor = after_break;
                            ended_with_break = true;
                        }
                        next_offset = Some(cursor);
                        break;
                    }
                }
                if next_offset.is_some() {
                    break;
                }
                span_start = span_end;
            }
            if next_offset.is_some() {
                break;
            }
            if cursor < block_end && !advanced {
                cursor = block_end;
            }
            if cursor >= block_end {
                if block_index + 1 < snapshot.document.blocks.len() {
                    next_offset = Some(block_end + 1);
                    ended_with_break = true;
                }
                break;
            }
        }

        let next = match next_offset {
            Some(offset) if offset < snapshot.output_len => Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Markdown(snapshot.position_at(offset)),
            }),
            Some(offset) if ended_with_break && offset == snapshot.output_len => Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Markdown(snapshot.position_at(offset)),
            }),
            _ => separator_after(transcript, entry),
        };
        ProjectedRow {
            row: TranscriptRow {
                anchor,
                role: entry.role,
                kind: TranscriptRowKind::Content,
                tone: TranscriptRowTone::Assistant,
                spans,
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
                let start = content_start(entry);
                if offset <= start {
                    return Some(RowAnchor {
                        entry_id: entry.id,
                        position: if presentation_start(entry) > 0 {
                            RowPosition::Omission
                        } else {
                            RowPosition::Header
                        },
                    });
                }
                self.content_anchor_before(transcript, entry, offset, width)
            }
            RowPosition::Markdown(position) => {
                if position.output_offset == 0 {
                    return Some(RowAnchor {
                        entry_id: entry.id,
                        position: if presentation_start(entry) > 0 {
                            RowPosition::Omission
                        } else {
                            RowPosition::Header
                        },
                    });
                }
                self.content_anchor_before(transcript, entry, position.output_offset, width)
            }
            RowPosition::Card(_) => self.card_anchor_before(transcript, entry, anchor, width),
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
        let content_start = content_start(entry);
        let width = width.max(1);
        let target_position = self.position_for_offset(entry, target);
        let target_key = RowKey {
            entry_id: entry.id,
            layout_epoch: entry.layout_epoch(),
            presentation_start,
            width,
            position: target_position,
        };
        if let Some(previous) = self.predecessors.get(&target_key).copied() {
            return Some(previous);
        }
        let furthest_key = (entry.id, entry.layout_epoch(), presentation_start, width);
        let candidate_start = self
            .furthest_content
            .get(&furthest_key)
            .and_then(|anchor| {
                let offset = row_position_offset(anchor.position)?;
                (content_start..target).contains(&offset).then_some(offset)
            })
            .unwrap_or(content_start);
        let mut anchor = RowAnchor {
            entry_id: entry.id,
            position: self.position_for_offset(entry, candidate_start),
        };
        let mut previous = anchor;
        loop {
            let cached = self.row_at(transcript, anchor, width)?;
            let Some(next) = cached.next else {
                return Some(previous);
            };
            match row_position_offset(next.position) {
                Some(offset) if offset < target => {
                    previous = next;
                    anchor = next;
                }
                Some(offset) if offset == target => return Some(anchor),
                Some(_) => return Some(anchor),
                None if next.position == RowPosition::Spacer => return Some(anchor),
                None => return Some(previous),
            }
        }
    }

    fn card_anchor_before(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        target: RowAnchor,
        width: usize,
    ) -> Option<RowAnchor> {
        let target_key = RowKey {
            entry_id: entry.id,
            layout_epoch: entry.layout_epoch(),
            presentation_start: 0,
            width: width.max(1),
            position: target.position,
        };
        if let Some(previous) = self.predecessors.get(&target_key).copied() {
            return Some(previous);
        }
        let mut current = RowAnchor {
            entry_id: entry.id,
            position: RowPosition::Header,
        };
        loop {
            let cached = self.row_at(transcript, current, width)?;
            let next = cached.next?;
            if next == target {
                return Some(current);
            }
            if next.entry_id != entry.id || next.position == RowPosition::Spacer {
                return Some(current);
            }
            current = next;
        }
    }

    fn last_anchor(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        width: usize,
    ) -> RowAnchor {
        if !matches!(entry.kind, TranscriptEntryKind::Message) {
            let mut current = RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Header,
            };
            loop {
                let Some(cached) = self.row_at(transcript, current, width) else {
                    return current;
                };
                let Some(next) = cached.next else {
                    return current;
                };
                if next.entry_id != entry.id || next.position == RowPosition::Spacer {
                    return current;
                }
                current = next;
            }
        }
        let (target, ends_with_break) =
            if entry.role == TranscriptRole::Assistant && !entry.content.is_empty() {
                let snapshot = self.markdown_snapshot(entry);
                (snapshot.output_len, snapshot.ends_with_line_break())
            } else {
                (entry.content.len(), ends_with_line_break(&entry.content))
            };
        if target == 0 || ends_with_break {
            return RowAnchor {
                entry_id: entry.id,
                position: self.position_for_offset(entry, target),
            };
        }
        self.content_anchor_before(transcript, entry, target, width)
            .unwrap_or(RowAnchor {
                entry_id: entry.id,
                position: if presentation_start(entry) > 0 {
                    RowPosition::Omission
                } else {
                    self.position_for_offset(entry, content_start(entry))
                },
            })
    }

    fn insert(&mut self, key: RowKey, value: CachedRow) {
        if self.rows.contains_key(&key) {
            return;
        }
        self.retained_bytes = self
            .retained_bytes
            .saturating_add(value.row.retained_bytes());
        if let Some(offset) = row_position_offset(value.row.anchor.position) {
            let furthest_key = (
                key.entry_id,
                key.layout_epoch,
                key.presentation_start,
                key.width,
            );
            let should_replace = self
                .furthest_content
                .get(&furthest_key)
                .is_none_or(|anchor| {
                    row_position_offset(anchor.position).is_some_and(|current| offset > current)
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
                self.retained_bytes = self
                    .retained_bytes
                    .saturating_sub(removed.row.retained_bytes());
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

fn card_projection(entry: &TranscriptEntry) -> Option<CardProjection> {
    match &entry.kind {
        TranscriptEntryKind::Message => None,
        TranscriptEntryKind::Tool(card) => {
            let retained_preview = card.retained_output.preview_head();
            let detail = if card.detail.is_empty() {
                retained_preview.text.clone()
            } else if retained_preview.text.is_empty() || retained_preview.text == card.detail {
                card.detail.clone()
            } else {
                format!("{}\n{}", card.detail, retained_preview.text)
            };
            let shown_output_bytes = retained_preview.text.len() as u64;
            let omitted = card
                .retained_output
                .source_bytes
                .saturating_sub(shown_output_bytes);
            let omission = card_omission(omitted, card.backend_truncated);
            Some(CardProjection {
                action: format!("• {}", card.action()),
                detail,
                detail_base: 0,
                omission,
                omission_before_detail: false,
                action_tone: tool_status_tone(card.status),
            })
        }
        TranscriptEntryKind::Process(card) => {
            let preview = card.preview();
            let omitted = preview
                .base_offset
                .saturating_add(card.backend_dropped_bytes);
            let omission = card_omission(omitted, card.backend_truncated);
            Some(CardProjection {
                action: format!("• {}", card.action()),
                detail: preview.text,
                detail_base: usize::try_from(preview.base_offset).unwrap_or(usize::MAX),
                omission,
                omission_before_detail: true,
                action_tone: process_state_tone(card.display_state),
            })
        }
    }
}

fn card_omission(omitted_bytes: u64, backend_truncated: bool) -> Option<String> {
    match (omitted_bytes, backend_truncated) {
        (0, false) => None,
        (0, true) => Some("⋯ tool reported truncated output".into()),
        (bytes, false) => Some(format!(
            "⋯ {bytes} input or presentation bytes omitted before this preview"
        )),
        (bytes, true) => Some(format!(
            "⋯ {bytes} input or presentation bytes omitted; tool also reported truncation"
        )),
    }
}

fn card_position_after_action(
    entry: &TranscriptEntry,
    projection: &CardProjection,
) -> Option<RowAnchor> {
    if projection.omission_before_detail && projection.omission.is_some() {
        return Some(RowAnchor {
            entry_id: entry.id,
            position: RowPosition::Card(CardPosition {
                section: CardSection::Omission,
                absolute_byte_offset: 0,
            }),
        });
    }
    if !projection.detail.is_empty() {
        return Some(RowAnchor {
            entry_id: entry.id,
            position: RowPosition::Card(CardPosition {
                section: CardSection::Detail,
                absolute_byte_offset: projection.detail_base,
            }),
        });
    }
    projection.omission.as_ref().map(|_| RowAnchor {
        entry_id: entry.id,
        position: RowPosition::Card(CardPosition {
            section: CardSection::Omission,
            absolute_byte_offset: 0,
        }),
    })
}

fn tool_status_tone(status: ToolStatus) -> TranscriptRowTone {
    match status {
        ToolStatus::Requested | ToolStatus::AwaitingApproval | ToolStatus::Running => {
            TranscriptRowTone::Pending
        }
        ToolStatus::Done => TranscriptRowTone::Success,
        ToolStatus::Denied | ToolStatus::Cancelled => TranscriptRowTone::Warning,
        ToolStatus::Error => TranscriptRowTone::Error,
    }
}

fn process_state_tone(state: ProcessDisplayState) -> TranscriptRowTone {
    tool_status_tone(state.status())
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
            self.top = self.top.and_then(|anchor| {
                normalize_anchor(
                    transcript,
                    cache,
                    anchor,
                    self.width,
                    AnchorNormalization::Geometry,
                )
            });
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
                    self.top = self.top.and_then(|anchor| {
                        normalize_anchor(
                            transcript,
                            cache,
                            anchor,
                            self.width,
                            AnchorNormalization::Content,
                        )
                    });
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

fn cached_row_valid(
    cached: &CachedRow,
    entry: &TranscriptEntry,
    markdown: Option<&MarkdownSnapshot>,
) -> bool {
    if cached.row.kind == TranscriptRowKind::Content
        && entry.role == TranscriptRole::Assistant
        && markdown.is_none_or(|snapshot| {
            cached.markdown_presentation_identity != Some(snapshot.presentation_identity)
        })
    {
        return false;
    }
    if cached.entry_revision == entry.revision() {
        return true;
    }
    match cached.row.kind {
        TranscriptRowKind::Header if entry.role == TranscriptRole::Assistant => false,
        TranscriptRowKind::Header => {
            let expected = if presentation_start(entry) > 0 {
                RowPosition::Omission
            } else {
                RowPosition::Content(content_start(entry))
            };
            cached
                .next
                .is_some_and(|next| next.entry_id == entry.id && next.position == expected)
        }
        TranscriptRowKind::Spacer => true,
        TranscriptRowKind::Content if entry.role == TranscriptRole::Assistant => {
            let Some(snapshot) = markdown else {
                return false;
            };
            let Some(start) = row_position_offset(cached.row.anchor.position) else {
                return false;
            };
            if start >= snapshot.stable_output_len {
                return false;
            }
            match cached.next {
                Some(next) => {
                    next.entry_id == entry.id
                        && row_position_offset(next.position)
                            .is_some_and(|offset| offset <= snapshot.stable_output_len)
                }
                None => false,
            }
        }
        TranscriptRowKind::Content => matches!(
            cached.next,
            Some(RowAnchor {
                entry_id,
                position: RowPosition::Content(offset),
            }) if entry_id == entry.id
                && offset <= cached.content_len
                && entry.content.len() >= cached.content_len
        ),
        TranscriptRowKind::CardAction
        | TranscriptRowKind::CardDetail
        | TranscriptRowKind::CardOmission
        | TranscriptRowKind::Placeholder
        | TranscriptRowKind::Omission => false,
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

#[derive(Clone, Copy)]
enum AnchorNormalization {
    Geometry,
    Content,
}

fn normalize_anchor(
    transcript: &Transcript,
    cache: &mut TranscriptRowCache,
    anchor: RowAnchor,
    width: usize,
    normalization: AnchorNormalization,
) -> Option<RowAnchor> {
    let entry = transcript.entry(anchor.entry_id)?;
    match anchor.position {
        RowPosition::Markdown(position) if entry.role == TranscriptRole::Assistant => {
            let snapshot = cache.markdown_snapshot(entry);
            let output = match normalization {
                AnchorNormalization::Geometry => snapshot.normalize_offset(position.output_offset),
                AnchorNormalization::Content => {
                    snapshot.output_for_source(position.source_offset, position.output_offset)
                }
            };
            if output >= snapshot.output_len {
                return cache.content_anchor_before(transcript, entry, snapshot.output_len, width);
            }
            cache.content_anchor_before(transcript, entry, snapshot.next_boundary(output), width)
        }
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
        RowPosition::Card(position) => {
            let projection = cache.card_snapshot(entry)?;
            let normalized = match position.section {
                CardSection::Action => {
                    let offset = position.absolute_byte_offset.min(projection.action.len());
                    RowAnchor {
                        entry_id: entry.id,
                        position: if offset == 0 {
                            RowPosition::Header
                        } else {
                            RowPosition::Card(CardPosition {
                                section: CardSection::Action,
                                absolute_byte_offset: offset,
                            })
                        },
                    }
                }
                CardSection::Detail if !projection.detail.is_empty() => {
                    let end = projection
                        .detail_base
                        .saturating_add(projection.detail.len());
                    let offset = position
                        .absolute_byte_offset
                        .clamp(projection.detail_base, end);
                    RowAnchor {
                        entry_id: entry.id,
                        position: RowPosition::Card(CardPosition {
                            section: CardSection::Detail,
                            absolute_byte_offset: offset,
                        }),
                    }
                }
                CardSection::Omission if projection.omission.is_some() => RowAnchor {
                    entry_id: entry.id,
                    position: RowPosition::Card(CardPosition {
                        section: CardSection::Omission,
                        absolute_byte_offset: position
                            .absolute_byte_offset
                            .min(projection.omission.as_ref().map_or(0, String::len)),
                    }),
                },
                _ => RowAnchor {
                    entry_id: entry.id,
                    position: RowPosition::Header,
                },
            };
            if cache.row_at(transcript, normalized, width).is_some() {
                Some(normalized)
            } else {
                Some(RowAnchor {
                    entry_id: entry.id,
                    position: RowPosition::Header,
                })
            }
        }
        _ => Some(anchor),
    }
}

fn ends_with_line_break(content: &str) -> bool {
    content.ends_with('\n') || content.ends_with('\r')
}

fn row_position_offset(position: RowPosition) -> Option<usize> {
    match position {
        RowPosition::Content(offset) => Some(offset),
        RowPosition::Markdown(position) => Some(position.output_offset),
        RowPosition::Header
        | RowPosition::Omission
        | RowPosition::Card(_)
        | RowPosition::Spacer => None,
    }
}

fn content_start(entry: &TranscriptEntry) -> usize {
    if entry.role == TranscriptRole::Assistant && !entry.content.is_empty() {
        0
    } else {
        presentation_start(entry)
    }
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

fn push_styled_span(
    spans: &mut Vec<TranscriptSpan>,
    text: String,
    style: TranscriptSpanStyle,
    affinity: SourceAffinity,
) {
    if text.is_empty() {
        return;
    }
    if let Some(last) = spans.last_mut().filter(|last| last.style == style) {
        last.text.push_str(&text);
        return;
    }
    spans.push(TranscriptSpan {
        text,
        style,
        affinity,
    });
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

fn line_break_prefix_len(source: &str) -> Option<usize> {
    if source.starts_with("\r\n") {
        Some(2)
    } else if source.starts_with('\n') || source.starts_with('\r') {
        Some(1)
    } else {
        None
    }
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

    fn numbered_blocks(prefix: &str, count: usize) -> String {
        let mut content = String::new();
        for block in 0..count {
            writeln!(content, "{prefix}-{block}\n").unwrap();
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

    fn tool_result(call_id: &str, output: &str) -> crate::tool_cards::ToolResultInput {
        crate::tool_cards::ToolResultInput {
            call_id: call_id.into(),
            name: "read".into(),
            output: output.into(),
            output_source_bytes: output.len() as u64,
            is_error: false,
            failure_code: None,
            retryable: false,
            recovery_hint: None,
            exit_code: None,
            output_has_exit_status: false,
            before_text: None,
            created: false,
            summary: None,
            truncated: false,
            process_id: None,
            process_state: None,
            process_error: None,
            stdout: None,
            stdout_source_bytes: 0,
            stderr: None,
            stderr_source_bytes: 0,
            stdout_truncated: false,
            stderr_truncated: false,
            stdout_dropped_bytes: 0,
            stderr_dropped_bytes: 0,
        }
    }

    #[test]
    fn tool_card_rows_update_in_place_with_semantic_tones() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("read".into());
        let card_id = transcript.observe_approval_requested(crate::tool_cards::ToolCallInput {
            call_id: "call-1".into(),
            name: "read".into(),
            arguments: serde_json::json!({"path": "README.md"}),
        });
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 8);
        let pending = viewport.visible_rows(&transcript, &mut cache);
        let pending_action = pending
            .iter()
            .find(|row| row.anchor.entry_id == card_id && row.kind == TranscriptRowKind::CardAction)
            .unwrap();
        assert!(
            pending_action
                .plain_text()
                .contains("Awaiting approval to read")
        );
        assert_eq!(pending_action.tone, TranscriptRowTone::Pending);

        transcript.observe_approval_resolved("call-1", true, None);
        transcript.observe_tool_result(tool_result("call-1", "first\nsecond"));
        viewport.reduce(TranscriptViewAction::OutputChanged, &transcript, &mut cache);
        cache.reset_work();
        let completed = viewport.visible_rows(&transcript, &mut cache);
        let action = completed
            .iter()
            .find(|row| row.anchor.entry_id == card_id && row.kind == TranscriptRowKind::CardAction)
            .unwrap();
        assert!(action.plain_text().contains("Read  README.md"));
        assert_eq!(action.tone, TranscriptRowTone::Success);
        assert!(completed.iter().any(|row| {
            row.kind == TranscriptRowKind::CardDetail && row.plain_text() == "first"
        }));
    }

    #[test]
    fn process_card_coalesces_calls_and_bounds_terminal_safe_tail_rows() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("poll".into());
        let call = |call_id: &str| crate::tool_cards::ToolCallInput {
            call_id: call_id.into(),
            name: "bash".into(),
            arguments: serde_json::json!({
                "operation": "poll",
                "process_id": "process-1"
            }),
        };
        let card_id = transcript.observe_tool_call(call("poll-1"));
        let mut first = tool_result("poll-1", "");
        first.name = "bash".into();
        first.process_id = Some("process-1".into());
        first.process_state = Some("running".into());
        first.stdout = Some(format!("old\n{}", "x\n".repeat(20_000)));
        transcript.observe_tool_result(first);
        assert_eq!(transcript.observe_tool_call(call("poll-2")), card_id);
        let mut second = tool_result("poll-2", "");
        second.name = "bash".into();
        second.process_id = Some("process-1".into());
        second.process_state = Some("completed".into());
        second.stdout = Some("safe\u{1b}[2Jtail".into());
        transcript.observe_tool_result(second);

        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 100, 20);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let card_rows = rows
            .iter()
            .filter(|row| row.anchor.entry_id == card_id)
            .collect::<Vec<_>>();
        assert_eq!(
            card_rows
                .iter()
                .filter(|row| row.kind == TranscriptRowKind::CardAction)
                .count(),
            1
        );
        assert!(card_rows[0].plain_text().contains("2 calls · 2 polls"));
        assert_eq!(card_rows[0].tone, TranscriptRowTone::Success);
        assert!(
            card_rows
                .iter()
                .any(|row| row.kind == TranscriptRowKind::CardOmission)
        );
        let rendered = card_rows
            .iter()
            .map(|row| row.plain_text())
            .collect::<Vec<_>>()
            .join("\n");
        assert!(rendered.contains("safe"));
        assert!(rendered.contains('\u{fffd}'));
        assert!(!rendered.contains('\u{1b}'));
        assert!(!rendered.contains("old"));
    }

    #[test]
    fn narrow_process_tail_projection_scans_each_bounded_preview_once() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("poll".into());
        let card_id = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "poll-1".into(),
            name: "bash".into(),
            arguments: serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        });
        let mut completed = tool_result("poll-1", "");
        completed.name = "bash".into();
        completed.process_id = Some("process-1".into());
        completed.process_state = Some("completed".into());
        completed.stdout = Some("x".repeat(crate::tool_cards::TOOL_OUTPUT_MAX_BYTES));
        completed.stdout_source_bytes = crate::tool_cards::TOOL_OUTPUT_MAX_BYTES as u64;
        transcript.observe_tool_result(completed);
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 1, 5);
        cache.reset_work();

        let rows = viewport.visible_rows(&transcript, &mut cache);

        assert!(rows.iter().any(|row| row.anchor.entry_id == card_id));
        assert!(cache.work().bytes_scanned <= 5_000);
        assert_eq!(cache.cards.len(), 1);
    }

    #[test]
    fn card_exact_width_breaks_and_sanitized_affinities_are_precise() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("read".into());
        let card_id = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "call-1".into(),
            name: "read".into(),
            arguments: serde_json::json!({}),
        });
        transcript.observe_tool_result(tool_result("call-1", "1234\nnext"));
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 4, 20);
        let details = viewport
            .visible_rows(&transcript, &mut cache)
            .into_iter()
            .filter(|row| {
                row.anchor.entry_id == card_id && row.kind == TranscriptRowKind::CardDetail
            })
            .map(|row| row.plain_text())
            .collect::<Vec<_>>();
        assert_eq!(details, ["1234", "next"]);

        let mut sanitized = Transcript::default();
        sanitized.append_prompt("read".into());
        let card_id = sanitized.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "call-2".into(),
            name: "read".into(),
            arguments: serde_json::json!({}),
        });
        let source = "a\tb\u{1b}";
        sanitized.observe_tool_result(tool_result("call-2", source));
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&sanitized, &mut cache, 40, 20);
        let detail = viewport
            .visible_rows(&sanitized, &mut cache)
            .into_iter()
            .find(|row| row.anchor.entry_id == card_id && row.kind == TranscriptRowKind::CardDetail)
            .unwrap();
        assert_eq!(detail.plain_text(), "a   b\u{fffd}");
        let affinity = detail.spans.first().unwrap().affinity;
        assert_eq!(affinity.source_offset, 0);
        assert_eq!(affinity.source_end, source.len());
    }

    #[test]
    fn process_tail_updates_preserve_scrolled_card_anchor_and_mark_unseen() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("poll".into());
        let call = |call_id: &str| crate::tool_cards::ToolCallInput {
            call_id: call_id.into(),
            name: "bash".into(),
            arguments: serde_json::json!({
                "operation": "poll",
                "process_id": "process-1"
            }),
        };
        let card_id = transcript.observe_tool_call(call("poll-1"));
        let mut first = tool_result("poll-1", "");
        first.name = "bash".into();
        first.process_id = Some("process-1".into());
        first.process_state = Some("running".into());
        first.stdout = Some("first\n".repeat(5_000));
        transcript.observe_tool_result(first);
        let first_base = usize::try_from(
            transcript
                .entry(card_id)
                .unwrap()
                .process_card()
                .unwrap()
                .preview()
                .base_offset,
        )
        .unwrap();

        let mut viewport = TranscriptViewport {
            top: Some(RowAnchor {
                entry_id: card_id,
                position: RowPosition::Card(CardPosition {
                    section: CardSection::Detail,
                    absolute_byte_offset: first_base,
                }),
            }),
            follow_tail: false,
            unseen_output: false,
            width: 40,
            height: 5,
        };
        let mut cache = TranscriptRowCache::default();
        let before = viewport.visible_rows(&transcript, &mut cache);
        assert!(
            before
                .iter()
                .any(|row| row.kind == TranscriptRowKind::CardDetail)
        );

        transcript.observe_tool_call(call("poll-2"));
        let mut second = tool_result("poll-2", "");
        second.name = "bash".into();
        second.process_id = Some("process-1".into());
        second.process_state = Some("running".into());
        second.stdout = Some("second\n".repeat(20_000));
        transcript.observe_tool_result(second);
        let new_base = usize::try_from(
            transcript
                .entry(card_id)
                .unwrap()
                .process_card()
                .unwrap()
                .preview()
                .base_offset,
        )
        .unwrap();
        assert!(new_base > first_base);

        viewport.reduce(TranscriptViewAction::OutputChanged, &transcript, &mut cache);

        assert!(viewport.has_unseen_output());
        assert!(!viewport.follows_tail());
        assert!(matches!(
            viewport.top,
            Some(RowAnchor {
                entry_id,
                position: RowPosition::Card(CardPosition {
                    section: CardSection::Detail,
                    absolute_byte_offset,
                }),
            }) if entry_id == card_id && absolute_byte_offset >= new_base
        ));
    }

    #[test]
    fn omission_row_resize_preserves_its_semantic_offset() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("poll".into());
        let card_id = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "poll-1".into(),
            name: "bash".into(),
            arguments: serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        });
        let mut completed = tool_result("poll-1", "");
        completed.name = "bash".into();
        completed.process_id = Some("process-1".into());
        completed.process_state = Some("completed".into());
        completed.stdout = Some("x".repeat(20_000));
        completed.stdout_source_bytes = 20_000;
        transcript.observe_tool_result(completed);
        let mut viewport = TranscriptViewport {
            top: Some(RowAnchor {
                entry_id: card_id,
                position: RowPosition::Card(CardPosition {
                    section: CardSection::Omission,
                    absolute_byte_offset: 8,
                }),
            }),
            follow_tail: false,
            unseen_output: false,
            width: 10,
            height: 4,
        };
        let mut cache = TranscriptRowCache::default();

        viewport.set_geometry(&transcript, &mut cache, 8, 4);

        assert!(matches!(
            viewport.top,
            Some(RowAnchor {
                entry_id,
                position: RowPosition::Card(CardPosition {
                    section: CardSection::Omission,
                    absolute_byte_offset: 8,
                }),
            }) if entry_id == card_id
        ));
    }

    #[test]
    fn follows_tail_then_preserves_anchor_and_marks_unseen_output() {
        let mut transcript = transcript_with(40);
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 20, 6);
        let tail = viewport.visible_rows(&transcript, &mut cache);
        assert!(tail.iter().any(|row| row.plain_text() == "line-39"));

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
        assert!(rows.iter().any(|row| row.plain_text() == "ééé final"));
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
    fn card_projection_cache_enforces_entry_and_byte_caps() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("tools".into());
        for index in 0..500 {
            let call_id = format!("call-{index}");
            transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: call_id.clone(),
                name: "read".into(),
                arguments: serde_json::json!({"path": format!("file-{index}")}),
            });
            transcript.observe_tool_result(tool_result(&call_id, &"x".repeat(4_000)));
        }
        let mut cache = TranscriptRowCache::default();
        for entry in transcript.entries() {
            let _ = cache.row_at(
                &transcript,
                RowAnchor {
                    entry_id: entry.id,
                    position: RowPosition::Header,
                },
                80,
            );
        }

        assert!(cache.cards.len() <= CARD_CACHE_MAX_ENTRIES);
        assert!(cache.card_retained_bytes <= CARD_CACHE_MAX_BYTES);
    }

    #[test]
    fn streaming_append_reuses_the_stable_wrapped_prefix() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.start_message(1);
        transcript.append_message_delta(1, &numbered_blocks("stable", 5_000));
        transcript.append_message_delta(1, "growing");
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 12);
        let _ = viewport.visible_rows(&transcript, &mut cache);
        cache.reset_work();

        transcript.append_message_delta(1, " tail");
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let work = cache.work();

        assert!(rows.iter().any(|row| row.plain_text() == "growing tail"));
        assert!(work.rows_built <= 2, "stable rows were rebuilt: {work:?}");
        assert!(
            work.bytes_scanned <= 80 * (12 + OVERSCAN_ROWS),
            "work exceeded the visible window and overscan: {work:?}"
        );
        assert!(
            work.markdown_source_bytes_parsed <= 64,
            "stable Markdown blocks were reparsed: {work:?}"
        );
        assert!(work.markdown_source_bytes_reused > 0);
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
                position: RowPosition::Markdown(MarkdownPosition {
                    output_offset: 0,
                    ..
                }),
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
                .any(|row| row.plain_text() == "streaming draft")
        );

        transcript.complete_message(1, "authoritative answer".into());
        let rows = viewport.visible_rows(&transcript, &mut cache);

        assert!(
            rows.iter()
                .any(|row| row.plain_text() == "authoritative answer")
        );
        assert!(rows.iter().all(|row| row.plain_text() != "streaming draft"));
    }

    #[test]
    fn markdown_code_blocks_preserve_blank_source_lines() {
        fn projected_content(source: &str, width: usize) -> Vec<String> {
            let mut transcript = Transcript::default();
            transcript.append_exchange("prompt".into());
            transcript.complete_message(1, source.into());
            let mut viewport = TranscriptViewport::default();
            let mut cache = TranscriptRowCache::default();
            viewport.set_geometry(&transcript, &mut cache, width, 10);
            viewport
                .visible_rows(&transcript, &mut cache)
                .into_iter()
                .filter(|row| {
                    row.kind == TranscriptRowKind::Content && row.role == TranscriptRole::Assistant
                })
                .map(|row| row.plain_text())
                .collect()
        }

        for source in ["```text\na\n\nb\n```", "    a\n\n    b"] {
            let content = projected_content(source, 20);
            assert!(
                content.windows(3).any(|rows| rows == ["a", "", "b"]),
                "missing blank code row for {source:?}: {content:?}"
            );
        }

        let exact_width = projected_content("```text\n1234\nnext\n```", 4);
        assert_eq!(&exact_width[..2], ["1234", "next"]);
        let exact_width_with_blank = projected_content("```text\n1234\n\nnext\n```", 4);
        assert_eq!(&exact_width_with_blank[..3], ["1234", "", "next"]);
    }

    #[test]
    fn exact_width_markdown_boundaries_do_not_create_empty_rows() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.complete_message(1, "1234\n\nnext".into());
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 4, 8);
        let content = viewport
            .visible_rows(&transcript, &mut cache)
            .into_iter()
            .filter(|row| {
                row.kind == TranscriptRowKind::Content && row.role == TranscriptRole::Assistant
            })
            .map(|row| row.plain_text())
            .collect::<Vec<_>>();

        assert_eq!(content, ["1234", "next"]);
    }

    #[test]
    fn markdown_resize_keeps_the_nearest_output_row() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.complete_message(1, "abcdefghijklmnopqrstuvwxyz0123456789".into());
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 6, 2);
        viewport.reduce(TranscriptViewAction::PageUp, &transcript, &mut cache);
        let RowPosition::Markdown(before) = viewport.top.unwrap().position else {
            panic!("assistant Markdown must use a Markdown anchor");
        };

        viewport.set_geometry(&transcript, &mut cache, 5, 2);
        let RowPosition::Markdown(after) = viewport.top.unwrap().position else {
            panic!("assistant Markdown must retain a Markdown anchor");
        };

        assert!(after.output_offset <= before.output_offset);
        assert!(before.output_offset - after.output_offset < 5);
        assert!(after.output_offset > 0);
    }

    #[test]
    fn styled_markdown_wraps_on_grapheme_boundaries_across_spans() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.complete_message(1, "**ab界**cd".into());
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 4, 4);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let content = rows
            .iter()
            .filter(|row| row.kind == TranscriptRowKind::Content)
            .collect::<Vec<_>>();

        assert_eq!(content[0].plain_text(), "ab界");
        assert!(content[0].spans.iter().all(|span| span.style.strong));
        assert_eq!(content[1].plain_text(), "cd");
        assert!(content[1].spans.iter().all(|span| !span.style.strong));
    }

    #[test]
    fn highlighted_tabs_keep_terminal_safe_text_and_source_affinity() {
        let source = "```rust\nfn\tmain() {}\n```";
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.complete_message(1, source.into());
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 40, 8);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let code = rows
            .iter()
            .find(|row| {
                row.role == TranscriptRole::Assistant
                    && row.kind == TranscriptRowKind::Content
                    && row.plain_text().contains("main")
            })
            .unwrap();

        assert_eq!(code.plain_text(), "fn  main() {}");
        assert!(!code.plain_text().contains('\t'));
        assert!(
            code.spans
                .iter()
                .any(|span| span.style.syntax == crate::syntax::SyntaxClass::Keyword)
        );
        let expanded_tab = code.spans.iter().find(|span| span.text == "  ").unwrap();
        assert_eq!(
            &source[expanded_tab.affinity.source_offset..expanded_tab.affinity.source_end],
            "\t"
        );
    }

    #[test]
    fn closing_and_promoting_a_fence_invalidates_uniform_cached_rows() {
        let mut transcript = Transcript::default();
        let (_, assistant) = transcript.append_exchange("prompt".into());
        transcript.start_message(1);
        transcript.append_message_delta(1, "before\n\n```rust\nfn main() {}\n");
        let mut cache = TranscriptRowCache::default();
        let entry = transcript.entry(assistant).unwrap();
        let snapshot = cache.markdown_snapshot(entry);
        let code_start = snapshot.document.plain_text().find("fn main").unwrap();
        let anchor = RowAnchor {
            entry_id: assistant,
            position: RowPosition::Markdown(snapshot.position_at(code_start)),
        };
        let uniform = cache.row_at(&transcript, anchor, 80).unwrap().row;
        assert!(
            uniform
                .spans
                .iter()
                .all(|span| span.style.syntax == crate::syntax::SyntaxClass::Plain)
        );

        transcript.append_message_delta(1, "```\n\nafter");
        cache.reset_work();
        let highlighted = cache.row_at(&transcript, anchor, 80).unwrap().row;
        assert!(
            highlighted
                .spans
                .iter()
                .any(|span| span.style.syntax != crate::syntax::SyntaxClass::Plain)
        );
        assert_eq!(cache.work().cache_hits, 0);
        assert_eq!(cache.work().syntax_fences_highlighted, 1);

        transcript.append_message_delta(1, " grows");
        cache.reset_work();
        let _ = cache.row_at(&transcript, anchor, 80).unwrap();
        assert_eq!(cache.work().syntax_fences_considered, 0);
        assert_eq!(cache.work().syntax_source_bytes, 0);
    }

    #[test]
    fn promoting_an_already_highlighted_fence_reuses_cached_rows() {
        let mut transcript = Transcript::default();
        let (_, assistant) = transcript.append_exchange("prompt".into());
        transcript.start_message(1);
        transcript.append_message_delta(1, "before\n\n```rust\nfn main() {}\n```");
        let mut cache = TranscriptRowCache::default();
        let entry = transcript.entry(assistant).unwrap();
        let snapshot = cache.markdown_snapshot(entry);
        let code_start = snapshot.document.plain_text().find("fn main").unwrap();
        let anchor = RowAnchor {
            entry_id: assistant,
            position: RowPosition::Markdown(snapshot.position_at(code_start)),
        };
        let highlighted = cache.row_at(&transcript, anchor, 80).unwrap().row;
        assert!(
            highlighted
                .spans
                .iter()
                .any(|span| span.style.syntax != crate::syntax::SyntaxClass::Plain)
        );

        transcript.append_message_delta(1, "\n\nafter");
        cache.reset_work();
        let promoted = cache.row_at(&transcript, anchor, 80).unwrap().row;

        assert_eq!(promoted, highlighted);
        assert_eq!(cache.work().cache_hits, 1);
        assert_eq!(cache.work().syntax_fences_highlighted, 1);
    }

    #[test]
    fn markdown_checkpoint_transitions_invalidate_cached_rows() {
        let mut transcript = Transcript::default();
        let (_, assistant) = transcript.append_exchange("prompt".into());
        transcript.start_message(1);
        transcript.append_message_delta(1, &"**bold** ".repeat(800));
        let anchor = RowAnchor {
            entry_id: assistant,
            position: RowPosition::Markdown(MarkdownPosition {
                source_offset: 0,
                output_offset: 0,
            }),
        };
        let mut cache = TranscriptRowCache::default();

        let parsed = cache
            .row_at(&transcript, anchor, 80)
            .unwrap()
            .row
            .plain_text();
        assert!(!parsed.contains("**bold**"));

        transcript.append_message_delta(1, &"**bold** ".repeat(400));
        let checkpointed = cache
            .row_at(&transcript, anchor, 80)
            .unwrap()
            .row
            .plain_text();
        assert!(checkpointed.contains("**bold**"));

        let authoritative = transcript.entry(assistant).unwrap().content.clone();
        transcript.complete_message(1, authoritative);
        let settled = cache
            .row_at(&transcript, anchor, 80)
            .unwrap()
            .row
            .plain_text();
        assert!(!settled.contains("**bold**"));
    }

    #[test]
    fn markdown_state_eviction_cannot_reuse_a_checkpoint_identity() {
        let mut transcript = Transcript::default();
        let (_, assistant) = transcript.append_exchange("prompt".into());
        transcript.start_message(1);
        transcript.append_message_delta(1, &"**bold** ".repeat(1_200));
        let anchor = RowAnchor {
            entry_id: assistant,
            position: RowPosition::Markdown(MarkdownPosition {
                source_offset: 0,
                output_offset: 0,
            }),
        };
        let mut cache = TranscriptRowCache::default();
        let checkpointed = cache
            .row_at(&transcript, anchor, 80)
            .unwrap()
            .row
            .plain_text();
        assert!(checkpointed.contains("**bold**"));

        let markdown_key = MarkdownKey {
            entry_id: assistant,
            layout_epoch: transcript.entry(assistant).unwrap().layout_epoch(),
            presentation_start: 0,
        };
        let removed = cache.markdown.remove(&markdown_key).unwrap();
        cache.markdown_retained_bytes = cache
            .markdown_retained_bytes
            .saturating_sub(removed.retained_bytes);
        cache
            .markdown_order
            .retain(|candidate| candidate != &markdown_key);
        cache.reset_work();
        let rebuilt = cache
            .row_at(&transcript, anchor, 80)
            .unwrap()
            .row
            .plain_text();
        assert!(rebuilt.contains("**bold**"));
        assert_eq!(cache.work().rows_built, 1);
        assert_eq!(cache.work().cache_hits, 0);

        transcript.append_message_delta(1, "\n\n[label]: /target\n\n[linked][label]");
        let authoritative = transcript.entry(assistant).unwrap().content.clone();
        transcript.complete_message(1, authoritative);
        let settled = cache
            .row_at(&transcript, anchor, 80)
            .unwrap()
            .row
            .plain_text();

        assert!(!settled.contains("**bold**"));
    }

    #[test]
    fn markdown_resize_rewraps_without_reparsing_source() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.complete_message(
            1,
            "# Heading\n\nUse **bold text** and `inline code` across a long line.\n\n```rust\nfn demo() {}\n```".into(),
        );
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 40, 8);
        let _ = viewport.visible_rows(&transcript, &mut cache);
        cache.reset_work();

        viewport.set_geometry(&transcript, &mut cache, 20, 8);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let work = cache.work();

        assert!(!rows.is_empty());
        assert_eq!(work.markdown_source_bytes_parsed, 0);
        assert_eq!(work.markdown_blocks_built, 0);
        assert_eq!(work.syntax_fences_considered, 0);
        assert_eq!(work.syntax_source_bytes, 0);
    }

    #[test]
    fn markdown_rows_style_content_and_neutralize_untrusted_terminal_text() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.complete_message(
            1,
            "# Plan\n\n[`safe`](https://secret.example) **bold** <b>raw\u{1b}]52;c;owned\u{7}</b>\n\n```sh\nprintf ok\n```".into(),
        );
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 100, 20);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let text = rows
            .iter()
            .map(TranscriptRow::plain_text)
            .collect::<String>();

        assert!(text.contains("Plan"));
        assert!(text.contains("safe"));
        assert!(text.contains("printf ok"));
        assert!(!text.contains("https://secret.example"));
        assert!(
            text.chars()
                .all(|character| !terminal_control_character(character))
        );
        assert!(
            rows.iter()
                .flat_map(|row| &row.spans)
                .any(|span| matches!(span.style.block, crate::markdown::BlockStyle::Heading(1)))
        );
        assert!(
            rows.iter()
                .flat_map(|row| &row.spans)
                .any(|span| span.style.inline == crate::markdown::InlineStyle::Code)
        );
    }

    #[test]
    fn markdown_parser_cache_enforces_entry_and_byte_caps() {
        let mut transcript = Transcript::default();
        let mut assistant_ids = Vec::new();
        for turn in 0..200_u64 {
            let (_, assistant) = transcript.append_exchange(format!("prompt-{turn}"));
            transcript.complete_message(
                turn,
                format!("# Answer {turn}\n\n{}", "content ".repeat(2_500)),
            );
            assistant_ids.push(assistant);
        }
        let mut cache = TranscriptRowCache::default();
        for assistant in assistant_ids {
            let entry = transcript.entry(assistant).unwrap();
            let _ = cache.markdown_snapshot(entry);
        }

        assert!(cache.markdown.len() <= MARKDOWN_CACHE_MAX_ENTRIES);
        assert!(cache.markdown_retained_bytes <= MARKDOWN_CACHE_MAX_BYTES);
        assert!(cache.markdown_order.len() <= MARKDOWN_CACHE_MAX_ENTRIES);
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

        assert!(rows.iter().any(|row| row.plain_text().contains("TAIL")));
        assert!(rows.iter().all(|row| {
            row.plain_text()
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
        assert_eq!(rows[0].plain_text(), "界d");

        transcript.complete_message(2, "abc\td".into());
        let tab_rows = viewport.visible_rows(&transcript, &mut cache);
        assert_eq!(tab_rows.len(), 1);
        assert_eq!(tab_rows[0].plain_text(), "d");
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
            .map(TranscriptRow::plain_text)
            .collect::<Vec<_>>();

        assert!(content.iter().any(|row| row == "a   "));
        assert!(
            content
                .iter()
                .all(|row| UnicodeWidthStr::width(row.as_str()) <= 4)
        );
    }
}
