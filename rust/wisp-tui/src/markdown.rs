//! Bounded, terminal-independent Markdown presentation for assistant transcript entries.

use std::ops::Range;
use std::sync::Arc;

use pulldown_cmark::{
    Alignment, BrokenLink, CodeBlockKind, Event, HeadingLevel, Options, Parser, RefDefs, Tag,
    TagEnd,
};
use unicode_width::UnicodeWidthStr;

use crate::syntax::{
    MAX_SYNTAX_FRAGMENTS_PER_BUILD, MAX_SYNTAX_SOURCE_BYTES_PER_BUILD, SyntaxClass,
    SyntaxHighlight, highlight_fence,
};

mod table_layout;
pub(crate) use table_layout::layout_document;

const REFERENCE_DEFINITION_MARKER: &str = "]:";
const MAX_MUTABLE_SOURCE_BYTES: usize = 8 * 1024;
const MAX_PRESENTATION_OUTPUT_BYTES: usize = 256 * 1024;
const MAX_PRESENTATION_FRAGMENTS: usize = 4_096;
const MAX_PRESENTATION_BLOCKS: usize = 2_048;
const MAX_PRESENTATION_RETAINED_BYTES: usize = 1024 * 1024;
const PRESENTATION_TRUNCATED: &str = "… Markdown presentation truncated …";
const MARKDOWN_OPTIONS: Options = Options::ENABLE_STRIKETHROUGH
    .union(Options::ENABLE_TABLES)
    .union(Options::ENABLE_TASKLISTS);

#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub enum BlockStyle {
    #[default]
    Normal,
    Heading(u8),
    Code,
    RawHtml,
}

#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub enum InlineStyle {
    #[default]
    Normal,
    Code,
    Link,
    QuoteMarker,
    ListMarker,
    TableBorder,
    ToolName,
    ToolStatus,
}

#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub struct TranscriptSpanStyle {
    pub block: BlockStyle,
    pub inline: InlineStyle,
    pub syntax: SyntaxClass,
    pub strong: bool,
    pub emphasis: bool,
    pub struck: bool,
}

#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub struct SourceAffinity {
    pub source_offset: usize,
    pub source_end: usize,
    pub output_offset: u32,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TranscriptSpan {
    pub text: String,
    pub style: TranscriptSpanStyle,
    pub affinity: SourceAffinity,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct MarkdownBlock {
    pub source: Range<usize>,
    pub spans: Vec<TranscriptSpan>,
    has_table: bool,
    tables: Vec<MarkdownTable>,
    syntax_source_bytes: usize,
    syntax_fragments: usize,
    syntax_attempted_source_bytes: usize,
    syntax_attempted_fragments: usize,
    truncated: bool,
}

#[derive(Clone, Debug, PartialEq)]
struct MarkdownTable {
    spans: Range<usize>,
    rows: Vec<Vec<Range<usize>>>,
    alignments: Vec<Alignment>,
    continuation_prefix: String,
}

impl Eq for MarkdownTable {}

impl MarkdownBlock {
    #[cfg(test)]
    pub fn plain_text(&self) -> String {
        self.spans.iter().map(|span| span.text.as_str()).collect()
    }

    fn has_syntax(&self) -> bool {
        self.spans
            .iter()
            .any(|span| span.style.syntax != SyntaxClass::Plain)
    }

    pub fn retained_bytes(&self) -> usize {
        self.spans
            .iter()
            .map(|span| span.text.len())
            .sum::<usize>()
            .saturating_add(
                self.spans
                    .len()
                    .saturating_mul(std::mem::size_of::<TranscriptSpan>()),
            )
            .saturating_add(
                self.tables
                    .iter()
                    .map(|table| {
                        std::mem::size_of::<MarkdownTable>()
                            + table.continuation_prefix.len()
                            + table.alignments.len() * std::mem::size_of::<Alignment>()
                            + table
                                .rows
                                .iter()
                                .map(|row| {
                                    std::mem::size_of::<Vec<Range<usize>>>()
                                        + row.len() * std::mem::size_of::<Range<usize>>()
                                })
                                .sum::<usize>()
                    })
                    .sum::<usize>(),
            )
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct MarkdownDocument {
    pub blocks: Vec<Arc<MarkdownBlock>>,
}

impl MarkdownDocument {
    pub(crate) fn has_tables(&self) -> bool {
        self.blocks.iter().any(|block| block.has_table)
    }
    #[cfg(test)]
    pub fn plain_text(&self) -> String {
        self.blocks
            .iter()
            .map(|block| block.plain_text())
            .collect::<Vec<_>>()
            .join("\n")
    }

    pub fn retained_bytes(&self) -> usize {
        self.blocks
            .iter()
            .map(|block| block.retained_bytes())
            .sum::<usize>()
            .saturating_add(
                self.blocks
                    .len()
                    .saturating_mul(std::mem::size_of::<Arc<MarkdownBlock>>()),
            )
    }
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub struct MarkdownWork {
    pub source_bytes_parsed: usize,
    pub source_bytes_reused: usize,
    pub blocks_built: usize,
    pub full_reparses: usize,
    pub incremental_builds: usize,
    pub fragments_emitted: usize,
    pub syntax_fences_considered: usize,
    pub syntax_fences_highlighted: usize,
    pub syntax_fallbacks: usize,
    pub syntax_source_bytes: usize,
    pub syntax_lines: usize,
    pub syntax_fragments: usize,
}

#[derive(Clone, Debug, Default)]
pub struct MarkdownBuild {
    pub document: MarkdownDocument,
    pub stable_blocks: usize,
    pub presentation_epoch: u64,
    pub work: MarkdownWork,
}

#[derive(Clone, Debug, Default)]
pub struct IncrementalMarkdownState {
    layout_epoch: Option<u64>,
    presentation_start: usize,
    stable_source_end: usize,
    stable_blocks: Vec<Arc<MarkdownBlock>>,
    full_reparse_only: bool,
    used_literal_checkpoint: bool,
    presentation_epoch: u64,
    mutable_syntax_presentations: Vec<(Range<usize>, bool)>,
}

impl IncrementalMarkdownState {
    pub fn reset(&mut self) {
        *self = Self::default();
    }

    pub fn build(
        &mut self,
        source: &str,
        presentation_start: usize,
        layout_epoch: u64,
        settled: bool,
    ) -> MarkdownBuild {
        if self.layout_epoch != Some(layout_epoch)
            || self.presentation_start != presentation_start
            || self.stable_source_end > source.len()
        {
            self.reset();
            self.layout_epoch = Some(layout_epoch);
            self.presentation_start = presentation_start;
        }

        if settled && self.used_literal_checkpoint {
            self.stable_source_end = 0;
            self.stable_blocks.clear();
            self.used_literal_checkpoint = false;
            self.bump_presentation_epoch();
        }

        let was_full_reparse_only = self.full_reparse_only;
        let mutable_source = &source[self.stable_source_end..];
        if mutable_source.contains(REFERENCE_DEFINITION_MARKER) {
            if !self.full_reparse_only {
                self.bump_presentation_epoch();
            }
            self.full_reparse_only = true;
            self.stable_source_end = 0;
            self.stable_blocks.clear();
        }

        if self.full_reparse_only {
            let parsed = parse_blocks(source, presentation_start, SyntaxUsage::default());
            let blocks = parsed.blocks;
            let syntax_work = parsed.syntax_work;
            let work = MarkdownWork {
                source_bytes_parsed: source.len(),
                source_bytes_reused: 0,
                blocks_built: blocks.len(),
                full_reparses: 1,
                incremental_builds: 0,
                fragments_emitted: blocks.iter().map(|block| block.spans.len()).sum(),
                syntax_fences_considered: syntax_work.fences_considered,
                syntax_fences_highlighted: syntax_work.fences_highlighted,
                syntax_fallbacks: syntax_work.fallbacks,
                syntax_source_bytes: syntax_work.source_bytes,
                syntax_lines: syntax_work.lines,
                syntax_fragments: syntax_work.fragments,
            };
            if was_full_reparse_only
                && (syntax_presentation_changed(&self.mutable_syntax_presentations, &blocks)
                    || (settled && blocks.iter().any(|block| block.has_table)))
            {
                self.bump_presentation_epoch();
            }
            if settled {
                self.stable_source_end = source.len();
                self.stable_blocks = blocks.clone();
                self.full_reparse_only = false;
                self.mutable_syntax_presentations.clear();
            } else {
                self.mutable_syntax_presentations = syntax_presentations(&blocks);
            }
            let stable_blocks = if settled { blocks.len() } else { 0 };
            return MarkdownBuild {
                document: MarkdownDocument { blocks },
                stable_blocks,
                presentation_epoch: self.presentation_epoch,
                work,
            };
        }

        let checkpointed = if !settled
            && source.len().saturating_sub(self.stable_source_end) > MAX_MUTABLE_SOURCE_BYTES
        {
            let checkpoint_end = mutable_checkpoint_end(source, self.stable_source_end);
            let checkpoint = &source[self.stable_source_end..checkpoint_end];
            self.stable_blocks.push(Arc::new(literal_block(
                checkpoint,
                presentation_start + self.stable_source_end,
            )));
            let checkpointed = checkpoint.len();
            self.stable_source_end = checkpoint_end;
            self.used_literal_checkpoint = true;
            self.bump_presentation_epoch();
            checkpointed
        } else {
            0
        };
        let reused = self.stable_source_end;
        let mutable_source = &source[reused..];
        let parsed_build = parse_blocks(
            mutable_source,
            presentation_start + reused,
            SyntaxUsage::from_blocks(&self.stable_blocks),
        );
        let syntax_work = parsed_build.syntax_work;
        let parsed = parsed_build.blocks;
        let promote = if settled {
            parsed.len()
        } else {
            parsed.len().saturating_sub(1)
        };
        if promote > 0 {
            self.stable_source_end = parsed[promote - 1]
                .source
                .end
                .saturating_sub(presentation_start);
            // A final table row can widen earlier columns in the same append that
            // promotes the block. Rows cached while it was mutable must be rebuilt.
            if syntax_presentation_changed(&self.mutable_syntax_presentations, &parsed[..promote])
                || parsed[..promote].iter().any(|block| block.has_table)
            {
                self.bump_presentation_epoch();
            }
            self.stable_blocks.extend(parsed[..promote].iter().cloned());
        }
        self.mutable_syntax_presentations = syntax_presentations(&parsed[promote..]);
        let mut document = self.stable_blocks.clone();
        document.extend(parsed[promote..].iter().cloned());
        let presentation_source_end = presentation_start.saturating_add(source.len());
        let was_truncated = self
            .stable_blocks
            .last()
            .is_some_and(|block| is_truncation_block(block));
        let document_truncated = enforce_document_budget(&mut document, presentation_source_end);
        if document_truncated {
            if !was_truncated {
                self.bump_presentation_epoch();
            }
            self.stable_source_end = source.len();
            self.stable_blocks = document.clone();
            self.mutable_syntax_presentations.clear();
        }
        let work = MarkdownWork {
            source_bytes_parsed: checkpointed.saturating_add(mutable_source.len()),
            source_bytes_reused: reused,
            blocks_built: parsed.len(),
            full_reparses: 0,
            incremental_builds: 1,
            fragments_emitted: parsed.iter().map(|block| block.spans.len()).sum(),
            syntax_fences_considered: syntax_work.fences_considered,
            syntax_fences_highlighted: syntax_work.fences_highlighted,
            syntax_fallbacks: syntax_work.fallbacks,
            syntax_source_bytes: syntax_work.source_bytes,
            syntax_lines: syntax_work.lines,
            syntax_fragments: syntax_work.fragments,
        };
        MarkdownBuild {
            document: MarkdownDocument { blocks: document },
            stable_blocks: self.stable_blocks.len(),
            presentation_epoch: self.presentation_epoch,
            work,
        }
    }

    fn bump_presentation_epoch(&mut self) {
        self.presentation_epoch = self
            .presentation_epoch
            .checked_add(1)
            .expect("Markdown presentation epoch exhausted");
    }
}

fn syntax_presentations(blocks: &[Arc<MarkdownBlock>]) -> Vec<(Range<usize>, bool)> {
    blocks
        .iter()
        .map(|block| (block.source.clone(), block.has_syntax()))
        .collect()
}

fn syntax_presentation_changed(
    previous: &[(Range<usize>, bool)],
    current: &[Arc<MarkdownBlock>],
) -> bool {
    !previous
        .iter()
        .filter_map(|(source, highlighted)| highlighted.then_some(source))
        .eq(current
            .iter()
            .filter(|block| block.has_syntax())
            .map(|block| &block.source))
}

fn mutable_checkpoint_end(source: &str, stable_start: usize) -> usize {
    let raw = source.len().saturating_sub(MAX_MUTABLE_SOURCE_BYTES);
    let mut cut = raw.max(stable_start);
    while cut < source.len() && !source.is_char_boundary(cut) {
        cut += 1;
    }
    let search_end = cut.saturating_add(1024).min(source.len());
    if let Some(newline) = source[cut..search_end].find('\n') {
        cut += newline + 1;
    }
    cut.max(stable_start)
}

fn literal_block(source: &str, base_offset: usize) -> MarkdownBlock {
    MarkdownBlock {
        source: base_offset..(base_offset + source.len()),
        has_table: false,
        tables: Vec::new(),
        spans: if source.is_empty() {
            Vec::new()
        } else {
            vec![TranscriptSpan {
                text: source.to_owned(),
                style: TranscriptSpanStyle::default(),
                affinity: SourceAffinity {
                    source_offset: base_offset,
                    source_end: base_offset + source.len(),
                    output_offset: 0,
                },
            }]
        },
        syntax_source_bytes: 0,
        syntax_fragments: 0,
        syntax_attempted_source_bytes: 0,
        syntax_attempted_fragments: 0,
        truncated: false,
    }
}

fn truncation_block(source_offset: usize) -> Arc<MarkdownBlock> {
    Arc::new(MarkdownBlock {
        source: source_offset..source_offset,
        has_table: false,
        tables: Vec::new(),
        spans: vec![TranscriptSpan {
            text: PRESENTATION_TRUNCATED.to_owned(),
            style: TranscriptSpanStyle {
                inline: InlineStyle::ListMarker,
                ..TranscriptSpanStyle::default()
            },
            affinity: SourceAffinity {
                source_offset,
                source_end: source_offset,
                output_offset: 0,
            },
        }],
        syntax_source_bytes: 0,
        syntax_fragments: 0,
        syntax_attempted_source_bytes: 0,
        syntax_attempted_fragments: 0,
        truncated: true,
    })
}

fn finish_truncation_block(block: &mut Arc<MarkdownBlock>, source_end: usize) {
    let block = Arc::make_mut(block);
    block.source.end = source_end;
    if let Some(span) = block.spans.first_mut() {
        span.affinity.source_end = source_end;
    }
}

fn is_truncation_block(block: &MarkdownBlock) -> bool {
    block.truncated
}

#[derive(Default)]
struct ParseBudget {
    retained_bytes: usize,
    fragments: usize,
    truncated: bool,
}

#[derive(Default)]
struct ParsedBlocks {
    blocks: Vec<Arc<MarkdownBlock>>,
    syntax_work: SyntaxBuildWork,
}

#[derive(Clone, Copy, Debug, Default)]
struct SyntaxBuildWork {
    fences_considered: usize,
    fences_highlighted: usize,
    fallbacks: usize,
    source_bytes: usize,
    lines: usize,
    fragments: usize,
}

#[derive(Clone, Copy, Debug, Default)]
struct SyntaxUsage {
    source_bytes: usize,
    fragments: usize,
    attempted_source_bytes: usize,
    attempted_fragments: usize,
}

impl SyntaxUsage {
    fn from_blocks(blocks: &[Arc<MarkdownBlock>]) -> Self {
        blocks.iter().fold(Self::default(), |usage, block| Self {
            source_bytes: usage.source_bytes.saturating_add(block.syntax_source_bytes),
            fragments: usage.fragments.saturating_add(block.syntax_fragments),
            attempted_source_bytes: usage
                .attempted_source_bytes
                .saturating_add(block.syntax_attempted_source_bytes),
            attempted_fragments: usage
                .attempted_fragments
                .saturating_add(block.syntax_attempted_fragments),
        })
    }
}

#[derive(Default)]
struct SyntaxBuildBudget {
    usage: SyntaxUsage,
    attempted_source_bytes: usize,
    attempted_fragments: usize,
    work: SyntaxBuildWork,
}

impl SyntaxBuildBudget {
    fn new(usage: SyntaxUsage) -> Self {
        Self {
            attempted_source_bytes: usage.attempted_source_bytes,
            attempted_fragments: usage.attempted_fragments,
            usage,
            ..Self::default()
        }
    }

    fn attempt(&mut self, info: &str, source: &str, closed: bool) -> Option<SyntaxHighlight> {
        self.work.fences_considered = self.work.fences_considered.saturating_add(1);
        if !closed
            || self.usage.source_bytes.saturating_add(source.len())
                > MAX_SYNTAX_SOURCE_BYTES_PER_BUILD
            || self.attempted_source_bytes.saturating_add(source.len())
                > MAX_SYNTAX_SOURCE_BYTES_PER_BUILD
            || self.attempted_fragments >= MAX_SYNTAX_FRAGMENTS_PER_BUILD
        {
            self.work.fallbacks = self.work.fallbacks.saturating_add(1);
            return None;
        }
        match highlight_fence(info, source) {
            Ok(highlighted) => {
                self.attempted_source_bytes = self
                    .attempted_source_bytes
                    .saturating_add(highlighted.work.source_bytes);
                self.work.source_bytes = self
                    .work
                    .source_bytes
                    .saturating_add(highlighted.work.source_bytes);
                self.work.lines = self.work.lines.saturating_add(highlighted.work.lines);
                Some(highlighted)
            }
            Err(failure) => {
                self.attempted_source_bytes = self
                    .attempted_source_bytes
                    .saturating_add(failure.work.source_bytes);
                self.attempted_fragments = self
                    .attempted_fragments
                    .saturating_add(failure.work.fragments);
                self.work.source_bytes = self
                    .work
                    .source_bytes
                    .saturating_add(failure.work.source_bytes);
                self.work.lines = self.work.lines.saturating_add(failure.work.lines);
                self.work.fragments = self.work.fragments.saturating_add(failure.work.fragments);
                self.work.fallbacks = self.work.fallbacks.saturating_add(1);
                None
            }
        }
    }

    fn commit(&mut self, source_bytes: usize, mapped_fragments: usize) -> bool {
        let exceeds_budget = self.usage.fragments.saturating_add(mapped_fragments)
            > MAX_SYNTAX_FRAGMENTS_PER_BUILD
            || self.attempted_fragments.saturating_add(mapped_fragments)
                > MAX_SYNTAX_FRAGMENTS_PER_BUILD;
        self.attempted_fragments = self.attempted_fragments.saturating_add(mapped_fragments);
        self.work.fragments = self.work.fragments.saturating_add(mapped_fragments);
        if exceeds_budget {
            self.work.fallbacks = self.work.fallbacks.saturating_add(1);
            return false;
        }
        self.usage.source_bytes = self.usage.source_bytes.saturating_add(source_bytes);
        self.usage.fragments = self.usage.fragments.saturating_add(mapped_fragments);
        self.work.fences_highlighted = self.work.fences_highlighted.saturating_add(1);
        true
    }
}

fn block_retained_bytes(block: &MarkdownBlock) -> usize {
    block
        .retained_bytes()
        .saturating_add(std::mem::size_of::<Arc<MarkdownBlock>>())
}

fn block_fits_budget(block_count: usize, budget: &ParseBudget, block: &MarkdownBlock) -> bool {
    block_count < MAX_PRESENTATION_BLOCKS
        && budget
            .retained_bytes
            .saturating_add(block_retained_bytes(block))
            <= MAX_PRESENTATION_RETAINED_BYTES
        && budget.fragments.saturating_add(block.spans.len()) <= MAX_PRESENTATION_FRAGMENTS
}

fn record_block(budget: &mut ParseBudget, block: &MarkdownBlock) {
    budget.retained_bytes = budget
        .retained_bytes
        .saturating_add(block_retained_bytes(block));
    budget.fragments = budget.fragments.saturating_add(block.spans.len());
}

fn append_truncation_block(
    blocks: &mut Vec<Arc<MarkdownBlock>>,
    budget: &mut ParseBudget,
    mut source_start: usize,
) {
    loop {
        let marker = truncation_block(source_start);
        if block_fits_budget(blocks.len(), budget, &marker) {
            record_block(budget, &marker);
            blocks.push(marker);
            budget.truncated = true;
            return;
        }
        let Some(removed) = blocks.pop() else {
            return;
        };
        source_start = source_start.min(removed.source.start);
        budget.retained_bytes = budget
            .retained_bytes
            .saturating_sub(block_retained_bytes(&removed));
        budget.fragments = budget.fragments.saturating_sub(removed.spans.len());
    }
}

fn push_bounded_block(
    blocks: &mut Vec<Arc<MarkdownBlock>>,
    budget: &mut ParseBudget,
    block: MarkdownBlock,
) {
    let block = Arc::new(block);
    if block_fits_budget(blocks.len(), budget, &block) {
        record_block(budget, &block);
        blocks.push(block);
    } else {
        append_truncation_block(blocks, budget, block.source.start);
    }
}

fn enforce_document_budget(blocks: &mut Vec<Arc<MarkdownBlock>>, source_end: usize) -> bool {
    let original = std::mem::take(blocks);
    let mut bounded = Vec::with_capacity(original.len().min(MAX_PRESENTATION_BLOCKS));
    let mut budget = ParseBudget::default();
    for block in original {
        if is_truncation_block(&block) {
            append_truncation_block(&mut bounded, &mut budget, block.source.start);
            break;
        }
        if block_fits_budget(bounded.len(), &budget, &block) {
            record_block(&mut budget, &block);
            bounded.push(block);
        } else {
            append_truncation_block(&mut bounded, &mut budget, block.source.start);
            break;
        }
    }
    if budget.truncated {
        if let Some(last) = bounded.last_mut() {
            finish_truncation_block(last, source_end);
        }
    }
    *blocks = bounded;
    budget.truncated
}

fn parse_blocks(
    source: &str,
    base_offset: usize,
    initial_syntax_usage: SyntaxUsage,
) -> ParsedBlocks {
    let mut parser = Parser::new_ext(source, MARKDOWN_OPTIONS).into_offset_iter();
    let mut blocks = Vec::new();
    let mut budget = ParseBudget::default();
    let mut syntax_budget = SyntaxBuildBudget::new(initial_syntax_usage);
    let mut events = Vec::new();
    let mut depth = 0_usize;
    let mut block_start = None;

    while let Some((event, range)) = parser.next() {
        let starts_block = matches!(&event, Event::Start(tag) if is_block_tag(tag));
        let ends_block = matches!(&event, Event::End(tag) if is_block_end(*tag));
        if block_start.is_none() {
            block_start = Some(range.start);
        }
        if starts_block {
            depth = depth.saturating_add(1);
        }
        events.push((event, range.clone()));
        if ends_block {
            depth = depth.saturating_sub(1);
        }
        if depth == 0 {
            let start = block_start.take().unwrap_or(range.start);
            push_bounded_block(
                &mut blocks,
                &mut budget,
                render_block(
                    &events,
                    (base_offset + start)..(base_offset + range.end),
                    base_offset,
                    source,
                    parser.reference_definitions(),
                    &mut syntax_budget,
                ),
            );
            events.clear();
            if budget.truncated {
                break;
            }
        }
    }
    if !events.is_empty() && !budget.truncated {
        let start = block_start.unwrap_or(0);
        push_bounded_block(
            &mut blocks,
            &mut budget,
            render_block(
                &events,
                (base_offset + start)..(base_offset + source.len()),
                base_offset,
                source,
                parser.reference_definitions(),
                &mut syntax_budget,
            ),
        );
    }
    if budget.truncated {
        if let Some(last) = blocks.last_mut() {
            finish_truncation_block(last, base_offset + source.len());
        }
    }
    ParsedBlocks {
        blocks,
        syntax_work: syntax_budget.work,
    }
}

/// Accept the complete pipe-enclosed rows some assistants emit without a header.
/// Only inspect paragraphs: code blocks and literal inline-code examples keep
/// their original meaning. Synthetic header bytes never enter source affinities.
fn headerless_table_paragraph(
    source: &str,
    paragraph: Range<usize>,
    references: &RefDefs<'_>,
) -> Option<Vec<(Event<'static>, Range<usize>)>> {
    let text = &source[paragraph.clone()];
    let columns = pipe_row_columns(text.lines().next()?)?;
    let mut table_len = 0;
    let mut rows = 0;
    for line in text.split_inclusive('\n') {
        if pipe_row_columns(line) != Some(columns) {
            break;
        }
        table_len += line.len();
        rows += 1;
    }
    if rows < 2 {
        return None;
    }

    // Let the existing GFM parser handle escaping and inline markup in cells.
    // The empty synthetic header is discarded; every original row remains data.
    let prefix = format!("|{}\n|{}\n", " |".repeat(columns), "---|".repeat(columns));
    let table = format!("{prefix}{}", &text[..table_len]);
    let mut events = Vec::new();
    let mut in_header = false;
    let mut resolve_reference = |link: BrokenLink<'_>| {
        references.get(&link.reference).map(|definition| {
            (
                definition.dest.clone().into_static(),
                definition
                    .title
                    .clone()
                    .unwrap_or_else(|| "".into())
                    .into_static(),
            )
        })
    };
    for (event, range) in Parser::new_with_broken_link_callback(
        &table,
        MARKDOWN_OPTIONS,
        Some(&mut resolve_reference),
    )
    .into_offset_iter()
    {
        match event {
            Event::Start(Tag::TableHead) => in_header = true,
            Event::End(TagEnd::TableHead) => in_header = false,
            _ if in_header => {}
            _ => events.push((
                event.into_static(),
                (paragraph.start + range.start.saturating_sub(prefix.len()))
                    ..(paragraph.start + range.end.saturating_sub(prefix.len())),
            )),
        }
    }
    if table_len < text.len() {
        let remainder_start = paragraph.start + table_len;
        events.push((Event::SoftBreak, remainder_start..remainder_start));
        events.extend(
            Parser::new_with_broken_link_callback(
                &text[table_len..],
                MARKDOWN_OPTIONS,
                Some(&mut resolve_reference),
            )
            .into_offset_iter()
            .map(|(event, range)| {
                (
                    event.into_static(),
                    (remainder_start + range.start)..(remainder_start + range.end),
                )
            }),
        );
    }
    Some(events)
}

fn pipe_row_columns(line: &str) -> Option<usize> {
    // Do not reinterpret indented code or nested container continuation syntax.
    if line.starts_with('\t') || line.bytes().take_while(|byte| *byte == b' ').count() > 3 {
        return None;
    }
    let line = line.trim();
    if !line.starts_with('|') || !line.ends_with('|') {
        return None;
    }
    let mut pipes = 0_usize;
    let mut escaped = false;
    let mut final_separator = false;
    for byte in line.bytes() {
        final_separator = byte == b'|' && !escaped;
        if final_separator {
            pipes += 1;
        }
        if byte == b'\\' {
            escaped = !escaped;
        } else {
            escaped = false;
        }
    }
    (pipes >= 3 && final_separator).then_some(pipes.saturating_sub(1))
}

fn expand_headerless_tables<'a>(
    events: &[(Event<'a>, Range<usize>)],
    source: &str,
    references: &RefDefs<'_>,
) -> Option<Vec<(Event<'a>, Range<usize>)>> {
    let mut expanded = None::<Vec<_>>;
    let mut index = 0;
    while index < events.len() {
        let (event, range) = &events[index];
        if matches!(event, Event::Start(Tag::Paragraph)) {
            if let Some(replacement) = headerless_table_paragraph(source, range.clone(), references)
            {
                let end = events[index + 1..]
                    .iter()
                    .position(|(event, _)| matches!(event, Event::End(TagEnd::Paragraph)))
                    .map(|relative| index + 1 + relative)?;
                expanded
                    .get_or_insert_with(|| events[..index].to_vec())
                    .extend(replacement);
                index = end + 1;
                continue;
            }
        }
        if let Some(expanded) = &mut expanded {
            expanded.push(events[index].clone());
        }
        index += 1;
    }
    expanded
}

fn is_block_tag(tag: &Tag<'_>) -> bool {
    matches!(
        tag,
        Tag::Paragraph
            | Tag::Heading { .. }
            | Tag::BlockQuote(_)
            | Tag::CodeBlock(_)
            | Tag::HtmlBlock
            | Tag::List(_)
            | Tag::Item
            | Tag::Table(_)
            | Tag::TableHead
            | Tag::TableRow
            | Tag::TableCell
    )
}

fn is_block_end(tag: TagEnd) -> bool {
    matches!(
        tag,
        TagEnd::Paragraph
            | TagEnd::Heading(_)
            | TagEnd::BlockQuote(_)
            | TagEnd::CodeBlock
            | TagEnd::HtmlBlock
            | TagEnd::List(_)
            | TagEnd::Item
            | TagEnd::Table
            | TagEnd::TableHead
            | TagEnd::TableRow
            | TagEnd::TableCell
    )
}

#[derive(Clone, Copy, Debug)]
struct ListState {
    next: Option<u64>,
}

#[derive(Debug)]
struct TableState {
    widths: Vec<usize>,
    alignments: Vec<Alignment>,
    column: usize,
    cell_width: usize,
    trailing_padding: usize,
    header: bool,
    cell_start: usize,
    layout: MarkdownTable,
}

impl TableState {
    fn new(alignments: &[Alignment], events: &[(Event<'_>, Range<usize>)]) -> Self {
        let mut widths = vec![0_usize; alignments.len()];
        let mut column = 0_usize;
        let mut cell_width = 0_usize;
        for (event, _) in events {
            match event {
                Event::End(TagEnd::Table) => break,
                Event::Start(Tag::TableHead | Tag::TableRow) => column = 0,
                Event::Start(Tag::TableCell) => cell_width = 0,
                Event::Text(text) | Event::Code(text) | Event::InlineHtml(text) => {
                    cell_width = cell_width.saturating_add(text.width());
                }
                Event::End(TagEnd::TableCell) => {
                    if let Some(width) = widths.get_mut(column) {
                        // Limit synthetic padding even for hostile, extremely wide cells.
                        *width = (*width).max(cell_width.min(256));
                    }
                    column = column.saturating_add(1);
                }
                _ => {}
            }
        }
        Self {
            widths,
            alignments: alignments.to_vec(),
            column: 0,
            cell_width: 0,
            trailing_padding: 0,
            header: false,
            cell_start: 0,
            layout: MarkdownTable {
                spans: 0..0,
                rows: Vec::new(),
                alignments: alignments.to_vec(),
                continuation_prefix: String::new(),
            },
        }
    }
}

#[derive(Clone, Debug)]
struct CodeTextSegment {
    output: Range<usize>,
    source: Range<usize>,
}

#[derive(Debug, Default)]
struct BlockRenderer {
    spans: Vec<TranscriptSpan>,
    style: TranscriptSpanStyle,
    style_stack: Vec<TranscriptSpanStyle>,
    lists: Vec<ListState>,
    quote_depth: usize,
    output_bytes: usize,
    line_has_content: bool,
    paragraph_inline: bool,
    truncated: bool,
    table: Option<TableState>,
    tables: Vec<MarkdownTable>,
}

fn collect_code_text(events: &[(Event<'_>, Range<usize>)]) -> (String, Vec<CodeTextSegment>) {
    let mut code = String::new();
    let mut segments = Vec::new();
    for (event, source) in events {
        let Event::Text(text) = event else {
            continue;
        };
        let start = code.len();
        code.push_str(text);
        segments.push(CodeTextSegment {
            output: start..code.len(),
            source: source.clone(),
        });
    }
    (code, segments)
}

fn mapped_syntax_fragment_count(
    highlighted: &SyntaxHighlight,
    segments: &[CodeTextSegment],
) -> usize {
    let mut token_index = 0_usize;
    let mut segment_index = 0_usize;
    let mut fragments = 0_usize;
    while token_index < highlighted.tokens.len() && segment_index < segments.len() {
        let token = &highlighted.tokens[token_index];
        let segment = &segments[segment_index];
        if token.range.start < segment.output.end && segment.output.start < token.range.end {
            fragments = fragments.saturating_add(1);
        }
        if token.range.end <= segment.output.end {
            token_index += 1;
        } else {
            segment_index += 1;
        }
    }
    fragments
}

fn fenced_code_is_closed(source: &str, block: Range<usize>, segments: &[CodeTextSegment]) -> bool {
    if block.start >= block.end || block.end > source.len() {
        return false;
    }
    let delimiter = source.as_bytes()[block.start];
    if !matches!(delimiter, b'`' | b'~') {
        return false;
    }
    let opening_len = source.as_bytes()[block.start..block.end]
        .iter()
        .take_while(|byte| **byte == delimiter)
        .count();
    if opening_len < 3 {
        return false;
    }
    let opening_end = source[block.start..block.end]
        .find('\n')
        .map_or(block.end, |newline| block.start + newline + 1);
    let suffix_start = segments
        .last()
        .map_or(opening_end, |segment| segment.source.end)
        .max(opening_end)
        .min(block.end);
    source[suffix_start..block.end]
        .lines()
        .rev()
        .find(|line| !line.trim().is_empty())
        .is_some_and(|line| is_closing_fence_line(line, delimiter, opening_len))
}

fn is_closing_fence_line(line: &str, delimiter: u8, opening_len: usize) -> bool {
    let line = line.trim_end_matches('\r');
    let candidate = line.trim_start_matches([' ', '\t', '>']);
    let delimiter_len = candidate
        .as_bytes()
        .iter()
        .take_while(|byte| **byte == delimiter)
        .count();
    delimiter_len >= opening_len
        && candidate[delimiter_len..]
            .bytes()
            .all(|byte| matches!(byte, b' ' | b'\t'))
}

impl BlockRenderer {
    fn render(
        mut self,
        events: &[(Event<'_>, Range<usize>)],
        base: usize,
        source: &str,
        syntax_budget: &mut SyntaxBuildBudget,
    ) -> Self {
        let mut index = 0_usize;
        while index < events.len() {
            if self.truncated {
                break;
            }
            let (event, range) = &events[index];
            if let Event::Start(Tag::Table(alignments)) = event {
                self.table = Some(TableState::new(alignments, &events[index + 1..]));
            }
            if matches!(event, Event::Start(Tag::TableCell)) {
                if let Some(table) = &mut self.table {
                    table.cell_width = events[index + 1..]
                        .iter()
                        .take_while(|(event, _)| !matches!(event, Event::End(TagEnd::TableCell)))
                        .filter_map(|(event, _)| match event {
                            Event::Text(text) | Event::Code(text) | Event::InlineHtml(text) => {
                                Some(text.width())
                            }
                            _ => None,
                        })
                        .sum();
                }
            }
            if let Event::Start(Tag::CodeBlock(CodeBlockKind::Fenced(info))) = event {
                if let Some(end_index) = events[index + 1..]
                    .iter()
                    .position(|(event, _)| matches!(event, Event::End(TagEnd::CodeBlock)))
                    .map(|relative| index + 1 + relative)
                {
                    self.start_tag(
                        &Tag::CodeBlock(CodeBlockKind::Fenced(info.clone())),
                        base + range.start,
                    );
                    let interior = &events[index + 1..end_index];
                    let (code, segments) = collect_code_text(interior);
                    let closed = fenced_code_is_closed(source, range.clone(), &segments);
                    let highlighted =
                        syntax_budget
                            .attempt(info, &code, closed)
                            .filter(|highlighted| {
                                syntax_budget.commit(
                                    code.len(),
                                    mapped_syntax_fragment_count(highlighted, &segments),
                                )
                            });
                    if let Some(highlighted) = highlighted {
                        self.emit_highlighted_code(&code, &segments, highlighted, base);
                    } else {
                        for (event, range) in interior {
                            self.render_event(event, range, base);
                        }
                    }
                    self.end_tag(TagEnd::CodeBlock);
                    index = end_index + 1;
                    continue;
                }
            }
            self.render_event(event, range, base);
            index += 1;
        }
        if let Some(mut table) = self.table.take() {
            // Keep the retained complete rows structured when a later cell exhausts
            // the presentation budget; leave the truncation marker outside the grid.
            table.layout.rows.retain(|row| {
                row.len() == table.alignments.len()
                    && row.iter().all(|cell| cell.end <= self.spans.len())
            });
            if !table.layout.rows.is_empty() {
                table.layout.spans.end = self
                    .spans
                    .iter()
                    .position(|span| span.text == PRESENTATION_TRUNCATED)
                    .unwrap_or(self.spans.len());
                self.tables.push(table.layout);
            }
        }
        self
    }

    fn render_event(&mut self, event: &Event<'_>, range: &Range<usize>, base: usize) {
        let source_offset = base + range.start;
        let source_end = base + range.end;
        match event {
            Event::Start(tag) => self.start_tag(tag, source_offset),
            Event::End(tag) => {
                self.end_table_tag(*tag, source_end);
                self.end_tag(*tag);
            }
            Event::Text(text) => self.emit_source(text, source_offset, source_end, self.style),
            Event::Code(text) => {
                let mut style = self.style;
                style.inline = InlineStyle::Code;
                self.emit_source(text, source_offset, source_end, style);
            }
            Event::Html(html) | Event::InlineHtml(html) => {
                let mut style = self.style;
                style.block = BlockStyle::RawHtml;
                self.emit_source(html, source_offset, source_end, style);
            }
            Event::SoftBreak | Event::HardBreak => self.emit_break(source_offset),
            Event::Rule => {
                let mut style = self.style;
                style.inline = InlineStyle::ListMarker;
                self.emit("───", source_offset, style);
            }
            Event::TaskListMarker(checked) => {
                let mut style = self.style;
                style.inline = InlineStyle::ListMarker;
                self.emit(if *checked { "☑ " } else { "☐ " }, source_offset, style);
            }
            _ => {}
        }
    }

    fn emit_highlighted_code(
        &mut self,
        code: &str,
        segments: &[CodeTextSegment],
        highlighted: SyntaxHighlight,
        base: usize,
    ) {
        let mut token_index = 0_usize;
        let mut segment_index = 0_usize;
        while token_index < highlighted.tokens.len() && segment_index < segments.len() {
            let token = &highlighted.tokens[token_index];
            let segment = &segments[segment_index];
            let start = token.range.start.max(segment.output.start);
            let end = token.range.end.min(segment.output.end);
            if start < end {
                let relative_start = start - segment.output.start;
                let relative_end = end - segment.output.start;
                let source_len = segment.source.end.saturating_sub(segment.source.start);
                let mut style = self.style;
                style.syntax = token.class;
                self.emit_source(
                    &code[start..end],
                    base + segment.source.start + relative_start.min(source_len),
                    base + segment.source.start + relative_end.min(source_len),
                    style,
                );
            }
            if token.range.end <= segment.output.end {
                token_index += 1;
            } else {
                segment_index += 1;
            }
        }
    }

    fn start_tag(&mut self, tag: &Tag<'_>, source_offset: usize) {
        self.style_stack.push(self.style);
        match tag {
            Tag::Paragraph => {
                if self.paragraph_inline {
                    self.paragraph_inline = false;
                } else if self.line_has_content && (!self.lists.is_empty() || self.quote_depth > 0)
                {
                    self.emit_break(source_offset);
                }
            }
            Tag::Heading { level, .. } => {
                self.style.block = BlockStyle::Heading(heading_level(*level));
            }
            Tag::CodeBlock(_) => self.style.block = BlockStyle::Code,
            Tag::Emphasis => self.style.emphasis = true,
            Tag::Strong => self.style.strong = true,
            Tag::Strikethrough => self.style.struck = true,
            Tag::TableHead | Tag::TableRow => {
                if self.paragraph_inline {
                    self.paragraph_inline = false;
                } else if self.line_has_content {
                    self.emit_break(source_offset);
                }
                if let Some(table) = &mut self.table {
                    if table.layout.rows.is_empty() {
                        table.layout.spans.start = self.spans.len();
                        table.layout.continuation_prefix = self
                            .spans
                            .iter()
                            .rev()
                            .take_while(|span| !span.text.contains('\n'))
                            .collect::<Vec<_>>()
                            .into_iter()
                            .rev()
                            .map(|span| {
                                if span.style.inline == InlineStyle::QuoteMarker {
                                    span.text.clone()
                                } else {
                                    " ".repeat(span.text.width())
                                }
                            })
                            .collect();
                    }
                    table.layout.rows.push(Vec::new());
                    table.column = 0;
                    table.header = matches!(tag, Tag::TableHead);
                }
            }
            Tag::TableCell => {
                if let Some(table) = &mut self.table {
                    self.style.strong |= table.header;
                    let width = table.widths.get(table.column).copied().unwrap_or(0);
                    let padding = width.saturating_sub(table.cell_width);
                    let leading = match table.alignments.get(table.column) {
                        Some(Alignment::Right) => padding,
                        Some(Alignment::Center) => padding / 2,
                        _ => 0,
                    };
                    table.trailing_padding = padding - leading;
                    // Emit padding separately so source text keeps its original affinity.
                    self.emit(&" ".repeat(leading), source_offset, self.style);
                    if let Some(table) = &mut self.table {
                        table.cell_start = self.spans.len();
                    }
                }
            }
            Tag::Link { .. } | Tag::Image { .. } => self.style.inline = InlineStyle::Link,
            Tag::BlockQuote(_) => {
                self.quote_depth = self.quote_depth.saturating_add(1);
                let mut style = self.style;
                style.inline = InlineStyle::QuoteMarker;
                self.emit(&"│ ".repeat(self.quote_depth.min(8)), source_offset, style);
                self.paragraph_inline = true;
            }
            Tag::List(start) => self.lists.push(ListState { next: *start }),
            Tag::Item => {
                if self.line_has_content {
                    self.emit("\n", source_offset, self.style);
                }
                let indent = "  ".repeat(self.lists.len().saturating_sub(1).min(8));
                let marker = self.lists.last_mut().map_or_else(
                    || "• ".to_owned(),
                    |list| match &mut list.next {
                        Some(next) => {
                            let marker = format!("{next}. ");
                            *next = next.saturating_add(1);
                            marker
                        }
                        None => "• ".to_owned(),
                    },
                );
                let mut style = self.style;
                style.inline = InlineStyle::ListMarker;
                self.emit(&format!("{indent}{marker}"), source_offset, style);
                self.paragraph_inline = true;
            }
            _ => {}
        }
    }

    fn end_tag(&mut self, tag: TagEnd) {
        match tag {
            TagEnd::BlockQuote(_) => self.quote_depth = self.quote_depth.saturating_sub(1),
            TagEnd::List(_) => {
                self.lists.pop();
            }
            _ => {}
        }
        self.style = self.style_stack.pop().unwrap_or_default();
    }

    fn end_table_tag(&mut self, tag: TagEnd, source_offset: usize) {
        let Some(table) = &mut self.table else {
            return;
        };
        let marker_style = TranscriptSpanStyle {
            inline: InlineStyle::ListMarker,
            ..self.style
        };
        match tag {
            TagEnd::TableCell => {
                if let Some(row) = table.layout.rows.last_mut() {
                    row.push(table.cell_start..self.spans.len());
                }
                let trailing = table.trailing_padding;
                table.column = table.column.saturating_add(1);
                let last = table.column >= table.widths.len();
                self.emit(&" ".repeat(trailing), source_offset, self.style);
                if !last {
                    self.emit(" │ ", source_offset, marker_style);
                }
            }
            TagEnd::TableHead => {
                let widths = table.widths.clone();
                self.emit_break(source_offset);
                for (column, width) in widths.iter().enumerate() {
                    if column > 0 {
                        self.emit("─┼─", source_offset, marker_style);
                    }
                    self.emit(&"─".repeat(*width), source_offset, marker_style);
                }
            }
            TagEnd::Table => {
                let mut table = self.table.take().expect("table is active");
                table.layout.spans.end = self.spans.len();
                self.tables.push(table.layout);
            }
            _ => {}
        }
    }

    fn emit_break(&mut self, source_offset: usize) {
        self.emit("\n", source_offset, self.style);
        if self.quote_depth > 0 {
            let mut style = self.style;
            style.inline = InlineStyle::QuoteMarker;
            self.emit(&"│ ".repeat(self.quote_depth.min(8)), source_offset, style);
        }
    }

    fn emit(&mut self, text: &str, source_offset: usize, style: TranscriptSpanStyle) {
        self.emit_source(text, source_offset, source_offset, style);
    }

    fn emit_source(
        &mut self,
        text: &str,
        source_offset: usize,
        source_end: usize,
        style: TranscriptSpanStyle,
    ) {
        if text.is_empty() || self.truncated {
            return;
        }
        if self.spans.len() >= MAX_PRESENTATION_FRAGMENTS
            || self.output_bytes.saturating_add(text.len()) > MAX_PRESENTATION_OUTPUT_BYTES
        {
            self.truncate(source_offset);
            return;
        }
        let output_offset = u32::try_from(self.output_bytes).unwrap_or(u32::MAX);
        self.output_bytes = self.output_bytes.saturating_add(text.len());
        if text.contains('\n') || text.contains('\r') {
            self.line_has_content = !ends_with_source_line_break(text);
        } else {
            self.line_has_content = true;
        }
        self.spans.push(TranscriptSpan {
            text: text.to_owned(),
            style,
            affinity: SourceAffinity {
                source_offset,
                source_end,
                output_offset,
            },
        });
    }

    fn truncate(&mut self, source_offset: usize) {
        self.truncated = true;
        let mut marker_source = source_offset;
        while self.spans.len() >= MAX_PRESENTATION_FRAGMENTS {
            let removed = self
                .spans
                .pop()
                .expect("a full fragment budget must contain a span");
            marker_source = marker_source.min(removed.affinity.source_offset);
            self.output_bytes = self.output_bytes.saturating_sub(removed.text.len());
        }
        while self
            .output_bytes
            .saturating_add(PRESENTATION_TRUNCATED.len())
            > MAX_PRESENTATION_OUTPUT_BYTES
        {
            let Some(last) = self.spans.last_mut() else {
                return;
            };
            let excess = self
                .output_bytes
                .saturating_add(PRESENTATION_TRUNCATED.len())
                .saturating_sub(MAX_PRESENTATION_OUTPUT_BYTES);
            if excess >= last.text.len() {
                let removed = self.spans.pop().expect("last span must exist");
                marker_source = marker_source.min(removed.affinity.source_offset);
                self.output_bytes = self.output_bytes.saturating_sub(removed.text.len());
                continue;
            }
            let mut keep = last.text.len() - excess;
            while keep > 0 && !last.text.is_char_boundary(keep) {
                keep -= 1;
            }
            let source_len = last
                .affinity
                .source_end
                .saturating_sub(last.affinity.source_offset);
            marker_source = marker_source.min(
                last.affinity
                    .source_offset
                    .saturating_add(keep.min(source_len)),
            );
            let removed = last.text.len() - keep;
            last.text.truncate(keep);
            self.output_bytes = self.output_bytes.saturating_sub(removed);
            if last.text.is_empty() {
                self.spans.pop();
            }
        }
        let output_offset = u32::try_from(self.output_bytes).unwrap_or(u32::MAX);
        self.spans.push(TranscriptSpan {
            text: PRESENTATION_TRUNCATED.to_owned(),
            style: TranscriptSpanStyle {
                inline: InlineStyle::ListMarker,
                ..TranscriptSpanStyle::default()
            },
            affinity: SourceAffinity {
                source_offset: marker_source,
                source_end: source_offset,
                output_offset,
            },
        });
        self.output_bytes = self
            .output_bytes
            .saturating_add(PRESENTATION_TRUNCATED.len());
    }
}

fn ends_with_source_line_break(text: &str) -> bool {
    text.ends_with('\n') || text.ends_with('\r')
}

fn render_block(
    events: &[(Event<'_>, Range<usize>)],
    source_range: Range<usize>,
    base_offset: usize,
    source: &str,
    references: &RefDefs<'_>,
    syntax_budget: &mut SyntaxBuildBudget,
) -> MarkdownBlock {
    let expanded = expand_headerless_tables(events, source, references);
    let events = expanded.as_deref().unwrap_or(events);
    let usage_before = syntax_budget.usage;
    let attempted_source_before = syntax_budget.attempted_source_bytes;
    let attempted_fragments_before = syntax_budget.attempted_fragments;
    let rendered = BlockRenderer::default().render(events, base_offset, source, syntax_budget);
    MarkdownBlock {
        source: source_range,
        has_table: events
            .iter()
            .any(|(event, _)| matches!(event, Event::Start(Tag::Table(_)))),
        spans: rendered.spans,
        tables: rendered.tables,
        syntax_source_bytes: syntax_budget
            .usage
            .source_bytes
            .saturating_sub(usage_before.source_bytes),
        syntax_fragments: syntax_budget
            .usage
            .fragments
            .saturating_sub(usage_before.fragments),
        syntax_attempted_source_bytes: syntax_budget
            .attempted_source_bytes
            .saturating_sub(attempted_source_before),
        syntax_attempted_fragments: syntax_budget
            .attempted_fragments
            .saturating_sub(attempted_fragments_before),
        truncated: false,
    }
}

fn heading_level(level: HeadingLevel) -> u8 {
    match level {
        HeadingLevel::H1 => 1,
        HeadingLevel::H2 => 2,
        HeadingLevel::H3 => 3,
        HeadingLevel::H4 => 4,
        HeadingLevel::H5 => 5,
        HeadingLevel::H6 => 6,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fmt::Write as _;

    #[test]
    fn renders_supported_markdown_without_link_destinations() {
        let mut state = IncrementalMarkdownState::default();
        let source = "# Plan\n\nUse **bold**, *care*, ~~old~~, [`code`](https://example.test).\n\n> quote\n\n1. first\n2. second\n\n```rust\nlet x = 1;\n```";
        let build = state.build(source, 0, 0, true);
        let rendered = build.document.plain_text();

        assert!(rendered.contains("Plan"));
        assert!(rendered.contains("bold"));
        assert!(rendered.contains("│ quote"));
        assert!(rendered.contains("1. first\n2. second"));
        assert!(rendered.contains("let x = 1;"));
        assert!(!rendered.contains("https://example.test"));
        assert!(
            build
                .document
                .blocks
                .iter()
                .flat_map(|block| &block.spans)
                .any(|span| span.style.inline == InlineStyle::Code)
        );
    }

    #[test]
    fn tables_preserve_inline_styles_alignment_and_source_affinity() {
        let source = "| Name | Count | State |\n| :--- | ---: | :---: |\n| **界** | `12` | *ok* |\n| a\\|b | 1 | |\n\nAfter";
        let build = IncrementalMarkdownState::default().build(source, 0, 0, true);
        assert_eq!(
            build.document.plain_text(),
            "Name │ Count │ State\n─────┼───────┼──────\n界   │    12 │  ok  \na|b  │     1 │      \nAfter"
        );
        let spans = &build.document.blocks[0].spans;
        assert!(
            spans
                .iter()
                .any(|span| span.text == "Name" && span.style.strong)
        );
        assert!(
            spans
                .iter()
                .any(|span| span.text == "界" && span.style.strong)
        );
        assert!(
            spans
                .iter()
                .any(|span| span.text == "ok" && span.style.emphasis)
        );
        let code = spans.iter().find(|span| span.text == "12").unwrap();
        assert_eq!(code.style.inline, InlineStyle::Code);
        assert_eq!(
            &source[code.affinity.source_offset..code.affinity.source_end],
            "`12`"
        );
        let mut output = 0;
        for span in spans {
            assert_eq!(span.affinity.output_offset as usize, output);
            output += span.text.len();
        }
    }

    #[test]
    fn headerless_tool_rows_preserve_data_and_following_prose() {
        let source = "| read | Reads text files, optionally selecting a range of lines. |\n | write | Creates or overwrites text files, creating parent directories when needed. |\n | edit | Makes precise, exact-text replacements without rewriting the whole file. |\nAdditional tools come through MCP integrations.";
        let build = IncrementalMarkdownState::default().build(source, 17, 0, true);
        let text = build.document.plain_text();
        let lines = text.lines().map(str::trim_end).collect::<Vec<_>>();
        assert_eq!(
            lines,
            [
                "read  │ Reads text files, optionally selecting a range of lines.",
                "write │ Creates or overwrites text files, creating parent directories when needed.",
                "edit  │ Makes precise, exact-text replacements without rewriting the whole file.",
                "Additional tools come through MCP integrations.",
            ]
        );
        let read = build
            .document
            .blocks
            .iter()
            .flat_map(|block| &block.spans)
            .find(|span| span.text == "read")
            .unwrap();
        assert!(!read.style.strong);
        assert_eq!(read.affinity.source_offset, 19);
        assert_eq!(read.affinity.source_end, 23);
    }

    #[test]
    fn headerless_tables_keep_inline_markup_references_and_crlf_offsets() {
        let source = "Before\r\n\r\n| [manual][ref] | **read** |\r\n| `a\\|b` | file |\r\nMore [help][ref].\r\n\r\n[ref]: https://example.test\r\n";
        let build = IncrementalMarkdownState::default().build(source, 0, 0, true);
        let text = build.document.plain_text();
        assert!(text.contains("manual │ read"), "{text}");
        assert!(text.contains("a|b    │ file"), "{text}");
        assert!(text.contains("\nMore help."), "{text}");
        assert!(!text.contains("https://"));
        let spans = build
            .document
            .blocks
            .iter()
            .flat_map(|block| &block.spans)
            .collect::<Vec<_>>();
        let code = spans.iter().find(|span| span.text == "a|b").unwrap();
        assert_eq!(code.style.inline, InlineStyle::Code);
        assert_eq!(
            &source[code.affinity.source_offset..code.affinity.source_end],
            "`a\\|b`"
        );
        assert!(
            spans
                .iter()
                .any(|span| span.text == "read" && span.style.strong)
        );
        assert!(
            spans
                .iter()
                .any(|span| span.text == "help" && span.style.inline == InlineStyle::Link)
        );
    }

    #[test]
    fn headerless_detection_leaves_code_single_rows_and_ragged_rows_literal() {
        for source in [
            "| read | description |",
            "| read | description |\n| write | extra | description |",
            "| one |\n| two |",
            "cat file | grep read\ncat file | grep write",
            "`| read | description |`\n`| write | description |`",
            "```text\n| read | description |\n| write | description |\n```",
            "    | read | description |\n    | write | description |",
            "> | read | description |\n> | write | description |",
        ] {
            let build = IncrementalMarkdownState::default().build(source, 0, 0, true);
            assert!(
                build.document.blocks.iter().all(|block| !block.has_table),
                "{source}"
            );
            assert!(build.document.plain_text().contains('|'), "{source}");
        }
        assert_eq!(pipe_row_columns(r"| a\|b | c |"), Some(2));
        assert_eq!(pipe_row_columns(r"| a\\|b | c |"), Some(3));
        assert_eq!(pipe_row_columns(r"| a | c \|"), None);
    }

    #[test]
    fn headerless_tables_stream_and_settle_like_a_fresh_document() {
        let source = "| read | text |\n| longer name | **more** |\nFollowing prose\n\nDone.";
        let mut state = IncrementalMarkdownState::default();
        for (end, _) in source.char_indices().skip(1) {
            state.build(&source[..end], 0, 0, false);
        }
        let streamed = state.build(source, 0, 0, true);
        let fresh = IncrementalMarkdownState::default().build(source, 0, 0, true);
        assert_eq!(streamed.document, fresh.document);
        assert!(fresh.document.plain_text().contains("longer name │ more"));
    }

    #[test]
    fn headerless_table_expansion_respects_presentation_budgets() {
        let source = format!(
            "| {} | b |\n{}",
            "wide".repeat(128),
            "| a | b |\n".repeat(2_000)
        );
        let build = IncrementalMarkdownState::default().build(&source, 0, 0, true);
        assert!(build.document.plain_text().contains(PRESENTATION_TRUNCATED));
        assert!(build.document.retained_bytes() <= MAX_PRESENTATION_RETAINED_BYTES);
        for block in &build.document.blocks {
            assert!(block.spans.len() <= MAX_PRESENTATION_FRAGMENTS);
            assert!(
                block
                    .spans
                    .iter()
                    .map(|span| span.text.len())
                    .sum::<usize>()
                    <= MAX_PRESENTATION_OUTPUT_BYTES
            );
        }
    }

    #[test]
    fn tables_and_task_lists_stream_to_the_same_completed_document() {
        let source = "> | Item | Done |\n> | --- | --- |\n> | longer | yes |\n\n- [x] **Shipped**\n- [ ] Pending\n";
        let mut state = IncrementalMarkdownState::default();
        for (end, _) in source.char_indices().skip(1) {
            state.build(&source[..end], 0, 0, false);
        }
        let streamed = state.build(source, 0, 0, true);
        let fresh = IncrementalMarkdownState::default().build(source, 0, 0, true);
        assert_eq!(streamed.document, fresh.document);
        let text = fresh.document.plain_text();
        assert!(text.contains("│ Item   │ Done"), "{text}");
        assert!(text.contains("│ longer │ yes"), "{text}");
        assert!(text.contains("• ☑ Shipped\n• ☐ Pending"), "{text}");
        assert!(!text.contains("[x]"));
    }

    #[test]
    fn table_padding_respects_presentation_budgets() {
        let mut source = format!("| {} | right |\n| --- | ---: |\n", "wide".repeat(128));
        for _ in 0..2_000 {
            source.push_str("| a | b |\n");
        }
        let build = IncrementalMarkdownState::default().build(&source, 0, 0, true);
        assert!(build.document.plain_text().contains(PRESENTATION_TRUNCATED));
        assert!(build.document.retained_bytes() <= MAX_PRESENTATION_RETAINED_BYTES);
        for block in &build.document.blocks {
            assert!(block.spans.len() <= MAX_PRESENTATION_FRAGMENTS);
            assert!(
                block
                    .spans
                    .iter()
                    .map(|span| span.text.len())
                    .sum::<usize>()
                    <= MAX_PRESENTATION_OUTPUT_BYTES
            );
        }
    }

    #[test]
    fn tables_inside_quotes_and_lists_keep_following_paragraphs_separate() {
        for (source, expected) in [
            (
                "> | A | B |\n> | --- | --- |\n> | x | y |\n>\n> After",
                "│ A │ B\n│ ──┼──\n│ x │ y\n│ After",
            ),
            (
                "- | A | B |\n  | --- | --- |\n  | x | y |\n\n  After",
                "• A │ B\n──┼──\nx │ y\nAfter",
            ),
        ] {
            let build = IncrementalMarkdownState::default().build(source, 0, 0, true);
            assert_eq!(build.document.plain_text(), expected);
        }
    }

    #[test]
    fn highlights_only_recognized_closed_fenced_code() {
        let cases = [
            ("```rust\nfn main() {}\n```", true, 1, 0),
            ("```rust\nfn main() {}\n", false, 0, 1),
            ("```unknown-language\nfn main() {}\n```", false, 0, 1),
            ("    fn main() {}", false, 0, 0),
            ("> ```rust\n> fn main() {}\n> ```\n", true, 1, 0),
            ("~~~~js\r\nconst value = 1;\r\n~~~~~~\r\n", true, 1, 0),
        ];

        for (source, highlighted, highlighted_count, fallback_count) in cases {
            let build = IncrementalMarkdownState::default().build(source, 0, 0, true);
            let syntax_spans = build
                .document
                .blocks
                .iter()
                .flat_map(|block| &block.spans)
                .filter(|span| span.style.syntax != SyntaxClass::Plain)
                .collect::<Vec<_>>();
            assert_eq!(!syntax_spans.is_empty(), highlighted, "source: {source:?}");
            assert_eq!(
                build.work.syntax_fences_highlighted, highlighted_count,
                "source: {source:?}"
            );
            assert_eq!(
                build.work.syntax_fallbacks, fallback_count,
                "source: {source:?}"
            );
        }
    }

    #[test]
    fn highlighted_spans_preserve_code_text_and_source_affinity() {
        let source = "```rust\nfn main() { let value = 42; }\n```";
        let build = IncrementalMarkdownState::default().build(source, 0, 0, true);
        let spans = build
            .document
            .blocks
            .iter()
            .flat_map(|block| &block.spans)
            .collect::<Vec<_>>();
        let rebuilt = spans
            .iter()
            .map(|span| span.text.as_str())
            .collect::<String>();
        assert_eq!(rebuilt, "fn main() { let value = 42; }\n");
        let keyword = spans
            .iter()
            .find(|span| span.text == "fn" && span.style.syntax == SyntaxClass::Keyword)
            .unwrap();
        assert_eq!(
            &source[keyword.affinity.source_offset..keyword.affinity.source_end],
            "fn"
        );
        assert_eq!(build.work.syntax_fences_considered, 1);
        assert_eq!(build.work.syntax_fences_highlighted, 1);
        assert_eq!(build.work.syntax_source_bytes, rebuilt.len());
        assert_eq!(build.work.syntax_fragments, spans.len());
    }

    #[test]
    fn cumulative_syntax_source_budget_falls_back_per_fence() {
        let code_line = format!("// {}\n", "x".repeat(54));
        let code = code_line.repeat(500);
        assert!(code.len() < crate::syntax::MAX_SYNTAX_SOURCE_BYTES_PER_FENCE);
        let mut source = String::new();
        for _ in 0..3 {
            source.push_str("```rust\n");
            source.push_str(&code);
            source.push_str("```\n\n");
        }

        let build = IncrementalMarkdownState::default().build(&source, 0, 0, true);

        assert_eq!(build.work.syntax_fences_considered, 3);
        assert_eq!(build.work.syntax_fences_highlighted, 2);
        assert_eq!(build.work.syntax_fallbacks, 1);
        assert!(build.work.syntax_source_bytes <= MAX_SYNTAX_SOURCE_BYTES_PER_BUILD);
    }

    #[test]
    fn streamed_highlighting_matches_fresh_document_budgets() {
        let code = format!("// {}\n", "x".repeat(54)).repeat(125);
        assert!(code.len() < MAX_MUTABLE_SOURCE_BYTES);
        let mut source = String::new();
        let mut streamed = IncrementalMarkdownState::default();
        for index in 0..10 {
            source.push_str("```rust\n");
            source.push_str(&code);
            writeln!(source, "```\n\nafter {index}\n").unwrap();
            let _ = streamed.build(&source, 0, 0, false);
        }

        let streamed_final = streamed.build(&source, 0, 0, true);
        let fresh = IncrementalMarkdownState::default().build(&source, 0, 0, true);

        assert_eq!(streamed_final.document, fresh.document);
        assert_eq!(
            streamed_final
                .document
                .blocks
                .iter()
                .filter(|block| block.has_syntax())
                .count(),
            9
        );
        assert!(
            SyntaxUsage::from_blocks(&streamed_final.document.blocks).source_bytes
                <= MAX_SYNTAX_SOURCE_BYTES_PER_BUILD
        );
    }

    #[test]
    fn streamed_failed_highlights_match_fresh_attempt_budgets() {
        let mut dense = String::new();
        for index in 0..250 {
            writeln!(dense, "let value_{index}: i32 = {index};").unwrap();
        }
        assert!(dense.len() < MAX_MUTABLE_SOURCE_BYTES);
        assert_eq!(
            highlight_fence("rust", &dense).unwrap_err().reason,
            crate::syntax::SyntaxFallback::FragmentLimit
        );
        let mut source = String::new();
        let mut streamed = IncrementalMarkdownState::default();
        for index in 0..2 {
            source.push_str("```rust\n");
            source.push_str(&dense);
            writeln!(source, "```\n\nafter {index}\n").unwrap();
            let _ = streamed.build(&source, 0, 0, false);
        }
        source.push_str("```rust\nfn final_fence() {}\n```\n\nafter final");

        let streamed_final = streamed.build(&source, 0, 0, true);
        let fresh = IncrementalMarkdownState::default().build(&source, 0, 0, true);

        assert_eq!(streamed_final.document, fresh.document);
        assert!(
            streamed_final
                .document
                .blocks
                .iter()
                .all(|block| !block.has_syntax())
        );
    }

    #[test]
    fn mapped_fragment_rejection_is_charged_to_attempt_work() {
        let mut budget = SyntaxBuildBudget::new(SyntaxUsage {
            fragments: MAX_SYNTAX_FRAGMENTS_PER_BUILD - 1,
            attempted_fragments: MAX_SYNTAX_FRAGMENTS_PER_BUILD - 1,
            ..SyntaxUsage::default()
        });
        let highlighted = budget.attempt("rust", "fn demo() {}\n", true).unwrap();
        let mapped_fragments = highlighted.tokens.len();
        assert!(mapped_fragments > 1);

        assert!(!budget.commit("fn demo() {}\n".len(), mapped_fragments));
        assert_eq!(budget.work.fragments, mapped_fragments);
        assert_eq!(
            budget.attempted_fragments,
            MAX_SYNTAX_FRAGMENTS_PER_BUILD - 1 + mapped_fragments
        );
        assert!(budget.attempt("rust", "fn later() {}\n", true).is_none());
    }

    #[test]
    fn repeated_late_highlighter_failures_consume_work_budget() {
        let mut dense = String::new();
        for index in 0..400 {
            writeln!(dense, "let value_{index}: i32 = {index};").unwrap();
        }
        let mut source = String::new();
        for _ in 0..5 {
            source.push_str("```rust\n");
            source.push_str(&dense);
            source.push_str("```\n\n");
        }

        let build = IncrementalMarkdownState::default().build(&source, 0, 0, true);

        assert_eq!(build.work.syntax_fences_highlighted, 0);
        assert_eq!(build.work.syntax_fallbacks, 5);
        assert!(build.work.syntax_source_bytes > 0);
        assert!(build.work.syntax_source_bytes <= MAX_SYNTAX_SOURCE_BYTES_PER_BUILD);
        assert!(
            build.work.syntax_fragments
                <= MAX_SYNTAX_FRAGMENTS_PER_BUILD + crate::syntax::MAX_SYNTAX_FRAGMENTS_PER_FENCE
        );
    }

    #[test]
    fn quote_continuations_and_entities_keep_structure_and_styles() {
        let mut state = IncrementalMarkdownState::default();
        let source = "> first\n> second\n\n**A &amp; 界**";
        let build = state.build(source, 0, 0, true);
        let rendered = build.document.plain_text();

        assert!(rendered.contains("│ first\n│ second"));
        assert!(rendered.contains("A & 界"));
        assert!(
            build
                .document
                .blocks
                .iter()
                .flat_map(|block| &block.spans)
                .any(|span| span.text.contains('A') && span.style.strong)
        );
    }

    #[test]
    fn streaming_reuses_complete_blocks_and_reparses_the_mutable_tail() {
        let mut state = IncrementalMarkdownState::default();
        let first = state.build("first\n\nsecond", 0, 0, false);
        assert_eq!(first.work.source_bytes_reused, 0);

        let second = state.build("first\n\nsecond grows", 0, 0, false);

        assert_eq!(second.work.incremental_builds, 1);
        assert!(second.work.source_bytes_reused >= "first".len());
        assert!(second.work.source_bytes_parsed < "first\n\nsecond grows".len());
        assert_eq!(second.document.plain_text(), "first\nsecond grows");
    }

    #[test]
    fn streamed_and_settled_documents_render_identically() {
        let source = "# Plan\n\nUse **bold** and `code`.\n\n> quote";
        let mut streamed = IncrementalMarkdownState::default();
        let _ = streamed.build("# Plan\n\nUse ", 0, 0, false);
        let streamed_final = streamed.build(source, 0, 0, true);
        let mut settled = IncrementalMarkdownState::default();
        let settled_final = settled.build(source, 0, 0, true);

        assert_eq!(streamed_final.document, settled_final.document);
    }

    #[test]
    fn open_fence_stays_mutable_until_a_later_block_arrives() {
        let mut state = IncrementalMarkdownState::default();
        let first = state.build("before\n\n```rust\nlet x =", 0, 0, false);
        assert_eq!(first.stable_blocks, 1);
        assert_eq!(first.work.syntax_fences_highlighted, 0);
        assert_eq!(first.work.syntax_fallbacks, 1);

        let closed = state.build("before\n\n```rust\nlet x = 1;\n```", 0, 0, false);
        assert_eq!(closed.stable_blocks, 1);
        assert_eq!(closed.work.syntax_fences_highlighted, 1);

        let promoted = state.build("before\n\n```rust\nlet x = 1;\n```\n\nafter", 0, 0, false);
        assert_eq!(promoted.stable_blocks, 2);
        assert!(promoted.work.source_bytes_reused > 0);
        assert_eq!(promoted.work.syntax_fences_highlighted, 1);
        assert!(promoted.document.plain_text().contains("let x = 1;"));

        let stable = state.build(
            "before\n\n```rust\nlet x = 1;\n```\n\nafter grows",
            0,
            0,
            false,
        );
        assert_eq!(stable.work.syntax_fences_considered, 0);
        assert_eq!(stable.work.syntax_source_bytes, 0);
    }

    #[test]
    fn growing_single_blocks_use_bounded_literal_checkpoints() {
        let mut list_state = IncrementalMarkdownState::default();
        let mut list = String::new();
        for item in 0..2_000 {
            writeln!(list, "- item {item}").unwrap();
        }
        let _ = list_state.build(&list, 0, 0, false);
        list.push_str("- final item\n");
        let list_append = list_state.build(&list, 0, 0, false);
        assert!(list_append.work.source_bytes_parsed <= MAX_MUTABLE_SOURCE_BYTES + 1024);
        assert!(list_append.work.source_bytes_reused > 0);

        let mut paragraph_state = IncrementalMarkdownState::default();
        let mut paragraph = "word ".repeat(4_000);
        let _ = paragraph_state.build(&paragraph, 0, 0, false);
        paragraph.push_str("tail");
        let paragraph_append = paragraph_state.build(&paragraph, 0, 0, false);
        assert!(paragraph_append.work.source_bytes_parsed <= MAX_MUTABLE_SOURCE_BYTES + 1024);
        assert!(paragraph_append.work.source_bytes_reused > 0);

        let settled = paragraph_state.build(&paragraph, 0, 0, true);
        assert!(settled.document.plain_text().ends_with("tail"));
    }

    #[test]
    fn settling_a_literal_checkpoint_rebuilds_closed_fence_highlighting() {
        let code = format!("// {}\n", "x".repeat(54)).repeat(200);
        let source = format!("```rust\n{code}```");
        assert!(source.len() > MAX_MUTABLE_SOURCE_BYTES);
        let mut streamed = IncrementalMarkdownState::default();
        let checkpointed = streamed.build(&source, 0, 0, false);
        assert_eq!(checkpointed.work.syntax_fences_highlighted, 0);

        let settled = streamed.build(&source, 0, 0, true);
        let fresh = IncrementalMarkdownState::default().build(&source, 0, 0, true);

        assert_eq!(settled.document, fresh.document);
        assert_eq!(settled.work.syntax_fences_highlighted, 1);
        assert!(
            settled
                .document
                .blocks
                .iter()
                .flat_map(|block| &block.spans)
                .any(|span| span.style.syntax == SyntaxClass::Comment)
        );
    }

    #[test]
    fn streaming_many_blocks_enforces_cumulative_presentation_budgets() {
        let mut state = IncrementalMarkdownState::default();
        let mut source = String::new();
        let mut latest = MarkdownBuild::default();
        for batch in 0..40 {
            for item in 0..64 {
                writeln!(source, "paragraph {batch}-{item}\n").unwrap();
            }
            latest = state.build(&source, 0, 0, false);
            assert!(latest.document.blocks.len() <= MAX_PRESENTATION_BLOCKS);
            assert!(latest.document.retained_bytes() <= MAX_PRESENTATION_RETAINED_BYTES);
            assert!(
                latest
                    .document
                    .blocks
                    .iter()
                    .map(|block| block.spans.len())
                    .sum::<usize>()
                    <= MAX_PRESENTATION_FRAGMENTS
            );
        }
        assert!(
            latest
                .document
                .plain_text()
                .contains(PRESENTATION_TRUNCATED)
        );

        let settled = state.build(&source, 0, 0, true);
        let from_scratch = IncrementalMarkdownState::default().build(&source, 0, 0, true);
        assert_eq!(settled.document, from_scratch.document);
        assert!(settled.document.blocks.len() <= MAX_PRESENTATION_BLOCKS);
        assert!(settled.document.retained_bytes() <= MAX_PRESENTATION_RETAINED_BYTES);
    }

    #[test]
    fn block_renderer_reserves_a_visible_truncation_marker() {
        let mut fragments = BlockRenderer::default();
        for index in 0..MAX_PRESENTATION_FRAGMENTS {
            fragments.emit_source("x", index, index + 1, TranscriptSpanStyle::default());
        }
        assert_eq!(fragments.spans.len(), MAX_PRESENTATION_FRAGMENTS);
        assert!(
            fragments
                .spans
                .iter()
                .all(|span| span.text != PRESENTATION_TRUNCATED)
        );
        fragments.emit(
            "tail",
            MAX_PRESENTATION_FRAGMENTS,
            TranscriptSpanStyle::default(),
        );
        assert_eq!(fragments.spans.len(), MAX_PRESENTATION_FRAGMENTS);
        assert_eq!(fragments.spans.last().unwrap().text, PRESENTATION_TRUNCATED);

        let mut bytes = BlockRenderer::default();
        bytes.emit_source(
            &"x".repeat(MAX_PRESENTATION_OUTPUT_BYTES),
            0,
            MAX_PRESENTATION_OUTPUT_BYTES,
            TranscriptSpanStyle::default(),
        );
        assert_eq!(bytes.output_bytes, MAX_PRESENTATION_OUTPUT_BYTES);
        bytes.emit(
            "tail",
            MAX_PRESENTATION_OUTPUT_BYTES,
            TranscriptSpanStyle::default(),
        );
        assert_eq!(bytes.spans.last().unwrap().text, PRESENTATION_TRUNCATED);
        assert!(bytes.output_bytes <= MAX_PRESENTATION_OUTPUT_BYTES);
        assert_eq!(
            bytes.spans.first().unwrap().text.len() + PRESENTATION_TRUNCATED.len(),
            MAX_PRESENTATION_OUTPUT_BYTES
        );
        assert_eq!(
            bytes.spans.last().unwrap().affinity.source_offset,
            bytes.spans.first().unwrap().text.len()
        );
    }

    #[test]
    fn document_budget_boundaries_and_truncation_identity_are_exact() {
        fn synthetic_block(
            start: usize,
            fragments: usize,
            fragment_bytes: usize,
        ) -> Arc<MarkdownBlock> {
            Arc::new(MarkdownBlock {
                source: start..start + 1,
                has_table: false,
                tables: Vec::new(),
                spans: (0..fragments)
                    .map(|_| TranscriptSpan {
                        text: "x".repeat(fragment_bytes),
                        style: TranscriptSpanStyle::default(),
                        affinity: SourceAffinity {
                            source_offset: start,
                            source_end: start + 1,
                            output_offset: 0,
                        },
                    })
                    .collect(),
                syntax_source_bytes: 0,
                syntax_fragments: 0,
                syntax_attempted_source_bytes: 0,
                syntax_attempted_fragments: 0,
                truncated: false,
            })
        }

        let mut exact_blocks = (0..MAX_PRESENTATION_BLOCKS)
            .map(|index| synthetic_block(index, 0, 0))
            .collect::<Vec<_>>();
        assert!(!enforce_document_budget(
            &mut exact_blocks,
            MAX_PRESENTATION_BLOCKS
        ));
        assert_eq!(exact_blocks.len(), MAX_PRESENTATION_BLOCKS);
        exact_blocks.push(synthetic_block(MAX_PRESENTATION_BLOCKS, 0, 0));
        assert!(enforce_document_budget(
            &mut exact_blocks,
            MAX_PRESENTATION_BLOCKS + 1
        ));
        assert!(exact_blocks.len() <= MAX_PRESENTATION_BLOCKS);

        let mut exact_fragments = vec![synthetic_block(0, MAX_PRESENTATION_FRAGMENTS, 0)];
        assert!(!enforce_document_budget(&mut exact_fragments, 1));
        exact_fragments.push(synthetic_block(1, 1, 0));
        assert!(enforce_document_budget(&mut exact_fragments, 2));
        assert!(
            exact_fragments
                .iter()
                .map(|block| block.spans.len())
                .sum::<usize>()
                <= MAX_PRESENTATION_FRAGMENTS
        );

        let mut retained = (0..16)
            .map(|index| synthetic_block(index, 1, 128 * 1024))
            .collect::<Vec<_>>();
        assert!(enforce_document_budget(&mut retained, 16));
        assert!(
            MarkdownDocument { blocks: retained }.retained_bytes()
                <= MAX_PRESENTATION_RETAINED_BYTES
        );

        let mut state = IncrementalMarkdownState::default();
        let source = format!("{PRESENTATION_TRUNCATED}\n\nstill visible");
        let legitimate_text = state.build(&source, 0, 0, true);
        assert!(
            legitimate_text
                .document
                .plain_text()
                .contains("still visible")
        );
        assert!(
            legitimate_text
                .document
                .blocks
                .iter()
                .all(|block| !block.truncated)
        );
    }

    #[test]
    fn adversarial_expansion_is_truncated_within_the_retained_budget() {
        let mut state = IncrementalMarkdownState::default();
        let nested = format!("{} text", ">".repeat(30_000));
        let nested_build = state.build(&nested, 0, 0, true);
        assert!(nested_build.document.retained_bytes() <= MAX_PRESENTATION_RETAINED_BYTES);

        let many_blocks = "# heading\n\n".repeat(MAX_PRESENTATION_BLOCKS + 100);
        let block_build = state.build(&many_blocks, 0, 1, true);
        assert!(block_build.document.retained_bytes() <= MAX_PRESENTATION_RETAINED_BYTES);
        assert!(
            block_build
                .document
                .plain_text()
                .contains(PRESENTATION_TRUNCATED)
        );
    }

    #[test]
    fn loose_lists_and_multi_paragraph_quotes_keep_structural_breaks() {
        let mut state = IncrementalMarkdownState::default();
        let source = "- first\n\n  second paragraph\n- third\n\n> first\n>\n> second";
        let rendered = state.build(source, 0, 0, true).document.plain_text();

        assert!(rendered.contains("• first\nsecond paragraph\n• third"));
        assert!(rendered.contains("│ first\n│ second"));
    }

    #[test]
    fn full_reparse_completion_invalidates_new_fence_highlighting() {
        let mut state = IncrementalMarkdownState::default();
        let open_source = "[id]: https://example.test\n\n```rust\nfn main() {}";
        let open = state.build(open_source, 0, 0, false);
        assert_eq!(open.work.full_reparses, 1);
        assert!(open.document.blocks.iter().all(|block| !block.has_syntax()));

        let growing_source = format!("{open_source}\nlet value = 1;");
        let growing = state.build(&growing_source, 0, 0, false);
        assert_eq!(growing.presentation_epoch, open.presentation_epoch);

        let closed_source = format!("{growing_source}\n```");
        let closed = state.build(&closed_source, 0, 0, true);

        assert!(closed.presentation_epoch > growing.presentation_epoch);
        assert!(
            closed
                .document
                .blocks
                .iter()
                .any(|block| block.has_syntax())
        );
    }

    #[test]
    fn authoritative_epoch_and_reference_definitions_reparse_safely() {
        let mut state = IncrementalMarkdownState::default();
        let _ = state.build("[label][id]\n\nopen", 0, 1, false);
        let late = state.build(
            "[label][id]\n\nopen\n\n[id]: https://example.test",
            0,
            1,
            false,
        );
        assert_eq!(late.work.incremental_builds, 0);
        assert_eq!(late.work.full_reparses, 1);

        let replacement = state.build("replacement", 0, 2, true);
        assert_eq!(replacement.work.incremental_builds, 1);
        assert_eq!(replacement.work.source_bytes_parsed, "replacement".len());
    }
}
