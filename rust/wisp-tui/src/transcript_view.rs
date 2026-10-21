//! Bounded styled projection and viewport navigation for transcripts.

use std::collections::{HashMap, HashSet, VecDeque, hash_map::Entry};
use std::sync::Arc;

use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;

use crate::markdown::{
    IncrementalMarkdownState, MarkdownDocument, MarkdownWork, SourceAffinity, TranscriptSpan,
    TranscriptSpanStyle,
};
use crate::tool_cards::{ProcessDisplayState, ToolStatus};
use crate::tool_detail::{
    DetailAvailability, DetailPresentationKind, DetailRow, DetailRowKind, ToolDetailPresentation,
};
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
const MARKDOWN_SOURCE_MAX_BYTES: usize = 64 * 1024;
#[cfg(test)]
const ENTRY_PRESENTATION_CHUNK_BYTES: usize = 4 * 1024;
const OVERSCAN_ROWS: usize = 8;
/// The marker that opens a tool or process card's action line. Its shape carries
/// the state, so it reads without colour: `✓` done, `✗` failed, `⊘` denied or
/// cancelled, `?` awaiting approval. In-progress cards keep a steady `●`, which the
/// renderer animates as a spinner while the run is active (a resumed session's
/// stale in-progress card stays a steady dot). Every glyph is one column wide.
pub(crate) fn status_marker(status: ToolStatus) -> &'static str {
    match status {
        ToolStatus::Done => "✓  ",
        ToolStatus::Error => "✗  ",
        ToolStatus::Denied | ToolStatus::Cancelled => "⊘  ",
        ToolStatus::AwaitingApproval => "?  ",
        ToolStatus::Requested | ToolStatus::Running => "●  ",
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum RowPosition {
    Header,
    Thought,
    ThoughtContent(usize),
    Content(usize),
    Markdown(MarkdownPosition),
    Card(CardPosition),
    Spacer,
    /// Highlighted padding above the next user's prompt. The preceding entry owns it so its
    /// `Spacer` stays a plain gap and every entry still starts at `Header`.
    UserLeadIn,
    /// A plain gap below a prompt's highlighted surface, before the reply or card that follows.
    /// The prompt owns it, after its `Spacer`, which is the surface's bottom padding.
    UserLeadOut,
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum CardSection {
    Action,
    Metadata,
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

/// Groups and their first member share an entry ID but have independent controls.
#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
pub enum BrowseTarget {
    Entry(TranscriptEntryId),
    Group(TranscriptEntryId),
}

impl BrowseTarget {
    pub fn entry_id(self) -> TranscriptEntryId {
        match self {
            Self::Entry(id) | Self::Group(id) => id,
        }
    }

    pub fn for_row(row: &TranscriptRow) -> Self {
        if row.kind == TranscriptRowKind::CardGroup {
            Self::Group(row.anchor.entry_id)
        } else {
            Self::Entry(row.anchor.entry_id)
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TranscriptRowKind {
    Header,
    Content,
    Placeholder,
    Thought,
    CardAction,
    CardGroup,
    CardDetail,
    CardOmission,
    Spacer,
    /// The gap between back-to-back collapsed cards, drawn as a thin rule. Like a spacer it
    /// holds no text, so a copied selection gets an empty line.
    Rule,
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
    DiffAddition,
    DiffDeletion,
}

/// The result connector that opens the first row under a card's action line.
pub(crate) const RESULT_CONNECTOR: &str = "└ ";
/// What later rows under a card start with, so they align under the connector.
pub(crate) const RESULT_INDENT: &str = "  ";
/// The action columns a card must keep before its row also reserves a fold hint.
const ACTION_COLUMNS_BESIDE_HINT: usize = 24;

/// What the selected row says Right or Left, or a click, would do. Browse marks its
/// selection only with this hint, so the row otherwise looks like any other.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum FoldHint {
    #[default]
    None,
    /// Right shows hidden rows or the full action line.
    Expand,
    /// Left folds the row back.
    Collapse,
    /// `Expand`, worded for a terminal where a click also folds rows.
    ClickExpand,
    /// `Collapse`, worded for a terminal where a click also folds rows.
    ClickCollapse,
}

impl FoldHint {
    pub fn text(self) -> Option<&'static str> {
        match self {
            Self::None => None,
            Self::Expand => Some(" (→ to expand)"),
            Self::Collapse => Some(" (← to collapse)"),
            Self::ClickExpand => Some(" (click to expand)"),
            Self::ClickCollapse => Some(" (click to collapse)"),
        }
    }

    fn width(self) -> usize {
        self.text().map_or(0, UnicodeWidthStr::width)
    }
}

/// A muted gutter the renderer paints before a card's detail or omission row.
#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub enum RowGutter {
    #[default]
    None,
    /// `RESULT_CONNECTOR`, on the first row under the action line.
    Connector,
    /// `RESULT_INDENT`, on every later detail or omission row.
    Indent,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TranscriptRow {
    pub anchor: RowAnchor,
    pub role: TranscriptRole,
    pub kind: TranscriptRowKind,
    pub tone: TranscriptRowTone,
    pub spans: Vec<TranscriptSpan>,
    /// The projection reserves the gutter's and the hint's columns when it wraps the
    /// row, so `spans` and anchors still address only source text.
    pub gutter: RowGutter,
    /// What Right or Left would do to this row. A card's action row reserves the
    /// hint's columns whether or not it is selected, so moving the selection never
    /// rewraps a row; the renderer shows the hint only on the selected row.
    pub fold_hint: FoldHint,
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
            gutter: RowGutter::None,
            fold_hint: FoldHint::None,
        }
    }
}

fn default_row_tone(role: TranscriptRole, kind: TranscriptRowKind) -> TranscriptRowTone {
    match kind {
        TranscriptRowKind::Placeholder | TranscriptRowKind::Thought => TranscriptRowTone::Muted,
        TranscriptRowKind::CardGroup => TranscriptRowTone::Pending,
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
#[cfg_attr(feature = "transcript-benchmark", derive(serde::Serialize))]
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

#[derive(Clone, Debug, Default)]
pub struct FoldState {
    expanded: HashSet<TranscriptEntryId>,
    expanded_groups: HashSet<TranscriptEntryId>,
    /// Whether a click folds rows, so fold hints say "click" instead of naming the arrow
    /// keys. It changes the hints' reserved width, so a change rewraps like a fold.
    click_hints: bool,
    generation: u64,
}

impl FoldState {
    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn set_click_hints(&mut self, click_hints: bool) {
        if self.click_hints != click_hints {
            self.click_hints = click_hints;
            self.bump();
        }
    }

    fn expand_hint(&self) -> FoldHint {
        if self.click_hints {
            FoldHint::ClickExpand
        } else {
            FoldHint::Expand
        }
    }

    fn collapse_hint(&self) -> FoldHint {
        if self.click_hints {
            FoldHint::ClickCollapse
        } else {
            FoldHint::Collapse
        }
    }

    pub fn clear(&mut self) {
        if self.expanded.is_empty() && self.expanded_groups.is_empty() {
            return;
        }
        self.expanded.clear();
        self.expanded_groups.clear();
        self.bump();
    }

    pub fn is_expanded(&self, id: TranscriptEntryId) -> bool {
        self.expanded.contains(&id)
    }

    pub fn expand(&mut self, id: TranscriptEntryId) {
        if self.expanded.insert(id) {
            self.bump();
        }
    }

    pub fn collapse(&mut self, id: TranscriptEntryId) {
        if self.expanded.remove(&id) {
            self.bump();
        }
    }

    pub fn is_group_expanded(&self, start: TranscriptEntryId) -> bool {
        self.expanded_groups.contains(&start)
    }

    /// A completion may prepend calls to an open group. Preserve its expansion by
    /// recognizing the previous start anywhere in the current run.
    pub fn is_run_expanded(&self, members: &[TranscriptEntryId]) -> bool {
        members.iter().any(|id| self.is_group_expanded(*id))
    }

    pub fn expand_group(&mut self, start: TranscriptEntryId) {
        if self.expanded_groups.insert(start) {
            self.bump();
        }
    }

    pub fn collapse_group(&mut self, members: &[TranscriptEntryId]) {
        let mut changed = false;
        for id in members {
            changed |= self.expanded_groups.remove(id);
        }
        if changed {
            self.bump();
        }
    }

    fn bump(&mut self) {
        self.generation = self.generation.wrapping_add(1);
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct RowKey {
    markdown: bool,
    entry_id: TranscriptEntryId,
    layout_epoch: u64,
    width: usize,
    position: RowPosition,
    fold_generation: u64,
}

#[derive(Clone, Debug)]
pub(crate) struct CachedRow {
    pub row: TranscriptRow,
    pub next: Option<RowAnchor>,
    entry_revision: u64,
    content_len: usize,
    markdown_presentation_identity: Option<u64>,
    /// What a collapsed MCP group row showed when it was built; see `McpGroupHeader`.
    group_signature: Option<u64>,
}

struct ProjectedRow {
    row: TranscriptRow,
    next: Option<RowAnchor>,
}

#[derive(Debug)]
struct CardDetailTone {
    start: usize,
    end: usize,
    tone: TranscriptRowTone,
}

/// How a card text row styles its source text: the action line's status, name,
/// argument and summary ranges, or the changed-word ranges of a diff preview.
#[derive(Clone, Copy)]
struct CardRowStyle<'a> {
    marker_end: usize,
    label_end: usize,
    argument_start: usize,
    argument_style: crate::markdown::InlineStyle,
    suffix_start: usize,
    emphasis: &'a [std::ops::Range<usize>],
    syntax: &'a [crate::syntax::SyntaxToken],
    gutters: &'a [std::ops::Range<usize>],
}

impl CardRowStyle<'_> {
    const PLAIN: CardRowStyle<'static> = CardRowStyle {
        marker_end: 0,
        label_end: 0,
        argument_start: usize::MAX,
        argument_style: crate::markdown::InlineStyle::Normal,
        suffix_start: usize::MAX,
        emphasis: &[],
        syntax: &[],
        gutters: &[],
    };

    fn inline_at(&self, kind: TranscriptRowKind, offset: usize) -> crate::markdown::InlineStyle {
        use crate::markdown::InlineStyle;
        if !matches!(
            kind,
            TranscriptRowKind::CardAction | TranscriptRowKind::CardGroup
        ) {
            if let Some(range) = self.gutters.iter().find(|range| range.contains(&offset)) {
                return if offset + 2 >= range.end {
                    InlineStyle::DiffSign
                } else {
                    InlineStyle::DiffGutter
                };
            }
            return if self.emphasis.iter().any(|range| range.contains(&offset)) {
                InlineStyle::DiffChange
            } else {
                InlineStyle::Normal
            };
        }
        if offset < self.marker_end {
            InlineStyle::ToolStatus
        } else if offset < self.label_end {
            InlineStyle::ToolName
        } else if offset >= self.suffix_start {
            InlineStyle::Normal
        } else if offset >= self.argument_start {
            self.argument_style
        } else {
            InlineStyle::Normal
        }
    }
}

#[derive(Debug)]
struct CardProjection {
    action: String,
    action_marker_end: usize,
    action_label_end: usize,
    action_argument_start: usize,
    action_argument_style: crate::markdown::InlineStyle,
    metadata: String,
    detail: String,
    detail_base: usize,
    detail_tones: Vec<CardDetailTone>,
    /// Byte ranges of `detail` holding changed words in a diff preview.
    detail_emphasis: Vec<std::ops::Range<usize>>,
    detail_syntax: Vec<crate::syntax::SyntaxToken>,
    detail_gutters: Vec<std::ops::Range<usize>>,
    /// Where a status note or a diff's `+N -M` summary starts in `action`. It is
    /// styled like plain text and kept whole when the row is truncated.
    action_suffix_start: usize,
    inline_detail: bool,
    omission: Option<String>,
    omission_before_detail: bool,
    action_tone: TranscriptRowTone,
}

impl CardProjection {
    fn retained_bytes(&self) -> usize {
        self.action
            .len()
            .saturating_add(self.detail.len())
            .saturating_add(self.metadata.len())
            .saturating_add(
                self.detail_tones
                    .len()
                    .saturating_mul(std::mem::size_of::<CardDetailTone>()),
            )
            .saturating_add(
                self.detail_emphasis
                    .len()
                    .saturating_mul(std::mem::size_of::<std::ops::Range<usize>>()),
            )
            .saturating_add(
                self.detail_syntax
                    .len()
                    .saturating_mul(std::mem::size_of::<crate::syntax::SyntaxToken>()),
            )
            .saturating_add(
                self.detail_gutters
                    .len()
                    .saturating_mul(std::mem::size_of::<std::ops::Range<usize>>()),
            )
            .saturating_add(self.omission.as_ref().map_or(0, String::len))
            .saturating_add(std::mem::size_of::<Self>())
    }

    fn detail_tone(&self, absolute_offset: usize) -> TranscriptRowTone {
        let offset = absolute_offset.saturating_sub(self.detail_base);
        self.detail_tones
            .iter()
            .find(|range| range.start <= offset && offset < range.end)
            .map_or(TranscriptRowTone::Default, |range| range.tone)
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, PartialEq)]
struct MarkdownKey {
    entry_id: TranscriptEntryId,
    layout_epoch: u64,
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
    parsed_document: MarkdownDocument,
    stable_blocks: usize,
    layout_width: Option<usize>,
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
    furthest_content: HashMap<(TranscriptEntryId, u64, bool, usize), RowAnchor>,
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
    fold: FoldState,
}

impl TranscriptRowCache {
    pub fn fold(&self) -> &FoldState {
        &self.fold
    }

    pub fn fold_mut(&mut self) -> &mut FoldState {
        &mut self.fold
    }

    fn row_key(&self, entry: &TranscriptEntry, width: usize, position: RowPosition) -> RowKey {
        RowKey {
            entry_id: entry.id,
            layout_epoch: entry.layout_epoch(),
            markdown: uses_markdown(entry),
            width: width.max(1),
            position,
            fold_generation: self.fold.generation(),
        }
    }

    #[cfg(any(test, feature = "transcript-benchmark"))]
    pub fn work(&self) -> LayoutWork {
        self.work
    }

    #[cfg(any(test, feature = "transcript-benchmark"))]
    pub fn reset_work(&mut self) {
        self.work = LayoutWork::default();
    }

    pub(crate) fn row_at(
        &mut self,
        transcript: &Transcript,
        anchor: RowAnchor,
        width: usize,
    ) -> Option<CachedRow> {
        let entry = transcript.entry(anchor.entry_id)?;
        let key = self.row_key(entry, width, anchor.position);
        let markdown_snapshot = uses_markdown(entry).then(|| self.markdown_snapshot(entry, width));
        if let Some(cached) = self.rows.get(&key).cloned() {
            if cached_row_valid(
                transcript,
                &self.fold,
                &cached,
                entry,
                markdown_snapshot.as_deref(),
            ) {
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
                if let Some(row) = self.build_group_row(transcript, entry, anchor, width) {
                    row
                } else {
                    self.build_card_row(transcript, entry, anchor, CardSection::Action, 0, width)?
                }
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
                    position: self.after_header_position(entry, width),
                }),
            },
            RowPosition::Thought if !entry.thinking().is_empty() => {
                self.build_thought_row(transcript, entry, anchor, width)
            }
            RowPosition::ThoughtContent(start)
                if self.fold.is_expanded(entry.id)
                    && start <= entry.thinking().len()
                    && entry.thinking().is_char_boundary(start) =>
            {
                self.build_thought_content_row(transcript, entry, anchor, start, width)
            }
            RowPosition::Content(start)
                if !uses_markdown(entry)
                    && start <= entry.display_content().len()
                    && entry.display_content().is_char_boundary(start) =>
            {
                self.build_content_row(transcript, entry, anchor, start, width)
            }
            RowPosition::Markdown(position) if uses_markdown(entry) => {
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
            RowPosition::Spacer if next_entry().is_some() => {
                let rule = transcript
                    .entry_after(entry.id)
                    .is_some_and(|next| cards_stack(entry, next))
                    && !self.shows_card_results(entry);
                ProjectedRow {
                    row: TranscriptRow::plain(
                        anchor,
                        entry.role,
                        if rule {
                            TranscriptRowKind::Rule
                        } else {
                            TranscriptRowKind::Spacer
                        },
                        String::new(),
                        entry.display_content().len(),
                    ),
                    next: spacer_next(transcript, entry),
                }
            }
            RowPosition::UserLeadIn if user_lead_in(transcript, entry) == Some(anchor) => {
                ProjectedRow {
                    row: TranscriptRow::plain(
                        anchor,
                        TranscriptRole::User,
                        TranscriptRowKind::Spacer,
                        String::new(),
                        entry.display_content().len(),
                    ),
                    next: next_entry(),
                }
            }
            RowPosition::UserLeadOut => {
                let next = transcript.entry_after(entry.id)?;
                if user_lead_out(transcript, entry) != Some(anchor) {
                    return None;
                }
                ProjectedRow {
                    row: TranscriptRow::plain(
                        anchor,
                        next.role,
                        TranscriptRowKind::Spacer,
                        String::new(),
                        entry.display_content().len(),
                    ),
                    next: next_entry(),
                }
            }
            _ => return None,
        };
        let group_signature = (projected.row.kind == TranscriptRowKind::CardGroup)
            .then(|| card_group_signature(transcript, entry))
            .flatten();
        Some(CachedRow {
            row: projected.row,
            next: projected.next,
            entry_revision: entry.revision(),
            content_len: entry.display_content().len(),
            markdown_presentation_identity,
            group_signature,
        })
    }

    fn position_for_offset(
        &mut self,
        entry: &TranscriptEntry,
        offset: usize,
        width: usize,
    ) -> RowPosition {
        if uses_markdown(entry) {
            RowPosition::Markdown(self.markdown_snapshot(entry, width).position_at(offset))
        } else {
            RowPosition::Content(offset)
        }
    }

    fn after_header_position(&mut self, entry: &TranscriptEntry, width: usize) -> RowPosition {
        if !entry.thinking().is_empty() {
            RowPosition::Thought
        } else {
            self.position_for_offset(entry, 0, width)
        }
    }

    fn after_thought_position(&mut self, entry: &TranscriptEntry, width: usize) -> RowPosition {
        self.position_for_offset(entry, 0, width)
    }

    fn build_thought_row(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        anchor: RowAnchor,
        width: usize,
    ) -> ProjectedRow {
        let line_count = entry
            .thinking()
            .lines()
            .filter(|line| !line.is_empty())
            .count();
        let mut text = if line_count > 1 {
            format!("thought · {line_count} lines")
        } else {
            "thought".into()
        };
        if let Some(milliseconds) = entry.thinking_ms() {
            text.push_str(" · ");
            text.push_str(&crate::tool_cards::format_duration(u64::from(milliseconds)));
        }
        let next = if self.fold.is_expanded(entry.id) && !entry.thinking().is_empty() {
            Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::ThoughtContent(0),
            })
        } else {
            Some(RowAnchor {
                entry_id: entry.id,
                position: self.after_thought_position(entry, width),
            })
            .filter(|_| {
                !entry.content.is_empty()
                    || entry.state != TranscriptEntryState::Complete
                    || separator_after(transcript, entry).is_some()
            })
            .or_else(|| separator_after(transcript, entry))
        };
        let next = next.or_else(|| {
            if entry.content.is_empty() {
                separator_after(transcript, entry)
            } else {
                Some(RowAnchor {
                    entry_id: entry.id,
                    position: self.after_thought_position(entry, width),
                })
            }
        });
        let _ = width;
        let mut row = TranscriptRow::plain(anchor, entry.role, TranscriptRowKind::Thought, text, 0);
        if !entry.thinking().is_empty() {
            row.fold_hint = if self.fold.is_expanded(entry.id) {
                self.fold.collapse_hint()
            } else {
                self.fold.expand_hint()
            };
        }
        ProjectedRow { row, next }
    }

    fn build_thought_content_row(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        anchor: RowAnchor,
        start: usize,
        width: usize,
    ) -> ProjectedRow {
        let content = entry.thinking();
        if start >= content.len() {
            return ProjectedRow {
                row: TranscriptRow::plain(
                    anchor,
                    entry.role,
                    TranscriptRowKind::Thought,
                    String::new(),
                    start,
                ),
                next: Some(RowAnchor {
                    entry_id: entry.id,
                    position: self.after_thought_position(entry, width),
                })
                .or_else(|| separator_after(transcript, entry)),
            };
        }
        let mut text = String::new();
        let mut column = 0_usize;
        let mut next_offset = None;
        let mut ended_with_break = false;
        for (relative_offset, grapheme) in content[start..].grapheme_indices(true) {
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
            Some(offset) if offset < content.len() => Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::ThoughtContent(offset),
            }),
            Some(offset) if ended_with_break && offset == content.len() => Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::ThoughtContent(offset),
            }),
            _ => Some(RowAnchor {
                entry_id: entry.id,
                position: self.after_thought_position(entry, width),
            }),
        };
        ProjectedRow {
            row: TranscriptRow::plain(anchor, entry.role, TranscriptRowKind::Thought, text, start),
            next,
        }
    }

    fn build_group_row(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        anchor: RowAnchor,
        width: usize,
    ) -> Option<ProjectedRow> {
        let (start, members) = card_run(transcript, entry.id)?;
        if start != entry.id {
            return None;
        }
        let expanded = self.fold.is_run_expanded(&members);
        let last = transcript.entry(*members.last()?)?;
        let next = if expanded {
            Some(card_action_anchor(entry.id))
        } else {
            separator_after(transcript, last)
        };
        let hint = if expanded {
            self.fold.collapse_hint()
        } else {
            self.fold.expand_hint()
        };
        let kind = card_group_key(entry)?;
        if !matches!(kind, CardGroupKey::Mcp(_)) {
            let tone = explore_group_tone(transcript, &members);
            let marker = status_marker(match tone {
                TranscriptRowTone::Success => ToolStatus::Done,
                TranscriptRowTone::Error => ToolStatus::Error,
                TranscriptRowTone::Warning => ToolStatus::Cancelled,
                _ => ToolStatus::Running,
            });
            let (label, unit) = match &kind {
                CardGroupKey::Explore => ("Explored".to_owned(), "actions"),
                CardGroupKey::Tool(name) => (
                    entry.tool_card()?.chip_label(),
                    if name == "bash" { "commands" } else { "calls" },
                ),
                CardGroupKey::Mcp(_) => unreachable!("MCP has its own header"),
            };
            let arrow = if expanded { "▾" } else { "▸" };
            let mut source = format!("{marker}{label}  {arrow} {} {unit}", members.len());
            let count_end = source.len();
            let (row_width, fold_hint) = if width >= ACTION_COLUMNS_BESIDE_HINT + hint.width() {
                (width - hint.width(), hint)
            } else {
                (width, FoldHint::None)
            };
            // An open group lists its members below, so only a closed one previews them.
            if !expanded {
                let budget = row_width.saturating_sub(self.action_columns(&source));
                source.push_str(&self.group_preview(transcript, &kind, &members, budget));
            }
            let spans = self.single_action_row_spans(
                &source,
                row_width,
                CardRowStyle {
                    marker_end: marker.len(),
                    label_end: marker.len() + label.len(),
                    argument_start: count_end,
                    argument_style: crate::markdown::InlineStyle::ToolArgument,
                    ..CardRowStyle::PLAIN
                },
                TranscriptRowKind::CardGroup,
            );
            let mut row = TranscriptRow::plain(
                anchor,
                TranscriptRole::Tool,
                TranscriptRowKind::CardGroup,
                String::new(),
                0,
            );
            row.spans = spans;
            row.tone = tone;
            row.fold_hint = fold_hint;
            return Some(ProjectedRow { row, next });
        }
        // A repeated MCP call reads like one card's action line, with a count after its
        // target, so it keeps the marker, chip and argument columns of the rows around it.
        let (row_width, fold_hint) = if width >= ACTION_COLUMNS_BESIDE_HINT + hint.width() {
            (width - hint.width(), hint)
        } else {
            (width, FoldHint::None)
        };
        let mut header = McpGroupHeader::new(transcript, &members, false)?;
        if self.action_columns(&header.source[..header.count_end]) > row_width {
            header = McpGroupHeader::new(transcript, &members, true)?;
        }
        let spans = self.single_action_row_spans(
            &header.source,
            row_width,
            header.style(),
            TranscriptRowKind::CardGroup,
        );
        let text = spans.iter().map(|span| span.text.as_str()).collect();
        let mut row =
            TranscriptRow::plain(anchor, entry.role, TranscriptRowKind::CardGroup, text, 0);
        row.spans = spans;
        row.tone = header.tone;
        row.fold_hint = fold_hint;
        Some(ProjectedRow { row, next })
    }

    fn build_content_row(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        anchor: RowAnchor,
        start: usize,
        width: usize,
    ) -> ProjectedRow {
        let content = entry.display_content();
        if content.is_empty() {
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
        if start == content.len() {
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
        let mut word_break = None::<(usize, usize)>;
        let mut previous_space = false;
        for (relative_offset, grapheme) in content[start..].grapheme_indices(true) {
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
            let space = entry.role == TranscriptRole::User
                && grapheme != "\t"
                && grapheme.chars().all(char::is_whitespace);
            if space && column > 0 {
                if previous_space {
                    if let Some((offset, _)) = &mut word_break {
                        *offset = absolute_offset + grapheme.len();
                    }
                } else {
                    word_break = Some((absolute_offset + grapheme.len(), text.len()));
                }
            }
            previous_space = space;
            if !text.is_empty() && column.saturating_add(grapheme_width) > width {
                if let Some((offset, text_len)) = word_break {
                    text.truncate(text_len);
                    next_offset = Some(offset);
                } else {
                    next_offset = Some(absolute_offset);
                }
                break;
            }
            text.push_str(&safe);
            column = column.saturating_add(grapheme_width);
        }

        let next = match next_offset {
            Some(offset) if offset < content.len() => Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Content(offset),
            }),
            Some(offset) if ended_with_break && offset == content.len() => Some(RowAnchor {
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
                let next = if self.fold.is_expanded(entry.id) || projection.inline_detail {
                    card_position_after_action(entry, &projection)
                        .or_else(|| separator_after(transcript, entry))
                } else {
                    separator_after(transcript, entry)
                };
                let style = CardRowStyle {
                    marker_end: projection.action_marker_end,
                    label_end: projection.action_label_end,
                    argument_start: projection.action_argument_start,
                    argument_style: projection.action_argument_style,
                    suffix_start: projection.action_suffix_start,
                    emphasis: &[],
                    syntax: &[],
                    gutters: &[],
                };
                // A collapsed card keeps its action on one row; expanding it (Right in
                // browse) wraps the full command or path instead and shows any hidden
                // rows. Only a card that hides something offers to expand.
                let expanded = self.fold.is_expanded(entry.id);
                let mut row_width = width;
                let mut fold_hint = FoldHint::None;
                if absolute_offset == 0 {
                    let hint = if expanded {
                        self.fold.collapse_hint()
                    } else if (!projection.inline_detail
                        && card_position_after_action(entry, &projection).is_some())
                        || self.action_columns(&projection.action) > width
                    {
                        self.fold.expand_hint()
                    } else {
                        FoldHint::None
                    };
                    if hint != FoldHint::None && width >= ACTION_COLUMNS_BESIDE_HINT + hint.width()
                    {
                        row_width = width - hint.width();
                        fold_hint = hint;
                    }
                }
                if absolute_offset == 0 && !expanded {
                    if let Some(mut projected) = self.build_truncated_action_row(
                        entry,
                        anchor,
                        &projection.action,
                        row_width,
                        projection.action_tone,
                        style,
                        next,
                    ) {
                        projected.row.fold_hint = fold_hint;
                        return Some(projected);
                    }
                }
                let mut projected = self.build_card_text_row(
                    entry,
                    anchor,
                    section,
                    &projection.action,
                    0,
                    absolute_offset,
                    row_width,
                    TranscriptRowKind::CardAction,
                    projection.action_tone,
                    style,
                    next,
                );
                projected.row.fold_hint = fold_hint;
                Some(projected)
            }
            CardSection::Metadata if !projection.metadata.is_empty() => {
                let next = card_position_after_metadata(entry, &projection)
                    .or_else(|| separator_after(transcript, entry));
                let mut projected = self.build_card_text_row(
                    entry,
                    anchor,
                    section,
                    &projection.metadata,
                    0,
                    absolute_offset,
                    result_width(width),
                    TranscriptRowKind::CardDetail,
                    TranscriptRowTone::Default,
                    CardRowStyle::PLAIN,
                    next,
                );
                projected.row.gutter = result_gutter(entry, &projection, anchor);
                Some(projected)
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
                let tone = projection.detail_tone(absolute_offset);
                let mut projected = self.build_card_text_row(
                    entry,
                    anchor,
                    section,
                    &projection.detail,
                    projection.detail_base,
                    absolute_offset,
                    result_width(width),
                    TranscriptRowKind::CardDetail,
                    tone,
                    CardRowStyle {
                        emphasis: &projection.detail_emphasis,
                        syntax: &projection.detail_syntax,
                        gutters: &projection.detail_gutters,
                        ..CardRowStyle::PLAIN
                    },
                    next,
                );
                projected.row.gutter = result_gutter(entry, &projection, anchor);
                Some(projected)
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
                let mut projected = self.build_card_text_row(
                    entry,
                    anchor,
                    section,
                    omission,
                    0,
                    absolute_offset,
                    result_width(width),
                    TranscriptRowKind::CardOmission,
                    TranscriptRowTone::Warning,
                    CardRowStyle::PLAIN,
                    next,
                );
                projected.row.gutter = result_gutter(entry, &projection, anchor);
                Some(projected)
            }
            CardSection::Detail | CardSection::Metadata => None,
        }
    }

    /// The display columns of an action line, or `usize::MAX` when it holds a line
    /// break and so can never fit one row.
    fn action_columns(&mut self, text: &str) -> usize {
        let mut column = 0_usize;
        for grapheme in text.graphemes(true) {
            self.work.graphemes_scanned = self.work.graphemes_scanned.saturating_add(1);
            self.work.bytes_scanned = self.work.bytes_scanned.saturating_add(grapheme.len());
            if is_line_break(grapheme) {
                return usize::MAX;
            }
            column += UnicodeWidthStr::width(sanitize_grapheme(grapheme, column).as_str());
        }
        column
    }

    /// Fit an action line into one row ending in `…`, or return `None` when it
    /// already fits. A diff's change-count suffix is kept whole and the text before
    /// it is cut, at a `/` or space when that keeps at least half the room.
    #[allow(clippy::too_many_arguments)]
    fn build_truncated_action_row(
        &mut self,
        entry: &TranscriptEntry,
        anchor: RowAnchor,
        source: &str,
        width: usize,
        tone: TranscriptRowTone,
        style: CardRowStyle<'_>,
        next: Option<RowAnchor>,
    ) -> Option<ProjectedRow> {
        if self.action_columns(source) <= width {
            return None;
        }
        let spans =
            self.single_action_row_spans(source, width, style, TranscriptRowKind::CardAction);
        let text = spans.iter().map(|span| span.text.as_str()).collect();
        let mut row =
            TranscriptRow::plain(anchor, entry.role, TranscriptRowKind::CardAction, text, 0);
        row.tone = tone;
        row.spans = spans;
        Some(ProjectedRow { row, next })
    }

    /// What a closed group ran, in order, as `" · first, second +2"`, fitted to
    /// `budget` columns. Members that do not fit are counted rather than cut, and the
    /// preview is left out when not even one member fits.
    fn group_preview(
        &mut self,
        transcript: &Transcript,
        kind: &CardGroupKey,
        members: &[TranscriptEntryId],
        budget: usize,
    ) -> String {
        let items = group_preview_items(transcript, kind, members);
        // Members whose arguments are unknown are still counted in `+N`, so the
        // preview always agrees with the header's count.
        let total = members.len();
        let mut preview = String::new();
        let mut shown = 0;
        for (index, item) in items.iter().enumerate() {
            let separator = if index == 0 { " · " } else { ", " };
            let item = self.clip_to_columns(item, GROUP_PREVIEW_ITEM_COLUMNS);
            let candidate = format!("{preview}{separator}{item}");
            let hidden = total - index - 1;
            let more = if hidden == 0 {
                String::new()
            } else {
                format!(" +{hidden}")
            };
            if self.action_columns(&candidate) + self.action_columns(&more) > budget {
                break;
            }
            preview = candidate;
            shown += 1;
        }
        if shown > 0 && shown < total {
            preview.push_str(&format!(" +{}", total - shown));
        }
        preview
    }

    /// Shorten `text` to at most `columns` display columns, ending in `…` when cut.
    fn clip_to_columns(&mut self, text: &str, columns: usize) -> String {
        if self.action_columns(text) <= columns {
            return text.to_owned();
        }
        let mut clipped = String::new();
        let mut used = 0;
        for grapheme in text.graphemes(true) {
            let width = UnicodeWidthStr::width(sanitize_grapheme(grapheme, used).as_str());
            if used + width + 1 > columns {
                break;
            }
            clipped.push_str(grapheme);
            used += width;
        }
        clipped.push('…');
        clipped
    }

    /// Style an action line as one row of at most `width` columns. A line that does not
    /// fit is cut before the suffix, at a path or word break when one is near, and an
    /// ellipsis stands for what was dropped; the suffix stays whole.
    fn single_action_row_spans(
        &mut self,
        source: &str,
        width: usize,
        style: CardRowStyle<'_>,
        kind: TranscriptRowKind,
    ) -> Vec<TranscriptSpan> {
        let fits = self.action_columns(source) <= width;
        let split = if fits {
            source.len()
        } else {
            style.suffix_start.min(source.len())
        };
        let (head, mut tail) = source.split_at(split);
        let mut kept = head.len();
        if !fits {
            let mut tail_width = self.action_columns(tail);
            if tail_width.saturating_add(2) > width {
                tail = "";
                tail_width = 0;
            }
            let budget = width.saturating_sub(tail_width + 1);

            kept = 0;
            let mut column = 0;
            let mut separator = None;
            for (offset, grapheme) in head.grapheme_indices(true) {
                let grapheme_width =
                    UnicodeWidthStr::width(sanitize_grapheme(grapheme, column).as_str());
                if is_line_break(grapheme) || column + grapheme_width > budget {
                    break;
                }
                column += grapheme_width;
                kept = offset + grapheme.len();
                if offset >= style.argument_start && matches!(grapheme, "/" | " ") {
                    separator = Some((kept, column));
                }
            }
            if let Some((end, separator_column)) = separator {
                if separator_column * 2 >= budget && end < kept {
                    kept = end;
                }
            }
        }
        let mut spans: Vec<TranscriptSpan> = Vec::new();
        let mut push = |text: &str, inline, start: usize, end: usize| {
            if let Some(last) = spans.last_mut().filter(|last| last.style.inline == inline) {
                last.text.push_str(text);
                last.affinity.source_end = end;
                return;
            }
            spans.push(TranscriptSpan {
                text: text.to_owned(),
                style: TranscriptSpanStyle {
                    inline,
                    ..TranscriptSpanStyle::default()
                },
                affinity: SourceAffinity {
                    source_offset: start,
                    source_end: end,
                    output_offset: 0,
                },
            });
        };
        let mut column = 0;
        for (offset, grapheme) in head[..kept].grapheme_indices(true) {
            let safe = sanitize_grapheme(grapheme, column);
            column += UnicodeWidthStr::width(safe.as_str());
            push(
                &safe,
                style.inline_at(kind, offset),
                offset,
                offset + grapheme.len(),
            );
        }
        // The ellipsis stands for the omitted source, so the row's spans stay contiguous.
        if !fits {
            push("…", crate::markdown::InlineStyle::Normal, kept, split);
        }
        for (offset, grapheme) in tail.grapheme_indices(true) {
            let safe = sanitize_grapheme(grapheme, column);
            column += UnicodeWidthStr::width(safe.as_str());
            push(
                &safe,
                crate::markdown::InlineStyle::Normal,
                split + offset,
                split + offset + grapheme.len(),
            );
        }
        spans
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
        style: CardRowStyle<'_>,
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
        let mut action_spans: Vec<TranscriptSpan> = Vec::new();
        let logical_start = source[..relative_start]
            .rfind('\n')
            .map_or(0, |index| index + 1);
        let indent = if relative_start > logical_start {
            style
                .gutters
                .iter()
                .find(|range| range.start == logical_start)
                .map_or(0, |range| range.len())
        } else {
            0
        };
        let indent = if width > indent + 4 { indent } else { 0 };
        let mut column = indent;
        if indent > 0 {
            let prefix = format!("{}↪ ", " ".repeat(indent.saturating_sub(2)));
            text.push_str(&prefix);
            action_spans.push(TranscriptSpan {
                text: prefix,
                style: TranscriptSpanStyle {
                    inline: crate::markdown::InlineStyle::DiffGutter,
                    ..TranscriptSpanStyle::default()
                },
                affinity: SourceAffinity {
                    source_offset: relative_start,
                    source_end: relative_start,
                    output_offset: 0,
                },
            });
        }
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
            if kind == TranscriptRowKind::CardAction
                || !style.emphasis.is_empty()
                || !style.syntax.is_empty()
                || !style.gutters.is_empty()
            {
                let inline = style.inline_at(kind, relative);
                let syntax = style
                    .syntax
                    .iter()
                    .find(|token| token.range.contains(&relative))
                    .map_or(crate::syntax::SyntaxClass::Plain, |token| token.class);
                if let Some(last) = action_spans
                    .last_mut()
                    .filter(|last| last.style.inline == inline && last.style.syntax == syntax)
                {
                    last.text.push_str(&safe);
                    last.affinity.source_end = relative + grapheme.len();
                } else {
                    action_spans.push(TranscriptSpan {
                        text: safe.clone(),
                        style: TranscriptSpanStyle {
                            inline,
                            syntax,
                            ..TranscriptSpanStyle::default()
                        },
                        affinity: SourceAffinity {
                            source_offset: relative,
                            source_end: relative + grapheme.len(),
                            output_offset: 0,
                        },
                    });
                }
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
        if !action_spans.is_empty() {
            row.spans = action_spans;
        }
        ProjectedRow { row, next }
    }

    fn markdown_snapshot(
        &mut self,
        entry: &TranscriptEntry,
        width: usize,
    ) -> Arc<MarkdownSnapshot> {
        let start = 0;
        let key = MarkdownKey {
            entry_id: entry.id,
            layout_epoch: entry.layout_epoch(),
        };
        if let Some(cached) = self.markdown.get(&key) {
            if cached.entry_revision == Some(entry.revision()) && cached.layout_width == Some(width)
            {
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
            let build = (cached.entry_revision != Some(entry.revision())).then(|| {
                cached.state.build(
                    &entry.markdown_source()[start..],
                    start,
                    entry.layout_epoch(),
                    entry.state == TranscriptEntryState::Complete,
                )
            });
            let presentation_changed = build.as_ref().is_some_and(|build| {
                cached
                    .state_presentation_epoch
                    .is_some_and(|epoch| epoch != build.presentation_epoch)
            }) || (cached
                .layout_width
                .is_some_and(|previous| previous != width)
                && cached.parsed_document.has_tables());
            if let Some(build) = &build {
                cached.state_presentation_epoch = Some(build.presentation_epoch);
            }
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
            let work = build
                .as_ref()
                .map_or(MarkdownWork::default(), |build| build.work);
            let parsed = build
                .as_ref()
                .map_or(&cached.parsed_document, |build| &build.document);
            let document = crate::markdown::layout_document(
                parsed,
                width,
                (cached.layout_width == Some(width))
                    .then_some((&cached.parsed_document, &cached.snapshot.document)),
            );
            if let Some(build) = build {
                cached.parsed_document = build.document;
                cached.stable_blocks = build.stable_blocks;
            }
            cached.snapshot = Arc::new(MarkdownSnapshot::new(
                document,
                cached.stable_blocks,
                cached.presentation_identity,
            ));
            cached.layout_width = Some(width);
            cached.entry_revision = Some(entry.revision());
            cached.retained_bytes = cached
                .snapshot
                .retained_bytes()
                .saturating_add(cached.parsed_document.retained_bytes());
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
        let snapshot = self.markdown_snapshot(entry, width);
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
        let mut word_break = None::<(usize, usize, usize)>;
        let mut previous_space = false;
        let mut row_full_width = false;
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
            for (span_index, span) in block.spans.iter().enumerate() {
                let span_end = span_start + span.text.len();
                if local >= span_end {
                    span_start = span_end;
                    continue;
                }
                let relative_start = local.saturating_sub(span_start);
                // Code and tables keep the full width from where they start in a row,
                // even after a list or quote marker; prose wraps at the measure. The
                // span decides this once, not per grapheme.
                let span_full_width = keeps_full_width(block, span_index, span);
                for (relative_offset, grapheme) in
                    span.text[relative_start..].grapheme_indices(true)
                {
                    self.work.graphemes_scanned = self.work.graphemes_scanned.saturating_add(1);
                    self.work.bytes_scanned =
                        self.work.bytes_scanned.saturating_add(grapheme.len());
                    let absolute_offset =
                        block_start + span_start + relative_start + relative_offset;
                    if is_line_break(grapheme) {
                        // An empty fenced-code line still owns a painted code surface.
                        if spans.is_empty() && span.style.block == crate::markdown::BlockStyle::Code
                        {
                            spans.push(TranscriptSpan {
                                text: String::new(),
                                style: span.style,
                                affinity: span.affinity,
                            });
                        }
                        cursor = absolute_offset + grapheme.len();
                        next_offset = Some(cursor);
                        ended_with_break = true;
                        break;
                    }
                    let safe = sanitize_grapheme(grapheme, column);
                    let grapheme_width = UnicodeWidthStr::width(safe.as_str());
                    let space = span.style.block != crate::markdown::BlockStyle::Code
                        && !matches!(
                            span.style.inline,
                            crate::markdown::InlineStyle::ListMarker
                                | crate::markdown::InlineStyle::TableBorder
                        )
                        && grapheme.chars().all(char::is_whitespace);
                    if space && column > 0 {
                        if previous_space {
                            if let Some((offset, _, _)) = &mut word_break {
                                *offset = absolute_offset + grapheme.len();
                            }
                        } else {
                            word_break = Some((
                                absolute_offset + grapheme.len(),
                                spans.len(),
                                spans
                                    .last()
                                    .map_or(0, |span: &TranscriptSpan| span.text.len()),
                            ));
                        }
                    }
                    previous_space = space;
                    row_full_width |= span_full_width;
                    let wrap_width = if row_full_width {
                        width
                    } else {
                        width.min(PROSE_COLUMNS)
                    };
                    if !spans.is_empty() && column.saturating_add(grapheme_width) > wrap_width {
                        if let Some((offset, count, last_len)) = word_break {
                            spans.truncate(count);
                            if let Some(last) = spans.last_mut() {
                                last.text.truncate(last_len);
                            }
                            next_offset = Some(offset);
                        } else {
                            next_offset = Some(absolute_offset);
                        }
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
                    if column >= width && span.style.block == crate::markdown::BlockStyle::Code {
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
                gutter: RowGutter::None,
                fold_hint: FoldHint::None,
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
            RowPosition::Header => {
                let previous = transcript.entry_before(entry.id)?;
                Some(if let Some(lead_in) = user_lead_in(transcript, previous) {
                    lead_in
                } else if let Some(lead_out) = user_lead_out(transcript, previous) {
                    lead_out
                } else {
                    // A collapsed group's gap belongs to its last member; walking back from
                    // that gap reaches the group's row.
                    RowAnchor {
                        entry_id: previous.id,
                        position: RowPosition::Spacer,
                    }
                })
            }
            RowPosition::Thought => Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Header,
            }),
            RowPosition::ThoughtContent(_) => {
                self.thought_anchor_before(transcript, entry, anchor, width)
            }
            RowPosition::Content(offset) => {
                let start = 0;
                if offset <= start {
                    return Some(self.before_body_anchor(transcript, entry, width));
                }
                self.content_anchor_before(transcript, entry, offset, width)
            }
            RowPosition::Markdown(position) => {
                if position.output_offset == 0 {
                    return Some(self.before_body_anchor(transcript, entry, width));
                }
                self.content_anchor_before(transcript, entry, position.output_offset, width)
            }
            RowPosition::Card(_) => self.card_anchor_before(transcript, entry, anchor, width),
            RowPosition::UserLeadIn | RowPosition::UserLeadOut => Some(RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Spacer,
            }),
            RowPosition::Spacer => {
                if let Some(start) = collapsed_group_start(&self.fold, transcript, entry.id) {
                    Some(RowAnchor {
                        entry_id: start,
                        position: RowPosition::Header,
                    })
                } else {
                    Some(self.last_anchor(transcript, entry, width))
                }
            }
        }
    }

    /// Whether `entry` is a card showing result rows, expanded or as an inline diff preview.
    /// Its last row is then a result row, which links on to a spacer rather than the next card.
    fn shows_card_results(&mut self, entry: &TranscriptEntry) -> bool {
        let Some(projection) = self.card_snapshot(entry) else {
            return false;
        };
        (self.fold.is_expanded(entry.id) || projection.inline_detail)
            && card_position_after_action(entry, &projection).is_some()
    }

    fn content_anchor_before(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        target: usize,
        width: usize,
    ) -> Option<RowAnchor> {
        let content_start = 0;
        let width = width.max(1);
        let target_position = self.position_for_offset(entry, target, width);
        let target_key = self.row_key(entry, width, target_position);
        if let Some(previous) = self.predecessors.get(&target_key).copied() {
            return Some(previous);
        }
        let furthest_key = (entry.id, entry.layout_epoch(), uses_markdown(entry), width);
        let candidate_start = self
            .furthest_content
            .get(&furthest_key)
            .and_then(|anchor| {
                if matches!(anchor.position, RowPosition::Markdown(_)) != uses_markdown(entry) {
                    return None;
                }
                let offset = row_position_offset(anchor.position)?;
                (content_start..target).contains(&offset).then_some(offset)
            })
            .unwrap_or(content_start);
        let mut anchor = RowAnchor {
            entry_id: entry.id,
            position: self.position_for_offset(entry, candidate_start, width),
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
        let target_key = self.row_key(entry, width, target.position);
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

    fn before_body_anchor(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        width: usize,
    ) -> RowAnchor {
        if entry.thinking().is_empty() {
            return RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Header,
            };
        }
        if !self.fold.is_expanded(entry.id) {
            return RowAnchor {
                entry_id: entry.id,
                position: RowPosition::Thought,
            };
        }
        let mut current = RowAnchor {
            entry_id: entry.id,
            position: RowPosition::Thought,
        };
        loop {
            let Some(cached) = self.row_at(transcript, current, width) else {
                return current;
            };
            let Some(next) = cached.next else {
                return current;
            };
            match next.position {
                RowPosition::Thought | RowPosition::ThoughtContent(_) => current = next,
                _ => return current,
            }
        }
    }

    fn thought_anchor_before(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        target: RowAnchor,
        width: usize,
    ) -> Option<RowAnchor> {
        let mut current = RowAnchor {
            entry_id: entry.id,
            position: RowPosition::Thought,
        };
        loop {
            let cached = self.row_at(transcript, current, width)?;
            let next = cached.next?;
            if next == target {
                return Some(current);
            }
            match next.position {
                RowPosition::Thought | RowPosition::ThoughtContent(_) => current = next,
                _ => return Some(current),
            }
        }
    }

    fn last_anchor(
        &mut self,
        transcript: &Transcript,
        entry: &TranscriptEntry,
        width: usize,
    ) -> RowAnchor {
        if !matches!(entry.kind, TranscriptEntryKind::Message) {
            if let Some(start) = collapsed_group_start(&self.fold, transcript, entry.id) {
                return RowAnchor {
                    entry_id: start,
                    position: RowPosition::Header,
                };
            }
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
        let (target, ends_with_break) = if uses_markdown(entry) {
            let snapshot = self.markdown_snapshot(entry, width);
            (snapshot.output_len, snapshot.ends_with_line_break())
        } else {
            let content = entry.display_content();
            (content.len(), ends_with_line_break(content))
        };
        if target == 0 || ends_with_break {
            return RowAnchor {
                entry_id: entry.id,
                position: self.position_for_offset(entry, target, width),
            };
        }
        self.content_anchor_before(transcript, entry, target, width)
            .unwrap_or(RowAnchor {
                entry_id: entry.id,
                position: self.position_for_offset(entry, 0, width),
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
            let furthest_key = (key.entry_id, key.layout_epoch, key.markdown, key.width);
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

    /// Keep surviving entry interiors while rebuilding links across history boundaries.
    ///
    /// A history page splices into the transcript and leaves surviving entries with their
    /// identifiers intact, so their interior rows, markdown snapshots, and card projections stay
    /// valid. Terminal rows and separators depend on neighboring entries, whose identities can
    /// change without revising the surviving entry.
    /// Rebuilding the whole cache on every page instead re-laid out the viewport at exactly the
    /// moment a paging round trip had already stalled scrolling.
    pub(crate) fn retain_entries(&mut self, transcript: &Transcript) {
        let live = |entry_id| transcript.entry(entry_id).is_some();
        self.rows.retain(|key, cached| {
            live(key.entry_id)
                && cached.next.is_some_and(|next| {
                    next.entry_id == key.entry_id
                        && !matches!(
                            next.position,
                            RowPosition::Spacer
                                | RowPosition::UserLeadIn
                                | RowPosition::UserLeadOut
                        )
                })
        });
        let rows = &self.rows;
        self.predecessors.retain(|key, previous| {
            rows.contains_key(&RowKey {
                position: previous.position,
                ..*key
            })
        });
        self.furthest_content.retain(|key, _| live(key.0));
        self.markdown.retain(|key, _| live(key.entry_id));
        self.cards.retain(|key, _| live(key.entry_id));
        let rows = &self.rows;
        self.insertion_order.retain(|key| rows.contains_key(key));
        let markdown = &self.markdown;
        self.markdown_order.retain(|key| markdown.contains_key(key));
        let cards = &self.cards;
        self.card_order.retain(|key| cards.contains_key(key));
        self.retained_bytes = self
            .rows
            .values()
            .map(|cached| cached.row.retained_bytes())
            .sum();
        self.markdown_retained_bytes = self
            .markdown
            .values()
            .map(|entry| entry.retained_bytes)
            .sum();
        self.card_retained_bytes = self.cards.values().map(|card| card.retained_bytes()).sum();
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
        let furthest_key = (key.entry_id, key.layout_epoch, key.markdown, key.width);
        if self.furthest_content.get(&furthest_key) == Some(&cached.row.anchor) {
            self.furthest_content.remove(&furthest_key);
        }
    }
}

fn process_marker(card: &crate::tool_cards::ProcessCardSnapshot) -> &'static str {
    status_marker(card.display_state.status())
}

fn tool_action_projection(
    card: &crate::tool_cards::ToolCardSnapshot,
) -> (String, usize, usize, usize, crate::markdown::InlineStyle) {
    let marker = status_marker(card.status);
    let label_end = marker.len() + card.chip_label().len();
    let argument_start = if card.mcp_target().is_some()
        || (card.arguments_available && !card.action_arguments.is_empty())
    {
        label_end.saturating_add(2)
    } else {
        usize::MAX
    };
    let argument_style = match card.name.as_str() {
        "bash" => crate::markdown::InlineStyle::ToolCommand,
        "read" | "ls" | "edit" | "write" => crate::markdown::InlineStyle::ToolPath,
        _ => crate::markdown::InlineStyle::ToolArgument,
    };
    (
        format!(
            "{marker}{}",
            if card.mcp_target().is_none() && card.arguments_available {
                let arguments = card.display_arguments();
                if arguments.is_empty() {
                    card.chip_label()
                } else {
                    format!("{}  {arguments}", card.chip_label())
                }
            } else {
                card.action()
            }
        ),
        marker.len(),
        label_end,
        argument_start,
        argument_style,
    )
}

/// Close a finished call's action line with how long it ran, such as ` · 4m 12s`.
/// Returns where the duration starts, so a truncated row keeps it whole, or
/// `usize::MAX` without one.
fn push_duration(action: &mut String, card: &crate::tool_cards::ToolCardSnapshot) -> usize {
    let Some(duration) = card.duration_label() else {
        return usize::MAX;
    };
    let start = action.len();
    action.push_str(" · ");
    action.push_str(&duration);
    start
}

/// Close an action line with its status note, such as ` · denied`. Returns where the
/// note starts, so a truncated row keeps it whole, or `usize::MAX` without one.
fn push_status_note(action: &mut String, note: Option<&str>) -> usize {
    let Some(note) = note else {
        return usize::MAX;
    };
    let start = action.len();
    action.push_str(" · ");
    action.push_str(note);
    start
}

fn card_projection(entry: &TranscriptEntry) -> Option<CardProjection> {
    match &entry.kind {
        TranscriptEntryKind::Message => None,
        TranscriptEntryKind::Tool(card) => {
            let (
                mut action,
                action_marker_end,
                action_label_end,
                action_argument_start,
                action_argument_style,
            ) = tool_action_projection(card);
            let note_start = push_status_note(&mut action, card.status_note());
            if let DetailAvailability::LiveRetained(detail) = &card.structured_detail {
                let diff = matches!(
                    detail.kind,
                    DetailPresentationKind::DiffCreate | DetailPresentationKind::DiffModify
                );
                let mut action_suffix_start = note_start;
                let (preview, detail_tones, detail_emphasis, detail_syntax, detail_gutters) =
                    if diff {
                        // The action line already names the file; carry the change counts
                        // there instead of a separate `M path +N -M` heading row.
                        action_suffix_start = action_suffix_start.min(action.len());
                        if detail.kind == DetailPresentationKind::DiffCreate {
                            action.push_str("  new file");
                        }
                        if !detail.summary.is_empty() {
                            action.push_str("  ");
                            action.push_str(&detail.summary);
                        }
                        let preview = crate::diff_card::preview(detail);
                        let tones = preview
                            .tones
                            .into_iter()
                            .map(|(range, tone)| CardDetailTone {
                                start: range.start,
                                end: range.end,
                                tone,
                            })
                            .collect();
                        (
                            preview.text,
                            tones,
                            preview.emphasis,
                            preview.syntax,
                            preview.gutters,
                        )
                    } else {
                        let (preview, tones) = structured_card_preview(detail);
                        (preview, tones, Vec::new(), Vec::new(), Vec::new())
                    };
                action_suffix_start = action_suffix_start.min(push_duration(&mut action, card));
                return Some(CardProjection {
                    action,
                    action_marker_end,
                    action_label_end,
                    action_argument_start,
                    action_argument_style,
                    metadata: String::new(),
                    detail: preview,
                    detail_base: 0,
                    detail_tones,
                    detail_emphasis,
                    detail_syntax,
                    detail_gutters,
                    action_suffix_start,
                    inline_detail: diff,
                    omission: None,
                    omission_before_detail: false,
                    action_tone: tool_status_tone(card.status),
                });
            }
            let tail_preview = card.status == ToolStatus::Error
                || (card.status == ToolStatus::Done && card.name == "bash");
            let retained_preview = if tail_preview {
                card.retained_output.preview_tail()
            } else {
                card.retained_output.preview_head()
            };
            let mut metadata = if card.detail == retained_preview.text {
                String::new()
            } else {
                card.detail.clone()
            };
            if card.display_arguments() != card.action_arguments {
                metadata = if metadata.is_empty() {
                    card.action_arguments.clone()
                } else {
                    format!("{}\n{metadata}", card.action_arguments)
                };
            }
            let detail = retained_preview.text.clone();
            let shown_output_bytes = retained_preview.text.len() as u64;
            let omitted = card
                .retained_output
                .source_bytes
                .saturating_sub(shown_output_bytes);
            let omission = card_omission(omitted, card.backend_truncated, tail_preview);
            let duration_start = push_duration(&mut action, card);
            Some(CardProjection {
                action,
                action_marker_end,
                action_label_end,
                action_argument_start,
                action_argument_style,
                metadata,
                detail,
                detail_base: if tail_preview {
                    usize::try_from(retained_preview.base_offset).unwrap_or(usize::MAX)
                } else {
                    0
                },
                detail_tones: Vec::new(),
                detail_emphasis: Vec::new(),
                detail_syntax: Vec::new(),
                detail_gutters: Vec::new(),
                action_suffix_start: note_start.min(duration_start),
                inline_detail: false,
                omission,
                omission_before_detail: tail_preview,
                action_tone: tool_status_tone(card.status),
            })
        }
        TranscriptEntryKind::Process(card) => {
            let preview = card.preview();
            let omitted = preview
                .base_offset
                .saturating_add(card.backend_dropped_bytes);
            let omission = card_omission(omitted, card.backend_truncated, true);
            let mut action = format!(
                "{}{}  {}",
                process_marker(card),
                card.chip_label(),
                card.task_label.as_deref().unwrap_or("Background command")
            );
            let note_start = push_status_note(&mut action, card.status_note());
            // Background output has no completion line, so the row names the exit code.
            let exit = card.exit_code.map(|code| format!("exit {code}"));
            let exit_start = push_status_note(&mut action, exit.as_deref());
            Some(CardProjection {
                action,
                action_marker_end: process_marker(card).len(),
                action_label_end: process_marker(card).len() + card.chip_label().len(),
                action_argument_start: usize::MAX,
                action_argument_style: crate::markdown::InlineStyle::Normal,
                metadata: card.action(),
                detail: preview.text,
                detail_base: usize::try_from(preview.base_offset).unwrap_or(usize::MAX),
                detail_tones: Vec::new(),
                detail_emphasis: Vec::new(),
                detail_syntax: Vec::new(),
                detail_gutters: Vec::new(),
                action_suffix_start: note_start.min(exit_start),
                inline_detail: false,
                omission,
                omission_before_detail: true,
                action_tone: process_state_tone(card.display_state),
            })
        }
    }
}

fn structured_card_preview(detail: &ToolDetailPresentation) -> (String, Vec<CardDetailTone>) {
    fn push_line(
        preview: &mut String,
        tones: &mut Vec<CardDetailTone>,
        line: &str,
        tone: TranscriptRowTone,
    ) {
        if !preview.is_empty() {
            preview.push('\n');
        }
        let start = preview.len();
        preview.push_str(line);
        let end = preview.len();
        if tone != TranscriptRowTone::Default && start < end {
            tones.push(CardDetailTone { start, end, tone });
        }
    }

    // Diff presentations use `diff_card::preview` instead.
    let mut preview = String::new();
    let mut tones = Vec::new();
    let heading = if detail.summary.is_empty() {
        detail.title.clone()
    } else {
        format!("{}  {}", detail.title, detail.summary)
    };
    push_line(
        &mut preview,
        &mut tones,
        &heading,
        TranscriptRowTone::Default,
    );
    for row in detail.visible_rows(false) {
        push_line(
            &mut preview,
            &mut tones,
            &format_structured_detail_row(&row),
            TranscriptRowTone::Default,
        );
    }
    if detail.truncated {
        push_line(
            &mut preview,
            &mut tones,
            "⋯ retained detail is incomplete",
            TranscriptRowTone::Warning,
        );
    }
    (preview, tones)
}

pub(crate) fn format_structured_detail_row(row: &DetailRow) -> String {
    match row.kind {
        DetailRowKind::Addition => format!(
            "{:>4} + {}",
            row.new_line.map_or(String::new(), |line| line.to_string()),
            row.text
        ),
        DetailRowKind::Deletion => format!(
            "{:>4} - {}",
            row.old_line.map_or(String::new(), |line| line.to_string()),
            row.text
        ),
        DetailRowKind::Context => format!(
            "{:>4}   {}",
            row.new_line
                .or(row.old_line)
                .map_or(String::new(), |line| line.to_string()),
            row.text
        ),
        DetailRowKind::ReadLine => format!(
            "{:>4} │ {}",
            row.old_line.map_or(String::new(), |line| line.to_string()),
            row.text
        ),
        DetailRowKind::GrepMatch => format!(
            "{:>4} │ {}",
            row.old_line.map_or(String::new(), |line| line.to_string()),
            row.text
        ),
        DetailRowKind::FindPath => format!("  {}", row.text),
        DetailRowKind::Hunk | DetailRowKind::Header | DetailRowKind::Note => row.text.clone(),
        DetailRowKind::Omission => format!("⋯ {}", row.text.trim_matches('…').trim()),
    }
}

fn card_omission(
    omitted_bytes: u64,
    backend_truncated: bool,
    before_preview: bool,
) -> Option<String> {
    match (omitted_bytes, backend_truncated) {
        (0, false) => None,
        (0, true) => Some("⋯ tool reported truncated output".into()),
        (bytes, false) => Some(format!(
            "⋯ {bytes} input or presentation bytes omitted {} this preview",
            if before_preview { "before" } else { "after" }
        )),
        (bytes, true) => Some(format!(
            "⋯ {bytes} input or presentation bytes omitted {} this preview; tool also reported truncation",
            if before_preview { "before" } else { "after" }
        )),
    }
}

/// The wrap width left for a detail or omission row once its gutter is reserved.
fn result_width(width: usize) -> usize {
    width
        .saturating_sub(UnicodeWidthStr::width(RESULT_CONNECTOR))
        .max(1)
}

/// The connector opens whichever row follows the action line; later rows indent.
fn result_gutter(
    entry: &TranscriptEntry,
    projection: &CardProjection,
    anchor: RowAnchor,
) -> RowGutter {
    if projection.inline_detail {
        return RowGutter::Indent;
    }
    if card_position_after_action(entry, projection) == Some(anchor) {
        RowGutter::Connector
    } else {
        RowGutter::Indent
    }
}

fn card_position_after_action(
    entry: &TranscriptEntry,
    projection: &CardProjection,
) -> Option<RowAnchor> {
    if !projection.metadata.is_empty() {
        return Some(RowAnchor {
            entry_id: entry.id,
            position: RowPosition::Card(CardPosition {
                section: CardSection::Metadata,
                absolute_byte_offset: 0,
            }),
        });
    }
    card_position_after_metadata(entry, projection)
}

fn card_position_after_metadata(
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

/// The action line that stands for a collapsed run of calls to one MCP tool, such as
/// `✓  MCP     linear/get_issue ×8  id=HY-270 … id=HY-278`.
///
/// While a call is still open the line follows it instead: its marker and arguments, and
/// a note of how many calls have finished. A call awaiting approval takes precedence over
/// one that is running, so a stalled run says why. Once every call has finished, the
/// marker is the worst outcome and the arguments are the first and last call's.
struct McpGroupHeader {
    source: String,
    /// Where the target ends, with its count unless the header is compact.
    count_end: usize,
    marker_end: usize,
    label_end: usize,
    suffix_start: usize,
    tone: TranscriptRowTone,
}

impl McpGroupHeader {
    /// Build the header. A `compact` header leaves out the arguments and keeps the count
    /// with the status note, so a row too narrow for the target cuts the target rather
    /// than the count that says the row stands for several calls.
    fn new(transcript: &Transcript, members: &[TranscriptEntryId], compact: bool) -> Option<Self> {
        let cards = members
            .iter()
            .filter_map(|id| transcript.entry(*id)?.tool_card())
            .collect::<Vec<_>>();
        let first = *cards.first()?;
        let last = *cards.last()?;
        let open = |status: &[ToolStatus]| {
            cards
                .iter()
                .rev()
                .find(|card| status.contains(&card.status))
                .copied()
        };
        let live = open(&[ToolStatus::AwaitingApproval])
            .or_else(|| open(&[ToolStatus::Requested, ToolStatus::Running]));
        let shown = live.unwrap_or_else(|| {
            cards
                .iter()
                .copied()
                .max_by_key(|card| explore_tone_rank(tool_status_tone(card.status)))
                .unwrap_or(first)
        });

        let marker = status_marker(shown.status);
        let mut source = format!("{marker}{}", first.chip_label());
        let label_end = source.len();
        source.push_str("  ");
        source.push_str(&first.mcp_target()?);
        let count = format!(" ×{}", cards.len());
        if !compact {
            source.push_str(&count);
        }
        let count_end = source.len();
        let summary = match live {
            Some(card) => shown_arguments(card).map(str::to_owned),
            None => match (shown_arguments(first), shown_arguments(last)) {
                (Some(first), Some(last)) if first == last => Some(first.to_owned()),
                (Some(first), Some(last)) if cards.len() == 2 => Some(format!("{first} · {last}")),
                (Some(first), Some(last)) => Some(format!("{first} … {last}")),
                (first, last) => first.or(last).map(str::to_owned),
            },
        };
        if let Some(summary) = summary.filter(|_| !compact) {
            source.push_str("  ");
            source.push_str(&summary);
        }
        let suffix_start = source.len();
        if compact {
            source.push_str(&count);
        }
        if live.is_some() {
            let finished = cards
                .iter()
                .filter(|card| {
                    !matches!(
                        card.status,
                        ToolStatus::Requested | ToolStatus::Running | ToolStatus::AwaitingApproval
                    )
                })
                .count();
            source.push_str(&format!(" · {finished} of {} done", cards.len()));
        }
        if let Some(note) = shown.status_note() {
            source.push_str(" · ");
            source.push_str(note);
        }
        Some(Self {
            source,
            count_end,
            marker_end: marker.len(),
            label_end,
            suffix_start,
            tone: tool_status_tone(shown.status),
        })
    }

    fn style(&self) -> CardRowStyle<'static> {
        CardRowStyle {
            marker_end: self.marker_end,
            label_end: self.label_end,
            argument_start: self.label_end + 2,
            argument_style: crate::markdown::InlineStyle::ToolArgument,
            suffix_start: self.suffix_start,
            emphasis: &[],
            syntax: &[],
            gutters: &[],
        }
    }

    /// Identifies what the header shows, since it depends on members other than the
    /// first, whose revision alone keys the cached row.
    fn signature(&self) -> u64 {
        use std::hash::{Hash, Hasher};
        let mut hasher = std::collections::hash_map::DefaultHasher::new();
        self.source.hash(&mut hasher);
        hasher.finish()
    }
}

/// Widest a single member may appear in a closed group's preview.
const GROUP_PREVIEW_ITEM_COLUMNS: usize = 40;

/// Each member's arguments for a closed group's preview, in order. Exploration mixes
/// tools, so each item there also names its tool.
fn group_preview_items(
    transcript: &Transcript,
    kind: &CardGroupKey,
    members: &[TranscriptEntryId],
) -> Vec<String> {
    members
        .iter()
        .filter_map(|id| transcript.entry(*id)?.tool_card())
        .filter_map(|card| {
            // The same text the member's own row shows, so a closed group and its
            // open members read alike.
            shown_arguments(card)?;
            let arguments = card.display_arguments();
            Some(match kind {
                CardGroupKey::Explore => format!("{} {arguments}", card.name),
                _ => arguments,
            })
        })
        .collect()
}

/// A card's arguments as its action line shows them, if it shows any.
fn shown_arguments(card: &crate::tool_cards::ToolCardSnapshot) -> Option<&str> {
    card.arguments_available
        .then_some(card.action_arguments.as_str())
        .filter(|arguments| !arguments.is_empty())
}

/// Identifies the content of a collapsed group independently of its clipped display.
fn card_group_signature(transcript: &Transcript, entry: &TranscriptEntry) -> Option<u64> {
    let (start, members) = card_run(transcript, entry.id)?;
    if start != entry.id {
        return None;
    }
    match card_group_key(entry)? {
        CardGroupKey::Explore | CardGroupKey::Tool(_) => {
            // A closed group previews its members, so their arguments key the row too.
            use std::hash::{Hash, Hasher};
            let key = card_group_key(entry)?;
            let mut hasher = std::collections::hash_map::DefaultHasher::new();
            members.len().hash(&mut hasher);
            group_preview_items(transcript, &key, &members).hash(&mut hasher);
            Some(hasher.finish())
        }
        CardGroupKey::Mcp(_) => {
            McpGroupHeader::new(transcript, &members, false).map(|header| header.signature())
        }
    }
}

fn explore_group_tone(transcript: &Transcript, members: &[TranscriptEntryId]) -> TranscriptRowTone {
    let mut tone = TranscriptRowTone::Pending;
    let mut saw_card = false;
    for id in members {
        let Some(card) = transcript.entry(*id).and_then(TranscriptEntry::tool_card) else {
            continue;
        };
        let member = tool_status_tone(card.status);
        if !saw_card || explore_tone_rank(member) > explore_tone_rank(tone) {
            tone = member;
        }
        saw_card = true;
    }
    tone
}

fn explore_tone_rank(tone: TranscriptRowTone) -> u8 {
    match tone {
        TranscriptRowTone::Error => 4,
        TranscriptRowTone::Pending => 3,
        TranscriptRowTone::Warning => 2,
        TranscriptRowTone::Success => 1,
        _ => 0,
    }
}

fn card_action_anchor(entry_id: TranscriptEntryId) -> RowAnchor {
    RowAnchor {
        entry_id,
        position: RowPosition::Card(CardPosition {
            section: CardSection::Action,
            absolute_byte_offset: 0,
        }),
    }
}

fn card_header_cache_valid(
    transcript: &Transcript,
    fold: &FoldState,
    entry: &TranscriptEntry,
    cached: &CachedRow,
) -> bool {
    if cached.entry_revision != entry.revision() {
        return false;
    }
    match cached.row.kind {
        TranscriptRowKind::CardGroup => {
            let Some((start, members)) = card_run(transcript, entry.id) else {
                return false;
            };
            if start != entry.id {
                return false;
            }
            let Some(last) = members.last().copied().and_then(|id| transcript.entry(id)) else {
                return false;
            };
            let next = if fold.is_run_expanded(&members) {
                Some(card_action_anchor(entry.id))
            } else {
                separator_after(transcript, last)
            };
            if cached.next != next {
                return false;
            }
            cached.group_signature.is_some()
                && cached.group_signature == card_group_signature(transcript, entry)
                && (matches!(card_group_key(entry), Some(CardGroupKey::Mcp(_)))
                    || cached.row.tone == explore_group_tone(transcript, &members))
        }
        TranscriptRowKind::CardAction => {
            cached.row.anchor.position != RowPosition::Header
                || card_run(transcript, entry.id).is_none_or(|(start, _)| start != entry.id)
        }
        _ => false,
    }
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
    Home,
    FollowTail,
    OutputChanged,
    /// A persisted page changed around existing semantic anchors, never live output.
    HistoryChanged,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct NavigationOutcome {
    pub requested_rows: usize,
    pub consumed_rows: usize,
    pub reader_generation: u64,
}

impl NavigationOutcome {
    pub fn remaining_rows(self) -> usize {
        self.requested_rows.saturating_sub(self.consumed_rows)
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TranscriptViewport {
    top: Option<RowAnchor>,
    follow_tail: bool,
    unseen_output: bool,
    width: usize,
    height: usize,
    reader_generation: u64,
    // False until a paint gives the viewport its real size.
    sized: bool,
}

impl Default for TranscriptViewport {
    fn default() -> Self {
        Self {
            top: None,
            follow_tail: true,
            unseen_output: false,
            width: 1,
            height: 1,
            reader_generation: 0,
            sized: false,
        }
    }
}

impl TranscriptViewport {
    pub(crate) fn reanchor_entry(&mut self, from: TranscriptEntryId, to: TranscriptEntryId) {
        if !self.follow_tail && self.top.is_some_and(|anchor| anchor.entry_id == from) {
            self.top = Some(RowAnchor {
                entry_id: to,
                position: RowPosition::Header,
            });
        }
    }

    /// Keep a run's header as the top row when an earlier call joins the run and the
    /// header moves to it; the old start's `Header` anchor now draws a member card.
    pub(crate) fn follow_moved_group_header(
        &mut self,
        from: TranscriptEntryId,
        to: TranscriptEntryId,
    ) {
        let header = |entry_id| RowAnchor {
            entry_id,
            position: RowPosition::Header,
        };
        if !self.follow_tail && self.top == Some(header(from)) {
            self.top = Some(header(to));
        }
    }

    pub fn follows_tail(&self) -> bool {
        self.follow_tail
    }

    /// The first row of a scrolled-back view, or `None` while following the tail.
    pub(crate) fn scrolled_top(&self) -> Option<RowAnchor> {
        self.top.filter(|_| !self.follow_tail)
    }

    pub fn has_unseen_output(&self) -> bool {
        self.unseen_output
    }

    pub fn reader_generation(&self) -> u64 {
        self.reader_generation
    }

    /// Whether a paint has given this viewport its size, so scrolling it is meaningful.
    pub fn is_sized(&self) -> bool {
        self.sized
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
        self.sized = true;
        if self.width == width && self.height == height {
            return;
        }
        self.width = width;
        self.height = height;
        self.reader_generation = self.reader_generation.wrapping_add(1);
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
    ) -> NavigationOutcome {
        self.reduce_with_newer_history(action, transcript, cache, false)
    }

    pub fn reduce_with_newer_history(
        &mut self,
        action: TranscriptViewAction,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
        newer_history_available: bool,
    ) -> NavigationOutcome {
        let requested_rows = match action {
            TranscriptViewAction::PageUp | TranscriptViewAction::PageDown => {
                self.height.saturating_sub(1).max(1)
            }
            TranscriptViewAction::ScrollLines(lines) => lines.unsigned_abs() as usize,
            _ => 0,
        };
        if matches!(
            action,
            TranscriptViewAction::ScrollLines(_)
                | TranscriptViewAction::PageUp
                | TranscriptViewAction::PageDown
                | TranscriptViewAction::Home
                | TranscriptViewAction::FollowTail
        ) {
            self.reader_generation = self.reader_generation.wrapping_add(1);
        }
        let consumed_rows = match action {
            TranscriptViewAction::HistoryChanged => {
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
                        self.top = transcript.entries().first().map(|entry| RowAnchor {
                            entry_id: entry.id,
                            position: RowPosition::Header,
                        });
                        self.follow_tail = self.top.is_none();
                    }
                }
                0
            }
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
                0
            }
            TranscriptViewAction::FollowTail => {
                self.follow_tail = true;
                self.unseen_output = false;
                self.top = None;
                0
            }
            TranscriptViewAction::PageUp => {
                let amount = self.height.saturating_sub(1).max(1);
                self.scroll_up(transcript, cache, amount)
            }
            TranscriptViewAction::PageDown => {
                let amount = self.height.saturating_sub(1).max(1);
                self.scroll_down(transcript, cache, amount, !newer_history_available)
            }
            TranscriptViewAction::Home => {
                self.jump_home(transcript, cache);
                0
            }
            TranscriptViewAction::ScrollLines(lines) if lines < 0 => {
                self.scroll_up(transcript, cache, lines.unsigned_abs() as usize)
            }
            TranscriptViewAction::ScrollLines(lines) if lines > 0 => {
                self.scroll_down(transcript, cache, lines as usize, !newer_history_available)
            }
            TranscriptViewAction::ScrollLines(_) => 0,
        };
        NavigationOutcome {
            requested_rows,
            consumed_rows,
            reader_generation: self.reader_generation,
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

    /// Whether fewer than one viewport of retained rows remains above the top row.
    ///
    /// Requesting the next page only once scrolling has already reached the oldest row makes every
    /// wheel tick at the boundary wait for a round trip. Reporting the approach one viewport early
    /// lets the request overlap the rows that are still locally available.
    pub fn near_oldest(&mut self, transcript: &Transcript, cache: &mut TranscriptRowCache) -> bool {
        let _ = self.visible_rows(transcript, cache);
        let Some(mut anchor) = self.top else {
            return false;
        };
        for _ in 0..self.height.max(1) {
            let Some(previous) = cache.previous_anchor(transcript, anchor, self.width) else {
                return true;
            };
            anchor = previous;
        }
        false
    }

    pub(crate) fn near_live_gap(
        &mut self,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
    ) -> bool {
        let _ = self.visible_rows(transcript, cache);
        let Some(mut anchor) = self.top else {
            return false;
        };
        for _ in 0..=self.height.max(1) {
            if transcript.is_live_history_marker(anchor.entry_id) {
                return true;
            }
            let Some(previous) = cache.previous_anchor(transcript, anchor, self.width) else {
                return false;
            };
            anchor = previous;
        }
        false
    }

    /// Estimate the visible interval in retained history without laying out offscreen rows.
    /// Entry order and source offsets provide a stable, bounded-cost approximation;
    /// wrapped rows, Markdown tables, and folded groups need not have equal heights.
    pub(crate) fn scrollbar_range(
        &self,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
        rows: &[TranscriptRow],
    ) -> Option<(f64, f64)> {
        let top = self.top?;
        let last = rows.last()?;
        let count = transcript.entries().len().max(1) as f64;
        let start = scroll_anchor_rank(transcript, cache, top)? / count;
        let next = cache
            .row_at(transcript, last.anchor, self.width)
            .and_then(|row| row.next);
        let end = if self.follow_tail || next.is_none() {
            1.0
        } else {
            scroll_anchor_rank(transcript, cache, next?)? / count
        };
        (start > 0.0 || end < 1.0).then_some((start.clamp(0.0, 1.0), end.clamp(start, 1.0)))
    }

    fn jump_home(&mut self, transcript: &Transcript, cache: &mut TranscriptRowCache) {
        let Some(first) = transcript.entries().first() else {
            return;
        };
        let oldest = RowAnchor {
            entry_id: first.id,
            position: RowPosition::Header,
        };
        let _ = self.visible_rows(transcript, cache);
        if self.top == Some(oldest) {
            return;
        }
        self.top = Some(oldest);
        self.follow_tail = false;
    }

    fn scroll_up(
        &mut self,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
        amount: usize,
    ) -> usize {
        if self.top.is_none() {
            self.top = self.tail_top(transcript, cache);
        }
        let Some(mut top) = self.top else {
            return 0;
        };
        let mut moved = 0;
        let mut seen = HashSet::from([top]);
        for _ in 0..amount {
            if transcript.is_live_history_marker(top.entry_id) {
                break;
            }
            let Some(previous) = cache.previous_anchor(transcript, top, self.width) else {
                break;
            };
            if !seen.insert(previous) {
                break;
            }
            top = previous;
            moved += 1;
        }
        if moved > 0 {
            self.top = Some(top);
            self.follow_tail = false;
        }
        moved
    }

    fn scroll_down(
        &mut self,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
        amount: usize,
        rejoin_tail: bool,
    ) -> usize {
        if self.follow_tail {
            return 0;
        }
        let Some(mut top) = self.top else {
            if rejoin_tail {
                self.follow_tail = true;
                self.unseen_output = false;
            }
            return 0;
        };
        let tail = rejoin_tail
            .then(|| self.tail_top(transcript, cache))
            .flatten();
        let mut moved = 0;
        for _ in 0..amount {
            if Some(top) == tail {
                self.follow_tail = true;
                self.unseen_output = false;
                self.top = None;
                return moved;
            }
            let Some(cached) = cache.row_at(transcript, top, self.width) else {
                break;
            };
            let Some(next) = cached.next else {
                if rejoin_tail {
                    self.follow_tail = true;
                    self.unseen_output = false;
                    self.top = None;
                } else if moved > 0 {
                    self.top = Some(top);
                }
                return moved;
            };
            top = next;
            moved += 1;
        }
        // Cap multi-row gestures at the live top. Overshooting and then snapping
        // backward to this anchor makes PageDown and batched wheel input jitter.
        if Some(top) == tail {
            self.follow_tail = true;
            self.unseen_output = false;
            self.top = None;
        } else {
            self.top = Some(top);
        }
        moved
    }

    pub fn continue_navigation(
        &mut self,
        older: bool,
        rows: usize,
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
        newer_history_available: bool,
    ) -> usize {
        if older {
            self.scroll_up(transcript, cache, rows)
        } else {
            self.scroll_down(transcript, cache, rows, !newer_history_available)
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

fn scroll_anchor_rank(
    transcript: &Transcript,
    cache: &mut TranscriptRowCache,
    anchor: RowAnchor,
) -> Option<f64> {
    let index = transcript.entry_index(anchor.entry_id)?;
    let entry = transcript.entry(anchor.entry_id)?;
    let source_length = entry
        .display_content()
        .len()
        .saturating_add(entry.thinking().len())
        .max(1);
    let content_fraction = |offset: usize| {
        0.05 + 0.9
            * (entry.thinking().len().saturating_add(offset) as f64 / source_length as f64).min(1.0)
    };
    let fraction = match anchor.position {
        RowPosition::Header => 0.0,
        RowPosition::Spacer | RowPosition::UserLeadIn | RowPosition::UserLeadOut => 1.0,
        RowPosition::Thought => 0.025,
        RowPosition::ThoughtContent(offset) => {
            0.05 + 0.9 * (offset as f64 / source_length as f64).min(1.0)
        }
        RowPosition::Content(offset) => content_fraction(offset),
        RowPosition::Markdown(position) => content_fraction(position.source_offset),
        RowPosition::Card(position) => {
            let card = cache.card_snapshot(entry)?;
            let total = card
                .action
                .len()
                .saturating_add(card.metadata.len())
                .saturating_add(card.detail.len())
                .max(1);
            let offset = match position.section {
                CardSection::Action => position.absolute_byte_offset,
                CardSection::Metadata => card
                    .action
                    .len()
                    .saturating_add(position.absolute_byte_offset),
                CardSection::Detail => card
                    .action
                    .len()
                    .saturating_add(card.metadata.len())
                    .saturating_add(
                        position
                            .absolute_byte_offset
                            .saturating_sub(card.detail_base),
                    ),
                CardSection::Omission => total,
            };
            0.05 + 0.9 * (offset as f64 / total as f64).min(1.0)
        }
    };
    Some(index as f64 + fraction)
}

fn cached_row_valid(
    transcript: &Transcript,
    fold: &FoldState,
    cached: &CachedRow,
    entry: &TranscriptEntry,
    markdown: Option<&MarkdownSnapshot>,
) -> bool {
    if cached.row.kind == TranscriptRowKind::Content
        && uses_markdown(entry)
        && markdown.is_none_or(|snapshot| {
            cached.markdown_presentation_identity != Some(snapshot.presentation_identity)
        })
    {
        return false;
    }
    if matches!(
        cached.row.kind,
        TranscriptRowKind::CardAction | TranscriptRowKind::CardGroup
    ) {
        return card_header_cache_valid(transcript, fold, entry, cached);
    }
    if cached.entry_revision == entry.revision() {
        return true;
    }
    match cached.row.kind {
        TranscriptRowKind::Header if entry.role == TranscriptRole::Assistant => false,
        TranscriptRowKind::Header => {
            let expected = RowPosition::Content(0);
            cached
                .next
                .is_some_and(|next| next.entry_id == entry.id && next.position == expected)
        }
        TranscriptRowKind::Spacer if cached.row.anchor.position == RowPosition::UserLeadIn => {
            user_lead_in(transcript, entry) == Some(cached.row.anchor)
                && cached.next
                    == transcript.entry_after(entry.id).map(|next| RowAnchor {
                        entry_id: next.id,
                        position: RowPosition::Header,
                    })
        }
        TranscriptRowKind::Spacer if cached.row.anchor.position == RowPosition::UserLeadOut => {
            user_lead_out(transcript, entry) == Some(cached.row.anchor)
                && cached.next
                    == transcript.entry_after(entry.id).map(|next| RowAnchor {
                        entry_id: next.id,
                        position: RowPosition::Header,
                    })
        }
        // Whether a card's gap is a rule depends on whether the card shows result rows, which
        // can change with its content, so a changed card rebuilds it.
        TranscriptRowKind::Spacer | TranscriptRowKind::Rule
            if !matches!(entry.kind, TranscriptEntryKind::Message) =>
        {
            false
        }
        TranscriptRowKind::Spacer => cached.next == spacer_next(transcript, entry),
        TranscriptRowKind::Content if uses_markdown(entry) => {
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
                && entry.display_content().len() >= cached.content_len
        ),
        TranscriptRowKind::CardAction
        | TranscriptRowKind::CardGroup
        | TranscriptRowKind::CardDetail
        | TranscriptRowKind::CardOmission
        | TranscriptRowKind::Placeholder
        | TranscriptRowKind::Thought
        | TranscriptRowKind::Rule => false,
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
        RowPosition::Markdown(position) if uses_markdown(entry) => {
            let snapshot = cache.markdown_snapshot(entry, width);
            let output = match normalization {
                AnchorNormalization::Geometry if !snapshot.document.has_tables() => {
                    snapshot.normalize_offset(position.output_offset)
                }
                AnchorNormalization::Geometry => {
                    snapshot.output_for_source(position.source_offset, position.output_offset)
                }
                AnchorNormalization::Content => {
                    snapshot.output_for_source(position.source_offset, position.output_offset)
                }
            };
            if output >= snapshot.output_len {
                return cache.content_anchor_before(transcript, entry, snapshot.output_len, width);
            }
            cache.content_anchor_before(transcript, entry, snapshot.next_boundary(output), width)
        }
        RowPosition::Markdown(position) => normalize_anchor(
            transcript,
            cache,
            RowAnchor {
                entry_id: anchor.entry_id,
                position: RowPosition::Content(position.source_offset),
            },
            width,
            normalization,
        ),
        RowPosition::Content(offset) => {
            let content = entry.display_content();
            let mut clamped = offset.min(content.len());
            while clamped > 0 && !content.is_char_boundary(clamped) {
                clamped -= 1;
            }
            let target = content[clamped..]
                .chars()
                .next()
                .map_or(clamped, |character| clamped + character.len_utf8());
            cache.content_anchor_before(transcript, entry, target, width)
        }
        RowPosition::Card(position) => {
            let collapsed_group = collapsed_group_start(&cache.fold, transcript, entry.id);
            let action = if card_run(transcript, entry.id).is_some_and(|(start, members)| {
                start == entry.id && cache.fold.is_run_expanded(&members)
            }) {
                card_action_anchor(entry.id)
            } else {
                RowAnchor {
                    entry_id: collapsed_group.unwrap_or(entry.id),
                    position: RowPosition::Header,
                }
            };
            let projection = cache.card_snapshot(entry)?;
            if (!cache.fold.is_expanded(entry.id) && !projection.inline_detail)
                || collapsed_group.is_some()
            {
                return Some(action);
            }
            let normalized = match position.section {
                CardSection::Action => {
                    let offset = position.absolute_byte_offset.min(projection.action.len());
                    RowAnchor {
                        entry_id: entry.id,
                        position: if offset == 0 {
                            action.position
                        } else {
                            RowPosition::Card(CardPosition {
                                section: CardSection::Action,
                                absolute_byte_offset: offset,
                            })
                        },
                    }
                }
                CardSection::Metadata if !projection.metadata.is_empty() => RowAnchor {
                    entry_id: entry.id,
                    position: RowPosition::Card(CardPosition {
                        section: CardSection::Metadata,
                        absolute_byte_offset: position
                            .absolute_byte_offset
                            .min(projection.metadata.len()),
                    }),
                },
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
        // The lead-in exists only while a prompt directly follows its owner. A history page or
        // recovery can splice entries between them, which would leave an unbuildable top row
        // and a blank viewport, so resume at the owner's own gap instead. A page of explore
        // cards can fold the owner into a collapsed group, which is drawn from its start.
        RowPosition::UserLeadIn if user_lead_in(transcript, entry) != Some(anchor) => {
            if let Some(start) = collapsed_group_start(&cache.fold, transcript, entry.id) {
                return Some(RowAnchor {
                    entry_id: start,
                    position: RowPosition::Header,
                });
            }
            Some(match separator_after(transcript, entry) {
                Some(spacer) => spacer,
                None => cache.last_anchor(transcript, entry, width),
            })
        }
        // The lead-out likewise needs its prompt to still be followed by a reply or card; the
        // prompt's own surface padding is the nearest row that exists either way.
        RowPosition::UserLeadOut if user_lead_out(transcript, entry) != Some(anchor) => {
            Some(match separator_after(transcript, entry) {
                Some(spacer) => spacer,
                None => cache.last_anchor(transcript, entry, width),
            })
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
        RowPosition::ThoughtContent(offset) => Some(offset),
        RowPosition::Header
        | RowPosition::Thought
        | RowPosition::Card(_)
        | RowPosition::Spacer
        | RowPosition::UserLeadIn
        | RowPosition::UserLeadOut => None,
    }
}

/// Assistant prose wraps at a readable measure even on wide terminals.
const PROSE_COLUMNS: usize = 100;

/// Whether a span keeps the full width: code, raw HTML and tables stay aligned, while
/// paragraphs, headings and lists wrap at [`PROSE_COLUMNS`].
fn keeps_full_width(
    block: &crate::markdown::MarkdownBlock,
    index: usize,
    span: &TranscriptSpan,
) -> bool {
    matches!(
        span.style.block,
        crate::markdown::BlockStyle::Code | crate::markdown::BlockStyle::RawHtml
    ) || block.span_in_table(index)
}

/// Large messages stay fully readable as plain text without building an oversized Markdown tree.
fn uses_markdown(entry: &TranscriptEntry) -> bool {
    entry.role == TranscriptRole::Assistant
        && !entry.content.is_empty()
        && entry.content.len() <= MARKDOWN_SOURCE_MAX_BYTES
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

/// The gap row after `entry`'s last row: blank, or a thin rule between back-to-back collapsed
/// cards.
fn separator_after(transcript: &Transcript, entry: &TranscriptEntry) -> Option<RowAnchor> {
    transcript.entry_after(entry.id).map(|_| RowAnchor {
        entry_id: entry.id,
        position: RowPosition::Spacer,
    })
}

/// Whether `next` follows `entry` as part of one list of cards, so the gap between them is a
/// rule rather than a blank row: both are tool or process cards. A card next to a message keeps
/// a blank row, and so does a card showing its result rows.
fn cards_stack(entry: &TranscriptEntry, next: &TranscriptEntry) -> bool {
    let card = |entry: &TranscriptEntry| !matches!(entry.kind, TranscriptEntryKind::Message);
    card(entry) && card(next)
}

/// The lead-in row owned by `entry` when a user's prompt follows a non-user entry.
///
/// A prompt that follows another prompt continues the same highlighted surface, so it needs no
/// lead-in: the earlier prompt's spacer already pads it.
fn user_lead_in(transcript: &Transcript, entry: &TranscriptEntry) -> Option<RowAnchor> {
    transcript
        .entry_after(entry.id)
        .filter(|next| next.role == TranscriptRole::User && entry.role != TranscriptRole::User)
        .map(|_| RowAnchor {
            entry_id: entry.id,
            position: RowPosition::UserLeadIn,
        })
}

/// The plain gap owned by a user's prompt when a reply or card follows it, below the prompt's
/// highlighted surface. A prompt followed by another prompt continues the surface instead.
fn user_lead_out(transcript: &Transcript, entry: &TranscriptEntry) -> Option<RowAnchor> {
    transcript
        .entry_after(entry.id)
        .filter(|next| entry.role == TranscriptRole::User && next.role != TranscriptRole::User)
        .map(|_| RowAnchor {
            entry_id: entry.id,
            position: RowPosition::UserLeadOut,
        })
}

/// The row after `entry`'s spacer: its lead-in or lead-out when one is due, else the next
/// entry's header.
fn spacer_next(transcript: &Transcript, entry: &TranscriptEntry) -> Option<RowAnchor> {
    user_lead_in(transcript, entry)
        .or_else(|| user_lead_out(transcript, entry))
        .or_else(|| {
            transcript.entry_after(entry.id).map(|next| RowAnchor {
                entry_id: next.id,
                position: RowPosition::Header,
            })
        })
}

/// What lets adjacent tool cards share one collapsed row.
#[derive(Clone, Debug, Eq, PartialEq)]
enum CardGroupKey {
    /// Reads, searches and listings, mixed freely: `Explored N actions`.
    Explore,
    /// Repeated calls to one MCP server's tool, keyed by the full tool name.
    Mcp(String),
    /// Consecutive successful calls to exactly the same tool.
    Tool(String),
}

/// The specialized group a card can join.
fn card_group_key(entry: &TranscriptEntry) -> Option<CardGroupKey> {
    let card = entry.tool_card()?;
    if matches!(card.name.as_str(), "read" | "grep" | "find" | "ls") {
        Some(CardGroupKey::Explore)
    } else if card.mcp_target().is_some() {
        Some(CardGroupKey::Mcp(card.name.clone()))
    } else if card.status == ToolStatus::Done {
        Some(CardGroupKey::Tool(card.name.clone()))
    } else {
        None
    }
}

/// Find a collapsible sequence. Specialized exploration/MCP runs retain their live
/// summaries; other groups contain successful calls to one tool only. A different
/// tool, assistant message, process card, or unfinished call ends the sequence.
pub(crate) fn card_run(
    transcript: &Transcript,
    entry_id: TranscriptEntryId,
) -> Option<(TranscriptEntryId, Vec<TranscriptEntryId>)> {
    let key = card_group_key(transcript.entry(entry_id)?)?;
    matching_card_run(transcript, entry_id, |entry| {
        card_group_key(entry).as_ref() == Some(&key)
    })
}

/// Collect a maximal adjacent run using one membership rule in both directions.
fn matching_card_run(
    transcript: &Transcript,
    entry_id: TranscriptEntryId,
    joins: impl Fn(&TranscriptEntry) -> bool,
) -> Option<(TranscriptEntryId, Vec<TranscriptEntryId>)> {
    if !joins(transcript.entry(entry_id)?) {
        return None;
    }
    let mut start = entry_id;
    while let Some(previous) = transcript.entry_before(start) {
        if !joins(previous) {
            break;
        }
        start = previous.id;
    }
    let mut members = vec![start];
    let mut current = start;
    while let Some(next) = transcript.entry_after(current) {
        if !joins(next) {
            break;
        }
        current = next.id;
        members.push(current);
    }
    (members.len() >= 2).then_some((start, members))
}

fn collapsed_group_start(
    fold: &FoldState,
    transcript: &Transcript,
    entry_id: TranscriptEntryId,
) -> Option<TranscriptEntryId> {
    let (start, members) = card_run(transcript, entry_id)?;
    (!fold.is_run_expanded(&members)).then_some(start)
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

pub(crate) fn sanitize_grapheme(grapheme: &str, column: usize) -> String {
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

    #[test]
    fn user_prose_wraps_at_words_and_long_tokens_still_wrap_safely() {
        let mut transcript = Transcript::default();
        let user = transcript.append_prompt("alpha beta gamma delta abcdefghijklmn".into());
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 12, 10);

        let content = viewport
            .visible_rows(&transcript, &mut cache)
            .into_iter()
            .filter(|row| row.anchor.entry_id == user && row.kind == TranscriptRowKind::Content)
            .map(|row| row.plain_text())
            .collect::<Vec<_>>();

        assert_eq!(content, ["alpha beta", "gamma delta", "abcdefghijkl", "mn"]);
    }

    #[test]
    fn prose_caps_but_code_stays_wide() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("explain".into());
        let prose = "word ".repeat(40);
        let code = format!("let value = {};", "x".repeat(130));
        let item = format!("- item {prose}\n\n  ```rust\n  {code}\n  ```");
        let quote = format!("> quoted {prose}\n>\n> ```\n> {code}\n> ```");
        let cell = "y".repeat(120);
        let table =
            format!("- table {prose}\n\n  | a | b |\n  |---|---|\n  | {cell} | z |\n\n  {prose}");
        let answer = transcript
            .complete_message(
                1,
                format!("{prose}\n\n```rust\n{code}\n```\n\n{item}\n\n{quote}\n\n{table}"),
            )
            .unwrap();
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 160, 120);

        let rows = viewport
            .visible_rows(&transcript, &mut cache)
            .into_iter()
            .filter(|row| row.anchor.entry_id == answer)
            .map(|row| row.plain_text())
            .collect::<Vec<_>>();

        // Lists and quotes holding code or a table still cap their prose, while the
        // code and table rows inside them keep the full width.
        let prose_rows = rows.iter().filter(|row| {
            row.contains("word") && !row.contains("let value") && !row.contains(&cell)
        });
        assert!(prose_rows.clone().count() >= 10, "{rows:#?}");
        assert!(
            prose_rows.clone().all(|row| row.trim_end().width() <= 100),
            "{rows:#?}"
        );
        assert!(rows.iter().any(|row| row.contains(&code)), "{rows:#?}");
        assert!(
            rows.iter()
                .any(|row| row.starts_with("│ ") && row.contains(&code)),
            "{rows:#?}"
        );
        assert!(rows.iter().any(|row| row.contains(&cell)), "{rows:#?}");
    }

    #[test]
    fn thought_row_shows_duration() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("think".into());
        let answer = transcript.append_thinking_delta(1, "first\nsecond");
        transcript.append_message_delta(1, "answer");
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 20);

        let thought = viewport
            .visible_rows(&transcript, &mut cache)
            .into_iter()
            .find(|row| row.anchor.entry_id == answer && row.kind == TranscriptRowKind::Thought)
            .map(|row| row.plain_text())
            .unwrap();

        assert!(thought.starts_with("thought · 2 lines · "), "{thought}");
        assert!(thought.ends_with('s'), "{thought}");
    }

    #[test]
    fn follow_tail_backfills_short_current_turn_and_scrolls_contiguously() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("older prompt".into());
        transcript.complete_message(1, numbered_lines("older answer", 20));
        transcript.append_prompt("latest prompt".into());
        transcript.complete_message(2, "latest answer".into());
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 40, 8);

        let rows = viewport.visible_rows(&transcript, &mut cache);
        assert_eq!(rows.len(), 8);
        assert!(rows.iter().any(|row| row.plain_text() == "latest prompt"));
        assert!(rows.iter().any(|row| row.plain_text() == "latest answer"));
        let anchors = rows.iter().map(|row| row.anchor).collect::<Vec<_>>();
        let expected_top = cache
            .previous_anchor(&transcript, anchors[0], 40)
            .expect("backfilled tail has an older row");

        transcript.append_message_delta(3, "unseen output\n");
        let outcome = viewport.reduce(
            TranscriptViewAction::ScrollLines(-1),
            &transcript,
            &mut cache,
        );
        let scrolled = viewport.visible_rows(&transcript, &mut cache);
        let scrolled_anchors = scrolled.iter().map(|row| row.anchor).collect::<Vec<_>>();

        assert_eq!(outcome.consumed_rows, 1);
        assert_eq!(scrolled_anchors[0], expected_top);
        assert_eq!(scrolled_anchors[1..], anchors[..anchors.len() - 1]);
    }

    #[test]
    fn scrolling_back_to_the_tail_preserves_the_rendered_rows() {
        let transcript = transcript_with(30);
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 20, 6);
        let tail = viewport.visible_rows(&transcript, &mut cache);

        viewport.reduce(
            TranscriptViewAction::ScrollLines(-1),
            &transcript,
            &mut cache,
        );
        assert!(!viewport.follows_tail());
        viewport.reduce(
            TranscriptViewAction::ScrollLines(1),
            &transcript,
            &mut cache,
        );

        assert!(viewport.follows_tail());
        assert_eq!(viewport.visible_rows(&transcript, &mut cache), tail);
    }

    #[test]
    fn multi_row_scroll_stops_at_the_live_top_without_snapping_backward() {
        let transcript = transcript_with(30);
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 20, 6);
        let tail = viewport.visible_rows(&transcript, &mut cache);

        let up = viewport.reduce(
            TranscriptViewAction::ScrollLines(-2),
            &transcript,
            &mut cache,
        );
        assert_eq!(up.consumed_rows, 2);
        let down = viewport.reduce(
            TranscriptViewAction::ScrollLines(5),
            &transcript,
            &mut cache,
        );

        assert_eq!(down.consumed_rows, 2);
        assert!(viewport.follows_tail());
        assert_eq!(viewport.visible_rows(&transcript, &mut cache), tail);
    }

    #[test]
    fn scrollbar_advances_inside_one_long_reply_and_reaches_both_ends() {
        let mut transcript = Transcript::default();
        transcript.complete_message(1, numbered_lines("line", 100));
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 40, 8);
        viewport.reduce(TranscriptViewAction::Home, &transcript, &mut cache);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let first = viewport
            .scrollbar_range(&transcript, &mut cache, &rows)
            .unwrap();
        assert_eq!(first.0, 0.0);
        assert!(first.1 < 1.0);
        viewport.reduce(TranscriptViewAction::PageDown, &transcript, &mut cache);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let next = viewport
            .scrollbar_range(&transcript, &mut cache, &rows)
            .unwrap();
        assert!(next.0 > first.0);
        assert!(next.1 > first.1);
        viewport.reduce(TranscriptViewAction::FollowTail, &transcript, &mut cache);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let tail = viewport
            .scrollbar_range(&transcript, &mut cache, &rows)
            .unwrap();
        assert!(tail.0 > next.0);
        assert_eq!(tail.1, 1.0);
    }

    #[test]
    fn styled_tool_actions_preserve_source_ranges_across_unicode_and_tab_wrapping() {
        let mut transcript = Transcript::default();
        let id = transcript.append_prompt("fixture".into());
        let entry = transcript.entry(id).unwrap();
        let source = "●  Read  界\tpath";
        for width in [8, 11] {
            let mut cache = TranscriptRowCache::default();
            let mut offset = 0;
            let mut covered = 0;
            let mut styles = Vec::new();
            loop {
                let projected = cache.build_card_text_row(
                    entry,
                    RowAnchor {
                        entry_id: id,
                        position: RowPosition::Card(CardPosition {
                            section: CardSection::Action,
                            absolute_byte_offset: offset,
                        }),
                    },
                    CardSection::Action,
                    source,
                    0,
                    offset,
                    width,
                    TranscriptRowKind::CardAction,
                    TranscriptRowTone::Success,
                    CardRowStyle {
                        marker_end: "●  ".len(),
                        label_end: "●  Read".len(),
                        argument_start: "●  Read  ".len(),
                        argument_style: crate::markdown::InlineStyle::ToolPath,
                        ..CardRowStyle::PLAIN
                    },
                    None,
                );
                for span in &projected.row.spans {
                    styles.push(span.style.inline);
                    assert_eq!(span.affinity.source_offset, covered);
                    assert!(span.affinity.source_end > span.affinity.source_offset);
                    covered = span.affinity.source_end;
                    if span.style.inline == crate::markdown::InlineStyle::ToolName {
                        assert_eq!(
                            &source[span.affinity.source_offset..span.affinity.source_end],
                            span.text
                        );
                    }
                }
                let Some(RowAnchor {
                    position: RowPosition::Card(next),
                    ..
                }) = projected.next
                else {
                    break;
                };
                offset = next.absolute_byte_offset;
            }
            assert_eq!(covered, source.len());
            assert!(styles.contains(&crate::markdown::InlineStyle::ToolStatus));
            assert!(styles.contains(&crate::markdown::InlineStyle::ToolName));
            assert!(styles.contains(&crate::markdown::InlineStyle::Normal));
            assert!(styles.contains(&crate::markdown::InlineStyle::ToolPath));
        }
    }

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

    #[test]
    fn user_rows_project_local_presentation_without_laying_out_raw_content() {
        let mut transcript = Transcript::default();
        let raw = "raw line\n".repeat(10_000);
        transcript.append_prompt_with_display(
            raw.clone(),
            Some("[pasted text: 90,000 characters, 10,001 lines]".into()),
        );
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 8);

        let rows = viewport.visible_rows(&transcript, &mut cache);
        let rendered = rows
            .iter()
            .map(TranscriptRow::plain_text)
            .collect::<Vec<_>>()
            .join("\n");
        assert!(rendered.contains("[pasted text:"));
        assert!(!rendered.contains("raw line"));
        assert_eq!(transcript.latest_user_text(), Some(raw.as_str()));
        assert!(cache.work().bytes_scanned < 1_000);
    }

    fn tool_result(call_id: &str, output: &str) -> crate::tool_cards::ToolResultInput {
        crate::tool_cards::ToolResultInput {
            call_id: call_id.into(),
            name: "read".into(),
            output: output.into(),
            output_tail: None,
            output_source_bytes: output.len() as u64,
            output_source_lines: crate::tool_cards::logical_line_count(output),
            output_projection_cut_mid_line: false,
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
            duration_ms: None,
        }
    }

    #[test]
    fn tool_card_rows_update_in_place_with_semantic_tones() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("read".into());
        let card_id = transcript.observe_approval_requested(crate::tool_cards::ToolCallInput {
            call_id: "call-1".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
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
                .contains("Read     README.md · awaiting approval")
        );
        assert_eq!(pending_action.tone, TranscriptRowTone::Pending);

        transcript.observe_approval_resolved("call-1", true, None);
        transcript.observe_tool_result(tool_result("call-1", "first\nsecond"));
        viewport.reduce(TranscriptViewAction::OutputChanged, &transcript, &mut cache);
        cache.fold_mut().expand(card_id);
        cache.reset_work();
        let completed = viewport.visible_rows(&transcript, &mut cache);
        let action = completed
            .iter()
            .find(|row| row.anchor.entry_id == card_id && row.kind == TranscriptRowKind::CardAction)
            .unwrap();
        assert!(action.plain_text().contains("Read     README.md"));
        assert_eq!(action.tone, TranscriptRowTone::Success);
        assert!(completed.iter().any(|row| {
            row.kind == TranscriptRowKind::CardDetail && row.plain_text() == "first"
        }));
    }

    #[test]
    fn cards_are_collapsed_until_expanded_and_explore_runs_group() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("explore".into());
        let first = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "read-1".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"path": "a.rs"}),
        });
        transcript.observe_tool_result(tool_result("read-1", "alpha"));
        transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "read-2".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"path": "b.rs"}),
        });
        let mut second = tool_result("read-2", "beta");
        second.call_id = "read-2".into();
        transcript.observe_tool_result(second);

        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 12);
        let collapsed = viewport.visible_rows(&transcript, &mut cache);
        assert!(collapsed.iter().any(|row| {
            row.kind == TranscriptRowKind::CardGroup
                && row.plain_text().contains("Explored  ▸ 2 actions")
                && row.spans.iter().any(|span| {
                    span.text == "Explored"
                        && span.style.inline == crate::markdown::InlineStyle::ToolName
                })
        }));
        assert!(
            !collapsed
                .iter()
                .any(|row| row.kind == TranscriptRowKind::CardDetail)
        );

        // Resizing a collapsed group clips the same semantic source, including its
        // marker and chip. A cached narrow row must remain valid on the next paint.
        for width in [1, 8, 16, 30, 80] {
            viewport.set_geometry(&transcript, &mut cache, width, 12);
            let rows = viewport.visible_rows(&transcript, &mut cache);
            let group = rows
                .iter()
                .find(|row| row.kind == TranscriptRowKind::CardGroup)
                .unwrap();
            assert!(group.plain_text().width() <= width);
            assert_eq!(rows, viewport.visible_rows(&transcript, &mut cache));
        }

        cache.fold_mut().expand_group(first);
        for width in [8, 30, 80] {
            viewport.set_geometry(&transcript, &mut cache, width, 20);
            let rows = viewport.visible_rows(&transcript, &mut cache);
            assert_eq!(
                rows.iter()
                    .filter(|row| row.kind == TranscriptRowKind::CardGroup)
                    .count(),
                1
            );
            assert_eq!(
                rows.iter()
                    .filter(|row| row.kind == TranscriptRowKind::CardAction)
                    .count(),
                2
            );
            assert_eq!(rows, viewport.visible_rows(&transcript, &mut cache));
        }
        let grouped = viewport.visible_rows(&transcript, &mut cache);
        assert!(grouped.iter().any(|row| {
            row.kind == TranscriptRowKind::CardAction && row.plain_text().contains("a.rs")
        }));
        assert!(
            !grouped
                .iter()
                .any(|row| row.kind == TranscriptRowKind::CardDetail)
        );

        cache.fold_mut().expand(first);
        let expanded = viewport.visible_rows(&transcript, &mut cache);
        assert!(expanded.iter().any(|row| {
            row.kind == TranscriptRowKind::CardDetail && row.plain_text() == "alpha"
        }));
    }

    #[test]
    fn explore_group_forms_when_a_consecutive_read_arrives_after_the_first_was_cached() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("explore".into());
        transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "read-1".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"path": "a.rs"}),
        });
        transcript.observe_tool_result(tool_result("read-1", "alpha"));

        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 12);
        let first_pass = viewport.visible_rows(&transcript, &mut cache);
        assert!(first_pass.iter().any(|row| {
            row.kind == TranscriptRowKind::CardAction && row.plain_text().contains("a.rs")
        }));
        assert!(
            !first_pass
                .iter()
                .any(|row| row.kind == TranscriptRowKind::CardGroup)
        );

        transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "read-2".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"path": "b.rs"}),
        });
        let mut second = tool_result("read-2", "beta");
        second.call_id = "read-2".into();
        transcript.observe_tool_result(second);
        viewport.reduce(TranscriptViewAction::OutputChanged, &transcript, &mut cache);

        let grouped = viewport.visible_rows(&transcript, &mut cache);
        assert!(grouped.iter().any(|row| {
            row.kind == TranscriptRowKind::CardGroup
                && row.plain_text().contains("Explored  ▸ 2 actions")
        }));
        assert!(!grouped.iter().any(|row| {
            row.kind == TranscriptRowKind::CardAction && row.plain_text().contains("a.rs")
        }));
    }

    #[test]
    fn collapsed_explore_group_uses_the_worst_member_tone() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("explore".into());
        transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "read-ok".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"path": "ok.rs"}),
        });
        let mut failed = tool_result("read-ok", "missing");
        failed.is_error = true;
        transcript.observe_tool_result(failed);
        transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "read-later".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"path": "later.rs"}),
        });
        let mut ok = tool_result("read-later", "beta");
        ok.call_id = "read-later".into();
        transcript.observe_tool_result(ok);

        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 12);
        let grouped = viewport.visible_rows(&transcript, &mut cache);
        let row = grouped
            .iter()
            .find(|row| {
                row.kind == TranscriptRowKind::CardGroup
                    && row.plain_text().contains("Explored  ▸ 2 actions")
            })
            .expect("collapsed explore group");
        assert_eq!(row.tone, TranscriptRowTone::Error);
    }

    #[test]
    fn different_tools_and_attention_rows_remain_separate() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("check the project".into());
        let mut ids = Vec::new();
        for (call_id, name) in [("read", "read"), ("shell", "bash"), ("edit", "edit")] {
            ids.push(
                transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
                    call_id: call_id.into(),
                    name: name.into(),
                    detail_source: crate::tool_detail::ToolDetailSource::None,
                    arguments: serde_json::json!({"path": "a.rs", "command": "cargo test"}),
                }),
            );
            if call_id != "edit" {
                let mut result = tool_result(call_id, "done");
                result.name = name.into();
                transcript.observe_tool_result(result);
            }
        }
        ids.push(
            transcript.observe_approval_requested(crate::tool_cards::ToolCallInput {
                call_id: "approval".into(),
                name: "bash".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: serde_json::json!({"command": "git push"}),
            }),
        );
        let mut cache = TranscriptRowCache::default();
        let mut viewport = TranscriptViewport::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 20);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        assert!(
            !rows
                .iter()
                .any(|row| row.kind == TranscriptRowKind::CardGroup)
        );
        assert!(rows.iter().any(|row| {
            row.anchor.entry_id == ids[2] && row.kind == TranscriptRowKind::CardAction
        }));
        assert!(card_run(&transcript, ids[2]).is_none());
        assert!(card_run(&transcript, ids[3]).is_none());
        assert!(rows.iter().any(|row| {
            row.anchor.entry_id == ids[3]
                && row.kind == TranscriptRowKind::CardAction
                && row.plain_text().contains("awaiting approval")
        }));

        let mut failed = tool_result("edit", "failed");
        failed.name = "edit".into();
        failed.is_error = true;
        transcript.observe_tool_result(failed);
        viewport.reduce(TranscriptViewAction::OutputChanged, &transcript, &mut cache);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        assert!(rows.iter().any(|row| {
            row.anchor.entry_id == ids[2]
                && row.kind == TranscriptRowKind::CardAction
                && row.tone == TranscriptRowTone::Error
        }));
    }

    #[test]
    fn expanded_activity_survives_out_of_order_completion_and_can_collapse() {
        let mut transcript = Transcript::default();
        let mut ids = Vec::new();
        for index in 0..3 {
            let call_id = format!("shell-{index}");
            ids.push(
                transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
                    call_id: call_id.clone(),
                    name: "bash".into(),
                    detail_source: crate::tool_detail::ToolDetailSource::None,
                    arguments: serde_json::json!({"command": format!("echo {index}")}),
                }),
            );
            if index > 0 {
                let mut result = tool_result(&call_id, "done");
                result.name = "bash".into();
                transcript.observe_tool_result(result);
            }
        }
        let mut cache = TranscriptRowCache::default();
        let mut viewport = TranscriptViewport::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 20);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        assert!(
            rows.iter()
                .any(|row| row.plain_text().contains("Bash     ▸ 2 commands"))
        );
        cache.fold_mut().expand_group(ids[1]);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        assert_eq!(
            rows.iter()
                .filter(|row| row.kind == TranscriptRowKind::CardAction)
                .count(),
            3
        );

        let mut result = tool_result("shell-0", "done");
        result.name = "bash".into();
        transcript.observe_tool_result(result);
        viewport.reduce(TranscriptViewAction::OutputChanged, &transcript, &mut cache);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        assert!(
            rows.iter()
                .any(|row| row.plain_text().contains("Bash     ▾ 3 commands"))
        );
        assert_eq!(
            rows.iter()
                .filter(|row| row.kind == TranscriptRowKind::CardAction)
                .count(),
            3
        );
        let (start, members) = card_run(&transcript, ids[2]).unwrap();
        assert_eq!(start, ids[0]);
        cache.fold_mut().collapse_group(&members);
        assert!(!cache.fold().is_group_expanded(ids[1]));
        assert!(
            viewport
                .visible_rows(&transcript, &mut cache)
                .iter()
                .any(|row| { row.plain_text().contains("Bash     ▸ 3 commands") })
        );
    }

    #[test]
    fn another_tool_splits_two_bash_groups() {
        let mut transcript = Transcript::default();
        let mut ids = Vec::new();
        for (index, name) in [
            "bash", "bash", "bash", "bash", "bash", "edit", "bash", "bash",
        ]
        .into_iter()
        .enumerate()
        {
            let call_id = format!("call-{index}");
            ids.push(
                transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
                    call_id: call_id.clone(),
                    name: name.into(),
                    detail_source: crate::tool_detail::ToolDetailSource::None,
                    arguments: serde_json::json!({"command": "git status", "path": "notes.md"}),
                }),
            );
            let mut result = tool_result(&call_id, "done");
            result.name = name.into();
            transcript.observe_tool_result(result);
        }
        for id in &ids[..5] {
            assert_eq!(
                card_run(&transcript, *id),
                Some((ids[0], ids[..5].to_vec()))
            );
        }
        assert!(card_run(&transcript, ids[5]).is_none());
        for id in &ids[6..] {
            assert_eq!(
                card_run(&transcript, *id),
                Some((ids[6], ids[6..].to_vec()))
            );
        }
        let mut cache = TranscriptRowCache::default();
        let mut viewport = TranscriptViewport::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 20);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        assert!(
            rows.iter()
                .any(|row| row.plain_text().contains("Bash     ▸ 5 commands"))
        );
        assert!(
            rows.iter()
                .any(|row| row.plain_text().contains("Bash     ▸ 2 commands"))
        );
        assert!(
            rows.iter()
                .any(|row| row.anchor.entry_id == ids[5]
                    && row.kind == TranscriptRowKind::CardAction)
        );
    }

    #[test]
    fn completed_bash_activity_grows_without_crossing_assistant_messages() {
        let mut transcript = Transcript::default();
        let mut cache = TranscriptRowCache::default();
        let mut viewport = TranscriptViewport::default();
        for count in 1..=3 {
            let call_id = format!("shell-{count}");
            transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: call_id.clone(),
                name: "bash".into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments: serde_json::json!({"command": format!("echo {count}")}),
            });
            let mut result = tool_result(&call_id, "done");
            result.name = "bash".into();
            transcript.observe_tool_result(result);
            viewport.set_geometry(&transcript, &mut cache, 80, 20);
            viewport.reduce(TranscriptViewAction::OutputChanged, &transcript, &mut cache);
            let rows = viewport.visible_rows(&transcript, &mut cache);
            if count >= 2 {
                assert!(rows.iter().any(|row| {
                    row.plain_text()
                        .contains(&format!("Bash     ▸ {count} commands"))
                }));
            }
        }
        transcript.complete_message(1, "Checks finished.".into());
        let last = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "after-message".into(),
            name: "bash".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"command": "git status"}),
        });
        let mut result = tool_result("after-message", "clean");
        result.name = "bash".into();
        transcript.observe_tool_result(result);
        assert!(card_run(&transcript, last).is_none());
    }

    #[test]
    fn thinking_is_collapsed_by_default_and_does_not_replace_text() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("why".into());
        transcript.append_thinking_delta(1, "secret plan\nmore");
        transcript.append_message_delta(1, "visible answer");
        transcript.complete_message(1, "visible answer".into());

        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 12);
        let collapsed = viewport.visible_rows(&transcript, &mut cache);
        assert!(collapsed.iter().any(|row| {
            row.kind == TranscriptRowKind::Thought && row.plain_text().contains("thought")
        }));
        assert!(
            !collapsed
                .iter()
                .any(|row| row.plain_text().contains("secret plan"))
        );
        assert!(
            collapsed
                .iter()
                .any(|row| row.plain_text().contains("visible answer"))
        );

        let assistant = transcript.entries().last().unwrap().id;
        cache.fold_mut().expand(assistant);
        let expanded = viewport.visible_rows(&transcript, &mut cache);
        assert!(
            expanded
                .iter()
                .any(|row| row.plain_text().contains("secret plan"))
        );
        assert_eq!(transcript.latest_assistant_text(), Some("visible answer"));
    }

    #[test]
    fn home_jumps_to_the_compact_user_echo_across_a_long_assistant_tail() {
        let mut transcript = Transcript::default();
        let raw = format!("{}\n🙂END", "界".repeat(2001));
        transcript.append_prompt_with_display(
            raw.clone(),
            Some("[Pasted content #1: 2006 chars, 2 lines, 6011 bytes]".into()),
        );
        transcript.complete_message(1, format!("fake response to: {raw}"));

        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 12);
        let tail = viewport.visible_rows(&transcript, &mut cache);
        assert!(viewport.follows_tail());
        assert!(
            !tail
                .iter()
                .any(|row| row.plain_text().contains("Pasted content #1"))
        );

        viewport.reduce(TranscriptViewAction::Home, &transcript, &mut cache);
        let home = viewport.visible_rows(&transcript, &mut cache);
        let text = home
            .iter()
            .map(TranscriptRow::plain_text)
            .collect::<Vec<_>>()
            .join("\n");
        assert!(text.contains("Pasted content #1"));
        assert!(!viewport.follows_tail());
    }

    #[test]
    fn generic_head_preview_labels_omitted_output_as_following_the_preview() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("read".into());
        let card_id = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "call-large".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"path": "large.txt"}),
        });
        transcript.observe_tool_result(tool_result("call-large", &"x".repeat(4_000)));

        let projection = card_projection(transcript.entry(card_id).unwrap()).unwrap();
        let omission = projection.omission.unwrap();
        assert!(omission.contains("omitted after this preview"));
        assert!(!omission.contains("omitted before this preview"));
        assert!(!projection.omission_before_detail);
    }

    #[test]
    fn failed_generic_tool_cards_retain_and_label_the_diagnostic_tail() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("test".into());
        let card_id = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "call-failed".into(),
            name: "bash".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"command": "pytest"}),
        });
        let output = format!(
            "EARLY PROGRESS MARKER\n{}ASSERTION FAILED AT DIAGNOSTIC TAIL",
            "progress\n".repeat(10_000)
        );
        let mut failed = tool_result("call-failed", &output);
        failed.name = "bash".into();
        failed.is_error = true;
        failed.exit_code = Some(1);
        transcript.observe_tool_result(failed);

        let projection = card_projection(transcript.entry(card_id).unwrap()).unwrap();
        assert!(
            projection
                .detail
                .contains("ASSERTION FAILED AT DIAGNOSTIC TAIL")
        );
        assert!(!projection.detail.contains("EARLY PROGRESS MARKER"));
        assert!(projection.omission_before_detail);
        assert!(
            projection
                .omission
                .as_deref()
                .unwrap()
                .contains("omitted before this preview")
        );
    }

    #[test]
    fn successful_bash_cards_retain_and_preview_the_summary_tail() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("build".into());
        let card_id = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "call-success".into(),
            name: "bash".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({"command": "cargo build"}),
        });
        let output = format!(
            "EARLY BUILD MARKER\n{}BUILD FINISHED SUCCESSFULLY",
            "compiling\n".repeat(10_000)
        );
        let mut completed = tool_result("call-success", &output);
        completed.name = "bash".into();
        completed.exit_code = Some(0);
        transcript.observe_tool_result(completed);

        let entry = transcript.entry(card_id).unwrap();
        let card = entry.tool_card().unwrap();
        assert!(
            card.retained_output
                .text
                .ends_with("BUILD FINISHED SUCCESSFULLY")
        );
        assert!(!card.retained_output.text.contains("EARLY BUILD MARKER"));
        let projection = card_projection(entry).unwrap();
        assert!(projection.detail.contains("BUILD FINISHED SUCCESSFULLY"));
        assert!(!projection.detail.contains("EARLY BUILD MARKER"));
        assert!(projection.omission_before_detail);
        assert!(
            projection
                .omission
                .as_deref()
                .unwrap()
                .contains("omitted before this preview")
        );
    }

    #[test]
    fn process_card_coalesces_calls_and_bounds_terminal_safe_tail_rows() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("poll".into());
        let call = |call_id: &str| crate::tool_cards::ToolCallInput {
            call_id: call_id.into(),
            name: "bash".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
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
        cache.fold_mut().expand(card_id);
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
        assert!(
            card_rows
                .iter()
                .any(|row| row.plain_text().contains("2 calls · 2 polls"))
        );
        assert_eq!(card_rows[0].tone, TranscriptRowTone::Success);
        let omission = card_rows
            .iter()
            .find(|row| row.kind == TranscriptRowKind::CardOmission)
            .unwrap();
        assert!(
            omission
                .plain_text()
                .contains("omitted before this preview")
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
            detail_source: crate::tool_detail::ToolDetailSource::None,
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

    fn read_card(transcript: &mut Transcript, path: &str, output: &str) -> TranscriptEntryId {
        transcript.append_prompt("read".into());
        let card_id = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "call-1".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({ "path": path }),
        });
        transcript.observe_tool_result(tool_result("call-1", output));
        card_id
    }

    fn card_rows(
        transcript: &Transcript,
        cache: &mut TranscriptRowCache,
        card_id: TranscriptEntryId,
        width: usize,
    ) -> Vec<TranscriptRow> {
        let mut viewport = TranscriptViewport::default();
        viewport.set_geometry(transcript, cache, width, 20);
        viewport
            .visible_rows(transcript, cache)
            .into_iter()
            .filter(|row| row.anchor.entry_id == card_id)
            .collect()
    }

    #[test]
    fn connector_reserves_gutter_width() {
        let mut transcript = Transcript::default();
        let card_id = read_card(&mut transcript, "a.rs", "abcdefgh\nij");
        let mut cache = TranscriptRowCache::default();
        cache.fold_mut().expand(card_id);
        let details = card_rows(&transcript, &mut cache, card_id, 6)
            .into_iter()
            .filter(|row| row.kind == TranscriptRowKind::CardDetail)
            .map(|row| (row.plain_text(), row.gutter))
            .collect::<Vec<_>>();
        assert_eq!(
            details,
            [
                ("abcd".to_owned(), RowGutter::Connector),
                ("efgh".to_owned(), RowGutter::Indent),
                ("ij".to_owned(), RowGutter::Indent),
            ]
        );
    }

    #[test]
    fn hint_reserves_action_width() {
        let mut transcript = Transcript::default();
        // No `/` or space, so the cut lands exactly at the reserved width.
        let path = "x".repeat(80);
        let card_id = read_card(&mut transcript, &path, "contents");
        let mut cache = TranscriptRowCache::default();
        let width = 50;
        let rows = card_rows(&transcript, &mut cache, card_id, width);
        let [action] = rows.as_slice() else {
            panic!("a collapsed card keeps one row: {rows:?}");
        };
        assert_eq!(action.fold_hint, FoldHint::Expand);
        assert!(action.plain_text().ends_with('…'));
        assert_eq!(
            UnicodeWidthStr::width(action.plain_text().as_str()),
            width - FoldHint::Expand.width()
        );

        // Too narrow for the hint, the action keeps the whole row instead.
        let rows = card_rows(&transcript, &mut cache, card_id, 30);
        assert_eq!(rows[0].fold_hint, FoldHint::None);
        assert_eq!(UnicodeWidthStr::width(rows[0].plain_text().as_str()), 30);
    }

    #[test]
    fn expanded_card_offers_collapse() {
        let mut transcript = Transcript::default();
        let card_id = read_card(&mut transcript, "a.rs", "contents");
        let mut cache = TranscriptRowCache::default();
        cache.fold_mut().expand(card_id);
        let rows = card_rows(&transcript, &mut cache, card_id, 60);
        assert_eq!(rows[0].fold_hint, FoldHint::Collapse);
        assert!(rows[1..].iter().all(|row| row.fold_hint == FoldHint::None));
        assert_eq!(rows[1].gutter, RowGutter::Connector);

        // A card with nothing hidden and an action that fits has nothing to expand.
        let mut transcript = Transcript::default();
        let card_id = read_card(&mut transcript, "a.rs", "");
        let rows = card_rows(&transcript, &mut TranscriptRowCache::default(), card_id, 60);
        assert_eq!(rows[0].fold_hint, FoldHint::None);
    }

    #[test]
    fn card_exact_width_breaks_and_sanitized_affinities_are_precise() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("read".into());
        let card_id = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "call-1".into(),
            name: "read".into(),
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({}),
        });
        transcript.observe_tool_result(tool_result("call-1", "1234\nnext"));
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        cache.fold_mut().expand(card_id);
        // Four columns of detail text once the two-column result gutter is reserved.
        viewport.set_geometry(&transcript, &mut cache, 6, 20);
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
            detail_source: crate::tool_detail::ToolDetailSource::None,
            arguments: serde_json::json!({}),
        });
        let source = "a\tb\u{1b}";
        sanitized.observe_tool_result(tool_result("call-2", source));
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        cache.fold_mut().expand(card_id);
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
            detail_source: crate::tool_detail::ToolDetailSource::None,
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
            reader_generation: 0,
            sized: true,
        };
        let mut cache = TranscriptRowCache::default();
        cache.fold_mut().expand(card_id);
        let before = viewport.visible_rows(&transcript, &mut cache);
        assert!(
            before
                .iter()
                .any(|row| row.kind == TranscriptRowKind::CardDetail)
        );

        let output_row = before
            .iter()
            .find(|row| {
                matches!(
                    row.anchor.position,
                    RowPosition::Card(CardPosition {
                        section: CardSection::Detail,
                        ..
                    })
                )
            })
            .unwrap();
        assert_eq!(output_row.plain_text(), "first");
        assert_eq!(
            output_row.spans.first().unwrap().affinity.source_offset,
            first_base
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
            detail_source: crate::tool_detail::ToolDetailSource::None,
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
            reader_generation: 0,
            sized: true,
        };
        let mut cache = TranscriptRowCache::default();
        cache.fold_mut().expand(card_id);

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
    fn older_page_continues_unconsumed_navigation_from_the_preserved_anchor() {
        let mut transcript = Transcript::default();
        let current = transcript.append_prompt("current".into());
        transcript.mark_history_entries(0, "current-entry");
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 20, 6);

        let outcome = viewport.reduce(TranscriptViewAction::PageUp, &transcript, &mut cache);
        assert_eq!(outcome.consumed_rows, 0);
        assert!(outcome.remaining_rows() > 0);
        assert_eq!(
            viewport.visible_rows(&transcript, &mut cache)[0]
                .anchor
                .entry_id,
            current
        );

        let mut older = Transcript::default();
        for index in 0..4 {
            let start = older.entries().len();
            older.append_prompt(format!("older-{index}"));
            older.mark_history_entries(start, &format!("older-entry-{index}"));
        }
        assert!(transcript.prepend_history_page(&older));
        cache = TranscriptRowCache::default();
        viewport.reduce(
            TranscriptViewAction::HistoryChanged,
            &transcript,
            &mut cache,
        );
        let moved = viewport.continue_navigation(
            true,
            outcome.remaining_rows(),
            &transcript,
            &mut cache,
            false,
        );

        assert!(moved > 0);
        assert_ne!(
            viewport.visible_rows(&transcript, &mut cache)[0]
                .anchor
                .entry_id,
            current
        );
        assert!(!viewport.follows_tail());
    }

    #[test]
    fn newer_page_continues_unconsumed_navigation_without_premature_tail_follow() {
        let mut transcript = Transcript::default();
        for index in 0..8 {
            let start = transcript.entries().len();
            transcript.append_prompt(format!("line-{index}"));
            transcript.mark_history_entries(start, &format!("entry-{index}"));
        }
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 20, 5);
        viewport.reduce(TranscriptViewAction::Home, &transcript, &mut cache);
        let outcome = viewport.reduce_with_newer_history(
            TranscriptViewAction::ScrollLines(100),
            &transcript,
            &mut cache,
            true,
        );
        assert!(outcome.remaining_rows() > 0);
        assert!(!viewport.follows_tail());

        let mut newer = Transcript::default();
        for index in 8..12 {
            let start = newer.entries().len();
            newer.append_prompt(format!("line-{index}"));
            newer.mark_history_entries(start, &format!("entry-{index}"));
        }
        assert!(transcript.append_history_page(&newer));
        cache = TranscriptRowCache::default();
        viewport.reduce(
            TranscriptViewAction::HistoryChanged,
            &transcript,
            &mut cache,
        );
        assert!(
            viewport.continue_navigation(
                false,
                outcome.remaining_rows(),
                &transcript,
                &mut cache,
                false,
            ) > 0
        );
        assert!(viewport.follows_tail());
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
    fn resize_invalidates_pending_reader_navigation() {
        let transcript = transcript_with(30);
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 20, 5);
        let generation = viewport
            .reduce(TranscriptViewAction::PageUp, &transcript, &mut cache)
            .reader_generation;

        viewport.set_geometry(&transcript, &mut cache, 9, 5);

        assert_ne!(viewport.reader_generation(), generation);
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
    fn retaining_history_cache_connects_a_loaded_page_to_the_previous_tail() {
        for live_suffix in [false, true] {
            let mut transcript = Transcript::default();
            let older = transcript.append_prompt("older".into());
            transcript.mark_history_entries(0, "older-entry");
            if live_suffix {
                transcript.append_prompt("live".into());
            }
            let mut cache = TranscriptRowCache::default();
            let top = RowAnchor {
                entry_id: older,
                position: RowPosition::Header,
            };
            let _ = collect_rows(&transcript, &mut cache, top, 40, 20);

            let mut page = Transcript::default();
            page.append_prompt("newer".into());
            page.mark_history_entries(0, "newer-entry");
            assert!(transcript.append_history_page(&page));
            cache.retain_entries(&transcript);

            let texts = collect_rows(&transcript, &mut cache, top, 40, 20)
                .iter()
                .map(TranscriptRow::plain_text)
                .collect::<Vec<_>>();
            assert!(texts.iter().any(|text| text == "newer"), "{texts:?}");
            assert_eq!(texts.iter().any(|text| text == "live"), live_suffix);
        }
    }

    #[test]
    fn user_lead_in_links_both_ways_after_a_history_page_adds_the_prompt() {
        let mut transcript = Transcript::default();
        let older = transcript.append_prompt("older".into());
        let answer = transcript.complete_message(1, "answer".into()).unwrap();
        transcript.mark_history_entries(0, "older-entry");
        let mut cache = TranscriptRowCache::default();
        let top = RowAnchor {
            entry_id: older,
            position: RowPosition::Header,
        };
        let _ = collect_rows(&transcript, &mut cache, top, 40, 20);

        let mut page = Transcript::default();
        page.append_prompt("newer".into());
        page.mark_history_entries(0, "newer-entry");
        assert!(transcript.append_history_page(&page));
        cache.retain_entries(&transcript);

        let rows = collect_rows(&transcript, &mut cache, top, 40, 20);
        let prompt = rows
            .iter()
            .position(|row| row.plain_text() == "newer")
            .expect("appended prompt");
        let [reply, spacer, lead_in, header] = &rows[prompt - 4..prompt] else {
            unreachable!()
        };
        assert_eq!(reply.plain_text(), "answer");
        assert_eq!(
            (spacer.anchor.entry_id, spacer.anchor.position, spacer.role),
            (answer, RowPosition::Spacer, TranscriptRole::Assistant)
        );
        assert_eq!(
            (
                lead_in.anchor.entry_id,
                lead_in.anchor.position,
                lead_in.role
            ),
            (answer, RowPosition::UserLeadIn, TranscriptRole::User)
        );
        assert_eq!(header.plain_text(), "you");

        let mut anchor = header.anchor;
        for expected in [lead_in, spacer, reply] {
            anchor = cache.previous_anchor(&transcript, anchor, 40).unwrap();
            assert_eq!(anchor, expected.anchor);
        }
    }

    #[test]
    fn cards_stack_between_rules() {
        let mut transcript = Transcript::default();
        let prompt = transcript.append_prompt("tidy up".into());
        let mut finish = |call_id: &str, name: &str, arguments: serde_json::Value| {
            let id = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
                call_id: call_id.into(),
                name: name.into(),
                detail_source: crate::tool_detail::ToolDetailSource::None,
                arguments,
            });
            let mut result = tool_result(call_id, "out\nmore");
            result.name = name.into();
            transcript.observe_tool_result(result);
            id
        };
        let first = finish("b1", "bash", serde_json::json!({"command": "cargo build"}));
        finish("b2", "bash", serde_json::json!({"command": "cargo test"}));
        let group = finish("r1", "read", serde_json::json!({"path": "a.rs"}));
        finish("r2", "read", serde_json::json!({"path": "b.rs"}));
        let last = finish("b3", "bash", serde_json::json!({"command": "cargo clippy"}));
        transcript.complete_message(1, "done".into());
        let mut cache = TranscriptRowCache::default();
        // An expanded card shows its result rows, so it keeps a blank row before the next card.
        cache.fold_mut().expand_group(first);
        cache.fold_mut().expand(first);
        let top = RowAnchor {
            entry_id: prompt,
            position: RowPosition::Header,
        };
        let rows = collect_rows(&transcript, &mut cache, top, 60, 40);
        let mut tool_rows = rows
            .iter()
            .skip_while(|row| row.anchor.entry_id != first)
            .map(|row| row.kind)
            .collect::<Vec<_>>();
        tool_rows.truncate(10);
        assert_eq!(
            tool_rows,
            [
                TranscriptRowKind::CardGroup,
                TranscriptRowKind::CardAction,
                TranscriptRowKind::CardDetail,
                TranscriptRowKind::CardDetail,
                TranscriptRowKind::CardDetail,
                TranscriptRowKind::Spacer,
                TranscriptRowKind::CardAction,
                TranscriptRowKind::Rule,
                TranscriptRowKind::CardGroup,
                TranscriptRowKind::Rule,
            ]
        );
        assert!(rows.iter().any(|row| row.anchor.entry_id == group));
        // The prompt's surface padding is followed by a plain gap before the first card.
        let after_prompt = rows
            .iter()
            .skip_while(|row| {
                row.anchor.entry_id != prompt || row.kind != TranscriptRowKind::Spacer
            })
            .take(3)
            .map(|row| (row.anchor.position, row.role))
            .collect::<Vec<_>>();
        assert_eq!(
            after_prompt,
            [
                (RowPosition::Spacer, TranscriptRole::User),
                (RowPosition::UserLeadOut, TranscriptRole::Tool),
                (RowPosition::Header, TranscriptRole::Tool),
            ]
        );
        // The reply keeps a blank row, not a rule, after the last card.
        let after_last = rows
            .iter()
            .skip_while(|row| row.anchor.entry_id != last)
            .nth(1)
            .unwrap();
        assert_eq!(
            (after_last.anchor.position, after_last.kind),
            (RowPosition::Spacer, TranscriptRowKind::Spacer)
        );

        // Walking back from the reply's header visits the same rows in reverse.
        let mut anchor = rows.last().unwrap().anchor;
        for expected in rows.iter().rev().skip(1) {
            anchor = cache.previous_anchor(&transcript, anchor, 60).unwrap();
            assert_eq!(anchor, expected.anchor);
        }
    }

    #[test]
    fn consecutive_prompts_share_one_spacer_without_a_lead_in() {
        let mut transcript = Transcript::default();
        let first = transcript.append_prompt("first".into());
        transcript.append_prompt("second".into());
        let mut cache = TranscriptRowCache::default();
        let top = RowAnchor {
            entry_id: first,
            position: RowPosition::Header,
        };
        let positions = collect_rows(&transcript, &mut cache, top, 40, 20)
            .iter()
            .map(|row| row.anchor.position)
            .collect::<Vec<_>>();
        assert!(positions.contains(&RowPosition::Spacer));
        assert!(!positions.contains(&RowPosition::UserLeadIn));
    }

    #[test]
    fn scrolled_lead_in_survives_a_history_page_landing_before_its_prompt() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("older".into());
        let answer = transcript.complete_message(1, "answer".into()).unwrap();
        transcript.mark_history_entries(0, "older-entry");
        transcript.append_prompt("live".into());
        transcript.complete_message(2, "live answer".into());
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 40, 3);
        viewport.reduce(TranscriptViewAction::Home, &transcript, &mut cache);
        let lead_in = RowAnchor {
            entry_id: answer,
            position: RowPosition::UserLeadIn,
        };
        while viewport.scrolled_top() != Some(lead_in) {
            let moved = viewport
                .reduce(
                    TranscriptViewAction::ScrollLines(1),
                    &transcript,
                    &mut cache,
                )
                .consumed_rows;
            assert_eq!(moved, 1, "never reached the lead-in");
        }

        // A page that starts with a reply lands between the answer and the live prompt.
        let mut page = Transcript::default();
        page.complete_message(9, "middle".into());
        page.mark_history_entries(0, "middle-entry");
        assert!(transcript.append_history_page(&page));
        cache.retain_entries(&transcript);
        viewport.reduce(
            TranscriptViewAction::HistoryChanged,
            &transcript,
            &mut cache,
        );

        let rows = viewport.visible_rows(&transcript, &mut cache);
        assert_eq!(
            rows.first().map(|row| row.anchor),
            Some(RowAnchor {
                entry_id: answer,
                position: RowPosition::Spacer,
            })
        );
        let mut seen = Vec::new();
        for _ in 0..12 {
            seen.extend(
                viewport
                    .visible_rows(&transcript, &mut cache)
                    .iter()
                    .map(TranscriptRow::plain_text),
            );
            viewport.reduce(
                TranscriptViewAction::ScrollLines(1),
                &transcript,
                &mut cache,
            );
        }
        assert!(seen.iter().any(|text| text == "middle"));
        assert!(seen.iter().any(|text| text == "live"));
    }

    #[test]
    fn retaining_history_cache_removes_links_into_an_evicted_suffix() {
        let mut transcript = Transcript::default();
        let older = transcript.append_prompt("older".into());
        transcript.mark_history_entries(0, "older-entry");
        let start = transcript.entries().len();
        transcript.append_prompt("newer".into());
        transcript.mark_history_entries(start, "newer-entry");
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 40, 2);
        viewport.reduce(TranscriptViewAction::Home, &transcript, &mut cache);
        let _ = viewport.visible_rows(&transcript, &mut cache);

        transcript.retain_historical_entries(1, true).unwrap();
        cache.retain_entries(&transcript);
        let last = cache.last_anchor(&transcript, transcript.entry(older).unwrap(), 40);
        assert!(cache.row_at(&transcript, last, 40).unwrap().next.is_none());
        viewport.reduce(
            TranscriptViewAction::HistoryChanged,
            &transcript,
            &mut cache,
        );
        viewport.reduce(TranscriptViewAction::PageDown, &transcript, &mut cache);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        assert!(rows.iter().any(|row| row.plain_text() == "older"));
        assert!(rows.iter().all(|row| row.anchor.entry_id == older));
    }

    #[test]
    fn retaining_history_cache_keeps_the_viewport_anchored_after_a_prepend() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("current\n".repeat(30));
        transcript.mark_history_entries(0, "current-entry");
        let mut cache = TranscriptRowCache::default();
        let mut viewport = TranscriptViewport::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 20);
        viewport.reduce(TranscriptViewAction::Home, &transcript, &mut cache);
        let before = viewport.visible_rows(&transcript, &mut cache);

        let mut page = Transcript::default();
        page.append_prompt("older".into());
        page.mark_history_entries(0, "older-entry");
        assert!(transcript.prepend_history_page(&page));
        cache.retain_entries(&transcript);
        viewport.reduce(
            TranscriptViewAction::HistoryChanged,
            &transcript,
            &mut cache,
        );
        assert_eq!(viewport.visible_rows(&transcript, &mut cache), before);

        viewport.reduce(
            TranscriptViewAction::ScrollLines(-20),
            &transcript,
            &mut cache,
        );
        let texts = viewport
            .visible_rows(&transcript, &mut cache)
            .iter()
            .map(TranscriptRow::plain_text)
            .collect::<Vec<_>>();
        let older = texts
            .iter()
            .position(|text| text == "older")
            .expect("prepended page");
        let current = texts
            .iter()
            .position(|text| text == "current")
            .expect("retained page");
        assert!(older < current, "{texts:?}");
    }

    #[test]
    fn retaining_entries_keeps_surviving_layout_and_drops_evicted_entries() {
        let mut transcript = Transcript::default();
        let current = transcript.append_prompt("current".into());
        transcript.mark_history_entries(0, "current-entry");
        let mut cache = TranscriptRowCache::default();
        let anchor = RowAnchor {
            entry_id: current,
            position: RowPosition::Header,
        };
        assert!(cache.row_at(&transcript, anchor, 40).is_some());

        // A prepended page leaves the surviving entry's cached layout reusable.
        let mut older = Transcript::default();
        older.append_prompt("older".into());
        older.mark_history_entries(0, "older-entry");
        assert!(transcript.prepend_history_page(&older));
        cache.retain_entries(&transcript);
        cache.reset_work();
        assert!(cache.row_at(&transcript, anchor, 40).is_some());
        assert_eq!(cache.work().rows_built, 0, "surviving row was rebuilt");
        assert_eq!(cache.work().cache_hits, 1);

        // Evicting the entry drops its rows and the bookkeeping that indexed them.
        transcript.retain_historical_entries(0, false).unwrap();
        cache.retain_entries(&transcript);
        assert!(transcript.entry(current).is_none());
        assert!(cache.rows.keys().all(|key| key.entry_id != current));
        assert!(
            cache
                .insertion_order
                .iter()
                .all(|key| key.entry_id != current)
        );
        assert_eq!(
            cache.retained_bytes,
            cache
                .rows
                .values()
                .map(|cached| cached.row.retained_bytes())
                .sum::<usize>(),
        );
    }

    #[test]
    fn history_changes_preserve_surviving_anchors_and_fall_back_after_eviction() {
        let mut transcript = Transcript::default();
        let current = transcript.append_prompt("current".into());
        transcript.mark_history_entries(0, "current-entry");
        let live = transcript.append_prompt("live".into());
        let mut viewport = TranscriptViewport {
            top: Some(RowAnchor {
                entry_id: current,
                position: RowPosition::Header,
            }),
            follow_tail: false,
            ..TranscriptViewport::default()
        };
        let mut cache = TranscriptRowCache::default();
        let mut older = Transcript::default();
        older.append_prompt("older".into());
        older.mark_history_entries(0, "older-entry");

        assert!(transcript.prepend_history_page(&older));
        viewport.reduce(
            TranscriptViewAction::HistoryChanged,
            &transcript,
            &mut cache,
        );
        assert_eq!(viewport.top.unwrap().entry_id, current);

        viewport.top = Some(RowAnchor {
            entry_id: transcript.entries()[0].id,
            position: RowPosition::Header,
        });
        transcript.retain_historical_entries(0, false).unwrap();
        cache = TranscriptRowCache::default();
        viewport.reduce(
            TranscriptViewAction::HistoryChanged,
            &transcript,
            &mut cache,
        );
        assert_eq!(viewport.top.unwrap().entry_id, live);
        assert!(!viewport.follow_tail);
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
    fn visible_work_is_independent_of_transcript_entry_count() {
        fn measured_work(entry_count: usize) -> (LayoutWork, LayoutWork) {
            let mut transcript = Transcript::default();
            for turn in 0..entry_count / 2 {
                transcript.append_exchange("same prompt".into());
                transcript.complete_message(turn as u64, "**same response**".into());
            }
            let mut viewport = TranscriptViewport::default();
            let mut cache = TranscriptRowCache::default();
            viewport.set_geometry(&transcript, &mut cache, 100, 15);
            let rows = viewport.visible_rows(&transcript, &mut cache);
            assert!(
                viewport
                    .scrollbar_range(&transcript, &mut cache, &rows)
                    .is_some()
            );
            let cold = cache.work();
            cache.reset_work();
            let rows = viewport.visible_rows(&transcript, &mut cache);
            assert!(
                viewport
                    .scrollbar_range(&transcript, &mut cache, &rows)
                    .is_some()
            );
            (cold, cache.work())
        }

        let small = measured_work(1_000);
        let large = measured_work(100_000);

        assert_eq!(large, small);
        assert_eq!(large.1.rows_built, 0);
        assert_eq!(large.1.markdown_source_bytes_parsed, 0);
        assert_eq!(large.1.syntax_source_bytes, 0);
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
                detail_source: crate::tool_detail::ToolDetailSource::None,
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
    fn moving_historical_process_to_live_suffix_invalidates_cached_links() {
        let mut transcript = Transcript::default();
        let older = transcript.append_prompt("older".into());
        transcript.mark_history_entries(0, "older-entry");
        let process_start = transcript.entries().len();
        let process = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "poll-history".into(),
            name: "bash".into(),
            arguments: serde_json::json!({"operation": "poll", "process_id": "process-live"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
        });
        let mut completed = tool_result("poll-history", "historical output");
        completed.name = "bash".into();
        completed.process_id = Some("process-live".into());
        completed.process_state = Some("completed".into());
        completed.stdout = Some("historical output".into());
        completed.stdout_source_bytes = 17;
        transcript.observe_tool_result(completed);
        transcript.mark_history_entries(process_start, "process-entry");
        let newer_start = transcript.entries().len();
        let newer = transcript.append_prompt("newer".into());
        transcript.mark_history_entries(newer_start, "newer-entry");

        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 40);
        viewport.reduce(TranscriptViewAction::Home, &transcript, &mut cache);
        let _ = viewport.visible_rows(&transcript, &mut cache);

        let reused = transcript.observe_tool_call(crate::tool_cards::ToolCallInput {
            call_id: "poll-live".into(),
            name: "bash".into(),
            arguments: serde_json::json!({"operation": "poll", "process_id": "process-live"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
        });
        assert_eq!(reused, process);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let mut rendered_order = Vec::new();
        for row in rows {
            if rendered_order.last() != Some(&row.anchor.entry_id) {
                rendered_order.push(row.anchor.entry_id);
            }
        }

        assert_eq!(
            transcript
                .entries()
                .iter()
                .map(|entry| entry.id)
                .collect::<Vec<_>>(),
            vec![older, newer, process]
        );
        assert_eq!(rendered_order, vec![older, newer, process]);
    }

    #[test]
    fn large_messages_switch_to_complete_plain_text_without_an_omission() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        let assistant = transcript.start_message(1);
        transcript.append_message_delta(1, &"x".repeat(MARKDOWN_SOURCE_MAX_BYTES - 1));
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
                position: RowPosition::Content(0),
                ..
            })
        ));
    }

    #[test]
    fn scrolled_markdown_anchor_survives_switch_to_full_plain_text() {
        let mut transcript = Transcript::default();
        let assistant = transcript.start_message(1);
        transcript.append_message_delta(1, &"**界 heading**\n".repeat(1_000));
        let mut cache = TranscriptRowCache::default();
        let mut viewport = TranscriptViewport::default();
        viewport.set_geometry(&transcript, &mut cache, 80, 8);
        viewport.reduce(TranscriptViewAction::PageUp, &transcript, &mut cache);
        let old = viewport.top.unwrap();
        assert!(matches!(old.position, RowPosition::Markdown(_)));
        transcript.append_message_delta(1, &"界 tail\n".repeat(10_000));
        viewport.reduce(
            TranscriptViewAction::HistoryChanged,
            &transcript,
            &mut cache,
        );
        assert!(!viewport.follows_tail());
        let anchor = viewport.top.unwrap();
        assert_eq!(anchor.entry_id, assistant);
        assert!(matches!(anchor.position, RowPosition::Content(_)));
        let rows = viewport.visible_rows(&transcript, &mut cache);
        assert!(!rows.is_empty());
        assert!(rows.iter().any(|row| row.plain_text().contains("界")));
        viewport.reduce(TranscriptViewAction::Home, &transcript, &mut cache);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        assert!(
            rows.iter()
                .any(|row| row.plain_text().contains("**界 heading**"))
        );
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
        let snapshot = cache.markdown_snapshot(entry, 80);
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
        let snapshot = cache.markdown_snapshot(entry, 80);
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
    fn widening_and_promoting_a_table_invalidates_cached_rows() {
        let mut transcript = Transcript::default();
        let (_, assistant) = transcript.append_exchange("prompt".into());
        transcript.start_message(1);
        transcript.append_message_delta(1, "| A | B |\n| --- | --- |\n| x | y |\n");
        let anchor = RowAnchor {
            entry_id: assistant,
            position: RowPosition::Markdown(MarkdownPosition {
                source_offset: 0,
                output_offset: 0,
            }),
        };
        let mut cache = TranscriptRowCache::default();
        assert_eq!(
            cache
                .row_at(&transcript, anchor, 80)
                .unwrap()
                .row
                .plain_text(),
            "┌───┬───┐"
        );
        transcript.append_message_delta(1, "| longer name | z |\n\nAfter");
        let warm = cache.row_at(&transcript, anchor, 80).unwrap().row;
        let fresh = TranscriptRowCache::default()
            .row_at(&transcript, anchor, 80)
            .unwrap()
            .row;
        assert_eq!(warm, fresh);
        assert_eq!(warm.plain_text(), "┌─────────────┬───┐");
    }

    #[test]
    fn narrow_tables_wrap_without_losing_cells_or_reparsing_on_resize() {
        let source = "| Name | Count |\n| --- | ---: |\n| 界界界 | 12345 |";
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.complete_message(1, source.into());
        let mut expected = "NameCount界界界12345".chars().collect::<Vec<_>>();
        expected.sort_unstable();
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        for width in [80, 8, 12, 80] {
            viewport.set_geometry(&transcript, &mut cache, width, 40);
            let rows = viewport.visible_rows(&transcript, &mut cache);
            let content = rows
                .iter()
                .filter(|row| {
                    row.role == TranscriptRole::Assistant && row.kind == TranscriptRowKind::Content
                })
                .map(TranscriptRow::plain_text)
                .collect::<Vec<_>>();
            assert!(
                content
                    .iter()
                    .all(|row| unicode_width::UnicodeWidthStr::width(row.as_str()) <= width)
            );
            let mut letters = content
                .concat()
                .chars()
                .filter(|ch| ch.is_alphanumeric())
                .collect::<Vec<_>>();
            letters.sort_unstable();
            assert_eq!(letters, expected);
            let mut fresh_viewport = TranscriptViewport::default();
            let mut fresh_cache = TranscriptRowCache::default();
            fresh_viewport.set_geometry(&transcript, &mut fresh_cache, width, 40);
            assert_eq!(
                rows,
                fresh_viewport.visible_rows(&transcript, &mut fresh_cache)
            );
            if width != 80 {
                assert_eq!(cache.work().markdown_source_bytes_parsed, 0);
            }
            cache.reset_work();
        }
    }

    #[test]
    fn headerless_table_detection_and_promotion_refresh_cached_literal_rows() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.start_message(1);
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        for (chunk, expected) in [
            ("| read | text |\n", "| read | text |"),
            ("| write | file |\n", "│ read  │ text │"),
            ("| longer name | more |\n\nAfter", "│ read        │ text │"),
        ] {
            transcript.append_message_delta(1, chunk);
            viewport.set_geometry(&transcript, &mut cache, 40, 20);
            let warm = viewport.visible_rows(&transcript, &mut cache);
            let mut fresh_viewport = TranscriptViewport::default();
            let mut fresh_cache = TranscriptRowCache::default();
            fresh_viewport.set_geometry(&transcript, &mut fresh_cache, 40, 20);
            assert_eq!(
                warm,
                fresh_viewport.visible_rows(&transcript, &mut fresh_cache)
            );
            assert!(
                warm.iter()
                    .any(|row| row.plain_text().trim_end() == expected)
            );
        }
        cache.reset_work();
        viewport.set_geometry(&transcript, &mut cache, 8, 20);
        let narrow = viewport.visible_rows(&transcript, &mut cache);
        let text = narrow
            .iter()
            .filter(|row| {
                row.role == TranscriptRole::Assistant && row.kind == TranscriptRowKind::Content
            })
            .map(TranscriptRow::plain_text)
            .collect::<String>();
        assert_eq!(
            text.chars()
                .filter(|ch| ch.is_alphanumeric())
                .collect::<String>(),
            "readtextwritefilelongernamemoreAfter"
        );
        assert_eq!(cache.work().markdown_source_bytes_parsed, 0);
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
    fn prose_wraps_at_words_without_splitting_commands() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.complete_message(
            1,
            "Runs tests and long-running commands. Retrieves their output.".into(),
        );
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 30, 20);
        let lines = viewport
            .visible_rows(&transcript, &mut cache)
            .into_iter()
            .filter(|row| {
                row.role == TranscriptRole::Assistant && row.kind == TranscriptRowKind::Content
            })
            .map(|row| row.plain_text())
            .collect::<Vec<_>>();
        assert_eq!(
            lines,
            [
                "Runs tests and long-running",
                "commands. Retrieves their",
                "output."
            ]
        );
    }

    #[test]
    fn resizing_a_wrapped_code_cell_keeps_the_visible_source_row() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("prompt".into());
        transcript.complete_message(1, "| path | `alpha beta gamma delta epsilon zeta eta theta iota kappa lambda mu` |\n| other | value |".into());
        let mut viewport = TranscriptViewport::default();
        let mut cache = TranscriptRowCache::default();
        viewport.set_geometry(&transcript, &mut cache, 30, 30);
        let rows = viewport.visible_rows(&transcript, &mut cache);
        let anchor = rows
            .iter()
            .find(|row| row.plain_text().contains("theta"))
            .unwrap()
            .anchor;
        let restored = normalize_anchor(
            &transcript,
            &mut cache,
            anchor,
            38,
            AnchorNormalization::Geometry,
        )
        .unwrap();
        let text = cache
            .row_at(&transcript, restored, 38)
            .unwrap()
            .row
            .plain_text();
        assert!(text.contains("theta"), "{text}");
        assert!(!text.contains("alpha"), "{text}");
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
            let _ = cache.markdown_snapshot(entry, 80);
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
            work.bytes_scanned <= 2 * 1024 * 1024,
            "unbroken content was repeatedly rescanned: {work:?}"
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
