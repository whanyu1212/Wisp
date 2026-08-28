//! Bounded, terminal-independent Markdown presentation for assistant transcript entries.

use std::ops::Range;
use std::sync::Arc;

use pulldown_cmark::{Event, HeadingLevel, Options, Parser, Tag, TagEnd};

const REFERENCE_DEFINITION_MARKER: &str = "]:";
const MAX_MUTABLE_SOURCE_BYTES: usize = 8 * 1024;
const MAX_PRESENTATION_OUTPUT_BYTES: usize = 256 * 1024;
const MAX_PRESENTATION_FRAGMENTS: usize = 4_096;
const MAX_PRESENTATION_BLOCKS: usize = 2_048;
const MAX_PRESENTATION_RETAINED_BYTES: usize = 1024 * 1024;
const PRESENTATION_TRUNCATED: &str = "… Markdown presentation truncated …";

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
}

#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub struct TranscriptSpanStyle {
    pub block: BlockStyle,
    pub inline: InlineStyle,
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
    truncated: bool,
}

impl MarkdownBlock {
    #[cfg(test)]
    pub fn plain_text(&self) -> String {
        self.spans.iter().map(|span| span.text.as_str()).collect()
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
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct MarkdownDocument {
    pub blocks: Vec<Arc<MarkdownBlock>>,
}

impl MarkdownDocument {
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
            let blocks = parse_blocks(source, presentation_start);
            let work = MarkdownWork {
                source_bytes_parsed: source.len(),
                source_bytes_reused: 0,
                blocks_built: blocks.len(),
                full_reparses: 1,
                incremental_builds: 0,
                fragments_emitted: blocks.iter().map(|block| block.spans.len()).sum(),
            };
            if settled {
                self.stable_source_end = source.len();
                self.stable_blocks = blocks.clone();
                self.full_reparse_only = false;
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
        let parsed = parse_blocks(mutable_source, presentation_start + reused);
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
            self.stable_blocks.extend(parsed[..promote].iter().cloned());
        }
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
        }
        let work = MarkdownWork {
            source_bytes_parsed: checkpointed.saturating_add(mutable_source.len()),
            source_bytes_reused: reused,
            blocks_built: parsed.len(),
            full_reparses: 0,
            incremental_builds: 1,
            fragments_emitted: parsed.iter().map(|block| block.spans.len()).sum(),
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
        truncated: false,
    }
}

fn truncation_block(source_offset: usize) -> Arc<MarkdownBlock> {
    Arc::new(MarkdownBlock {
        source: source_offset..source_offset,
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

fn parse_blocks(source: &str, base_offset: usize) -> Vec<Arc<MarkdownBlock>> {
    let options = Options::ENABLE_STRIKETHROUGH;
    let parser = Parser::new_ext(source, options).into_offset_iter();
    let mut blocks = Vec::new();
    let mut budget = ParseBudget::default();
    let mut events = Vec::new();
    let mut depth = 0_usize;
    let mut block_start = None;

    for (event, range) in parser {
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
            ),
        );
    }
    if budget.truncated {
        if let Some(last) = blocks.last_mut() {
            finish_truncation_block(last, base_offset + source.len());
        }
    }
    blocks
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
    )
}

#[derive(Clone, Copy, Debug)]
struct ListState {
    next: Option<u64>,
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
}

impl BlockRenderer {
    fn render(mut self, events: &[(Event<'_>, Range<usize>)], base: usize) -> Vec<TranscriptSpan> {
        for (event, range) in events {
            let source_offset = base + range.start;
            let source_end = base + range.end;
            match event {
                Event::Start(tag) => self.start_tag(tag, source_offset),
                Event::End(tag) => self.end_tag(*tag),
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
                    self.emit(if *checked { "[x] " } else { "[ ] " }, source_offset, style);
                }
                _ => {}
            }
        }
        self.spans
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
    source: Range<usize>,
    base_offset: usize,
) -> MarkdownBlock {
    MarkdownBlock {
        source,
        spans: BlockRenderer::default().render(events, base_offset),
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

        let closed = state.build("before\n\n```rust\nlet x = 1;\n```", 0, 0, false);
        assert_eq!(closed.stable_blocks, 1);

        let promoted = state.build("before\n\n```rust\nlet x = 1;\n```\n\nafter", 0, 0, false);
        assert_eq!(promoted.stable_blocks, 2);
        assert!(promoted.work.source_bytes_reused > 0);
        assert!(promoted.document.plain_text().contains("let x = 1;"));
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
