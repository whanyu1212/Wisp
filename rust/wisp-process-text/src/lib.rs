//! Bounded retention for incrementally decoded process output.

#![forbid(unsafe_code)]

use std::borrow::Cow;
use std::collections::VecDeque;

const REPLACEMENT_CHARACTER: char = '\u{fffd}';

/// One snapshot removed from [`PendingText`].
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct Drain {
    /// Retained decoded text.
    pub text: String,
    /// Original input bytes discarded by the configured bounds since the last drain.
    pub dropped_bytes: usize,
    /// Original input bytes represented by `text`.
    pub retained_source_bytes: usize,
    /// Original input-byte count represented by each character in `text`.
    pub source_byte_lengths: Vec<usize>,
}

#[derive(Debug)]
struct Fragment {
    text: String,
    source_byte_lengths: VecDeque<usize>,
    encoded_byte_lengths: VecDeque<usize>,
    source_bytes: usize,
    encoded_bytes: usize,
}

#[derive(Debug, Default)]
struct Line {
    fragments: VecDeque<Fragment>,
    complete: bool,
}

#[derive(Clone, Copy, Debug)]
struct DecodedUnit {
    character: char,
    source_bytes: usize,
}

/// Incrementally decodes and retains the bounded tail of process output.
#[derive(Debug)]
pub struct PendingText {
    max_bytes: usize,
    max_lines: usize,
    dropped_bytes: usize,
    lines: VecDeque<Line>,
    retained_source_bytes: usize,
    retained_encoded_bytes: usize,
    pending_utf8: Vec<u8>,
    pending_carriage_return: bool,
}

impl PendingText {
    /// Creates an empty buffer with encoded-output byte and logical-line limits.
    #[must_use]
    pub fn new(max_bytes: usize, max_lines: usize) -> Self {
        Self {
            max_bytes,
            max_lines,
            dropped_bytes: 0,
            lines: VecDeque::new(),
            retained_source_bytes: 0,
            retained_encoded_bytes: 0,
            pending_utf8: Vec::new(),
            pending_carriage_return: false,
        }
    }

    /// Appends raw process bytes, preserving an incomplete UTF-8 suffix unless `final` is true.
    pub fn append_bytes(&mut self, value: &[u8], final_chunk: bool) {
        let data = if self.pending_utf8.is_empty() {
            Cow::Borrowed(value)
        } else {
            let mut combined = std::mem::take(&mut self.pending_utf8);
            combined.extend_from_slice(value);
            Cow::Owned(combined)
        };

        if data.is_ascii() {
            self.pending_utf8.clear();
            self.append_units(data.iter().map(|byte| DecodedUnit {
                character: char::from(*byte),
                source_bytes: 1,
            }));
            return;
        }

        let (units, pending) = decode_utf8_units(&data, final_chunk);
        self.pending_utf8 = pending;
        self.append_units(units);
    }

    /// Appends valid Unicode text without consuming a pending raw UTF-8 suffix.
    pub fn append(&mut self, value: &str) {
        self.append_units(value.chars().map(|character| DecodedUnit {
            source_bytes: character.len_utf8(),
            character,
        }));
    }

    /// Returns whether decoded text is currently retained.
    #[must_use]
    pub fn has_text(&self) -> bool {
        !self.lines.is_empty()
    }

    /// Returns the original input bytes represented by the retained decoded text.
    #[must_use]
    pub fn retained_source_bytes(&self) -> usize {
        self.retained_source_bytes
    }

    /// Returns original input bytes discarded since the last drain.
    #[must_use]
    pub fn dropped_bytes(&self) -> usize {
        self.dropped_bytes
    }

    /// Returns the currently retained decoded text.
    #[must_use]
    pub fn text(&self) -> String {
        let mut text = String::with_capacity(self.retained_encoded_bytes);
        for line in &self.lines {
            for fragment in &line.fragments {
                text.push_str(&fragment.text);
            }
        }
        text
    }

    /// Removes retained text and accounting while preserving an incomplete UTF-8 suffix.
    pub fn drain(&mut self) -> Drain {
        let text = self.text();
        let mut source_byte_lengths = Vec::new();
        for line in &self.lines {
            for fragment in &line.fragments {
                source_byte_lengths.extend(fragment.source_byte_lengths.iter().copied());
            }
        }

        let drain = Drain {
            text,
            dropped_bytes: self.dropped_bytes,
            retained_source_bytes: self.retained_source_bytes,
            source_byte_lengths,
        };
        self.dropped_bytes = 0;
        self.lines.clear();
        self.retained_source_bytes = 0;
        self.retained_encoded_bytes = 0;
        self.pending_carriage_return = false;
        drain
    }

    fn append_units(&mut self, units: impl IntoIterator<Item = DecodedUnit>) {
        let mut text = String::new();
        let mut source_byte_lengths = VecDeque::new();
        let mut encoded_byte_lengths = VecDeque::new();
        let mut appended = false;

        for unit in units {
            appended = true;
            if self.pending_carriage_return {
                if unit.character == '\n' {
                    push_unit(
                        &mut text,
                        &mut source_byte_lengths,
                        &mut encoded_byte_lengths,
                        unit,
                    );
                    self.flush_fragment(
                        &mut text,
                        &mut source_byte_lengths,
                        &mut encoded_byte_lengths,
                    );
                    self.finish_current_line();
                    continue;
                }
                self.flush_fragment(
                    &mut text,
                    &mut source_byte_lengths,
                    &mut encoded_byte_lengths,
                );
                self.finish_current_line();
            }

            push_unit(
                &mut text,
                &mut source_byte_lengths,
                &mut encoded_byte_lengths,
                unit,
            );
            if unit.character == '\r' {
                self.pending_carriage_return = true;
            } else if is_line_break(unit.character) {
                self.flush_fragment(
                    &mut text,
                    &mut source_byte_lengths,
                    &mut encoded_byte_lengths,
                );
                self.finish_current_line();
            }
        }

        if appended {
            self.flush_fragment(
                &mut text,
                &mut source_byte_lengths,
                &mut encoded_byte_lengths,
            );
            self.trim();
        }
    }

    fn flush_fragment(
        &mut self,
        text: &mut String,
        source_byte_lengths: &mut VecDeque<usize>,
        encoded_byte_lengths: &mut VecDeque<usize>,
    ) {
        if text.is_empty() {
            return;
        }
        let source_bytes = source_byte_lengths.iter().sum();
        let encoded_bytes = encoded_byte_lengths.iter().sum();
        let fragment = Fragment {
            text: std::mem::take(text),
            source_byte_lengths: std::mem::take(source_byte_lengths),
            encoded_byte_lengths: std::mem::take(encoded_byte_lengths),
            source_bytes,
            encoded_bytes,
        };
        self.current_line().fragments.push_back(fragment);
        self.retained_source_bytes += source_bytes;
        self.retained_encoded_bytes += encoded_bytes;
    }

    fn current_line(&mut self) -> &mut Line {
        if self.lines.back().is_none_or(|line| line.complete) {
            self.lines.push_back(Line::default());
        }
        self.lines.back_mut().expect("a current line was inserted")
    }

    fn finish_current_line(&mut self) {
        if let Some(line) = self.lines.back_mut() {
            line.complete = true;
        }
        self.pending_carriage_return = false;
    }

    fn trim(&mut self) {
        if self.max_bytes == 0 || self.max_lines == 0 {
            self.drop_all();
            return;
        }
        while self.lines.len() > self.max_lines {
            self.drop_line();
        }
        while self.retained_encoded_bytes > self.max_bytes && !self.lines.is_empty() {
            let excess_bytes = self.retained_encoded_bytes - self.max_bytes;
            let should_drop_fragment = self
                .lines
                .front()
                .and_then(|line| line.fragments.front())
                .is_some_and(|fragment| fragment.encoded_bytes <= excess_bytes);
            if should_drop_fragment {
                let fragment = self
                    .lines
                    .front_mut()
                    .and_then(|line| line.fragments.pop_front())
                    .expect("the front fragment exists");
                self.drop_fragment(fragment);
            } else {
                self.trim_front_fragment(excess_bytes);
            }
            if self
                .lines
                .front()
                .is_some_and(|line| line.fragments.is_empty())
            {
                self.lines.pop_front();
            }
        }
        if self.lines.back().is_none_or(|line| line.complete) {
            self.pending_carriage_return = false;
        }
    }

    fn drop_all(&mut self) {
        while !self.lines.is_empty() {
            self.drop_line();
        }
        self.pending_carriage_return = false;
    }

    fn drop_line(&mut self) {
        if let Some(mut line) = self.lines.pop_front() {
            while let Some(fragment) = line.fragments.pop_front() {
                self.drop_fragment(fragment);
            }
        }
    }

    fn drop_fragment(&mut self, fragment: Fragment) {
        self.retained_source_bytes -= fragment.source_bytes;
        self.retained_encoded_bytes -= fragment.encoded_bytes;
        self.dropped_bytes += fragment.source_bytes;
    }

    fn trim_front_fragment(&mut self, excess_encoded_bytes: usize) {
        let fragment = self
            .lines
            .front_mut()
            .and_then(|line| line.fragments.front_mut())
            .expect("the front fragment exists");
        let mut removed_encoded_bytes = 0;
        let mut removed_source_bytes = 0;
        let mut removed_characters = 0;
        while removed_encoded_bytes < excess_encoded_bytes {
            let source_bytes = fragment
                .source_byte_lengths
                .pop_front()
                .expect("source lengths match fragment text");
            let encoded_bytes = fragment
                .encoded_byte_lengths
                .pop_front()
                .expect("encoded lengths match fragment text");
            removed_source_bytes += source_bytes;
            removed_encoded_bytes += encoded_bytes;
            removed_characters += 1;
        }
        let split_at = fragment
            .text
            .char_indices()
            .nth(removed_characters)
            .map_or(fragment.text.len(), |(index, _)| index);
        fragment.text.drain(..split_at);
        fragment.source_bytes -= removed_source_bytes;
        fragment.encoded_bytes -= removed_encoded_bytes;
        self.retained_source_bytes -= removed_source_bytes;
        self.retained_encoded_bytes -= removed_encoded_bytes;
        self.dropped_bytes += removed_source_bytes;
    }
}

fn push_unit(
    text: &mut String,
    source_byte_lengths: &mut VecDeque<usize>,
    encoded_byte_lengths: &mut VecDeque<usize>,
    unit: DecodedUnit,
) {
    text.push(unit.character);
    source_byte_lengths.push_back(unit.source_bytes);
    encoded_byte_lengths.push_back(unit.character.len_utf8());
}

fn is_line_break(character: char) -> bool {
    matches!(
        character,
        '\n' | '\u{000b}'
            | '\u{000c}'
            | '\u{001c}'
            | '\u{001d}'
            | '\u{001e}'
            | '\u{0085}'
            | '\u{2028}'
            | '\u{2029}'
    )
}

fn decode_utf8_units(data: &[u8], final_chunk: bool) -> (Vec<DecodedUnit>, Vec<u8>) {
    let mut units = Vec::with_capacity(data.len());
    let mut index = 0;
    while index < data.len() {
        let first = data[index];
        if first.is_ascii() {
            units.push(DecodedUnit {
                character: char::from(first),
                source_bytes: 1,
            });
            index += 1;
            continue;
        }

        let Some(expected) = utf8_sequence_length(first) else {
            units.push(DecodedUnit {
                character: REPLACEMENT_CHARACTER,
                source_bytes: 1,
            });
            index += 1;
            continue;
        };

        if index + expected > data.len() {
            let suffix = &data[index..];
            match std::str::from_utf8(suffix) {
                Ok(text) if final_chunk => {
                    let character = text.chars().next().expect("non-ASCII suffix is not empty");
                    units.push(DecodedUnit {
                        character,
                        source_bytes: suffix.len(),
                    });
                    return (units, Vec::new());
                }
                Ok(_) => return (units, suffix.to_vec()),
                Err(error) if !final_chunk && error.error_len().is_none() => {
                    return (units, suffix.to_vec());
                }
                Err(error) => {
                    let invalid_bytes = error.error_len().unwrap_or(suffix.len()).max(1);
                    units.push(DecodedUnit {
                        character: REPLACEMENT_CHARACTER,
                        source_bytes: invalid_bytes,
                    });
                    index += invalid_bytes;
                    continue;
                }
            }
        }

        let sequence = &data[index..index + expected];
        match std::str::from_utf8(sequence) {
            Ok(text) => {
                units.push(DecodedUnit {
                    character: text.chars().next().expect("UTF-8 sequence is not empty"),
                    source_bytes: expected,
                });
                index += expected;
            }
            Err(error) => {
                let invalid_bytes = error.error_len().unwrap_or(1).max(1);
                units.push(DecodedUnit {
                    character: REPLACEMENT_CHARACTER,
                    source_bytes: invalid_bytes,
                });
                index += invalid_bytes;
            }
        }
    }
    (units, Vec::new())
}

fn utf8_sequence_length(first: u8) -> Option<usize> {
    match first {
        0xc2..=0xdf => Some(2),
        0xe0..=0xef => Some(3),
        0xf0..=0xf4 => Some(4),
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::PendingText;

    #[test]
    fn reports_retained_state_before_and_after_drain() {
        let mut pending = PendingText::new(100, 10);
        assert!(!pending.has_text());
        assert_eq!(pending.retained_source_bytes(), 0);

        pending.append_bytes("a🙂".as_bytes(), false);
        assert!(pending.has_text());
        assert_eq!(pending.retained_source_bytes(), 5);

        let drain = pending.drain();
        assert_eq!(drain.text, "a🙂");
        assert_eq!(drain.source_byte_lengths, [1, 4]);
        assert!(!pending.has_text());
        assert_eq!(pending.retained_source_bytes(), 0);
    }
}
