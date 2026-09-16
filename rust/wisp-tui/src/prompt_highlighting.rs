//! Bounded semantic spans for the prompt editor's display projection.

use std::{collections::HashSet, ops::Range};

use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::events::{CommandDescriptor, ProjectFileKind, ProjectFileSnapshot};

pub(crate) const MAX_LINE_BYTES: usize = 32 * 1024;
const MAX_DOCUMENT_BYTES: usize = 128 * 1024;
const MAX_DOCUMENT_LINES: usize = 4_096;
pub(crate) const MAX_HIGHLIGHTS_PER_LINE: usize = 256;
const MAX_HIGHLIGHTS_PER_DOCUMENT: usize = 4_096;
const MAX_INLINE_CODE_RUNS_PER_LINE: usize = 2_048;
const TAB_WIDTH: usize = 4;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Kind {
    Command,
    ResolvedPath,
    UnresolvedPath,
    MarkdownHeading,
    MarkdownListMarker,
    MarkdownInlineCodeDelimiter,
    MarkdownInlineCode,
    MarkdownFenceDelimiter,
    MarkdownFenceInfo,
    MarkdownFenceBody,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct Highlight {
    pub columns: Range<usize>,
    pub kind: Kind,
}

#[derive(Clone, Copy)]
pub(crate) struct DocumentLine<'a> {
    pub text: &'a str,
    pub truncated: bool,
}

#[derive(Clone, Copy)]
struct Fence {
    character: u8,
    length: usize,
}

pub(crate) struct Context<'a> {
    catalog: Option<&'a [CommandDescriptor]>,
    files: HashSet<&'a str>,
    directories: HashSet<&'a str>,
    paths_complete: bool,
}

impl<'a> Context<'a> {
    pub(crate) fn new(
        catalog: Option<&'a [CommandDescriptor]>,
        project_files: Option<&'a ProjectFileSnapshot>,
    ) -> Self {
        let mut files = HashSet::new();
        let mut directories = HashSet::new();
        if let Some(snapshot) = project_files {
            for entry in &snapshot.entries {
                match entry.kind {
                    ProjectFileKind::File => {
                        files.insert(entry.path.as_str());
                    }
                    ProjectFileKind::Directory => {
                        directories.insert(entry.path.as_str());
                    }
                }
            }
        }
        Self {
            catalog,
            files,
            directories,
            paths_complete: project_files
                .is_some_and(|snapshot| !snapshot.truncated && !snapshot.entries.is_empty()),
        }
    }
}

pub(crate) fn document_highlights(
    lines: &[DocumentLine<'_>],
    context: Option<&Context<'_>>,
) -> Vec<Vec<Highlight>> {
    let mut highlighted = vec![Vec::new(); lines.len()];
    let mut remaining_bytes = MAX_DOCUMENT_BYTES;
    let mut remaining_highlights = MAX_HIGHLIGHTS_PER_DOCUMENT;
    let mut fence = None;

    for (line_index, line) in lines.iter().take(MAX_DOCUMENT_LINES).enumerate() {
        if remaining_bytes == 0 || remaining_highlights == 0 {
            break;
        }
        let scan_end = floor_char_boundary(
            line.text,
            line.text.len().min(MAX_LINE_BYTES).min(remaining_bytes),
        );
        let text = &line.text[..scan_end];
        let line_truncated = line.truncated || scan_end < line.text.len();
        let semantic = context.map_or_else(Vec::new, |context| {
            line_highlights(text, line_truncated, line_index, lines.len(), context)
        });
        let (markdown, next_fence) = markdown_line_highlights(text, !line_truncated, fence);
        fence = next_fence;

        let limit = MAX_HIGHLIGHTS_PER_LINE.min(remaining_highlights);
        highlighted[line_index] = merge_highlights(markdown, semantic, limit);
        remaining_highlights = remaining_highlights.saturating_sub(highlighted[line_index].len());
        remaining_bytes = remaining_bytes.saturating_sub(scan_end.saturating_add(1));
    }
    highlighted
}

pub(crate) fn line_highlights(
    line: &str,
    line_truncated: bool,
    line_index: usize,
    line_count: usize,
    context: &Context<'_>,
) -> Vec<Highlight> {
    let scan_end = floor_char_boundary(line, line.len().min(MAX_LINE_BYTES));
    let line_truncated = line_truncated || scan_end < line.len();
    let mut highlights = Vec::new();

    if line_index == 0 && line_count == 1 {
        let command_start = line[..scan_end]
            .find(|character: char| !character.is_whitespace())
            .unwrap_or(scan_end);
        let token_end = line[command_start..scan_end]
            .find(char::is_whitespace)
            .map_or(scan_end, |offset| command_start + offset);
        let token = &line[command_start..token_end];
        let token_complete = token_end < scan_end || !line_truncated;
        if token_complete
            && token.starts_with('/')
            && crate::commands::is_supported_token(token, context.catalog)
        {
            highlights.push(Highlight {
                columns: display_columns(line, command_start)..display_columns(line, token_end),
                kind: Kind::Command,
            });
        }
    }

    if context.files.is_empty() && context.directories.is_empty() && !context.paths_complete {
        return highlights;
    }
    let mut cursor = 0;
    while cursor < scan_end && highlights.len() < MAX_HIGHLIGHTS_PER_LINE {
        let Some(relative) = line[cursor..scan_end].find('@') else {
            break;
        };
        let start = cursor + relative;
        if start > 0
            && line[..start]
                .chars()
                .next_back()
                .is_some_and(|character| !character.is_whitespace())
        {
            cursor = start + 1;
            continue;
        }
        let Some((end, path)) = parse_reference(line, start, scan_end, line_truncated) else {
            cursor = start + 1;
            continue;
        };
        let resolved = path.as_deref().is_some_and(|path| {
            context.files.contains(path)
                || path
                    .strip_suffix('/')
                    .is_some_and(|directory| context.directories.contains(directory))
        });
        if resolved || context.paths_complete {
            highlights.push(Highlight {
                columns: display_columns(line, start)..display_columns(line, end),
                kind: if resolved {
                    Kind::ResolvedPath
                } else {
                    Kind::UnresolvedPath
                },
            });
        }
        cursor = end.max(start + 1);
    }
    highlights
}

fn markdown_line_highlights(
    line: &str,
    complete_line: bool,
    fence: Option<Fence>,
) -> (Vec<Highlight>, Option<Fence>) {
    if let Some(fence) = fence {
        if let Some((start, end)) = closing_fence(line, complete_line, fence) {
            return (
                vec![byte_highlight(
                    line,
                    start,
                    end,
                    Kind::MarkdownFenceDelimiter,
                )],
                None,
            );
        }
        return (
            (!line.is_empty())
                .then(|| byte_highlight(line, 0, line.len(), Kind::MarkdownFenceBody))
                .into_iter()
                .collect(),
            Some(fence),
        );
    }

    if let Some((delimiter_start, delimiter_end, info_start, info_end, fence)) = opening_fence(line)
    {
        let mut highlights = vec![byte_highlight(
            line,
            delimiter_start,
            delimiter_end,
            Kind::MarkdownFenceDelimiter,
        )];
        if info_start < info_end {
            highlights.push(byte_highlight(
                line,
                info_start,
                info_end,
                Kind::MarkdownFenceInfo,
            ));
        }
        return (highlights, Some(fence));
    }

    let mut highlights = Vec::new();
    if let Some((start, end)) = heading(line) {
        highlights.push(byte_highlight(line, start, end, Kind::MarkdownHeading));
    } else if let Some((start, end)) = list_marker(line) {
        highlights.push(byte_highlight(line, start, end, Kind::MarkdownListMarker));
    }
    highlights.extend(inline_code_highlights(line));
    (highlights, None)
}

fn opening_fence(line: &str) -> Option<(usize, usize, usize, usize, Fence)> {
    let start = after_optional_indent(line);
    let character = *line.as_bytes().get(start)?;
    if !matches!(character, b'`' | b'~') {
        return None;
    }
    let delimiter_end = run_end(line, start);
    let length = delimiter_end - start;
    if length < 3 || (character == b'`' && line[delimiter_end..].contains('`')) {
        return None;
    }
    let info_start = skip_whitespace(line, delimiter_end);
    let info_end = line[info_start..]
        .find(char::is_whitespace)
        .map_or(line.len(), |offset| info_start + offset);
    Some((
        start,
        delimiter_end,
        info_start,
        info_end,
        Fence { character, length },
    ))
}

fn closing_fence(line: &str, complete_line: bool, fence: Fence) -> Option<(usize, usize)> {
    if !complete_line {
        return None;
    }
    let start = after_optional_indent(line);
    if line.as_bytes().get(start).copied() != Some(fence.character) {
        return None;
    }
    let delimiter_end = run_end(line, start);
    (delimiter_end - start >= fence.length && line[delimiter_end..].trim().is_empty())
        .then_some((start, delimiter_end))
}

fn heading(line: &str) -> Option<(usize, usize)> {
    let start = after_optional_indent(line);
    if line.as_bytes().get(start).copied() != Some(b'#') {
        return None;
    }
    let marker_end = run_end(line, start);
    if marker_end - start > 6
        || line[marker_end..]
            .chars()
            .next()
            .is_some_and(|character| !character.is_whitespace())
    {
        return None;
    }
    Some((start, line.len()))
}

fn list_marker(line: &str) -> Option<(usize, usize)> {
    let start = after_optional_indent(line);
    let character = *line.as_bytes().get(start)?;
    if matches!(character, b'-' | b'*' | b'+') {
        let end = start + 1;
        return (end == line.len() || line[end..].chars().next().is_some_and(char::is_whitespace))
            .then_some((start, end));
    }
    if !character.is_ascii_digit() {
        return None;
    }
    let mut end = start;
    while end < line.len() && line.as_bytes()[end].is_ascii_digit() && end - start < 10 {
        end += 1;
    }
    let digit_count = end - start;
    let marker = line.as_bytes().get(end).copied();
    if !(1..=9).contains(&digit_count) || !matches!(marker, Some(b'.' | b')')) {
        return None;
    }
    let marker_end = end + 1;
    (marker_end == line.len()
        || line[marker_end..]
            .chars()
            .next()
            .is_some_and(char::is_whitespace))
    .then_some((start, marker_end))
}

fn inline_code_highlights(line: &str) -> Vec<Highlight> {
    let mut runs = Vec::new();
    let mut cursor = 0;
    while cursor < line.len() {
        let Some(relative) = line[cursor..].find('`') else {
            break;
        };
        let start = cursor + relative;
        let end = run_end(line, start);
        runs.push((start, end));
        if runs.len() > MAX_INLINE_CODE_RUNS_PER_LINE {
            return Vec::new();
        }
        cursor = end;
    }

    let mut highlights = Vec::new();
    let mut index = 0;
    while index < runs.len() && highlights.len() < MAX_HIGHLIGHTS_PER_LINE {
        let (opening_start, opening_end) = runs[index];
        let length = opening_end - opening_start;
        let closing = runs[index + 1..]
            .iter()
            .position(|(start, end)| end - start == length)
            .map(|offset| index + 1 + offset);
        let Some(closing_index) = closing else {
            index += 1;
            continue;
        };
        let (closing_start, closing_end) = runs[closing_index];
        highlights.push(byte_highlight(
            line,
            opening_start,
            opening_end,
            Kind::MarkdownInlineCodeDelimiter,
        ));
        if opening_end < closing_start {
            highlights.push(byte_highlight(
                line,
                opening_end,
                closing_start,
                Kind::MarkdownInlineCode,
            ));
        }
        highlights.push(byte_highlight(
            line,
            closing_start,
            closing_end,
            Kind::MarkdownInlineCodeDelimiter,
        ));
        index = closing_index + 1;
    }
    highlights.truncate(MAX_HIGHLIGHTS_PER_LINE);
    highlights
}

fn after_optional_indent(line: &str) -> usize {
    line.as_bytes()
        .iter()
        .take(3)
        .take_while(|character| **character == b' ')
        .count()
}

fn run_end(line: &str, start: usize) -> usize {
    let character = line.as_bytes()[start];
    let mut end = start + 1;
    while line.as_bytes().get(end).copied() == Some(character) {
        end += 1;
    }
    end
}

fn skip_whitespace(line: &str, mut start: usize) -> usize {
    while let Some(character) = line[start..].chars().next() {
        if !character.is_whitespace() {
            break;
        }
        start += character.len_utf8();
    }
    start
}

fn byte_highlight(line: &str, start: usize, end: usize, kind: Kind) -> Highlight {
    Highlight {
        columns: display_columns(line, start)..display_columns(line, end),
        kind,
    }
}

pub(crate) fn merge_highlights(
    markdown: Vec<Highlight>,
    semantic: Vec<Highlight>,
    limit: usize,
) -> Vec<Highlight> {
    let mut candidates = markdown;
    candidates.extend(semantic);
    let mut boundaries = candidates
        .iter()
        .flat_map(|highlight| [highlight.columns.start, highlight.columns.end])
        .collect::<Vec<_>>();
    boundaries.sort_unstable();
    boundaries.dedup();

    let mut merged: Vec<Highlight> = Vec::new();
    for interval in boundaries.windows(2) {
        let columns = interval[0]..interval[1];
        if columns.is_empty() {
            continue;
        }
        let Some(kind) = candidates
            .iter()
            .enumerate()
            .filter(|(_, highlight)| {
                highlight.columns.start <= columns.start && highlight.columns.end >= columns.end
            })
            .max_by_key(|(index, highlight)| (highlight_priority(highlight.kind), *index))
            .map(|(_, highlight)| highlight.kind)
        else {
            continue;
        };
        if let Some(previous) = merged
            .last_mut()
            .filter(|previous| previous.kind == kind && previous.columns.end == columns.start)
        {
            previous.columns.end = columns.end;
        } else if merged.len() < limit {
            merged.push(Highlight { columns, kind });
        } else {
            break;
        }
    }
    merged
}

fn highlight_priority(kind: Kind) -> u8 {
    match kind {
        Kind::Command | Kind::ResolvedPath | Kind::UnresolvedPath => 3,
        Kind::MarkdownInlineCodeDelimiter
        | Kind::MarkdownInlineCode
        | Kind::MarkdownFenceDelimiter
        | Kind::MarkdownFenceInfo
        | Kind::MarkdownListMarker => 2,
        Kind::MarkdownHeading | Kind::MarkdownFenceBody => 1,
    }
}

fn parse_reference(
    line: &str,
    start: usize,
    limit: usize,
    line_truncated: bool,
) -> Option<(usize, Option<String>)> {
    let rest = &line[start + 1..limit];
    if rest.is_empty() {
        return None;
    }
    if let Some(quoted) = rest.strip_prefix('"') {
        let mut escaped = false;
        let mut closing = None;
        for (offset, character) in quoted.char_indices() {
            if character == '"' && !escaped {
                closing = Some(start + 2 + offset + 1);
                break;
            }
            escaped = character == '\\' && !escaped;
            if character != '\\' {
                escaped = false;
            }
        }
        let quoted_end = match closing {
            Some(end) => end,
            None if line_truncated => return None,
            None => limit,
        };
        let end = line[quoted_end..limit]
            .find(char::is_whitespace)
            .map_or(limit, |offset| quoted_end + offset);
        if end == limit && line_truncated {
            return None;
        }
        if end != quoted_end {
            return Some((end, None));
        }
        let encoded = &line[start + 1..quoted_end];
        let path = serde_json::from_str::<String>(encoded).ok();
        return Some((end, path));
    }
    let end = rest
        .find(char::is_whitespace)
        .map_or(limit, |offset| start + 1 + offset);
    if end == limit && line_truncated {
        return None;
    }
    (end > start + 1).then(|| (end, Some(line[start + 1..end].to_owned())))
}

fn floor_char_boundary(text: &str, mut index: usize) -> usize {
    while !text.is_char_boundary(index) {
        index -= 1;
    }
    index
}

fn display_columns(text: &str, end: usize) -> usize {
    let mut columns = 0;
    for grapheme in text[..end].graphemes(true) {
        columns += if grapheme == "\t" {
            TAB_WIDTH - columns % TAB_WIDTH
        } else {
            grapheme.width()
        };
    }
    columns
}

#[cfg(test)]
mod tests {
    use super::*;
    use wisp_protocol::events::ProjectFileEntry;

    fn command(name: &str) -> CommandDescriptor {
        CommandDescriptor {
            name: name.into(),
            description: String::new(),
            slash_command: format!("/{name}"),
            slash_aliases: Vec::new(),
            order: 0,
        }
    }

    fn snapshot(truncated: bool) -> ProjectFileSnapshot {
        ProjectFileSnapshot {
            generation: 1,
            entries: vec![
                ProjectFileEntry {
                    path: "src/main.rs".into(),
                    kind: ProjectFileKind::File,
                },
                ProjectFileEntry {
                    path: "docs".into(),
                    kind: ProjectFileKind::Directory,
                },
                ProjectFileEntry {
                    path: "space name.md".into(),
                    kind: ProjectFileKind::File,
                },
                ProjectFileEntry {
                    path: "a b".into(),
                    kind: ProjectFileKind::File,
                },
            ],
            truncated,
        }
    }

    fn highlights(
        line: &str,
        line_truncated: bool,
        line_index: usize,
        line_count: usize,
        catalog: Option<&[CommandDescriptor]>,
        snapshot: Option<&ProjectFileSnapshot>,
    ) -> Vec<Highlight> {
        line_highlights(
            line,
            line_truncated,
            line_index,
            line_count,
            &Context::new(catalog, snapshot),
        )
    }

    #[test]
    fn commands_are_catalog_backed_and_single_line_only() {
        let catalog = [command("help")];
        assert_eq!(
            highlights("/help", false, 0, 1, Some(&catalog), None),
            vec![Highlight {
                columns: 0..5,
                kind: Kind::Command
            }]
        );
        assert_eq!(
            highlights("  /help argument", false, 0, 1, Some(&catalog), None),
            vec![Highlight {
                columns: 2..7,
                kind: Kind::Command
            }]
        );
        assert!(highlights("/missing", false, 0, 1, Some(&catalog), None).is_empty());
        assert!(highlights("/help", true, 0, 1, Some(&catalog), None).is_empty());
        let quit = CommandDescriptor {
            name: "quit".into(),
            description: String::new(),
            slash_command: "/quit".into(),
            slash_aliases: vec![":q".into()],
            order: 0,
        };
        assert!(highlights(":q", false, 0, 1, Some(&[quit]), None).is_empty());
        assert!(highlights("/help", false, 0, 2, Some(&catalog), None).is_empty());
    }

    #[test]
    fn markdown_structure_matches_textual_semantics_and_preserves_priority() {
        let catalog = [command("help")];
        let snapshot = snapshot(false);
        let lines = [
            DocumentLine {
                text: "# Review `src` with @src/main.rs",
                truncated: false,
            },
            DocumentLine {
                text: "- then run `/help`",
                truncated: false,
            },
            DocumentLine {
                text: "```rust",
                truncated: false,
            },
            DocumentLine {
                text: "fn main() {}",
                truncated: false,
            },
            DocumentLine {
                text: "```",
                truncated: false,
            },
        ];
        assert_eq!(
            document_highlights(&lines, Some(&Context::new(Some(&catalog), Some(&snapshot))),),
            vec![
                vec![
                    Highlight {
                        columns: 0..9,
                        kind: Kind::MarkdownHeading,
                    },
                    Highlight {
                        columns: 9..10,
                        kind: Kind::MarkdownInlineCodeDelimiter,
                    },
                    Highlight {
                        columns: 10..13,
                        kind: Kind::MarkdownInlineCode,
                    },
                    Highlight {
                        columns: 13..14,
                        kind: Kind::MarkdownInlineCodeDelimiter,
                    },
                    Highlight {
                        columns: 14..20,
                        kind: Kind::MarkdownHeading,
                    },
                    Highlight {
                        columns: 20..32,
                        kind: Kind::ResolvedPath,
                    },
                ],
                vec![
                    Highlight {
                        columns: 0..1,
                        kind: Kind::MarkdownListMarker,
                    },
                    Highlight {
                        columns: 11..12,
                        kind: Kind::MarkdownInlineCodeDelimiter,
                    },
                    Highlight {
                        columns: 12..17,
                        kind: Kind::MarkdownInlineCode,
                    },
                    Highlight {
                        columns: 17..18,
                        kind: Kind::MarkdownInlineCodeDelimiter,
                    },
                ],
                vec![
                    Highlight {
                        columns: 0..3,
                        kind: Kind::MarkdownFenceDelimiter,
                    },
                    Highlight {
                        columns: 3..7,
                        kind: Kind::MarkdownFenceInfo,
                    },
                ],
                vec![Highlight {
                    columns: 0..12,
                    kind: Kind::MarkdownFenceBody,
                }],
                vec![Highlight {
                    columns: 0..3,
                    kind: Kind::MarkdownFenceDelimiter,
                }],
            ]
        );
    }

    #[test]
    fn markdown_scanning_is_bounded_and_incomplete_fences_stay_editable() {
        let long = "`".repeat(MAX_LINE_BYTES * 2);
        let lines = (0..MAX_DOCUMENT_LINES + 2)
            .map(|index| DocumentLine {
                text: if index == 0 { "```rust" } else { &long },
                truncated: index != 0,
            })
            .collect::<Vec<_>>();
        let highlights = document_highlights(&lines, None);
        assert_eq!(highlights.len(), lines.len());
        assert!(highlights[MAX_DOCUMENT_LINES].is_empty());
        assert!(highlights[MAX_DOCUMENT_LINES + 1].is_empty());
        assert!(highlights.iter().flatten().count() <= MAX_HIGHLIGHTS_PER_DOCUMENT);
    }

    #[test]
    fn paths_resolve_quoted_and_directory_references() {
        let line =
            "read @src/main.rs @\"space name.md\" @docs/ @docs @\"space name.md\"suffix @missing";
        assert_eq!(
            highlights(line, false, 0, 1, None, Some(&snapshot(false))),
            vec![
                Highlight {
                    columns: 5..17,
                    kind: Kind::ResolvedPath
                },
                Highlight {
                    columns: 18..34,
                    kind: Kind::ResolvedPath
                },
                Highlight {
                    columns: 35..41,
                    kind: Kind::ResolvedPath
                },
                Highlight {
                    columns: 42..47,
                    kind: Kind::UnresolvedPath
                },
                Highlight {
                    columns: 48..70,
                    kind: Kind::UnresolvedPath
                },
                Highlight {
                    columns: 71..79,
                    kind: Kind::UnresolvedPath
                },
            ]
        );
    }

    #[test]
    fn malformed_quoted_references_are_unresolved_only_with_complete_snapshots() {
        let complete = highlights("@\"bad\\q\"", false, 0, 1, None, Some(&snapshot(false)));
        assert_eq!(
            complete,
            vec![Highlight {
                columns: 0..8,
                kind: Kind::UnresolvedPath,
            }]
        );
        assert!(highlights("@\"bad\\q\"", false, 0, 1, None, Some(&snapshot(true)),).is_empty());
        assert_eq!(
            highlights(
                "@\"space name.md",
                false,
                0,
                1,
                None,
                Some(&snapshot(false)),
            ),
            vec![Highlight {
                columns: 0..15,
                kind: Kind::UnresolvedPath,
            }]
        );
    }

    #[test]
    fn raw_tabs_cannot_resolve_as_display_spaces() {
        let complete = highlights("@\"a\tb\"", false, 0, 1, None, Some(&snapshot(false)));
        assert_eq!(
            complete,
            vec![Highlight {
                columns: 0..6,
                kind: Kind::UnresolvedPath,
            }]
        );
    }

    #[test]
    fn references_crossing_the_scan_limit_remain_neutral() {
        assert!(highlights("@src/main.rs", true, 0, 1, None, Some(&snapshot(false)),).is_empty());
        assert!(
            highlights(
                "@\"space name.md\"",
                true,
                0,
                1,
                None,
                Some(&snapshot(false)),
            )
            .is_empty()
        );
    }

    #[test]
    fn empty_snapshots_leave_references_neutral() {
        let empty = ProjectFileSnapshot {
            generation: 1,
            entries: Vec::new(),
            truncated: false,
        };
        assert!(highlights("@missing", false, 0, 1, None, Some(&empty)).is_empty());
    }

    #[test]
    fn incomplete_snapshots_leave_absent_paths_neutral() {
        let highlights = highlights(
            "@src/main.rs @missing",
            false,
            0,
            1,
            None,
            Some(&snapshot(true)),
        );
        assert_eq!(
            highlights,
            vec![Highlight {
                columns: 0..12,
                kind: Kind::ResolvedPath
            }]
        );
    }

    #[test]
    fn scanning_is_bounded_and_preserves_unicode_columns() {
        let line = format!("界 @missing {}", "x".repeat(MAX_LINE_BYTES * 2));
        let highlights = highlights(&line, false, 0, 1, None, Some(&snapshot(false)));
        assert_eq!(
            highlights[0],
            Highlight {
                columns: 3..11,
                kind: Kind::UnresolvedPath
            }
        );
    }
}
