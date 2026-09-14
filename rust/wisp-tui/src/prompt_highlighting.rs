//! Bounded semantic spans for the prompt editor's display projection.

use std::ops::Range;

use unicode_segmentation::UnicodeSegmentation;
use unicode_width::UnicodeWidthStr;
use wisp_protocol::events::{CommandDescriptor, ProjectFileKind, ProjectFileSnapshot};

pub(crate) const MAX_LINE_BYTES: usize = 32 * 1024;
const MAX_HIGHLIGHTS_PER_LINE: usize = 256;
const TAB_WIDTH: usize = 4;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum Kind {
    Command,
    ResolvedPath,
    UnresolvedPath,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct Highlight {
    pub columns: Range<usize>,
    pub kind: Kind,
}

pub(crate) fn line_highlights(
    line: &str,
    line_truncated: bool,
    line_index: usize,
    line_count: usize,
    catalog: Option<&[CommandDescriptor]>,
    project_files: Option<&ProjectFileSnapshot>,
) -> Vec<Highlight> {
    let scan_end = floor_char_boundary(line, line.len().min(MAX_LINE_BYTES));
    let mut highlights = Vec::new();

    if line_index == 0 && line_count == 1 {
        let command_start = line[..scan_end]
            .find(|character: char| !character.is_whitespace())
            .unwrap_or(scan_end);
        let token_end = line[command_start..scan_end]
            .find(char::is_whitespace)
            .map_or(scan_end, |offset| command_start + offset);
        let token = &line[command_start..token_end];
        if crate::commands::is_supported_token(token, catalog) {
            highlights.push(Highlight {
                columns: display_columns(line, command_start)..display_columns(line, token_end),
                kind: Kind::Command,
            });
        }
    }

    let Some(snapshot) = project_files else {
        return highlights;
    };
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
            snapshot.entries.iter().any(|entry| match entry.kind {
                ProjectFileKind::File => entry.path == path,
                ProjectFileKind::Directory => path
                    .strip_suffix('/')
                    .is_some_and(|directory| directory == entry.path),
            })
        });
        if resolved || !snapshot.truncated {
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
        let path = serde_json::from_str::<String>(encoded)
            .or_else(|_| serde_json::from_str(&format!("{encoded}\"")))
            .ok();
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
            ],
            truncated,
        }
    }

    #[test]
    fn commands_are_catalog_backed_and_single_line_only() {
        let catalog = [command("help")];
        assert_eq!(
            line_highlights("/help", false, 0, 1, Some(&catalog), None),
            vec![Highlight {
                columns: 0..5,
                kind: Kind::Command
            }]
        );
        assert_eq!(
            line_highlights("  /help argument", false, 0, 1, Some(&catalog), None),
            vec![Highlight {
                columns: 2..7,
                kind: Kind::Command
            }]
        );
        assert!(line_highlights("/missing", false, 0, 1, Some(&catalog), None).is_empty());
        assert!(line_highlights("/help", false, 0, 2, Some(&catalog), None).is_empty());
    }

    #[test]
    fn paths_resolve_quoted_and_directory_references() {
        let line =
            "read @src/main.rs @\"space name.md\" @docs/ @docs @\"space name.md\"suffix @missing";
        assert_eq!(
            line_highlights(line, false, 0, 1, None, Some(&snapshot(false))),
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
        let complete = line_highlights("@\"bad\\q\"", false, 0, 1, None, Some(&snapshot(false)));
        assert_eq!(
            complete,
            vec![Highlight {
                columns: 0..8,
                kind: Kind::UnresolvedPath,
            }]
        );
        assert!(
            line_highlights("@\"bad\\q\"", false, 0, 1, None, Some(&snapshot(true)),).is_empty()
        );
    }

    #[test]
    fn references_crossing_the_scan_limit_remain_neutral() {
        assert!(
            line_highlights("@src/main.rs", true, 0, 1, None, Some(&snapshot(false)),).is_empty()
        );
        assert!(
            line_highlights(
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
    fn incomplete_snapshots_leave_absent_paths_neutral() {
        let highlights = line_highlights(
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
        let highlights = line_highlights(&line, false, 0, 1, None, Some(&snapshot(false)));
        assert_eq!(
            highlights[0],
            Highlight {
                columns: 3..11,
                kind: Kind::UnresolvedPath
            }
        );
    }
}
