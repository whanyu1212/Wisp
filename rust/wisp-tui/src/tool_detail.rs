//! Bounded, terminal-independent structured detail for live tool cards.

use std::fmt;

use serde_json::{Map, Value};
use similar::{ChangeTag, TextDiff};

use crate::tool_cards::{
    TOOL_OUTPUT_MAX_BYTES, TOOL_OUTPUT_MAX_LINES, TOOL_PREVIEW_MAX_BYTES, TOOL_PREVIEW_MAX_LINES,
};

pub const DETAIL_EXPANDED_MAX_ROWS: usize = 400;
pub const DETAIL_EXPANDED_MAX_BYTES: usize = TOOL_OUTPUT_MAX_BYTES;
pub const DETAIL_COLLAPSED_MAX_ROWS: usize = TOOL_PREVIEW_MAX_LINES;
pub const DETAIL_COLLAPSED_MAX_BYTES: usize = TOOL_PREVIEW_MAX_BYTES;
pub const DETAIL_SOURCE_MAX_BYTES: usize = TOOL_OUTPUT_MAX_BYTES;
pub const DETAIL_SOURCE_MAX_LINES: usize = TOOL_OUTPUT_MAX_LINES;
pub const DETAIL_EDIT_MAX_HUNKS: usize = 256;
const DETAIL_PATH_MAX_CHARS: usize = 512;
const OMISSION_TEXT_MAX_BYTES: usize = 96;

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DetailUnavailableReason {
    MalformedSource,
    SourceOverBudget,
    MissingBeforeSnapshot,
    ConflictingLifecycle,
    RetentionPressure,
}

impl DetailUnavailableReason {
    pub fn label(self) -> &'static str {
        match self {
            Self::MalformedSource => "structured detail unavailable: malformed tool metadata",
            Self::SourceOverBudget => {
                "structured detail unavailable: source exceeds retained limits"
            }
            Self::MissingBeforeSnapshot => {
                "structured diff unavailable: previous file contents were not retained"
            }
            Self::ConflictingLifecycle => {
                "structured detail unavailable: conflicting lifecycle metadata"
            }
            Self::RetentionPressure => {
                "structured detail unavailable: pending detail retention limit reached"
            }
        }
    }
}

#[derive(Clone, Eq, PartialEq)]
pub enum ToolDetailSource {
    None,
    Edit(EditDetailSource),
    Write(WriteDetailSource),
    Read(ReadDetailSource),
    Grep(GrepDetailSource),
    Find,
    Unavailable(DetailUnavailableReason),
}

impl fmt::Debug for ToolDetailSource {
    fn fmt(&self, formatter: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::None => formatter.write_str("None"),
            Self::Edit(source) => formatter
                .debug_struct("Edit")
                .field("path", &source.path)
                .field("hunks", &source.hunks.len())
                .field("retained_bytes", &source.retained_bytes())
                .finish(),
            Self::Write(source) => formatter
                .debug_struct("Write")
                .field("path", &source.path)
                .field("source_bytes", &source.after.source_bytes)
                .field("source_lines", &source.after.source_lines)
                .finish(),
            Self::Read(source) => formatter
                .debug_struct("Read")
                .field("path", &source.path)
                .field("offset", &source.offset)
                .finish(),
            Self::Grep(source) => formatter
                .debug_struct("Grep")
                .field("path", &source.path)
                .field("pattern_chars", &source.pattern.chars().count())
                .field("literal", &source.literal)
                .finish(),
            Self::Find => formatter.write_str("Find"),
            Self::Unavailable(reason) => {
                formatter.debug_tuple("Unavailable").field(reason).finish()
            }
        }
    }
}

impl ToolDetailSource {
    pub fn retained_bytes(&self) -> usize {
        match self {
            Self::Edit(source) => source.retained_bytes(),
            Self::Write(source) => source.after.text.len().saturating_add(source.path.len()),
            Self::Read(source) => source.path.len(),
            Self::Grep(source) => source.path.len().saturating_add(source.pattern.len()),
            Self::None | Self::Find | Self::Unavailable(_) => 0,
        }
    }

    pub fn is_pending_payload(&self) -> bool {
        matches!(self, Self::Edit(_) | Self::Write(_))
    }
}

#[derive(Clone, Eq, PartialEq)]
pub struct EditDetailSource {
    pub path: String,
    pub hunks: Vec<EditDetailHunk>,
}

impl EditDetailSource {
    fn retained_bytes(&self) -> usize {
        self.path.len().saturating_add(
            self.hunks
                .iter()
                .map(|hunk| hunk.old_text.len().saturating_add(hunk.new_text.len()))
                .sum::<usize>(),
        )
    }
}

#[derive(Clone, Eq, PartialEq)]
pub struct EditDetailHunk {
    pub old_text: String,
    pub new_text: String,
}

#[derive(Clone, Eq, PartialEq)]
pub struct WriteDetailSource {
    pub path: String,
    pub after: DetailText,
}

#[derive(Clone, Eq, PartialEq)]
pub struct ReadDetailSource {
    pub path: String,
    pub offset: u64,
}

#[derive(Clone, Eq, PartialEq)]
pub struct GrepDetailSource {
    pub path: String,
    pub pattern: String,
    pub literal: bool,
}

#[derive(Clone, Eq, PartialEq)]
pub struct DetailText {
    pub text: String,
    pub source_bytes: u64,
    pub source_lines: u64,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DetailPresentationKind {
    DiffCreate,
    DiffModify,
    Read,
    Grep,
    Find,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum DetailRowKind {
    Header,
    Hunk,
    Context,
    Addition,
    Deletion,
    ReadLine,
    GrepMatch,
    FindPath,
    Omission,
    Note,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct DetailRow {
    pub key: u64,
    pub kind: DetailRowKind,
    pub text: String,
    pub old_line: Option<u64>,
    pub new_line: Option<u64>,
    pub hidden_rows: u64,
    pub hidden_bytes: u64,
}

impl DetailRow {
    fn source_evidence_rows(&self) -> u64 {
        if self.kind == DetailRowKind::Omission {
            self.hidden_rows
        } else {
            1
        }
    }

    fn source_evidence_bytes(&self) -> u64 {
        if self.kind == DetailRowKind::Omission {
            self.hidden_bytes
        } else {
            u64::try_from(self.text.len())
                .unwrap_or(u64::MAX)
                .saturating_add(self.hidden_bytes)
        }
    }

    fn retained_bytes(&self) -> usize {
        self.text.len().saturating_add(std::mem::size_of::<Self>())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ToolDetailPresentation {
    pub kind: DetailPresentationKind,
    pub title: String,
    pub summary: String,
    pub additions: u64,
    pub deletions: u64,
    pub rows: Vec<DetailRow>,
    pub truncated: bool,
}

impl ToolDetailPresentation {
    pub fn visible_rows(&self, expanded: bool) -> Vec<DetailRow> {
        let (max_rows, max_bytes) = if expanded {
            (DETAIL_EXPANDED_MAX_ROWS, DETAIL_EXPANDED_MAX_BYTES)
        } else {
            (DETAIL_COLLAPSED_MAX_ROWS, DETAIL_COLLAPSED_MAX_BYTES)
        };
        select_detail_rows(&self.rows, max_rows, max_bytes)
    }

    pub fn can_expand(&self) -> bool {
        self.visible_rows(false) != self.visible_rows(true)
    }

    pub fn retained_bytes(&self) -> usize {
        self.title
            .len()
            .saturating_add(self.summary.len())
            .saturating_add(
                self.rows
                    .iter()
                    .map(DetailRow::retained_bytes)
                    .sum::<usize>(),
            )
            .saturating_add(std::mem::size_of::<Self>())
    }
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum DetailAvailability {
    None,
    LiveRetained(ToolDetailPresentation),
    Unavailable(DetailUnavailableReason),
}

impl DetailAvailability {
    pub fn retained_bytes(&self) -> usize {
        match self {
            Self::LiveRetained(presentation) => presentation.retained_bytes(),
            Self::None | Self::Unavailable(_) => 0,
        }
    }
}

pub struct DetailResult<'a> {
    pub output: &'a str,
    pub before_text: Option<&'a str>,
    pub created: bool,
    pub summary: Option<&'a str>,
    pub truncated: bool,
    pub projection_omitted_bytes: u64,
    pub projection_omitted_rows: u64,
    pub projection_cut_mid_line: bool,
}

pub fn project_tool_detail_source(name: &str, arguments: &Map<String, Value>) -> ToolDetailSource {
    match name {
        "edit" => project_edit_source(arguments),
        "write" => project_write_source(arguments),
        "read" => {
            let Some(path) = arguments
                .get("path")
                .and_then(Value::as_str)
                .filter(|path| !path.is_empty())
            else {
                return ToolDetailSource::Unavailable(DetailUnavailableReason::MalformedSource);
            };
            ToolDetailSource::Read(ReadDetailSource {
                path: one_line(&clip_chars(path, DETAIL_PATH_MAX_CHARS)),
                offset: arguments
                    .get("offset")
                    .and_then(Value::as_u64)
                    .filter(|value| *value > 0)
                    .unwrap_or(1),
            })
        }
        "grep" => ToolDetailSource::Grep(GrepDetailSource {
            path: arguments
                .get("path")
                .and_then(Value::as_str)
                .map(|path| one_line(&clip_chars(path, DETAIL_PATH_MAX_CHARS)))
                .unwrap_or_else(|| ".".into()),
            pattern: arguments
                .get("pattern")
                .and_then(Value::as_str)
                .map(|pattern| one_line(&clip_chars(pattern, DETAIL_PATH_MAX_CHARS)))
                .unwrap_or_default(),
            literal: arguments
                .get("literal")
                .and_then(Value::as_bool)
                .unwrap_or(false),
        }),
        "find" => ToolDetailSource::Find,
        _ => ToolDetailSource::None,
    }
}

pub fn capture_write_before(
    name: &str,
    value: Option<&Value>,
) -> Result<Option<String>, DetailUnavailableReason> {
    if name != "write" {
        return Ok(None);
    }
    let Some(value) = value else {
        return Ok(None);
    };
    if value.is_null() {
        return Ok(None);
    }
    let Some(source) = value.as_str() else {
        return Err(DetailUnavailableReason::MalformedSource);
    };
    capture_text(source).map(|capture| Some(capture.text))
}

pub fn build_tool_detail(
    source: &ToolDetailSource,
    result: DetailResult<'_>,
) -> DetailAvailability {
    match source {
        ToolDetailSource::None => DetailAvailability::None,
        ToolDetailSource::Unavailable(reason) => DetailAvailability::Unavailable(*reason),
        ToolDetailSource::Edit(source) => build_edit_presentation(source),
        ToolDetailSource::Write(source) => build_write_presentation(source, result),
        ToolDetailSource::Read(source) => build_read_presentation(source, result),
        ToolDetailSource::Grep(source) => build_grep_presentation(source, result),
        ToolDetailSource::Find => build_find_presentation(result),
    }
}

fn project_edit_source(arguments: &Map<String, Value>) -> ToolDetailSource {
    let Some(edits) = arguments.get("edits").and_then(Value::as_array) else {
        return ToolDetailSource::Unavailable(DetailUnavailableReason::MalformedSource);
    };
    if edits.is_empty() || edits.len() > DETAIL_EDIT_MAX_HUNKS {
        return ToolDetailSource::Unavailable(if edits.len() > DETAIL_EDIT_MAX_HUNKS {
            DetailUnavailableReason::SourceOverBudget
        } else {
            DetailUnavailableReason::MalformedSource
        });
    }
    let mut retained_bytes = 0usize;
    let mut retained_lines = 0usize;
    let mut hunks = Vec::with_capacity(edits.len());
    for edit in edits {
        let Some(edit) = edit.as_object() else {
            return ToolDetailSource::Unavailable(DetailUnavailableReason::MalformedSource);
        };
        let (Some(old_text), Some(new_text)) = (
            edit.get("oldText").and_then(Value::as_str),
            edit.get("newText").and_then(Value::as_str),
        ) else {
            return ToolDetailSource::Unavailable(DetailUnavailableReason::MalformedSource);
        };
        retained_bytes = retained_bytes
            .saturating_add(old_text.len())
            .saturating_add(new_text.len());
        if retained_bytes > DETAIL_SOURCE_MAX_BYTES.saturating_mul(2) {
            return ToolDetailSource::Unavailable(DetailUnavailableReason::SourceOverBudget);
        }
        let old_text = normalize_newlines(old_text);
        let new_text = normalize_newlines(new_text);
        retained_lines = retained_lines
            .saturating_add(logical_line_count(&old_text))
            .saturating_add(logical_line_count(&new_text));
        if retained_lines > DETAIL_SOURCE_MAX_LINES.saturating_mul(2) {
            return ToolDetailSource::Unavailable(DetailUnavailableReason::SourceOverBudget);
        }
        hunks.push(EditDetailHunk { old_text, new_text });
    }
    ToolDetailSource::Edit(EditDetailSource {
        path: bounded_path(arguments.get("path")),
        hunks,
    })
}

fn project_write_source(arguments: &Map<String, Value>) -> ToolDetailSource {
    let Some(content) = arguments.get("content").and_then(Value::as_str) else {
        return ToolDetailSource::Unavailable(DetailUnavailableReason::MalformedSource);
    };
    match capture_text(content) {
        Ok(after) => ToolDetailSource::Write(WriteDetailSource {
            path: bounded_path(arguments.get("path")),
            after,
        }),
        Err(reason) => ToolDetailSource::Unavailable(reason),
    }
}

fn capture_text(source: &str) -> Result<DetailText, DetailUnavailableReason> {
    if source.len() > DETAIL_SOURCE_MAX_BYTES {
        return Err(DetailUnavailableReason::SourceOverBudget);
    }
    let normalized = normalize_newlines(source);
    let lines = logical_line_count(&normalized);
    if normalized.len() > DETAIL_SOURCE_MAX_BYTES || lines > DETAIL_SOURCE_MAX_LINES {
        return Err(DetailUnavailableReason::SourceOverBudget);
    }
    Ok(DetailText {
        source_bytes: u64::try_from(normalized.len()).unwrap_or(u64::MAX),
        source_lines: u64::try_from(lines).unwrap_or(u64::MAX),
        text: normalized,
    })
}

fn build_edit_presentation(source: &EditDetailSource) -> DetailAvailability {
    let mut rows = Vec::new();
    let mut additions = 0u64;
    let mut deletions = 0u64;
    for (index, hunk) in source.hunks.iter().enumerate() {
        if hunk.old_text == hunk.new_text {
            continue;
        }
        rows.push(row(
            DetailRowKind::Hunk,
            format!("@@ edit {} @@", index + 1),
        ));
        append_diff_rows(
            &mut rows,
            &hunk.old_text,
            &hunk.new_text,
            false,
            &mut additions,
            &mut deletions,
        );
    }
    if rows.is_empty() {
        return DetailAvailability::None;
    }
    DetailAvailability::LiveRetained(finalize_presentation(
        DetailPresentationKind::DiffModify,
        format!("M {}", display_path(&source.path)),
        format!("+{additions} -{deletions}"),
        additions,
        deletions,
        rows,
        false,
    ))
}

fn build_write_presentation(
    source: &WriteDetailSource,
    result: DetailResult<'_>,
) -> DetailAvailability {
    let before = if result.created {
        String::new()
    } else {
        let Some(before) = result.before_text else {
            return DetailAvailability::Unavailable(DetailUnavailableReason::MissingBeforeSnapshot);
        };
        match capture_text(before) {
            Ok(before) => before.text,
            Err(reason) => return DetailAvailability::Unavailable(reason),
        }
    };
    if before == source.after.text {
        return DetailAvailability::None;
    }
    let mut rows = Vec::new();
    let mut additions = 0u64;
    let mut deletions = 0u64;
    append_diff_rows(
        &mut rows,
        &before,
        &source.after.text,
        true,
        &mut additions,
        &mut deletions,
    );
    let kind = if result.created {
        DetailPresentationKind::DiffCreate
    } else {
        DetailPresentationKind::DiffModify
    };
    let marker = if result.created { 'A' } else { 'M' };
    DetailAvailability::LiveRetained(finalize_presentation(
        kind,
        format!("{marker} {}", display_path(&source.path)),
        format!("+{additions} -{deletions}"),
        additions,
        deletions,
        rows,
        false,
    ))
}

fn unified_diff_start(start: usize, len: usize) -> usize {
    start.saturating_add(usize::from(len > 0))
}

fn append_diff_rows(
    rows: &mut Vec<DetailRow>,
    before: &str,
    after: &str,
    line_numbers: bool,
    additions: &mut u64,
    deletions: &mut u64,
) {
    let diff = TextDiff::from_lines(before, after);
    for (group_index, operations) in diff.grouped_ops(3).into_iter().enumerate() {
        if line_numbers {
            let old_start = operations
                .first()
                .map_or(0, |operation| operation.old_range().start);
            let old_end = operations
                .last()
                .map_or(old_start, |operation| operation.old_range().end);
            let new_start = operations
                .first()
                .map_or(0, |operation| operation.new_range().start);
            let new_end = operations
                .last()
                .map_or(new_start, |operation| operation.new_range().end);
            let old_len = old_end.saturating_sub(old_start);
            let new_len = new_end.saturating_sub(new_start);
            rows.push(row(
                DetailRowKind::Hunk,
                format!(
                    "@@ -{},{old_len} +{},{new_len} @@",
                    unified_diff_start(old_start, old_len),
                    unified_diff_start(new_start, new_len),
                ),
            ));
        } else if group_index > 0 {
            rows.push(row(
                DetailRowKind::Hunk,
                format!("@@ replacement {} @@", group_index + 1),
            ));
        }
        for operation in operations {
            for change in diff.iter_changes(&operation) {
                let missing_terminator = !change.value().ends_with('\n');
                let text = change
                    .value()
                    .strip_suffix('\n')
                    .unwrap_or(change.value())
                    .to_owned();
                match change.tag() {
                    ChangeTag::Equal => {
                        let mut detail = row(DetailRowKind::Context, text);
                        if line_numbers {
                            detail.old_line = change
                                .old_index()
                                .map(|line| u64::try_from(line + 1).unwrap_or(u64::MAX));
                            detail.new_line = change
                                .new_index()
                                .map(|line| u64::try_from(line + 1).unwrap_or(u64::MAX));
                        }
                        rows.push(detail);
                        if missing_terminator {
                            rows.push(row(
                                DetailRowKind::Note,
                                "\\ No newline at end of either file".into(),
                            ));
                        }
                    }
                    ChangeTag::Delete => {
                        *deletions = deletions.saturating_add(1);
                        let mut detail = row(DetailRowKind::Deletion, text);
                        if line_numbers {
                            detail.old_line = change
                                .old_index()
                                .map(|line| u64::try_from(line + 1).unwrap_or(u64::MAX));
                        }
                        rows.push(detail);
                        if missing_terminator {
                            rows.push(row(
                                DetailRowKind::Note,
                                "\\ No newline at end of old file".into(),
                            ));
                        }
                    }
                    ChangeTag::Insert => {
                        *additions = additions.saturating_add(1);
                        let mut detail = row(DetailRowKind::Addition, text);
                        if line_numbers {
                            detail.new_line = change
                                .new_index()
                                .map(|line| u64::try_from(line + 1).unwrap_or(u64::MAX));
                        }
                        rows.push(detail);
                        if missing_terminator {
                            rows.push(row(
                                DetailRowKind::Note,
                                "\\ No newline at end of new file".into(),
                            ));
                        }
                    }
                }
            }
        }
    }
}

fn build_read_presentation(
    source: &ReadDetailSource,
    result: DetailResult<'_>,
) -> DetailAvailability {
    let Some(summary) = result.summary else {
        return DetailAvailability::None;
    };
    let mut retained_lines = logical_lines(result.output);
    if result.truncated
        && retained_lines
            .last()
            .is_some_and(|line| is_truncation_marker(line))
    {
        retained_lines.pop();
    }
    let mut presentation_omitted_rows = result.projection_omitted_rows;
    let mut presentation_omitted_bytes = result.projection_omitted_bytes;
    if result.projection_cut_mid_line {
        if let Some(partial) = retained_lines.pop() {
            presentation_omitted_rows = presentation_omitted_rows.saturating_add(1);
            presentation_omitted_bytes = presentation_omitted_bytes
                .saturating_add(u64::try_from(partial.len()).unwrap_or(u64::MAX));
        }
    }
    let mut rows = Vec::new();
    for (index, line) in retained_lines.into_iter().enumerate() {
        let mut detail = row(DetailRowKind::ReadLine, line.to_owned());
        detail.old_line = Some(source.offset.saturating_add(index as u64));
        rows.push(detail);
    }
    append_projection_omission(
        &mut rows,
        presentation_omitted_rows,
        presentation_omitted_bytes,
    );
    if rows.is_empty() {
        return DetailAvailability::None;
    }
    DetailAvailability::LiveRetained(finalize_presentation(
        DetailPresentationKind::Read,
        display_path(&source.path).to_owned(),
        summary.to_owned(),
        0,
        0,
        rows,
        result.truncated || result.projection_omitted_bytes > 0,
    ))
}

fn build_grep_presentation(
    source: &GrepDetailSource,
    result: DetailResult<'_>,
) -> DetailAvailability {
    let Some(summary) = result.summary else {
        return DetailAvailability::None;
    };
    let Some(total_count) = summary_count(summary, "grep:") else {
        return DetailAvailability::None;
    };
    let retained = bounded_result_records(
        result.output,
        result.truncated,
        result.projection_cut_mid_line,
        true,
    );
    let mut rows = Vec::new();
    for line in retained.lines {
        let Some((path, line_number, text)) = parse_grep_record(line) else {
            return DetailAvailability::None;
        };
        let mut detail = row(DetailRowKind::GrepMatch, format!("{path}: {text}"));
        detail.old_line = Some(line_number);
        rows.push(detail);
    }
    let retained_count = u64::try_from(rows.len()).unwrap_or(u64::MAX);
    let incomplete = result.truncated || result.projection_omitted_bytes > 0;
    if retained_count > total_count || (!incomplete && retained_count != total_count) {
        return DetailAvailability::None;
    }
    append_projection_omission(
        &mut rows,
        result
            .projection_omitted_rows
            .saturating_add(retained.hidden_rows),
        result
            .projection_omitted_bytes
            .saturating_add(retained.hidden_bytes),
    );
    if rows.is_empty() {
        return DetailAvailability::None;
    }
    let title = if source.path.is_empty() {
        "."
    } else {
        &source.path
    };
    DetailAvailability::LiveRetained(finalize_presentation(
        DetailPresentationKind::Grep,
        title.to_owned(),
        summary.to_owned(),
        0,
        0,
        rows,
        incomplete,
    ))
}

fn build_find_presentation(result: DetailResult<'_>) -> DetailAvailability {
    let Some(summary) = result.summary else {
        return DetailAvailability::None;
    };
    let Some(total_count) = summary_count(summary, "find:") else {
        return DetailAvailability::None;
    };
    let retained = bounded_result_records(
        result.output,
        result.truncated,
        result.projection_cut_mid_line,
        true,
    );
    let mut rows = Vec::new();
    for path in retained.lines {
        if path.is_empty() {
            return DetailAvailability::None;
        }
        rows.push(row(DetailRowKind::FindPath, path.to_owned()));
    }
    let retained_count = u64::try_from(rows.len()).unwrap_or(u64::MAX);
    let incomplete = result.truncated || result.projection_omitted_bytes > 0;
    if retained_count > total_count || (!incomplete && retained_count != total_count) {
        return DetailAvailability::None;
    }
    append_projection_omission(
        &mut rows,
        result
            .projection_omitted_rows
            .saturating_add(retained.hidden_rows),
        result
            .projection_omitted_bytes
            .saturating_add(retained.hidden_bytes),
    );
    if rows.is_empty() {
        return DetailAvailability::None;
    }
    DetailAvailability::LiveRetained(finalize_presentation(
        DetailPresentationKind::Find,
        "find results".into(),
        summary.to_owned(),
        0,
        0,
        rows,
        incomplete,
    ))
}

fn append_projection_omission(rows: &mut Vec<DetailRow>, omitted_rows: u64, omitted_bytes: u64) {
    if omitted_rows == 0 && omitted_bytes == 0 {
        return;
    }
    let text = if omitted_rows == 0 {
        format!("… {omitted_bytes} source bytes omitted before structured presentation …")
    } else {
        format!(
            "… {omitted_rows} rows / {omitted_bytes} source bytes omitted before structured presentation …"
        )
    };
    rows.push(DetailRow {
        key: 0,
        kind: DetailRowKind::Omission,
        text,
        old_line: None,
        new_line: None,
        hidden_rows: omitted_rows,
        hidden_bytes: omitted_bytes,
    });
}

fn finalize_presentation(
    kind: DetailPresentationKind,
    title: String,
    summary: String,
    additions: u64,
    deletions: u64,
    rows: Vec<DetailRow>,
    truncated: bool,
) -> ToolDetailPresentation {
    let rows = rekey(rows);
    let rows = select_detail_rows(&rows, DETAIL_EXPANDED_MAX_ROWS, DETAIL_EXPANDED_MAX_BYTES);
    ToolDetailPresentation {
        kind,
        title,
        summary,
        additions,
        deletions,
        rows,
        truncated,
    }
}

pub fn select_detail_rows(rows: &[DetailRow], max_rows: usize, max_bytes: usize) -> Vec<DetailRow> {
    if rows.is_empty() || max_rows == 0 || max_bytes == 0 {
        return Vec::new();
    }
    let retained_bytes = rows.iter().map(|row| row.text.len()).sum::<usize>();
    if rows.len() <= max_rows && retained_bytes <= max_bytes {
        return rows.to_vec();
    }
    if max_rows == 1 {
        let hidden_rows = rows.iter().map(DetailRow::source_evidence_rows).sum();
        let hidden_bytes = rows.iter().map(DetailRow::source_evidence_bytes).sum();
        return vec![omission_row(hidden_rows, hidden_bytes, u64::MAX)];
    }
    if rows.len() == 1 {
        let omission_budget = OMISSION_TEXT_MAX_BYTES.min(max_bytes);
        let mut selected = take_rows(rows.iter(), 1, max_bytes.saturating_sub(omission_budget));
        let clipped_bytes = selected
            .first_mut()
            .map(|row| std::mem::take(&mut row.hidden_bytes))
            .unwrap_or_else(|| rows[0].source_evidence_bytes());
        if clipped_bytes > 0 {
            selected.push(omission_row(0, clipped_bytes, u64::MAX));
        }
        return selected;
    }

    let omission_budget = OMISSION_TEXT_MAX_BYTES.min(max_bytes);
    let body_budget = max_bytes.saturating_sub(omission_budget);
    let head_row_budget = (max_rows - 1) / 2;
    let tail_row_budget = max_rows - 1 - head_row_budget;
    let head_byte_budget = body_budget / 2;
    let tail_byte_budget = body_budget.saturating_sub(head_byte_budget);

    let mut head = take_rows(rows.iter(), head_row_budget, head_byte_budget);
    let head_count = head.len();
    let tail_candidates = &rows[head_count..];
    let mut tail = take_rows(
        tail_candidates.iter().rev(),
        tail_row_budget,
        tail_byte_budget,
    );
    tail.reverse();
    let tail_count = tail.len();
    let hidden_end = rows.len().saturating_sub(tail_count);
    let hidden = if head_count <= hidden_end {
        &rows[head_count..hidden_end]
    } else {
        &[]
    };
    let hidden_rows = hidden.iter().map(DetailRow::source_evidence_rows).sum();
    let mut hidden_bytes: u64 = hidden.iter().map(DetailRow::source_evidence_bytes).sum();
    for row in head.iter_mut().chain(tail.iter_mut()) {
        if row.kind != DetailRowKind::Omission {
            hidden_bytes = hidden_bytes.saturating_add(std::mem::take(&mut row.hidden_bytes));
        }
    }

    let mut selected = Vec::with_capacity(head.len().saturating_add(tail.len()).saturating_add(1));
    selected.append(&mut head);
    if hidden_rows > 0 || hidden_bytes > 0 {
        let omission_key = hidden
            .first()
            .map_or(u64::MAX, |row| u64::MAX.saturating_sub(row.key));
        selected.push(omission_row(hidden_rows, hidden_bytes, omission_key));
    }
    selected.append(&mut tail);
    selected
}

fn take_rows<'a>(
    rows: impl Iterator<Item = &'a DetailRow>,
    max_rows: usize,
    max_bytes: usize,
) -> Vec<DetailRow> {
    let mut selected = Vec::new();
    let mut bytes = 0usize;
    for row in rows.take(max_rows) {
        let remaining = max_bytes.saturating_sub(bytes);
        if remaining == 0 {
            break;
        }
        if row.text.len() <= remaining {
            selected.push(row.clone());
            bytes = bytes.saturating_add(row.text.len());
            continue;
        }
        if selected.is_empty() && remaining >= '…'.len_utf8() {
            let mut clipped = row.clone();
            let retained =
                floor_char_boundary(&clipped.text, remaining.saturating_sub('…'.len_utf8()));
            let dropped = clipped.text.len().saturating_sub(retained);
            clipped.text.truncate(retained);
            clipped.text.push('…');
            clipped.hidden_bytes = clipped
                .hidden_bytes
                .saturating_add(u64::try_from(dropped).unwrap_or(u64::MAX));
            selected.push(clipped);
        }
        break;
    }
    selected
}

fn omission_row(hidden_rows: u64, hidden_bytes: u64, key: u64) -> DetailRow {
    let text = if hidden_rows == 0 {
        format!("… {hidden_bytes} bytes omitted …")
    } else {
        format!("… {hidden_rows} rows / {hidden_bytes} bytes omitted …")
    };
    DetailRow {
        key,
        kind: DetailRowKind::Omission,
        text,
        old_line: None,
        new_line: None,
        hidden_rows,
        hidden_bytes,
    }
}

fn row(kind: DetailRowKind, text: String) -> DetailRow {
    DetailRow {
        key: 0,
        kind,
        text,
        old_line: None,
        new_line: None,
        hidden_rows: 0,
        hidden_bytes: 0,
    }
}

fn rekey(mut rows: Vec<DetailRow>) -> Vec<DetailRow> {
    for (index, row) in rows.iter_mut().enumerate() {
        row.key = index as u64;
    }
    rows
}

struct BoundedResultRecords<'a> {
    lines: Vec<&'a str>,
    hidden_rows: u64,
    hidden_bytes: u64,
}

fn bounded_result_records(
    output: &str,
    backend_truncated: bool,
    projection_cut_mid_line: bool,
    drop_terminal_record: bool,
) -> BoundedResultRecords<'_> {
    let mut lines = logical_lines(output);
    if backend_truncated && lines.last().is_some_and(|line| is_truncation_marker(line)) {
        lines.pop();
    }
    let mut hidden_rows = 0;
    let mut hidden_bytes = 0;
    if (backend_truncated || projection_cut_mid_line) && drop_terminal_record && !lines.is_empty() {
        let partial = lines.pop().expect("checked non-empty");
        hidden_rows = 1;
        hidden_bytes = u64::try_from(partial.len()).unwrap_or(u64::MAX);
    }
    BoundedResultRecords {
        lines,
        hidden_rows,
        hidden_bytes,
    }
}

fn is_truncation_marker(line: &str) -> bool {
    !line.is_empty() && "[truncated]".starts_with(line)
}

fn parse_grep_record(line: &str) -> Option<(&str, u64, &str)> {
    let bytes = line.as_bytes();
    let mut found = None;
    let mut index = 0usize;
    while index < bytes.len() {
        let separator = bytes[index];
        if !matches!(separator, b':' | b'-') {
            index += 1;
            continue;
        }
        let start = index + 1;
        let mut end = start;
        while end < bytes.len() && bytes[end].is_ascii_digit() {
            end += 1;
        }
        if end == start || end >= bytes.len() || bytes[end] != separator {
            index += 1;
            continue;
        }
        if separator != b':' || found.is_some() {
            return None;
        }
        let number = line[start..end]
            .parse::<u64>()
            .ok()
            .filter(|value| *value > 0)?;
        found = Some((&line[..index], number, &line[end + 1..]));
        index = end + 1;
    }
    let (path, number, text) = found?;
    (!path.is_empty()).then_some((path, number, text))
}

fn summary_count(summary: &str, prefix: &str) -> Option<u64> {
    let rest = summary.strip_prefix(prefix)?.trim_start();
    let nouns: &[&str] = match prefix {
        "grep:" => &["match", "matches"],
        "find:" => &["file", "files"],
        _ => return None,
    };
    if rest == format!("no {}", nouns[1]) {
        return Some(0);
    }
    let mut words = rest.split_whitespace();
    let count = words.next()?.parse().ok()?;
    if !nouns.contains(&words.next()?) {
        return None;
    }
    let suffix = words.collect::<Vec<_>>().join(" ");
    if suffix.is_empty() || suffix == "(+ more)" {
        Some(count)
    } else {
        None
    }
}

fn logical_lines(source: &str) -> Vec<&str> {
    if source.is_empty() {
        return Vec::new();
    }
    let mut lines = source.split('\n').collect::<Vec<_>>();
    if source.ends_with('\n') {
        lines.pop();
    }
    lines
}

fn logical_line_count(source: &str) -> usize {
    if source.is_empty() {
        0
    } else {
        source.bytes().filter(|byte| *byte == b'\n').count() + usize::from(!source.ends_with('\n'))
    }
}

fn bounded_path(value: Option<&Value>) -> String {
    let path = value.and_then(Value::as_str).unwrap_or("(unnamed file)");
    one_line(&clip_chars(path, DETAIL_PATH_MAX_CHARS))
}

fn display_path(path: &str) -> &str {
    if path.is_empty() {
        "(unnamed file)"
    } else {
        path
    }
}

fn one_line(source: &str) -> String {
    source.split_whitespace().collect::<Vec<_>>().join(" ")
}

fn normalize_newlines(source: &str) -> String {
    source.replace("\r\n", "\n").replace('\r', "\n")
}

fn clip_chars(source: &str, max_chars: usize) -> String {
    if max_chars == 0 {
        return String::new();
    }
    let mut characters = source.chars();
    let mut clipped = characters.by_ref().take(max_chars).collect::<String>();
    if characters.next().is_none() {
        return clipped;
    }
    clipped.pop();
    clipped.push('…');
    clipped
}

fn floor_char_boundary(source: &str, index: usize) -> usize {
    let mut index = index.min(source.len());
    while index > 0 && !source.is_char_boundary(index) {
        index -= 1;
    }
    index
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fmt::Write as _;

    fn result<'a>(output: &'a str, summary: Option<&'a str>) -> DetailResult<'a> {
        DetailResult {
            output,
            before_text: None,
            created: false,
            summary,
            truncated: false,
            projection_omitted_bytes: 0,
            projection_omitted_rows: 0,
            projection_cut_mid_line: false,
        }
    }

    #[test]
    fn edit_projection_drops_raw_json_but_builds_a_bounded_diff() {
        let arguments = serde_json::json!({
            "path": "src/main.rs",
            "edits": [{"oldText": "old\n", "newText": "new\n"}]
        });
        let source = project_tool_detail_source("edit", arguments.as_object().unwrap());
        let DetailAvailability::LiveRetained(detail) =
            build_tool_detail(&source, result("ok", None))
        else {
            panic!("edit detail expected");
        };
        assert_eq!(detail.additions, 1);
        assert_eq!(detail.deletions, 1);
        assert!(
            detail
                .rows
                .iter()
                .any(|row| row.kind == DetailRowKind::Addition)
        );
        assert!(source.retained_bytes() <= DETAIL_SOURCE_MAX_BYTES * 2);
        let debug = format!("{source:?}");
        assert!(!debug.contains("old\n"));
        assert!(!debug.contains("new\n"));
    }

    #[test]
    fn write_requires_a_complete_previous_snapshot_for_overwrites() {
        let arguments = serde_json::json!({"path": "file.txt", "content": "after\n"});
        let source = project_tool_detail_source("write", arguments.as_object().unwrap());
        assert_eq!(
            build_tool_detail(&source, result("ok", None)),
            DetailAvailability::Unavailable(DetailUnavailableReason::MissingBeforeSnapshot)
        );
        let DetailAvailability::LiveRetained(detail) = build_tool_detail(
            &source,
            DetailResult {
                before_text: Some("before\n"),
                ..result("ok", None)
            },
        ) else {
            panic!("write detail expected");
        };
        assert_eq!((detail.additions, detail.deletions), (1, 1));

        let newline_arguments = serde_json::json!({"path": "file.txt", "content": "new\n"});
        let newline_source =
            project_tool_detail_source("write", newline_arguments.as_object().unwrap());
        let DetailAvailability::LiveRetained(newline_detail) = build_tool_detail(
            &newline_source,
            DetailResult {
                before_text: Some("old"),
                ..result("ok", None)
            },
        ) else {
            panic!("newline detail expected");
        };
        assert!(
            newline_detail
                .rows
                .iter()
                .any(|row| { row.kind == DetailRowKind::Note && row.text.contains("old file") })
        );
        assert!(
            !newline_detail
                .rows
                .iter()
                .any(|row| { row.kind == DetailRowKind::Note && row.text.contains("new file") })
        );
    }

    #[test]
    fn unified_diff_zero_length_ranges_use_line_zero_anchors() {
        let mut rows = Vec::new();
        append_diff_rows(&mut rows, "", "first\nsecond\n", true, &mut 0, &mut 0);
        assert_eq!(rows[0].text, "@@ -0,0 +1,2 @@");

        rows.clear();
        append_diff_rows(&mut rows, "first\nsecond\n", "", true, &mut 0, &mut 0);
        assert_eq!(rows[0].text, "@@ -1,2 +0,0 @@");
        assert_eq!(unified_diff_start(5, 0), 5);
        assert_eq!(unified_diff_start(5, 2), 6);
    }

    #[test]
    fn whole_file_diff_retains_only_bounded_context_around_changes() {
        let mut before = String::new();
        for index in 0..100 {
            writeln!(before, "line {index}").unwrap();
        }
        let after = before.replace("line 50\n", "changed 50\n");
        let arguments = serde_json::json!({"path": "file.txt", "content": after});
        let source = project_tool_detail_source("write", arguments.as_object().unwrap());
        let DetailAvailability::LiveRetained(detail) = build_tool_detail(
            &source,
            DetailResult {
                before_text: Some(&before),
                ..result("ok", None)
            },
        ) else {
            panic!("write detail expected");
        };
        assert!(detail.rows.len() < 20);
        assert!(detail.rows.iter().any(|row| row.text == "changed 50"));
        assert_eq!((detail.additions, detail.deletions), (1, 1));
    }

    #[test]
    fn file_results_fail_closed_on_ambiguous_or_inconsistent_records() {
        let grep = project_tool_detail_source(
            "grep",
            serde_json::json!({"pattern": "needle", "path": "."})
                .as_object()
                .unwrap(),
        );
        assert!(matches!(
            build_tool_detail(&grep, result("a.rs:2:needle\n", Some("grep: 1 match"))),
            DetailAvailability::LiveRetained(_)
        ));
        let default_grep = project_tool_detail_source(
            "grep",
            serde_json::json!({"pattern": "needle"})
                .as_object()
                .unwrap(),
        );
        let ToolDetailSource::Grep(default_grep_source) = &default_grep else {
            panic!("grep source expected");
        };
        assert_eq!(default_grep_source.path, ".");
        let DetailAvailability::LiveRetained(default_grep_detail) = build_tool_detail(
            &default_grep,
            result("a.rs:2:needle\n", Some("grep: 1 match")),
        ) else {
            panic!("grep detail expected");
        };
        assert_eq!(default_grep_detail.title, ".");
        assert_eq!(
            build_tool_detail(&grep, result("a:2:needle:3:other\n", Some("grep: 1 match"))),
            DetailAvailability::None
        );

        let find = ToolDetailSource::Find;
        assert_eq!(
            build_tool_detail(&find, result("a\n", Some("find: 2 files"))),
            DetailAvailability::None
        );
    }

    #[test]
    fn truncated_file_results_never_promote_the_terminal_record_or_marker() {
        let grep = project_tool_detail_source(
            "grep",
            serde_json::json!({"pattern": "x", "path": "."})
                .as_object()
                .unwrap(),
        );
        let DetailAvailability::LiveRetained(grep_detail) = build_tool_detail(
            &grep,
            DetailResult {
                truncated: true,
                ..result(
                    "a.rs:1:x\npartial.rs:2:x\n[truncated]",
                    Some("grep: 2 matches (+ more)"),
                )
            },
        ) else {
            panic!("grep detail expected");
        };
        assert_eq!(
            grep_detail
                .rows
                .iter()
                .filter(|row| row.kind == DetailRowKind::GrepMatch)
                .count(),
            1
        );
        assert!(grep_detail.rows.iter().any(|row| row.text.contains("a.rs")));
        assert!(
            !grep_detail
                .rows
                .iter()
                .any(|row| row.text.contains("partial"))
        );
        assert!(
            grep_detail
                .rows
                .iter()
                .any(|row| { row.kind == DetailRowKind::Omission && row.hidden_rows == 1 })
        );
        assert_eq!(
            build_tool_detail(
                &grep,
                DetailResult {
                    truncated: true,
                    ..result(
                        "a.rs:1:x\nb.rs:2:x\nc.rs:3:x\n[truncated]",
                        Some("grep: 1 match (+ more)"),
                    )
                },
            ),
            DetailAvailability::None
        );

        let read = project_tool_detail_source(
            "read",
            serde_json::json!({"path": "a.rs"}).as_object().unwrap(),
        );
        let DetailAvailability::LiveRetained(read_detail) = build_tool_detail(
            &read,
            DetailResult {
                truncated: true,
                ..result("line\n[truncated]", Some("read 1 line from a.rs"))
            },
        ) else {
            panic!("read detail expected");
        };
        assert_eq!(read_detail.rows.len(), 1);
        assert_eq!(read_detail.rows[0].text, "line");

        let DetailAvailability::LiveRetained(projected_read) = build_tool_detail(
            &read,
            DetailResult {
                projection_omitted_bytes: 9_999,
                ..result("retained\n", Some("read 1000 lines from a.rs"))
            },
        ) else {
            panic!("projected read detail expected");
        };
        assert!(projected_read.truncated);
        let omission = projected_read
            .rows
            .iter()
            .find(|row| row.kind == DetailRowKind::Omission)
            .expect("frontend omission row");
        assert_eq!(omission.hidden_bytes, 9_999);
    }

    #[test]
    fn row_selection_is_bounded_and_reports_hidden_evidence() {
        let rows = (0..20)
            .map(|index| row(DetailRowKind::Context, format!("line {index}")))
            .collect::<Vec<_>>();
        let selected = select_detail_rows(&rows, 8, 2_000);
        assert!(selected.len() <= 8);
        let omission = selected
            .iter()
            .find(|row| row.kind == DetailRowKind::Omission)
            .expect("omission row");
        assert_eq!(omission.hidden_rows, 13);

        let long = vec![row(DetailRowKind::ReadLine, "x".repeat(3_000))];
        let selected = select_detail_rows(&long, 8, 2_000);
        assert_eq!(selected.len(), 2);
        assert!(selected[0].text.len() > 1_800);
        assert!(selected[0].text.ends_with('…'));
        assert_eq!(selected[1].kind, DetailRowKind::Omission);
        assert!(selected[1].hidden_bytes >= 1_000);
        assert!(selected.iter().map(|row| row.text.len()).sum::<usize>() <= 2_000);
    }

    #[test]
    fn source_work_limits_fail_closed() {
        let arguments = serde_json::json!({
            "path": "huge.txt",
            "content": "x".repeat(DETAIL_SOURCE_MAX_BYTES + 1)
        });
        assert_eq!(
            project_tool_detail_source("write", arguments.as_object().unwrap()),
            ToolDetailSource::Unavailable(DetailUnavailableReason::SourceOverBudget)
        );

        let grep_arguments = serde_json::json!({
            "path": ".",
            "pattern": "x ".repeat(100_000),
        });
        let grep = project_tool_detail_source("grep", grep_arguments.as_object().unwrap());
        assert!(grep.retained_bytes() <= DETAIL_PATH_MAX_CHARS * 2);
    }
}
