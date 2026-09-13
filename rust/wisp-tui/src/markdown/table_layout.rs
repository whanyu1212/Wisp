//! Width-dependent table layout over parsed cells; resizing never reparses Markdown.

use super::*;
use unicode_segmentation::UnicodeSegmentation;

pub(crate) fn layout_document(
    document: &MarkdownDocument,
    width: usize,
    previous: Option<(&MarkdownDocument, &MarkdownDocument)>,
) -> MarkdownDocument {
    let mut blocks = Vec::with_capacity(document.blocks.len());
    for (index, block) in document.blocks.iter().enumerate() {
        if let Some((parsed, laid_out)) = previous {
            if let Some((old, layout)) = parsed.blocks.get(index).zip(laid_out.blocks.get(index)) {
                if Arc::ptr_eq(old, block) {
                    blocks.push(Arc::clone(layout));
                    continue;
                }
            }
        }
        if block.tables.is_empty()
            || block.tables.iter().any(|table| {
                table.spans.end > block.spans.len()
                    || table
                        .rows
                        .iter()
                        .flatten()
                        .any(|cell| cell.end > block.spans.len())
            })
        {
            blocks.push(Arc::clone(block));
            continue;
        }
        let mut output = BlockRenderer::default();
        let mut previous = 0;
        for table in &block.tables {
            copy_spans(&mut output, &block.spans[previous..table.spans.start]);
            render_table(&mut output, block, table, width);
            previous = table.spans.end;
            if output.truncated {
                break;
            }
        }
        if block
            .spans
            .get(previous)
            .is_some_and(|span| span.text == PRESENTATION_TRUNCATED)
        {
            marker(
                &mut output,
                "\n",
                block.spans[previous].affinity.source_offset,
            );
        }
        copy_spans(&mut output, &block.spans[previous..]);
        let mut laid_out = (**block).clone();
        laid_out.spans = output.spans;
        laid_out.tables.clear();
        blocks.push(Arc::new(laid_out));
    }
    enforce_document_budget(
        &mut blocks,
        document.blocks.last().map_or(0, |block| block.source.end),
    );
    MarkdownDocument { blocks }
}

fn copy_spans(output: &mut BlockRenderer, spans: &[TranscriptSpan]) {
    for span in spans {
        output.emit_source(
            &span.text,
            span.affinity.source_offset,
            span.affinity.source_end,
            span.style,
        );
    }
}

fn safe_cell(spans: &[TranscriptSpan]) -> Vec<TranscriptSpan> {
    let mut column = 0;
    spans
        .iter()
        .map(|span| {
            let mut safe = span.clone();
            safe.text = span
                .text
                .graphemes(true)
                .map(|grapheme| {
                    let text = if grapheme == "\t" {
                        " ".repeat(4 - column % 4)
                    } else {
                        grapheme
                            .chars()
                            .map(|character| {
                                if character.is_control() || crate::is_bidi_control(character) {
                                    '\u{fffd}'
                                } else {
                                    character
                                }
                            })
                            .collect::<String>()
                    };
                    column += text.width();
                    text
                })
                .collect();
            safe
        })
        .collect()
}

// Give short columns their natural width before spending remaining space on prose.
fn column_widths(desired: &[usize], budget: usize) -> Vec<usize> {
    let mut low = 1;
    let mut high = desired.iter().copied().max().unwrap_or(1);
    while low < high {
        let middle = low + (high - low).div_ceil(2);
        if desired
            .iter()
            .map(|width| (*width).min(middle))
            .sum::<usize>()
            <= budget
        {
            low = middle;
        } else {
            high = middle - 1;
        }
    }
    let mut widths = desired
        .iter()
        .map(|width| (*width).min(low))
        .collect::<Vec<_>>();
    let mut remaining = budget.saturating_sub(widths.iter().sum());
    for (width, desired) in widths.iter_mut().zip(desired) {
        if remaining > 0 && *width < *desired {
            *width += 1;
            remaining -= 1;
        }
    }
    widths
}

fn render_table(
    output: &mut BlockRenderer,
    block: &MarkdownBlock,
    table: &MarkdownTable,
    width: usize,
) {
    let available = width.saturating_sub(table.continuation_prefix.width());
    let columns = table.alignments.len();
    if columns == 0 || available < 5 {
        copy_spans(output, &block.spans[table.spans.clone()]);
        return;
    }
    let stacked = available < columns.saturating_mul(5).saturating_add(1);
    let widths = if stacked {
        vec![available - 4]
    } else {
        let mut desired = vec![1; columns];
        for row in &table.rows {
            for (index, cell) in row.iter().enumerate().take(columns) {
                desired[index] = desired[index].max(
                    safe_cell(&block.spans[cell.clone()])
                        .iter()
                        .map(|span| span.text.width())
                        .sum(),
                );
            }
        }
        column_widths(&desired, available - 3 * columns - 1)
    };
    let source = block
        .spans
        .get(table.spans.start)
        .map_or(block.source.start, |span| span.affinity.source_offset);
    border(output, &widths, ['┌', '┬', '┐'], source);
    for (index, row) in table.rows.iter().enumerate() {
        if output.truncated {
            break;
        }
        if stacked {
            for cell in row {
                render_row(
                    output,
                    block,
                    std::slice::from_ref(cell),
                    &widths,
                    &[Alignment::None],
                    &table.continuation_prefix,
                );
            }
        } else {
            render_row(
                output,
                block,
                row,
                &widths,
                &table.alignments,
                &table.continuation_prefix,
            );
        }
        let row_source = row
            .first()
            .and_then(|cell| block.spans.get(cell.start))
            .map_or(source, |span| span.affinity.source_offset);
        next_line(output, &table.continuation_prefix, row_source);
        border(
            output,
            &widths,
            if index + 1 == table.rows.len() {
                ['└', '┴', '┘']
            } else {
                ['├', '┼', '┤']
            },
            row_source,
        );
    }
}

fn marker(output: &mut BlockRenderer, text: &str, source: usize) {
    output.emit(
        text,
        source,
        TranscriptSpanStyle {
            inline: InlineStyle::TableBorder,
            ..TranscriptSpanStyle::default()
        },
    );
}

fn next_line(output: &mut BlockRenderer, prefix: &str, source: usize) {
    marker(output, "\n", source);
    marker(output, prefix, source);
}

fn border(output: &mut BlockRenderer, widths: &[usize], corners: [char; 3], source: usize) {
    let mut text = corners[0].to_string();
    for (index, width) in widths.iter().enumerate() {
        text.push_str(&"─".repeat(width + 2));
        text.push(if index + 1 == widths.len() {
            corners[2]
        } else {
            corners[1]
        });
    }
    marker(output, &text, source);
}

struct CellLines {
    spans: Vec<TranscriptSpan>,
    text: String,
    lines: Vec<Range<usize>>,
}

fn render_row(
    output: &mut BlockRenderer,
    block: &MarkdownBlock,
    cells: &[Range<usize>],
    widths: &[usize],
    alignments: &[Alignment],
    prefix: &str,
) {
    let cells = cells
        .iter()
        .zip(widths)
        .map(|(cell, width)| {
            let spans = safe_cell(&block.spans[cell.clone()]);
            let text = spans
                .iter()
                .map(|span| span.text.as_str())
                .collect::<String>();
            let lines = wrap_ranges(&text, *width);
            CellLines { spans, text, lines }
        })
        .collect::<Vec<_>>();
    let height = cells.iter().map(|cell| cell.lines.len()).max().unwrap_or(1);
    for line in 0..height {
        if output.truncated {
            break;
        }
        let source = cells
            .iter()
            .find_map(|cell| {
                cell.lines
                    .get(line)
                    .and_then(|range| source_at(&cell.spans, range.start))
            })
            .unwrap_or(block.source.start);
        next_line(output, prefix, source);
        marker(output, "│", source);
        for (column, width) in widths.iter().enumerate() {
            let cell = cells.get(column);
            let range = cell.and_then(|cell| cell.lines.get(line));
            let content_width = cell
                .zip(range)
                .map_or(0, |(cell, range)| cell.text[range.clone()].width());
            let padding = width.saturating_sub(content_width);
            let leading = match alignments.get(column) {
                Some(Alignment::Right) => padding,
                Some(Alignment::Center) => padding / 2,
                _ => 0,
            };
            marker(output, &" ".repeat(leading + 1), source);
            if let Some((cell, range)) = cell.zip(range) {
                emit_range(output, &cell.spans, range.clone());
            }
            marker(output, &" ".repeat(padding - leading + 1), source);
            marker(output, "│", source);
        }
    }
}

fn source_at(spans: &[TranscriptSpan], offset: usize) -> Option<usize> {
    let mut start = 0;
    for span in spans {
        if offset < start + span.text.len() {
            return Some(
                span.affinity.source_offset
                    + (offset - start).min(
                        span.affinity
                            .source_end
                            .saturating_sub(span.affinity.source_offset),
                    ),
            );
        }
        start += span.text.len();
    }
    None
}

fn emit_range(output: &mut BlockRenderer, spans: &[TranscriptSpan], range: Range<usize>) {
    let mut start = 0;
    for span in spans {
        let end = start + span.text.len();
        if range.start < end && range.end > start {
            let from = range.start.saturating_sub(start);
            let to = (range.end - start).min(span.text.len());
            let source_len = span
                .affinity
                .source_end
                .saturating_sub(span.affinity.source_offset);
            output.emit_source(
                &span.text[from..to],
                span.affinity.source_offset + from.min(source_len),
                if to == span.text.len() {
                    span.affinity.source_end
                } else {
                    span.affinity.source_offset + to.min(source_len)
                },
                span.style,
            );
        }
        start = end;
    }
}

fn wrap_ranges(text: &str, width: usize) -> Vec<Range<usize>> {
    let mut lines = Vec::new();
    let mut start = 0;
    while start < text.len() && lines.len() < MAX_PRESENTATION_FRAGMENTS {
        let mut columns = 0;
        let mut end = start;
        let mut last_space = None;
        let mut overflow = false;
        for (relative, grapheme) in text[start..].grapheme_indices(true) {
            let offset = start + relative;
            if columns + grapheme.width() > width && end > start {
                overflow = true;
                break;
            }
            if grapheme.chars().all(char::is_whitespace) {
                last_space = Some(offset);
            }
            columns += grapheme.width();
            end = offset + grapheme.len();
        }
        let next = if overflow {
            last_space.filter(|space| *space > start).unwrap_or(end)
        } else {
            end
        };
        lines.push(start..next);
        start = next;
        while start < text.len() {
            let character = text[start..].chars().next().expect("remaining text");
            if !character.is_whitespace() {
                break;
            }
            start += character.len_utf8();
        }
    }
    if lines.is_empty() {
        lines.push(0..0);
    }
    lines
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn tool_descriptions_wrap_inside_bordered_columns() {
        let source = "| read | Reads text files and selects lines. |\n| bash | Runs tests and long-running commands. |";
        let parsed = IncrementalMarkdownState::default().build(source, 0, 0, true);
        let laid_out = layout_document(&parsed.document, 40, None);
        let text = laid_out.plain_text();
        assert!(text.starts_with('┌'), "{text}");
        assert!(text.ends_with('┘'), "{text}");
        assert!(
            text.contains("│ read │ Reads text files and selects"),
            "{text}"
        );
        assert!(text.contains("│      │ lines."), "{text}");
        assert!(text.contains("│      │ commands."), "{text}");
        for line in text.lines() {
            assert_eq!(line.width(), 40, "{text}");
            if line.starts_with('│') {
                assert_eq!(
                    line.chars()
                        .enumerate()
                        .filter_map(|(index, ch)| (ch == '│').then_some(index))
                        .collect::<Vec<_>>(),
                    [0, 7, 39]
                );
            }
        }
    }

    #[test]
    fn layout_preserves_inline_styles_unicode_and_original_source() {
        let source = "| Item | Value |\n| --- | ---: |\n| **界** | `12` |\n| file | 1 |";
        let parsed = IncrementalMarkdownState::default().build(source, 0, 0, true);
        let laid_out = layout_document(&parsed.document, 24, None);
        let spans = &laid_out.blocks[0].spans;
        assert!(
            spans
                .iter()
                .any(|span| span.text == "界" && span.style.strong)
        );
        let code = spans.iter().find(|span| span.text == "12").unwrap();
        assert_eq!(code.style.inline, InlineStyle::Code);
        assert_eq!(
            &source[code.affinity.source_offset..code.affinity.source_end],
            "`12`"
        );
        let text = laid_out.plain_text();
        assert!(text.contains("│ 界   │    12 │"), "{text}");
        assert!(text.lines().all(|line| line.width() <= 24));
    }

    #[test]
    fn narrow_layout_and_large_tables_remain_bounded() {
        let source = format!(
            "| {} | b |\n{}",
            "wide".repeat(128),
            "| a | b |\n".repeat(2_000)
        );
        let parsed = IncrementalMarkdownState::default().build(&source, 0, 0, true);
        for width in [8, 30, 80] {
            let laid_out = layout_document(&parsed.document, width, None);
            assert!(laid_out.retained_bytes() <= MAX_PRESENTATION_RETAINED_BYTES);
            assert!(laid_out.plain_text().contains(PRESENTATION_TRUNCATED));
            assert!(laid_out.plain_text().starts_with('┌'));
            assert!(
                laid_out
                    .blocks
                    .iter()
                    .all(|block| block.spans.len() <= MAX_PRESENTATION_FRAGMENTS)
            );
        }
    }

    #[test]
    fn quoted_tables_repeat_the_prefix_and_keep_following_prose() {
        let source = "> | Name | Description |\n> | --- | --- |\n> | read | Reads files with optional line ranges. |\n>\n> After";
        let parsed = IncrementalMarkdownState::default().build(source, 0, 0, true);
        let text = layout_document(&parsed.document, 35, None).plain_text();
        assert!(text.lines().all(|line| line.starts_with("│ ")), "{text}");
        assert!(text.lines().all(|line| line.width() <= 35), "{text}");
        assert!(text.ends_with("│ After"), "{text}");
    }
}
