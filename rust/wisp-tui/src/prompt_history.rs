//! Bounded, process-local recall of exact submitted prompts.

const CAPACITY: usize = 100;
const TEXT_BYTES_LIMIT: usize = 4 * 1024 * 1024;
const PREVIEW_CHARS: usize = 160;
const SEARCH_CHARS: usize = 16_384;

pub(crate) struct Entry {
    pub id: u64,
    pub prompt: String,
    pub preview: String,
    search_text: String,
}

impl Entry {
    fn text_bytes(&self) -> usize {
        self.prompt.len() + self.preview.len() + self.search_text.len()
    }
}

#[derive(Default)]
pub(crate) struct PromptHistory {
    entries: Vec<Entry>,
    text_bytes: usize,
    next_id: u64,
}

impl PromptHistory {
    /// Retain an exact prompt once, newest first. Count cached text in the budget too.
    pub fn record(&mut self, prompt: String) -> bool {
        if prompt.len() > TEXT_BYTES_LIMIT {
            return false;
        }
        let normalized = normalized_prefix(&prompt, SEARCH_CHARS);
        if normalized.is_empty() {
            return false;
        }
        let preview = if normalized.chars().count() > PREVIEW_CHARS {
            normalized
                .chars()
                .take(PREVIEW_CHARS - 1)
                .chain(['…'])
                .collect()
        } else {
            normalized.clone()
        };
        let entry = Entry {
            id: self.next_id,
            prompt,
            preview,
            search_text: caseless::default_case_fold_str(&normalized),
        };
        if entry.text_bytes() > TEXT_BYTES_LIMIT {
            return false;
        }
        self.next_id = self
            .next_id
            .checked_add(1)
            .expect("prompt history IDs exhausted");
        if let Some(index) = self
            .entries
            .iter()
            .position(|old| old.prompt == entry.prompt)
        {
            self.text_bytes -= self.entries.remove(index).text_bytes();
        }
        self.text_bytes += entry.text_bytes();
        self.entries.insert(0, entry);
        while self.entries.len() > CAPACITY || self.text_bytes > TEXT_BYTES_LIMIT {
            self.text_bytes -= self
                .entries
                .pop()
                .expect("history exceeds its budget")
                .text_bytes();
        }
        true
    }

    pub fn is_empty(&self) -> bool {
        self.entries.is_empty()
    }

    pub fn entry(&self, id: u64) -> Option<&Entry> {
        self.entries.iter().find(|entry| entry.id == id)
    }

    pub fn search(&self, query: &str) -> Vec<u64> {
        let query = caseless::default_case_fold_str(&normalized_prefix(query, SEARCH_CHARS));
        self.entries
            .iter()
            .filter(|entry| entry.search_text.contains(&query))
            .map(|entry| entry.id)
            .collect()
    }
}

fn normalized_prefix(text: &str, limit: usize) -> String {
    let mut normalized = String::new();
    let mut count = 0;
    let mut pending_space = false;
    for character in text.chars() {
        if character.is_whitespace() {
            pending_space = !normalized.is_empty();
            continue;
        }
        if pending_space {
            normalized.push(' ');
            count += 1;
            if count == limit {
                break;
            }
            pending_space = false;
        }
        normalized.push(character);
        count += 1;
        if count == limit {
            break;
        }
    }
    normalized
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::prompt_editor::MAX_PROMPT_BYTES;

    #[test]
    fn exact_duplicates_move_to_front_and_blank_prompts_are_ignored() {
        let mut history = PromptHistory::default();
        assert!(!history.record(" \n\t".into()));
        for prompt in ["first", "second", "first", " first "] {
            assert!(history.record(prompt.into()));
        }
        let prompts: Vec<_> = history
            .entries
            .iter()
            .map(|entry| entry.prompt.as_str())
            .collect();
        assert_eq!(prompts, [" first ", "first", "second"]);
        assert_eq!(history.entries[1].id, 2);
        assert_eq!(
            history.text_bytes,
            history.entries.iter().map(Entry::text_bytes).sum::<usize>()
        );
    }

    #[test]
    fn oldest_entries_are_evicted_for_both_count_and_retained_text() {
        let mut history = PromptHistory::default();
        for index in 0..=CAPACITY {
            history.record(index.to_string());
        }
        assert_eq!(history.entries.len(), CAPACITY);
        assert_eq!(history.entries.last().unwrap().prompt, "1");
        let mut history = PromptHistory::default();
        for character in ['a', 'b', 'c', 'd'] {
            history.record(character.to_string().repeat(MAX_PROMPT_BYTES));
        }
        assert_eq!(history.entries.len(), 3);
        assert_eq!(history.entries[0].prompt, "d".repeat(MAX_PROMPT_BYTES));
        assert!(history.text_bytes <= TEXT_BYTES_LIMIT);
        assert_eq!(
            history.text_bytes,
            history.entries.iter().map(Entry::text_bytes).sum::<usize>()
        );
        assert!(!history.record("x".repeat(TEXT_BYTES_LIMIT + 1)));
        assert_eq!(history.entries.len(), 3);
    }

    #[test]
    fn search_collapses_whitespace_and_casefolds_without_changing_exact_text() {
        let mut history = PromptHistory::default();
        let prompt = "  Straße\n\tΣςσ [literal] e\u{301} 👩‍💻  ";
        history.record(prompt.into());
        for query in ["STRASSE σσσ", "[literal]", "e\u{301}", "👩‍💻", " \n "] {
            assert_eq!(history.search(query), [0]);
        }
        assert!(history.search(".*").is_empty());
        assert_eq!(history.entry(0).unwrap().prompt, prompt);
        assert_eq!(
            history.entry(0).unwrap().preview,
            "Straße Σςσ [literal] e\u{301} 👩‍💻"
        );
    }

    #[test]
    fn preview_and_search_prefix_are_bounded_by_characters() {
        let mut history = PromptHistory::default();
        let prompt = format!("{} hidden suffix", "界".repeat(SEARCH_CHARS));
        history.record(prompt.clone());
        let entry = history.entry(0).unwrap();
        assert_eq!(entry.preview.chars().count(), PREVIEW_CHARS);
        assert!(entry.preview.ends_with('…'));
        assert_eq!(entry.search_text.chars().count(), SEARCH_CHARS);
        assert!(history.search("hidden").is_empty());
        assert_eq!(entry.prompt, prompt);
    }
}
