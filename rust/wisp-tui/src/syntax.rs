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
}
