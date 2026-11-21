use std::ops::Range;
use std::sync::OnceLock;

use syntect::easy::ScopeRegionIterator;
use syntect::parsing::{ParseState, Scope, ScopeStack, SyntaxSet};

pub(crate) const MAX_SYNTAX_SOURCE_BYTES_PER_FENCE: usize = 32 * 1024;
pub(crate) const MAX_SYNTAX_LINE_BYTES: usize = 8 * 1024;
pub(crate) const MAX_SYNTAX_LINES_PER_FENCE: usize = 512;
pub(crate) const MAX_SYNTAX_FRAGMENTS_PER_FENCE: usize = 1_024;
pub(crate) const MAX_SYNTAX_SOURCE_BYTES_PER_BUILD: usize = 64 * 1024;
pub(crate) const MAX_SYNTAX_FRAGMENTS_PER_BUILD: usize = 2_048;
const MAX_DIFF_SYNTAX_BYTES: usize = 64 * 1024;
const MAX_DIFF_SYNTAX_LINES: usize = 2_000;
const MAX_LANGUAGE_TOKEN_BYTES: usize = 32;

#[derive(Clone, Copy, Debug, Default, Eq, Hash, PartialEq)]
pub enum SyntaxClass {
    #[default]
    Plain,
    Comment,
    Keyword,
    String,
    Number,
    Type,
    Function,
    Constant,
    Variable,
    Operator,
    Punctuation,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct SyntaxToken {
    pub range: Range<usize>,
    pub class: SyntaxClass,
}

#[derive(Clone, Copy, Debug, Default, Eq, PartialEq)]
pub(crate) struct SyntaxWork {
    pub source_bytes: usize,
    pub lines: usize,
    pub fragments: usize,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum SyntaxFallback {
    InvalidLanguage,
    UnknownLanguage,
    SourceLimit,
    LineLimit,
    LineByteLimit,
    FragmentLimit,
    ParseError,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) struct SyntaxFailure {
    pub reason: SyntaxFallback,
    pub work: SyntaxWork,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct SyntaxHighlight {
    pub tokens: Vec<SyntaxToken>,
    pub work: SyntaxWork,
}

pub(crate) fn highlight_fence(info: &str, source: &str) -> Result<SyntaxHighlight, SyntaxFailure> {
    let language = normalize_language(info).ok_or(SyntaxFailure {
        reason: SyntaxFallback::InvalidLanguage,
        work: SyntaxWork::default(),
    })?;
    if source.len() > MAX_SYNTAX_SOURCE_BYTES_PER_FENCE {
        return Err(SyntaxFailure {
            reason: SyntaxFallback::SourceLimit,
            work: SyntaxWork::default(),
        });
    }
    let syntax_set = syntax_set();
    let syntax = syntax_set
        .find_syntax_by_token(&language)
        .ok_or(SyntaxFailure {
            reason: SyntaxFallback::UnknownLanguage,
            work: SyntaxWork::default(),
        })?;
    let mut parser = ParseState::new(syntax);
    let mut stack = ScopeStack::new();
    let mut tokens = Vec::<SyntaxToken>::new();
    let mut work = SyntaxWork {
        source_bytes: source.len(),
        ..SyntaxWork::default()
    };
    let mut source_offset = 0_usize;

    for (line_index, line) in source.split_inclusive('\n').enumerate() {
        if line_index >= MAX_SYNTAX_LINES_PER_FENCE {
            return Err(SyntaxFailure {
                reason: SyntaxFallback::LineLimit,
                work,
            });
        }
        work.lines = work.lines.saturating_add(1);
        if line.len() > MAX_SYNTAX_LINE_BYTES {
            return Err(SyntaxFailure {
                reason: SyntaxFallback::LineByteLimit,
                work,
            });
        }
        let operations = parser
            .parse_line(line, syntax_set)
            .map_err(|_| SyntaxFailure {
                reason: SyntaxFallback::ParseError,
                work,
            })?;
        let mut line_offset = 0_usize;
        for (region, operation) in ScopeRegionIterator::new(&operations, line) {
            stack.apply(operation).map_err(|_| SyntaxFailure {
                reason: SyntaxFallback::ParseError,
                work,
            })?;
            if region.is_empty() {
                continue;
            }
            let start = source_offset.saturating_add(line_offset);
            let end = start.saturating_add(region.len());
            push_token(&mut tokens, start..end, classify_scope(&stack));
            if tokens.len() > MAX_SYNTAX_FRAGMENTS_PER_FENCE {
                work.fragments = tokens.len();
                return Err(SyntaxFailure {
                    reason: SyntaxFallback::FragmentLimit,
                    work,
                });
            }
            line_offset = line_offset.saturating_add(region.len());
        }
        if line_offset < line.len() {
            let start = source_offset.saturating_add(line_offset);
            push_token(
                &mut tokens,
                start..source_offset.saturating_add(line.len()),
                SyntaxClass::Plain,
            );
            if tokens.len() > MAX_SYNTAX_FRAGMENTS_PER_FENCE {
                work.fragments = tokens.len();
                return Err(SyntaxFailure {
                    reason: SyntaxFallback::FragmentLimit,
                    work,
                });
            }
        }
        source_offset = source_offset.saturating_add(line.len());
    }

    if source_offset < source.len() {
        push_token(&mut tokens, source_offset..source.len(), SyntaxClass::Plain);
    }
    work.fragments = tokens.len();
    Ok(SyntaxHighlight { tokens, work })
}

/// Derive a syntax token from a file path or diff title (such as `"M src/app.tsx"` or
/// `"Makefile"`). Returns an empty string when no language can be inferred.
pub(crate) fn language_for_path(path_or_title: &str) -> String {
    let trimmed = path_or_title.trim();
    let path = trimmed
        .strip_prefix("M ")
        .or_else(|| trimmed.strip_prefix("A "))
        .or_else(|| trimmed.strip_prefix("D "))
        .or_else(|| trimmed.strip_prefix("R "))
        .unwrap_or(trimmed);
    let file_name = path.rsplit('/').next().unwrap_or(path);
    match file_name.to_ascii_lowercase().as_str() {
        "makefile" | "gnumakefile" => return "makefile".into(),
        ".bashrc" | ".zshrc" | ".profile" | ".bash_profile" => return "bash".into(),
        _ => {}
    }
    let Some((_, ext)) = file_name.rsplit_once('.') else {
        return String::new();
    };
    match ext.to_ascii_lowercase().as_str() {
        "ts" | "tsx" | "mts" | "cts" | "jsx" => "javascript".into(),
        "jsonc" => "json".into(),
        "mdx" => "markdown".into(),
        "pyi" => "python".into(),
        other => other.to_owned(),
    }
}

/// Highlight a sequence of lines (without trailing newlines) with one continuous
/// `ParseState`, returning line-local token ranges (`0..line.len()`) per line.
pub(crate) fn highlight_lines(info: &str, lines: &[&str]) -> Option<Vec<Vec<SyntaxToken>>> {
    if lines.is_empty() || lines.len() > MAX_DIFF_SYNTAX_LINES {
        return None;
    }
    let total_bytes: usize = lines.iter().map(|line| line.len()).sum();
    if total_bytes > MAX_DIFF_SYNTAX_BYTES {
        return None;
    }
    let language = normalize_language(info)?;
    let syntax_set = syntax_set();
    let syntax = syntax_set.find_syntax_by_token(&language)?;
    let mut parser = ParseState::new(syntax);
    let mut stack = ScopeStack::new();
    let mut per_line = Vec::with_capacity(lines.len());
    let mut scratch = String::new();

    for line in lines {
        if line.len() > MAX_SYNTAX_LINE_BYTES {
            return None;
        }
        // `load_defaults_newlines` expects a trailing `\n` so line-scoped constructs
        // such as `//` or `#` comments terminate cleanly at the end of the line.
        scratch.clear();
        scratch.push_str(line);
        scratch.push('\n');
        let operations = parser.parse_line(&scratch, syntax_set).ok()?;
        let mut tokens = Vec::new();
        let mut offset = 0_usize;
        let limit = line.len();
        for (region, operation) in ScopeRegionIterator::new(&operations, &scratch) {
            stack.apply(operation).ok()?;
            if region.is_empty() || offset >= limit {
                continue;
            }
            let start = offset;
            let end = start.saturating_add(region.len()).min(limit);
            push_token(&mut tokens, start..end, classify_scope(&stack));
            offset = offset.saturating_add(region.len());
        }
        if offset < limit {
            push_token(&mut tokens, offset..limit, SyntaxClass::Plain);
        }
        per_line.push(tokens);
    }
    Some(per_line)
}

/// Highlight a whole source file with one continuous `ParseState`, returning line-local
/// token ranges indexed by 0-based line number (matching `DetailRow::old_line`/`new_line`).
pub(crate) fn highlight_source_lines(info: &str, source: &str) -> Option<Vec<Vec<SyntaxToken>>> {
    if source.is_empty() || source.len() > MAX_DIFF_SYNTAX_BYTES {
        return None;
    }
    let lines: Vec<&str> = source
        .split_inclusive('\n')
        .map(|line| line.strip_suffix('\n').unwrap_or(line))
        .map(|line| line.strip_suffix('\r').unwrap_or(line))
        .collect();
    highlight_lines(info, &lines)
}

fn push_token(tokens: &mut Vec<SyntaxToken>, range: Range<usize>, class: SyntaxClass) {
    if range.is_empty() {
        return;
    }
    if let Some(previous) = tokens
        .last_mut()
        .filter(|previous| previous.class == class && previous.range.end == range.start)
    {
        previous.range.end = range.end;
    } else {
        tokens.push(SyntaxToken { range, class });
    }
}

fn normalize_language(info: &str) -> Option<String> {
    let mut tokens = info.split_ascii_whitespace();
    let token = tokens.next()?;
    if tokens.next().is_some()
        || token.is_empty()
        || token.len() > MAX_LANGUAGE_TOKEN_BYTES
        || !token.is_ascii()
        || !token.bytes().all(|byte| {
            byte.is_ascii_alphanumeric() || matches!(byte, b'_' | b'+' | b'#' | b'.' | b'-')
        })
    {
        return None;
    }
    let lowercase = token.to_ascii_lowercase();
    Some(
        match lowercase.as_str() {
            "rs" => "rust",
            "py" => "python",
            "js" | "mjs" | "cjs" => "javascript",
            "sh" | "shell" | "zsh" => "bash",
            "yml" => "yaml",
            "rb" => "ruby",
            "cpp" | "cxx" => "c++",
            "cs" | "csharp" => "c#",
            "md" => "markdown",
            _ => lowercase.as_str(),
        }
        .to_owned(),
    )
}

fn syntax_set() -> &'static SyntaxSet {
    static SYNTAX_SET: OnceLock<SyntaxSet> = OnceLock::new();
    SYNTAX_SET.get_or_init(SyntaxSet::load_defaults_newlines)
}

struct ScopeClasses {
    comment: Vec<Scope>,
    string: Vec<Scope>,
    number: Vec<Scope>,
    operator: Vec<Scope>,
    keyword: Vec<Scope>,
    r#type: Vec<Scope>,
    function: Vec<Scope>,
    constant: Vec<Scope>,
    variable: Vec<Scope>,
    punctuation: Vec<Scope>,
}

impl ScopeClasses {
    fn new() -> Self {
        Self {
            comment: scopes(&["comment"]),
            string: scopes(&["string"]),
            number: scopes(&["constant.numeric"]),
            operator: scopes(&["keyword.operator"]),
            keyword: scopes(&[
                "keyword",
                "storage.modifier",
                "storage.control",
                "storage.type",
            ]),
            r#type: scopes(&["entity.name.type", "entity.name.class", "support.type"]),
            function: scopes(&["entity.name.function", "support.function"]),
            constant: scopes(&["constant", "support.constant"]),
            variable: scopes(&["variable"]),
            punctuation: scopes(&["punctuation"]),
        }
    }
}

fn scopes(names: &[&str]) -> Vec<Scope> {
    names
        .iter()
        .map(|name| Scope::new(name).expect("static syntax scope must be valid"))
        .collect()
}

fn classify_scope(stack: &ScopeStack) -> SyntaxClass {
    static CLASSES: OnceLock<ScopeClasses> = OnceLock::new();
    let classes = CLASSES.get_or_init(ScopeClasses::new);
    let stack = stack.as_slice();
    if matches_scope(stack, &classes.comment) {
        SyntaxClass::Comment
    } else if matches_scope(stack, &classes.string) {
        SyntaxClass::String
    } else if matches_scope(stack, &classes.number) {
        SyntaxClass::Number
    } else if matches_scope(stack, &classes.operator) {
        SyntaxClass::Operator
    } else if matches_scope(stack, &classes.keyword) {
        SyntaxClass::Keyword
    } else if matches_scope(stack, &classes.r#type) {
        SyntaxClass::Type
    } else if matches_scope(stack, &classes.function) {
        SyntaxClass::Function
    } else if matches_scope(stack, &classes.constant) {
        SyntaxClass::Constant
    } else if matches_scope(stack, &classes.variable) {
        SyntaxClass::Variable
    } else if matches_scope(stack, &classes.punctuation) {
        SyntaxClass::Punctuation
    } else {
        SyntaxClass::Plain
    }
}

fn matches_scope(stack: &[Scope], prefixes: &[Scope]) -> bool {
    stack
        .iter()
        .rev()
        .any(|scope| prefixes.iter().any(|prefix| prefix.is_prefix_of(*scope)))
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::fmt::Write as _;

    #[test]
    fn normalizes_bounded_single_language_tokens() {
        assert_eq!(normalize_language(" rs ").as_deref(), Some("rust"));
        assert_eq!(normalize_language("CXX").as_deref(), Some("c++"));
        assert_eq!(normalize_language("csharp").as_deref(), Some("c#"));
        for alias in [
            "rs", "py", "js", "mjs", "cjs", "sh", "shell", "zsh", "yml", "rb", "cpp", "cxx", "cs",
            "csharp", "md",
        ] {
            let normalized = normalize_language(alias).unwrap();
            assert!(
                syntax_set().find_syntax_by_token(&normalized).is_some(),
                "alias {alias:?} normalized to unavailable syntax {normalized:?}"
            );
        }
        assert!(syntax_set().find_syntax_by_token("typescript").is_none());
        assert_eq!(normalize_language("rust title=demo"), None);
        assert_eq!(normalize_language("rust\u{1b}"), None);
        assert_eq!(
            normalize_language(&"x".repeat(MAX_LANGUAGE_TOKEN_BYTES + 1)),
            None
        );
    }

    #[test]
    fn highlights_recognized_languages_without_changing_text() {
        let source =
            "fn main() {\n    // note\n    let value: i32 = 42;\n    println!(\"{value}\");\n}\n";
        let highlighted = highlight_fence("rust", source).unwrap();
        let rebuilt = highlighted
            .tokens
            .iter()
            .map(|token| &source[token.range.clone()])
            .collect::<String>();
        assert_eq!(rebuilt, source);
        assert!(highlighted.tokens.iter().any(|token| {
            token.class == SyntaxClass::Keyword && &source[token.range.clone()] == "fn"
        }));
        assert!(highlighted.tokens.iter().any(|token| {
            token.class == SyntaxClass::Comment && source[token.range.clone()].contains("// note")
        }));
        assert!(highlighted.tokens.iter().any(|token| {
            token.class == SyntaxClass::Number && &source[token.range.clone()] == "42"
        }));
        assert_eq!(highlighted.work.source_bytes, source.len());
        assert_eq!(highlighted.work.fragments, highlighted.tokens.len());
    }

    #[test]
    fn fails_closed_for_unknown_languages_and_exact_limits() {
        assert_eq!(
            highlight_fence("not-a-real-language", "text")
                .unwrap_err()
                .reason,
            SyntaxFallback::UnknownLanguage
        );
        assert_eq!(
            highlight_fence("rust", &"x".repeat(MAX_SYNTAX_SOURCE_BYTES_PER_FENCE + 1))
                .unwrap_err()
                .reason,
            SyntaxFallback::SourceLimit
        );
        assert_eq!(
            highlight_fence("rust", &"x".repeat(MAX_SYNTAX_LINE_BYTES + 1))
                .unwrap_err()
                .reason,
            SyntaxFallback::LineByteLimit
        );
        let too_many_lines = "\n".repeat(MAX_SYNTAX_LINES_PER_FENCE + 1);
        assert_eq!(
            highlight_fence("rust", &too_many_lines).unwrap_err().reason,
            SyntaxFallback::LineLimit
        );
        let mut dense = String::new();
        for index in 0..400 {
            writeln!(dense, "let value_{index}: i32 = {index};").unwrap();
        }
        assert!(dense.len() < MAX_SYNTAX_SOURCE_BYTES_PER_FENCE);
        let failure = highlight_fence("rust", &dense).unwrap_err();
        assert_eq!(failure.reason, SyntaxFallback::FragmentLimit);
        assert_eq!(failure.work.source_bytes, dense.len());
        assert!(failure.work.lines > 0);
        assert!(failure.work.fragments > MAX_SYNTAX_FRAGMENTS_PER_FENCE);
    }

    #[test]
    fn language_for_path_handles_diff_prefixes_ts_and_extensionless_files() {
        assert_eq!(language_for_path("M src/app.tsx"), "javascript");
        assert_eq!(language_for_path("A web/index.ts"), "javascript");
        assert_eq!(language_for_path("M Makefile"), "makefile");
        assert_eq!(language_for_path("src/retry.py"), "py");
        assert_eq!(language_for_path("README"), "");
        assert!(highlight_lines(&language_for_path("M src/app.tsx"), &["const x = 1;"]).is_some());
        assert!(highlight_lines(&language_for_path("M Makefile"), &["all:\n\techo hi"]).is_some());
    }

    #[test]
    fn highlight_source_lines_preserves_multiline_state_and_closes_line_comments() {
        let source = "/* open\nstill fn comment\n*/\n// line comment\nfn real() {}\n";
        let per_line = highlight_source_lines("rust", source).unwrap();
        assert_eq!(per_line.len(), 5);
        assert!(
            per_line[1]
                .iter()
                .all(|token| token.class == SyntaxClass::Comment),
            "{:?}",
            per_line[1]
        );
        assert!(
            per_line[3]
                .iter()
                .all(|token| token.class == SyntaxClass::Comment),
            "{:?}",
            per_line[3]
        );
        assert!(per_line[4].iter().any(|token| {
            token.class == SyntaxClass::Keyword && &"fn real() {}"[token.range.clone()] == "fn"
        }));
    }
}
