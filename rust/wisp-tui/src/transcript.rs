//! Terminal-independent transcript state for the native TUI.

use std::ops::{Deref, DerefMut};
use std::sync::Arc;

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct SharedTranscript(Arc<Transcript>);

impl SharedTranscript {
    #[cfg(test)]
    fn shares_storage_with(&self, other: &Self) -> bool {
        Arc::ptr_eq(&self.0, &other.0)
    }
}

impl Deref for SharedTranscript {
    type Target = Transcript;

    fn deref(&self) -> &Self::Target {
        &self.0
    }
}

impl DerefMut for SharedTranscript {
    fn deref_mut(&mut self) -> &mut Self::Target {
        Arc::make_mut(&mut self.0)
    }
}

#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub struct TranscriptEntryId(u64);

impl TranscriptEntryId {
    pub fn get(self) -> u64 {
        self.0
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TranscriptRole {
    User,
    Assistant,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TranscriptEntryState {
    Pending,
    Streaming,
    Complete,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TranscriptEntry {
    pub id: TranscriptEntryId,
    pub role: TranscriptRole,
    pub content: String,
    pub state: TranscriptEntryState,
    revision: u64,
    layout_epoch: u64,
}

impl TranscriptEntry {
    pub fn revision(&self) -> u64 {
        self.revision
    }

    pub fn layout_epoch(&self) -> u64 {
        self.layout_epoch
    }
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Transcript {
    entries: Vec<TranscriptEntry>,
    next_entry_id: u64,
    generation: u64,
    pending_response: Option<TranscriptEntryId>,
    active_response: Option<(u64, TranscriptEntryId)>,
}

impl Transcript {
    pub fn entries(&self) -> &[TranscriptEntry] {
        &self.entries
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    pub fn append_exchange(&mut self, prompt: String) -> (TranscriptEntryId, TranscriptEntryId) {
        self.finish_active_response();
        let user = self.push_entry(TranscriptRole::User, prompt, TranscriptEntryState::Complete);
        let assistant = self.push_entry(
            TranscriptRole::Assistant,
            String::new(),
            TranscriptEntryState::Pending,
        );
        self.pending_response = Some(assistant);
        (user, assistant)
    }

    pub fn start_message(&mut self, turn: u64) -> TranscriptEntryId {
        if let Some((active_turn, entry_id)) = self.active_response {
            if active_turn == turn {
                return entry_id;
            }
            self.finish_active_response();
        }
        let entry_id = self.pending_response.take().unwrap_or_else(|| {
            self.push_entry(
                TranscriptRole::Assistant,
                String::new(),
                TranscriptEntryState::Pending,
            )
        });
        self.active_response = Some((turn, entry_id));
        self.set_entry_state(entry_id, TranscriptEntryState::Streaming);
        entry_id
    }

    pub fn append_message_delta(&mut self, turn: u64, delta: &str) -> TranscriptEntryId {
        let entry_id = self.response_for_turn(turn);
        if !delta.is_empty() {
            let entry = self.entry_mut(entry_id);
            entry.content.push_str(delta);
            Self::bump_revision(entry);
            self.bump_generation();
        }
        entry_id
    }

    pub fn complete_message(&mut self, turn: u64, content: String) -> TranscriptEntryId {
        let entry_id = self.response_for_turn(turn);
        let entry = self.entry_mut(entry_id);
        if entry.content != content || entry.state != TranscriptEntryState::Complete {
            if entry.content != content {
                entry.layout_epoch = entry
                    .layout_epoch
                    .checked_add(1)
                    .expect("transcript layout epoch exhausted");
            }
            entry.content = content;
            entry.state = TranscriptEntryState::Complete;
            Self::bump_revision(entry);
            self.bump_generation();
        }
        if self.active_response == Some((turn, entry_id)) {
            self.active_response = None;
        }
        entry_id
    }

    pub fn finish_active_response(&mut self) {
        let entry_id = self
            .active_response
            .take()
            .map(|(_, entry_id)| entry_id)
            .or_else(|| self.pending_response.take());
        if let Some(entry_id) = entry_id {
            self.set_entry_state(entry_id, TranscriptEntryState::Complete);
        }
    }

    pub fn is_streaming_text(&self) -> bool {
        self.active_response.is_some_and(|(_, entry_id)| {
            self.entry(entry_id)
                .is_some_and(|entry| entry.state == TranscriptEntryState::Streaming)
        })
    }

    pub fn latest_user_text(&self) -> Option<&str> {
        self.entries
            .iter()
            .rev()
            .find(|entry| entry.role == TranscriptRole::User)
            .map(|entry| entry.content.as_str())
    }

    pub fn latest_assistant_entry(&self) -> Option<&TranscriptEntry> {
        self.entries
            .iter()
            .rev()
            .find(|entry| entry.role == TranscriptRole::Assistant)
    }

    pub fn latest_assistant_text(&self) -> Option<&str> {
        self.latest_assistant_entry()
            .map(|entry| entry.content.as_str())
    }

    fn response_for_turn(&mut self, turn: u64) -> TranscriptEntryId {
        match self.active_response {
            Some((active_turn, entry_id)) if active_turn == turn => entry_id,
            Some(_) => {
                self.finish_active_response();
                self.start_message(turn)
            }
            None => self.start_message(turn),
        }
    }

    fn push_entry(
        &mut self,
        role: TranscriptRole,
        content: String,
        state: TranscriptEntryState,
    ) -> TranscriptEntryId {
        if let Some(previous) = self.entries.last_mut() {
            Self::bump_revision(previous);
        }
        let id = TranscriptEntryId(self.next_entry_id);
        self.next_entry_id = self
            .next_entry_id
            .checked_add(1)
            .expect("transcript entry identifiers exhausted");
        self.entries.push(TranscriptEntry {
            id,
            role,
            content,
            state,
            revision: 0,
            layout_epoch: 0,
        });
        self.bump_generation();
        id
    }

    pub(crate) fn entry(&self, entry_id: TranscriptEntryId) -> Option<&TranscriptEntry> {
        let index = usize::try_from(entry_id.get()).ok()?;
        self.entries.get(index).filter(|entry| entry.id == entry_id)
    }

    pub(crate) fn entry_before(&self, entry_id: TranscriptEntryId) -> Option<&TranscriptEntry> {
        let index = usize::try_from(entry_id.get()).ok()?;
        let previous = index.checked_sub(1)?;
        self.entries
            .get(previous)
            .filter(|entry| usize::try_from(entry.id.get()) == Ok(previous))
    }

    pub(crate) fn entry_after(&self, entry_id: TranscriptEntryId) -> Option<&TranscriptEntry> {
        let index = usize::try_from(entry_id.get()).ok()?;
        let next = index.checked_add(1)?;
        self.entries
            .get(next)
            .filter(|entry| usize::try_from(entry.id.get()) == Ok(next))
    }

    fn entry_mut(&mut self, entry_id: TranscriptEntryId) -> &mut TranscriptEntry {
        let index =
            usize::try_from(entry_id.get()).expect("transcript entry identifier must fit in usize");
        let entry = self
            .entries
            .get_mut(index)
            .expect("active transcript entry must exist");
        assert_eq!(
            entry.id, entry_id,
            "transcript entry index must stay stable"
        );
        entry
    }

    fn set_entry_state(&mut self, entry_id: TranscriptEntryId, state: TranscriptEntryState) {
        let entry = self.entry_mut(entry_id);
        if entry.state == state {
            return;
        }
        entry.state = state;
        Self::bump_revision(entry);
        self.bump_generation();
    }

    fn bump_revision(entry: &mut TranscriptEntry) {
        entry.revision = entry
            .revision
            .checked_add(1)
            .expect("transcript entry revision exhausted");
    }

    fn bump_generation(&mut self) {
        self.generation = self
            .generation
            .checked_add(1)
            .expect("transcript generation exhausted");
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn shared_transcript_clones_without_copying_and_mutates_copy_on_write() {
        let mut original = SharedTranscript::default();
        original.append_exchange("one".into());
        let mut cloned = original.clone();
        assert!(original.shares_storage_with(&cloned));

        cloned.append_exchange("two".into());

        assert!(!original.shares_storage_with(&cloned));
        assert_eq!(original.entries().len(), 2);
        assert_eq!(cloned.entries().len(), 4);
    }

    #[test]
    fn exchanges_receive_stable_monotonic_ids() {
        let mut transcript = Transcript::default();
        let first = transcript.append_exchange("one".into());
        let second = transcript.append_exchange("two".into());

        assert_eq!((first.0.get(), first.1.get()), (0, 1));
        assert_eq!((second.0.get(), second.1.get()), (2, 3));
        assert_eq!(transcript.entries().len(), 4);
        assert_eq!(transcript.entries()[0].content, "one");
        assert_eq!(transcript.entries()[2].content, "two");
    }

    #[test]
    fn streaming_appends_and_completion_is_authoritative() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("hello".into());
        let entry_id = transcript.start_message(7);
        transcript.append_message_delta(7, "par");
        transcript.append_message_delta(7, "tial");

        let streaming = transcript.entry(entry_id).unwrap();
        assert_eq!(streaming.content, "partial");
        assert_eq!(streaming.state, TranscriptEntryState::Streaming);
        assert!(transcript.is_streaming_text());

        transcript.complete_message(7, "authoritative".into());
        let complete = transcript.entry(entry_id).unwrap();
        assert_eq!(complete.content, "authoritative");
        assert_eq!(complete.state, TranscriptEntryState::Complete);
        assert!(!transcript.is_streaming_text());
    }

    #[test]
    fn unexpected_turn_allocates_without_overwriting_history() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("hello".into());
        transcript.append_message_delta(1, "first");
        let first_id = transcript.latest_assistant_entry().unwrap().id;
        let second_id = transcript.append_message_delta(2, "second");

        assert_ne!(first_id, second_id);
        assert_eq!(transcript.entry(first_id).unwrap().content, "first");
        assert_eq!(
            transcript.entry(first_id).unwrap().state,
            TranscriptEntryState::Complete
        );
        assert_eq!(transcript.entry(second_id).unwrap().content, "second");
    }

    #[test]
    fn finishing_without_a_started_message_settles_the_pending_response() {
        let mut transcript = Transcript::default();
        let (_, assistant) = transcript.append_exchange("hello".into());

        transcript.finish_active_response();

        assert_eq!(
            transcript.entry(assistant).unwrap().state,
            TranscriptEntryState::Complete
        );
        assert!(!transcript.is_streaming_text());
    }

    #[test]
    fn empty_delta_does_not_change_revision() {
        let mut transcript = Transcript::default();
        transcript.append_exchange("hello".into());
        let entry_id = transcript.start_message(1);
        let revision = transcript.entry(entry_id).unwrap().revision();
        let generation = transcript.generation();

        transcript.append_message_delta(1, "");

        assert_eq!(transcript.entry(entry_id).unwrap().revision(), revision);
        assert_eq!(transcript.generation(), generation);
    }
}
