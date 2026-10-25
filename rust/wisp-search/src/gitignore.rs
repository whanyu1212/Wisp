//! Gitignore matching with the semantics of Python's `pathspec.GitIgnoreSpec`.
//!
//! The Python walk decides what is ignored with pathspec 1.1.1, so this ports its
//! pattern translation and match order rather than Git's wildmatch.
//! `tests/fixtures/gitignore_conformance.json` records pathspec's verdicts, and
//! `tests/gitignore_conformance.rs` checks this module reaches the same ones.
//!
//! Python decodes names and ignore files as UTF-8 with `surrogateescape`, so each
//! byte that is not valid UTF-8 becomes one lone surrogate (U+DC80 to U+DCFF).
//! Paths here are WTF-8: UTF-8 that may also hold those surrogates, encoded as
//! `ED B2 xx` or `ED B3 xx`. A pattern's `?` then matches one undecodable byte, as
//! in Python, and WTF-8 byte order is Python's string order.

use regex::bytes::Regex;

/// The rules of one ignore file, in file order.
#[derive(Debug)]
pub struct GitIgnore {
    rules: Vec<Rule>,
}

#[derive(Debug)]
struct Rule {
    /// The line as written, including its line ending.
    source: Vec<u32>,
    /// `None` for blank lines, comments and lines pathspec cannot translate.
    matcher: Option<Matcher>,
}

#[derive(Debug)]
struct Matcher {
    regex: Regex,
    ignored: bool,
    has_directory_mark: bool,
}

/// The rule that decided whether a path is ignored.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct IgnoreMatch {
    /// `true` for an ignoring rule, `false` for a `!` re-inclusion.
    pub ignored: bool,
    /// Zero-based line of the deciding rule.
    pub index: usize,
}

/// An ignore file has more lines than the caller allows.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub struct PatternLimitExceeded;

impl std::fmt::Display for PatternLimitExceeded {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        formatter.write_str("ignore file exceeds the pattern limit")
    }
}

impl std::error::Error for PatternLimitExceeded {}

impl GitIgnore {
    /// Compiles an ignore file's raw content.
    ///
    /// # Errors
    ///
    /// Returns [`PatternLimitExceeded`] when the content has more than
    /// `max_patterns` lines.
    pub fn parse(content: &[u8], max_patterns: usize) -> Result<Self, PatternLimitExceeded> {
        let lines = split_lines(&decode_surrogateescape(content));
        if lines.len() > max_patterns {
            return Err(PatternLimitExceeded);
        }
        let rules = lines
            .into_iter()
            .map(|source| Rule {
                matcher: compile_pattern(&source),
                source,
            })
            .collect();
        Ok(Self { rules })
    }

    /// Returns the rule that decides `path`, a WTF-8 path relative to the file's
    /// directory with a trailing `/` for a directory.
    ///
    /// The last matching rule wins, except that a rule matching only because the
    /// path is inside a matched directory loses to any rule matching the path
    /// itself.
    #[must_use]
    pub fn check(&self, path: &[u8]) -> Option<IgnoreMatch> {
        let mut directory_match = None;
        for (index, rule) in self.rules.iter().enumerate().rev() {
            let Some(matcher) = &rule.matcher else {
                continue;
            };
            let Some(through_directory) = matcher.find(path) else {
                continue;
            };
            let found = IgnoreMatch {
                ignored: matcher.ignored,
                index,
            };
            if !through_directory {
                return Some(found);
            }
            directory_match.get_or_insert(found);
        }
        directory_match
    }
}

impl Matcher {
    /// Returns whether the pattern matches `path`, and if so whether only through
    /// pathspec's directory marker.
    fn find(&self, path: &[u8]) -> Option<bool> {
        if !self.has_directory_mark {
            return self.regex.is_match(path).then_some(false);
        }
        let captures = self.regex.captures(path)?;
        Some(captures.name(DIRECTORY_MARK).is_some())
    }
}

/// Returns whether a `!` rule could make an entry below an ignored directory
/// visible again, so the walk must still descend into it.
///
/// Ports `_may_reinclude_descendant` from `wisp.tools.search.tools`. `rules` holds
/// each applicable ignore file in priority order, with the directory's WTF-8 path
/// relative to that file's directory, ending in `/`.
#[must_use]
pub fn may_reinclude_descendant(rules: &[(&GitIgnore, &[u8])]) -> bool {
    let mut ignored = false;
    let mut exclusion_allows_reinclude = false;
    for &(gitignore, relative) in rules {
        let Some(found) = gitignore.check(relative) else {
            continue;
        };
        ignored = found.ignored;
        exclusion_allows_reinclude = false;
        if !ignored {
            continue;
        }
        let source = rstrip(&gitignore.rules[found.index].source);
        if let Some(root) = source.strip_suffix(&[SLASH, STAR, STAR][..]) {
            let root = root.strip_prefix(&[SLASH][..]).unwrap_or(root);
            exclusion_allows_reinclude = matches_exact(relative, &directory_pattern(root));
        }
    }
    if !ignored || !exclusion_allows_reinclude {
        return false;
    }

    for &(gitignore, relative) in rules {
        for rule in &gitignore.rules {
            let is_negation = rule
                .matcher
                .as_ref()
                .is_some_and(|matcher| !matcher.ignored);
            if !is_negation {
                continue;
            }
            let Some(negated) = rule.source.strip_prefix(&[BANG][..]) else {
                continue;
            };
            let negated = negated.strip_prefix(&[SLASH][..]).unwrap_or(negated);
            let components: Vec<&[u32]> = negated.split(|&c| c == SLASH).collect();
            if components.len() == 1 {
                return true;
            }
            for end in 1..components.len() {
                let leading = components[..end].join(&SLASH);
                if matches_exact(relative, &directory_pattern(&leading)) {
                    return true;
                }
            }
        }
    }
    false
}

/// Converts a raw name or path to WTF-8, as Python decodes it with `surrogateescape`.
#[must_use]
pub fn wtf8_from_raw(raw: &[u8]) -> Vec<u8> {
    let mut wtf8 = Vec::with_capacity(raw.len());
    for chunk in raw.utf8_chunks() {
        wtf8.extend_from_slice(chunk.valid().as_bytes());
        for &byte in chunk.invalid() {
            wtf8.extend_from_slice(&surrogate_wtf8(byte));
        }
    }
    wtf8
}

const SLASH: u32 = '/' as u32;
const STAR: u32 = '*' as u32;
const BANG: u32 = '!' as u32;
const BACKSLASH: u32 = '\\' as u32;
const DIRECTORY_MARK: &str = "ps_d";
const SURROGATE_ESCAPE_FIRST: u32 = 0xDC80;
const SURROGATE_ESCAPE_LAST: u32 = 0xDCFF;
/// Python's `.`: any code point except a newline.
const ANY_BUT_NEWLINE: &str = r"(?:.|(?-u:\xED[\xB2\xB3][\x80-\xBF]))";
/// Python's `[^/]`: any code point except a slash.
const ANY_BUT_SLASH: &str = r"(?:[^/]|(?-u:\xED[\xB2\xB3][\x80-\xBF]))";
const DIRECTORY_MARK_GROUP: &str = "(?P<ps_d>/)";
/// Python's `$` matches at the end or before a final newline. Consuming that
/// newline changes only where the match ends, which is read only for directory
/// patterns, and those end in the directory mark instead.
const DIRECTORY_MARK_OR_END: &str = r"(?:(?P<ps_d>/)|\n?\z)";

fn surrogate_wtf8(byte: u8) -> [u8; 3] {
    // U+DC80..U+DCFF in UTF-8's three-byte form.
    [0xED, 0xB0 | (byte >> 6), 0x80 | (byte & 0x3F)]
}

fn decode_surrogateescape(raw: &[u8]) -> Vec<u32> {
    let mut code_points = Vec::with_capacity(raw.len());
    for chunk in raw.utf8_chunks() {
        code_points.extend(chunk.valid().chars().map(u32::from));
        code_points.extend(chunk.invalid().iter().map(|&byte| 0xDC00 + u32::from(byte)));
    }
    code_points
}

/// Python's `str.splitlines(keepends=True)`.
fn split_lines(text: &[u32]) -> Vec<Vec<u32>> {
    let mut lines = Vec::new();
    let mut start = 0;
    let mut index = 0;
    while index < text.len() {
        let code_point = text[index];
        index += 1;
        if !is_line_boundary(code_point) {
            continue;
        }
        if code_point == u32::from('\r') && text.get(index) == Some(&u32::from('\n')) {
            index += 1;
        }
        lines.push(text[start..index].to_vec());
        start = index;
    }
    if start < text.len() {
        lines.push(text[start..].to_vec());
    }
    lines
}

fn is_line_boundary(code_point: u32) -> bool {
    matches!(
        code_point,
        0x0A..=0x0D | 0x1C..=0x1E | 0x85 | 0x2028 | 0x2029
    )
}

/// Python's `str.isspace`.
fn is_python_space(code_point: u32) -> bool {
    matches!(
        code_point,
        0x09..=0x0D
            | 0x1C..=0x20
            | 0x85
            | 0xA0
            | 0x1680
            | 0x2000..=0x200A
            | 0x2028
            | 0x2029
            | 0x202F
            | 0x205F
            | 0x3000
    )
}

fn rstrip(text: &[u32]) -> &[u32] {
    let end = text
        .iter()
        .rposition(|&c| !is_python_space(c))
        .map_or(0, |last| last + 1);
    &text[..end]
}

fn directory_pattern(inner: &[u32]) -> Vec<u32> {
    let mut pattern = Vec::with_capacity(inner.len() + 2);
    pattern.push(SLASH);
    pattern.extend_from_slice(inner);
    pattern.push(SLASH);
    pattern
}

/// Ports `_gitignore_glob_matches_exact`: whether some match of the single
/// pattern ends exactly at the end of `path`.
fn matches_exact(path: &[u8], pattern: &[u32]) -> bool {
    compile_pattern(pattern).is_some_and(|matcher| {
        matcher
            .regex
            .find_iter(path)
            .any(|found| found.end() == path.len())
    })
}

fn compile_pattern(line: &[u32]) -> Option<Matcher> {
    let translation = translate_pattern(line)?;
    // pathspec would compile any translation; only a pattern too large for the
    // regex crate's limits fails here, and it is treated as a blank line.
    let regex = Regex::new(&translation.regex).ok()?;
    Some(Matcher {
        regex,
        ignored: translation.ignored,
        has_directory_mark: translation.regex.contains(DIRECTORY_MARK_GROUP),
    })
}

struct Translation {
    regex: String,
    ignored: bool,
}

/// Ports `GitIgnoreSpecPattern.pattern_to_regex`. Returns `None` for a line that
/// matches nothing: blank, a comment, or one pathspec rejects or discards.
fn translate_pattern(line: &[u32]) -> Option<Translation> {
    let mut pattern = line;
    if !pattern.ends_with(&[BACKSLASH, u32::from(' ')]) {
        pattern = rstrip(pattern);
    }
    if pattern.is_empty() || pattern[0] == u32::from('#') || pattern == [SLASH] {
        return None;
    }
    let ignored = pattern[0] != BANG;
    if !ignored {
        pattern = &pattern[1..];
    }

    let mut segments: Vec<&[u32]> = pattern.split(|&c| c == SLASH).collect();
    let is_directory_pattern = segments.last().is_some_and(|last| last.is_empty());
    let regex = match normalize_segments(is_directory_pattern, &mut segments)? {
        Some(special) => special.to_owned(),
        None => translate_segments(is_directory_pattern, &segments)?,
    };
    Some(Translation { regex, ignored })
}

const DOUBLE_STAR: &[u32] = &[STAR, STAR];
const SINGLE_STAR: &[u32] = &[STAR];

/// Ports `GitIgnoreSpecPattern.__normalize_segments`. Returns `None` for an
/// invalid pattern, or the regex for a pattern pathspec special-cases.
fn normalize_segments(
    is_directory_pattern: bool,
    segments: &mut Vec<&[u32]>,
) -> Option<Option<&'static str>> {
    if segments[0].is_empty() {
        segments.remove(0);
    } else if (segments.len() == 1 || (segments.len() == 2 && segments[1].is_empty()))
        && segments[0] != DOUBLE_STAR
    {
        segments.insert(0, DOUBLE_STAR);
    }
    let last = segments.last_mut()?;
    if last.is_empty() {
        *last = DOUBLE_STAR;
    }
    for index in (1..segments.len()).rev() {
        if segments[index - 1] == DOUBLE_STAR && segments[index] == DOUBLE_STAR {
            segments.remove(index);
        }
    }

    let special = match segments.as_slice() {
        [DOUBLE_STAR] if is_directory_pattern => Some(DIRECTORY_MARK_GROUP),
        [DOUBLE_STAR] | [DOUBLE_STAR, SINGLE_STAR] => Some(ANY_BUT_NEWLINE),
        [DOUBLE_STAR, SINGLE_STAR, DOUBLE_STAR] if is_directory_pattern => {
            Some(DIRECTORY_MARK_GROUP)
        }
        [DOUBLE_STAR, SINGLE_STAR, DOUBLE_STAR] => Some("/"),
        _ => None,
    };
    Some(special)
}

/// Ports `GitIgnoreSpecPattern.__translate_segments`.
fn translate_segments(is_directory_pattern: bool, segments: &[&[u32]]) -> Option<String> {
    let mut regex = String::new();
    let mut need_slash = false;
    let end = segments.len() - 1;
    for (index, &segment) in segments.iter().enumerate() {
        if segment == DOUBLE_STAR {
            if index == 0 {
                regex.push_str("^(?:");
                regex.push_str(ANY_BUT_NEWLINE);
                regex.push_str("+/)?");
            } else if index < end {
                regex.push_str("(?:/");
                regex.push_str(ANY_BUT_NEWLINE);
                regex.push_str("+)?");
                need_slash = true;
            } else if is_directory_pattern {
                regex.push_str(DIRECTORY_MARK_GROUP);
            } else {
                regex.push('/');
            }
            continue;
        }
        if index == 0 {
            regex.push('^');
        }
        if need_slash {
            regex.push('/');
        }
        if segment == SINGLE_STAR {
            regex.push_str(ANY_BUT_SLASH);
            regex.push('+');
        } else {
            translate_segment_glob(segment, &mut regex)?;
        }
        if index == end {
            regex.push_str(DIRECTORY_MARK_OR_END);
        }
        need_slash = true;
    }
    Some(regex)
}

/// Ports `_translate_segment_glob` with `range_error="raise"`. Returns `None`
/// when pathspec would discard or reject the pattern: an unclosed bracket, a
/// trailing backslash, or a range Python's `re` rejects.
fn translate_segment_glob(segment: &[u32], regex: &mut String) -> Option<()> {
    let end = segment.len();
    let mut escape = false;
    let mut index = 0;
    while index < end {
        let code_point = segment[index];
        index += 1;
        if escape {
            escape = false;
            push_literal(code_point, regex);
        } else if code_point == BACKSLASH {
            escape = true;
        } else if code_point == STAR {
            regex.push_str(ANY_BUT_SLASH);
            regex.push('*');
        } else if code_point == u32::from('?') {
            regex.push_str(ANY_BUT_SLASH);
        } else if code_point == u32::from('[') {
            let mut closing = index;
            if closing < end && is_bracket_negation(segment[closing]) {
                closing += 1;
            }
            if closing < end && segment[closing] == u32::from(']') {
                closing += 1;
            }
            while closing < end && segment[closing] != u32::from(']') {
                closing += 1;
            }
            if closing == end {
                return None;
            }
            let negate = is_bracket_negation(segment[index]);
            if negate {
                index += 1;
            }
            push_bracket_class(&segment[index..closing], negate, regex)?;
            index = closing + 1;
        } else {
            push_literal(code_point, regex);
        }
    }
    (!escape).then_some(())
}

fn is_bracket_negation(code_point: u32) -> bool {
    code_point == BANG || code_point == u32::from('^')
}

/// Emits the class Python's `re` builds from pathspec's bracket expression.
///
/// pathspec copies the bracket's contents into the regex with each backslash
/// doubled, so Python reads every character literally except `-` between two
/// characters, which forms a range. Rust's class syntax differs (nested classes,
/// `&&`, `--`, POSIX classes), so the set is rebuilt from code point ranges.
fn push_bracket_class(contents: &[u32], negate: bool, regex: &mut String) -> Option<()> {
    let mut ranges = Vec::new();
    let mut index = 0;
    while index < contents.len() {
        let low = contents[index];
        let is_range =
            contents.get(index + 1) == Some(&u32::from('-')) && index + 2 < contents.len();
        if is_range {
            let high = contents[index + 2];
            if high < low {
                return None;
            }
            ranges.push((low, high));
            index += 3;
        } else {
            ranges.push((low, low));
            index += 1;
        }
    }
    let mut ranges = merge_ranges(ranges);
    if negate {
        ranges = complement_ranges(&ranges);
    }
    push_code_point_set(&ranges, regex);
    Some(())
}

fn merge_ranges(mut ranges: Vec<(u32, u32)>) -> Vec<(u32, u32)> {
    ranges.sort_unstable();
    let mut merged: Vec<(u32, u32)> = Vec::with_capacity(ranges.len());
    for (low, high) in ranges {
        match merged.last_mut() {
            Some(last) if low <= last.1.saturating_add(1) => last.1 = last.1.max(high),
            _ => merged.push((low, high)),
        }
    }
    merged
}

fn complement_ranges(ranges: &[(u32, u32)]) -> Vec<(u32, u32)> {
    let mut complement = Vec::with_capacity(ranges.len() + 1);
    let mut next = 0;
    for &(low, high) in ranges {
        if low > next {
            complement.push((next, low - 1));
        }
        next = high + 1;
    }
    if next <= u32::from(char::MAX) {
        complement.push((next, u32::from(char::MAX)));
    }
    complement
}

/// Emits a regex matching one code point from `ranges`. Surrogates other than
/// U+DC80 to U+DCFF never occur in a decoded name, so they are dropped.
fn push_code_point_set(ranges: &[(u32, u32)], regex: &mut String) {
    let mut alternatives = Vec::new();
    let mut class = String::new();
    for &(low, high) in ranges {
        for (low, high) in [(low, high.min(0xD7FF)), (low.max(0xE000), high)] {
            if low <= high {
                class.push_str(&format!(r"\x{{{low:X}}}-\x{{{high:X}}}"));
            }
        }
    }
    if !class.is_empty() {
        alternatives.push(format!("[{class}]"));
    }
    for &(low, high) in ranges {
        let low = low.max(SURROGATE_ESCAPE_FIRST);
        let high = high.min(SURROGATE_ESCAPE_LAST);
        // Split at U+DCC0, where the WTF-8 middle byte changes from B2 to B3.
        for (low, high) in [(low, high.min(0xDCBF)), (low.max(0xDCC0), high)] {
            if low <= high {
                let [first, second, low_last] = surrogate_wtf8((low - 0xDC00) as u8);
                let [_, _, high_last] = surrogate_wtf8((high - 0xDC00) as u8);
                alternatives.push(format!(
                    r"(?-u:\x{first:02X}\x{second:02X}[\x{low_last:02X}-\x{high_last:02X}])"
                ));
            }
        }
    }
    match alternatives.as_slice() {
        [] => regex.push_str(r"(?-u:[^\x00-\xFF])"),
        [only] => regex.push_str(only),
        _ => {
            regex.push_str("(?:");
            regex.push_str(&alternatives.join("|"));
            regex.push(')');
        }
    }
}

fn push_literal(code_point: u32, regex: &mut String) {
    match char::from_u32(code_point) {
        Some(character) => regex.push_str(&regex::escape(character.encode_utf8(&mut [0; 4]))),
        None => {
            let [first, second, third] = surrogate_wtf8((code_point - 0xDC00) as u8);
            regex.push_str(&format!(r"(?-u:\x{first:02X}\x{second:02X}\x{third:02X})"));
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn rules(content: &str) -> GitIgnore {
        GitIgnore::parse(content.as_bytes(), usize::MAX).expect("no pattern limit")
    }

    #[test]
    fn last_matching_rule_wins() {
        let gitignore = rules("*.log\n!keep.log\n");

        assert_eq!(
            gitignore.check(b"a/keep.log"),
            Some(IgnoreMatch {
                ignored: false,
                index: 1
            })
        );
        assert_eq!(gitignore.check(b"a/x.txt"), None);
    }

    #[test]
    fn file_rule_beats_directory_rule() {
        let gitignore = rules("!build/keep.txt\nbuild/\n");

        assert_eq!(
            gitignore.check(b"build/keep.txt"),
            Some(IgnoreMatch {
                ignored: false,
                index: 0
            })
        );
    }

    #[test]
    fn rejects_too_many_lines() {
        assert_eq!(
            GitIgnore::parse(b"a\nb\nc\n", 2).err(),
            Some(PatternLimitExceeded)
        );
    }

    #[test]
    fn undecodable_byte_is_one_character() {
        let gitignore = rules("a?b\n");

        assert!(gitignore.check(&wtf8_from_raw(b"a\x80b")).is_some());
        assert!(gitignore.check(&wtf8_from_raw("aéb".as_bytes())).is_some());
        assert!(gitignore.check(&wtf8_from_raw(b"a\xC3b")).is_some());
        assert!(gitignore.check(&wtf8_from_raw(b"a\x80\x80b")).is_none());
    }

    #[test]
    fn wtf8_sorts_like_python() {
        // Python sorts the decoded U+DC80 below U+E000 and above U+00E9.
        let mut names = [
            wtf8_from_raw("\u{E000}".as_bytes()),
            wtf8_from_raw(b"\x80"),
            wtf8_from_raw("é".as_bytes()),
        ];
        names.sort();

        assert_eq!(
            names,
            [
                wtf8_from_raw("é".as_bytes()),
                wtf8_from_raw(b"\x80"),
                wtf8_from_raw("\u{E000}".as_bytes()),
            ]
        );
    }
}
