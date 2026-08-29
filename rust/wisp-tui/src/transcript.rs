//! Terminal-independent transcript state for the native TUI.

use std::collections::{HashMap, VecDeque};
use std::ops::{Deref, DerefMut};
use std::sync::Arc;

const MAX_CALL_INDEX_ENTRIES: usize = 1_024;
const MAX_CALL_INDEX_BYTES: usize = 1024 * 1024;
const MAX_PROCESS_INDEX_ENTRIES: usize = 128;
const MAX_PROCESS_INDEX_BYTES: usize = 256 * 1024;
const MAX_TRACKED_PROCESS_ID_BYTES: usize = 4 * 1024;

use crate::tool_cards::{
    INTERRUPTED_TOOL_RESULT_TEXT, ProcessCallIdentity, ProcessCardSnapshot, ProcessOperation,
    ToolCallInput, ToolCardSnapshot, ToolResultInput, ToolStatus, process_call_identity,
};

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
    Tool,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum TranscriptEntryState {
    Pending,
    Streaming,
    Complete,
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub enum TranscriptEntryKind {
    Message,
    Tool(ToolCardSnapshot),
    Process(ProcessCardSnapshot),
}

#[derive(Clone, Debug, Eq, PartialEq)]
pub struct TranscriptEntry {
    pub id: TranscriptEntryId,
    pub role: TranscriptRole,
    /// Message source. Tool/process entries keep structured content in `kind`.
    pub content: String,
    pub state: TranscriptEntryState,
    pub kind: TranscriptEntryKind,
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

    pub fn tool_card(&self) -> Option<&ToolCardSnapshot> {
        match &self.kind {
            TranscriptEntryKind::Tool(card) => Some(card),
            TranscriptEntryKind::Message | TranscriptEntryKind::Process(_) => None,
        }
    }

    pub fn process_card(&self) -> Option<&ProcessCardSnapshot> {
        match &self.kind {
            TranscriptEntryKind::Process(card) => Some(card),
            TranscriptEntryKind::Message | TranscriptEntryKind::Tool(_) => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
enum ToolBindingKind {
    Tool,
    Process(ProcessOperation),
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
struct ToolBinding {
    entry_id: TranscriptEntryId,
    kind: ToolBindingKind,
    resolved: bool,
    sequence: u64,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Transcript {
    entries: Vec<TranscriptEntry>,
    next_entry_id: u64,
    generation: u64,
    pending_response: Option<TranscriptEntryId>,
    started_turn: Option<u64>,
    active_response: Option<(u64, TranscriptEntryId)>,
    call_entries: HashMap<String, ToolBinding>,
    resolved_call_order: VecDeque<String>,
    call_index_bytes: usize,
    process_entries: HashMap<String, TranscriptEntryId>,
    process_order: VecDeque<String>,
    process_index_bytes: usize,
    next_tool_sequence: u64,
}

impl Transcript {
    pub fn entries(&self) -> &[TranscriptEntry] {
        &self.entries
    }

    pub fn generation(&self) -> u64 {
        self.generation
    }

    /// Append a user prompt without creating an out-of-order assistant placeholder.
    pub fn append_prompt(&mut self, prompt: String) -> TranscriptEntryId {
        self.finish_active_response();
        self.push_message(TranscriptRole::User, prompt, TranscriptEntryState::Complete)
    }

    /// Convenience used by presentation tests that explicitly exercise pending rows.
    pub fn append_exchange(&mut self, prompt: String) -> (TranscriptEntryId, TranscriptEntryId) {
        let user = self.append_prompt(prompt);
        let assistant = self.push_message(
            TranscriptRole::Assistant,
            String::new(),
            TranscriptEntryState::Pending,
        );
        self.pending_response = Some(assistant);
        (user, assistant)
    }

    /// Record the provider response boundary without allocating an empty message row.
    pub fn begin_message(&mut self, turn: u64) {
        if self
            .active_response
            .is_some_and(|(active_turn, _)| active_turn == turn)
            || self.started_turn == Some(turn)
        {
            return;
        }
        if self.active_response.is_some() {
            self.finish_active_response();
        } else {
            self.started_turn = None;
        }
        self.started_turn = Some(turn);
    }

    /// Ensure an assistant row immediately. Tests use this to inspect streaming state.
    pub fn start_message(&mut self, turn: u64) -> TranscriptEntryId {
        self.begin_message(turn);
        self.response_for_turn(turn)
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

    pub fn complete_message(&mut self, turn: u64, content: String) -> Option<TranscriptEntryId> {
        if content.is_empty()
            && self.active_response.is_none()
            && (self.started_turn == Some(turn) || self.started_turn.is_none())
            && self.pending_response.is_none()
        {
            if self.started_turn == Some(turn) {
                self.started_turn = None;
            }
            return None;
        }
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
        if self.started_turn == Some(turn) {
            self.started_turn = None;
        }
        Some(entry_id)
    }

    pub fn observe_tool_call(&mut self, input: ToolCallInput) -> TranscriptEntryId {
        self.ensure_tool_entry(&input, ToolStatus::Requested)
    }

    pub fn observe_approval_requested(&mut self, input: ToolCallInput) -> TranscriptEntryId {
        let entry_id = self.ensure_tool_entry(&input, ToolStatus::AwaitingApproval);
        let changed = match self.entry_mut(entry_id).kind {
            TranscriptEntryKind::Tool(ref mut card) => card.approval_requested(),
            TranscriptEntryKind::Process(_) => false,
            TranscriptEntryKind::Message => unreachable!("tool binding must target a card"),
        };
        if changed {
            self.bump_card(entry_id);
        }
        entry_id
    }

    pub fn observe_approval_resolved(
        &mut self,
        call_id: &str,
        approved: bool,
        reason: Option<&str>,
    ) -> Option<TranscriptEntryId> {
        let binding = *self.call_entries.get(call_id)?;
        if binding.resolved {
            self.touch_resolved_tool_binding(call_id);
            return Some(binding.entry_id);
        }
        let changed = match binding.kind {
            ToolBindingKind::Tool => {
                let entry = self.entry_mut(binding.entry_id);
                let TranscriptEntryKind::Tool(card) = &mut entry.kind else {
                    unreachable!("tool binding must target a tool card")
                };
                card.approval_resolved(approved, reason)
            }
            ToolBindingKind::Process(operation) => {
                if approved {
                    false
                } else {
                    let entry = self.entry_mut(binding.entry_id);
                    let TranscriptEntryKind::Process(card) = &mut entry.kind else {
                        unreachable!("process binding must target a process card")
                    };
                    card.deny(operation, reason, binding.sequence)
                }
            }
        };
        if changed {
            self.bump_card(binding.entry_id);
        }
        if !approved {
            self.mark_tool_binding_resolved(call_id);
        }
        Some(binding.entry_id)
    }

    pub fn observe_tool_result(&mut self, input: ToolResultInput) -> TranscriptEntryId {
        let Some(binding) = self.call_entries.get(&input.call_id).copied() else {
            self.prepare_non_message_entry();
            let card = ToolCardSnapshot::result_without_request(&input);
            let entry_id = self.push_card(TranscriptEntryKind::Tool(card));
            let binding = self.new_tool_binding(entry_id, ToolBindingKind::Tool, true);
            self.insert_tool_binding(input.call_id.clone(), binding);
            return entry_id;
        };
        if binding.resolved {
            self.touch_resolved_tool_binding(&input.call_id);
            return binding.entry_id;
        }
        let changed = match binding.kind {
            ToolBindingKind::Tool => {
                let entry = self.entry_mut(binding.entry_id);
                let TranscriptEntryKind::Tool(card) = &mut entry.kind else {
                    unreachable!("tool binding must target a tool card")
                };
                card.apply_result(&input)
            }
            ToolBindingKind::Process(operation) => {
                let entry = self.entry_mut(binding.entry_id);
                let TranscriptEntryKind::Process(card) = &mut entry.kind else {
                    unreachable!("process binding must target a process card")
                };
                if input.process_id.is_none()
                    && input.process_state.as_deref() == Some("cancelled")
                    && input.output == INTERRUPTED_TOOL_RESULT_TEXT
                {
                    card.interrupt(operation, binding.sequence)
                } else if input
                    .process_id
                    .as_deref()
                    .is_some_and(|process_id| process_id != card.process_id)
                {
                    card.observe(
                        operation,
                        &ToolResultInput {
                            output: String::new(),
                            output_source_bytes: 0,
                            process_state: None,
                            process_error: Some(
                                "tool result referenced a different process".into(),
                            ),
                            recovery_hint: None,
                            stdout: None,
                            stdout_source_bytes: 0,
                            stderr: None,
                            stderr_source_bytes: 0,
                            truncated: false,
                            stdout_truncated: false,
                            stderr_truncated: false,
                            stdout_dropped_bytes: 0,
                            stderr_dropped_bytes: 0,
                            is_error: true,
                            ..input.clone()
                        },
                        binding.sequence,
                    )
                } else if input.process_id.is_none() {
                    let preserve_bound_error = input.is_error
                        && (!input.output.is_empty()
                            || input
                                .process_error
                                .as_deref()
                                .is_some_and(|error| !error.is_empty())
                            || input
                                .recovery_hint
                                .as_deref()
                                .is_some_and(|hint| !hint.is_empty()));
                    let process_error = if preserve_bound_error {
                        input.process_error.clone().or_else(|| {
                            input
                                .output
                                .is_empty()
                                .then(|| input.recovery_hint.clone())
                                .flatten()
                        })
                    } else {
                        Some("tool result did not identify its process".into())
                    };
                    card.observe(
                        operation,
                        &ToolResultInput {
                            output: if preserve_bound_error {
                                input.output.clone()
                            } else {
                                String::new()
                            },
                            output_source_bytes: if preserve_bound_error {
                                input.output_source_bytes
                            } else {
                                0
                            },
                            process_state: None,
                            process_error,
                            recovery_hint: preserve_bound_error
                                .then(|| input.recovery_hint.clone())
                                .flatten(),
                            stdout: None,
                            stdout_source_bytes: 0,
                            stderr: None,
                            stderr_source_bytes: 0,
                            truncated: preserve_bound_error && input.truncated,
                            stdout_truncated: false,
                            stderr_truncated: false,
                            stdout_dropped_bytes: 0,
                            stderr_dropped_bytes: 0,
                            is_error: true,
                            ..input.clone()
                        },
                        binding.sequence,
                    )
                } else {
                    card.observe(operation, &input, binding.sequence)
                }
            }
        };
        if changed {
            self.bump_card(binding.entry_id);
        }
        self.mark_tool_binding_resolved(&input.call_id);
        binding.entry_id
    }

    pub fn settle_unresolved_tools(&mut self, reason: &str) {
        let mut unresolved = self
            .call_entries
            .iter()
            .filter(|(_, binding)| !binding.resolved)
            .map(|(call_id, binding)| (binding.sequence, call_id.clone()))
            .collect::<Vec<_>>();
        unresolved.sort_unstable_by_key(|(sequence, _)| *sequence);
        for (_, call_id) in unresolved {
            let binding = *self.call_entries.get(&call_id).expect("binding exists");
            let changed = match binding.kind {
                ToolBindingKind::Tool => {
                    let entry = self.entry_mut(binding.entry_id);
                    let TranscriptEntryKind::Tool(card) = &mut entry.kind else {
                        unreachable!("tool binding must target a tool card")
                    };
                    card.cancel(reason)
                }
                ToolBindingKind::Process(operation) => {
                    let entry = self.entry_mut(binding.entry_id);
                    let TranscriptEntryKind::Process(card) = &mut entry.kind else {
                        unreachable!("process binding must target a process card")
                    };
                    card.interrupt(operation, binding.sequence)
                }
            };
            if changed {
                self.bump_card(binding.entry_id);
            }
            self.mark_tool_binding_resolved(&call_id);
        }
    }

    pub fn finish_active_response(&mut self) {
        self.started_turn = None;
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
            .find(|entry| {
                entry.role == TranscriptRole::User
                    && matches!(entry.kind, TranscriptEntryKind::Message)
            })
            .map(|entry| entry.content.as_str())
    }

    pub fn latest_assistant_entry(&self) -> Option<&TranscriptEntry> {
        self.entries.iter().rev().find(|entry| {
            entry.role == TranscriptRole::Assistant
                && matches!(entry.kind, TranscriptEntryKind::Message)
        })
    }

    pub fn latest_assistant_text(&self) -> Option<&str> {
        self.latest_assistant_entry()
            .map(|entry| entry.content.as_str())
    }

    fn ensure_tool_entry(
        &mut self,
        input: &ToolCallInput,
        initial_status: ToolStatus,
    ) -> TranscriptEntryId {
        if let Some(binding) = self.call_entries.get(&input.call_id).copied() {
            if binding.resolved {
                self.touch_resolved_tool_binding(&input.call_id);
                self.prepare_non_message_entry();
                let mut card = ToolCardSnapshot::requested(input, initial_status);
                card.cancel("call ID was reused; result correlation is ambiguous");
                return self.push_card(TranscriptEntryKind::Tool(card));
            } else {
                if binding.kind == ToolBindingKind::Tool {
                    let changed = {
                        let entry = self.entry_mut(binding.entry_id);
                        let TranscriptEntryKind::Tool(card) = &mut entry.kind else {
                            unreachable!("tool binding must target a tool card")
                        };
                        card.enrich_call(input)
                    };
                    if changed {
                        self.bump_card(binding.entry_id);
                    }
                }
                return binding.entry_id;
            }
        }

        self.prepare_non_message_entry();
        if !self.prepare_unresolved_tool_binding(&input.call_id) {
            let mut card = ToolCardSnapshot::requested(input, initial_status);
            card.cancel("too many concurrent tool calls to track");
            return self.push_card(TranscriptEntryKind::Tool(card));
        }
        if let Some(ProcessCallIdentity {
            process_id,
            operation,
        }) = process_call_identity(&input.name, &input.arguments)
        {
            if let Some(entry_id) = self.process_entry_for(&process_id) {
                let binding =
                    self.new_tool_binding(entry_id, ToolBindingKind::Process(operation), false);
                let changed = {
                    let entry = self.entry_mut(entry_id);
                    let TranscriptEntryKind::Process(card) = &mut entry.kind else {
                        unreachable!("process index must target a process card")
                    };
                    card.begin(operation, binding.sequence)
                };
                if changed {
                    self.bump_card(entry_id);
                }
                self.insert_tool_binding(input.call_id.clone(), binding);
                return entry_id;
            }
        }

        let entry_id = self.push_card(TranscriptEntryKind::Tool(ToolCardSnapshot::requested(
            input,
            initial_status,
        )));
        let binding = self.new_tool_binding(entry_id, ToolBindingKind::Tool, false);
        self.insert_tool_binding(input.call_id.clone(), binding);
        entry_id
    }

    fn new_tool_binding(
        &mut self,
        entry_id: TranscriptEntryId,
        kind: ToolBindingKind,
        resolved: bool,
    ) -> ToolBinding {
        let sequence = self.next_tool_sequence;
        self.next_tool_sequence = self
            .next_tool_sequence
            .checked_add(1)
            .expect("tool lifecycle sequence exhausted");
        ToolBinding {
            entry_id,
            kind,
            resolved,
            sequence,
        }
    }

    fn insert_tool_binding(&mut self, call_id: String, binding: ToolBinding) {
        self.call_index_bytes = self.call_index_bytes.saturating_add(call_id.len());
        if binding.resolved {
            self.resolved_call_order.push_back(call_id.clone());
        }
        self.call_entries.insert(call_id, binding);
        self.evict_resolved_tool_bindings();
        debug_assert!(self.call_entries.len() <= MAX_CALL_INDEX_ENTRIES);
        debug_assert!(self.call_index_bytes <= MAX_CALL_INDEX_BYTES);
    }

    fn prepare_unresolved_tool_binding(&mut self, call_id: &str) -> bool {
        while self.call_entries.len().saturating_add(1) > MAX_CALL_INDEX_ENTRIES
            || self.call_index_bytes.saturating_add(call_id.len()) > MAX_CALL_INDEX_BYTES
        {
            if !self.evict_oldest_resolved_tool_binding() {
                return false;
            }
        }
        true
    }

    fn mark_tool_binding_resolved(&mut self, call_id: &str) {
        let Some(binding) = self.call_entries.get_mut(call_id) else {
            return;
        };
        if binding.resolved {
            return;
        }
        binding.resolved = true;
        self.resolved_call_order.push_back(call_id.to_owned());
        self.evict_resolved_tool_bindings();
    }

    fn touch_resolved_tool_binding(&mut self, call_id: &str) {
        self.resolved_call_order
            .retain(|candidate| candidate != call_id);
        self.resolved_call_order.push_back(call_id.to_owned());
    }

    fn evict_resolved_tool_bindings(&mut self) {
        while self.call_entries.len() > MAX_CALL_INDEX_ENTRIES
            || self.call_index_bytes > MAX_CALL_INDEX_BYTES
        {
            if !self.evict_oldest_resolved_tool_binding() {
                break;
            }
        }
    }

    fn evict_oldest_resolved_tool_binding(&mut self) -> bool {
        while let Some(call_id) = self.resolved_call_order.pop_front() {
            if self
                .call_entries
                .get(&call_id)
                .is_some_and(|binding| binding.resolved)
            {
                self.remove_tool_binding(&call_id);
                return true;
            }
        }
        false
    }

    fn remove_tool_binding(&mut self, call_id: &str) {
        if self.call_entries.remove(call_id).is_some() {
            self.call_index_bytes = self.call_index_bytes.saturating_sub(call_id.len());
        }
        self.resolved_call_order
            .retain(|candidate| candidate != call_id);
    }

    fn process_entry_for(&mut self, process_id: &str) -> Option<TranscriptEntryId> {
        if let Some(entry_id) = self.process_entries.get(process_id).copied() {
            self.process_order
                .retain(|candidate| candidate != process_id);
            self.process_order.push_back(process_id.to_owned());
            return Some(entry_id);
        }
        if process_id.len() > MAX_TRACKED_PROCESS_ID_BYTES {
            return None;
        }
        while self.process_entries.len().saturating_add(1) > MAX_PROCESS_INDEX_ENTRIES
            || self.process_index_bytes.saturating_add(process_id.len()) > MAX_PROCESS_INDEX_BYTES
        {
            if !self.evict_oldest_terminal_process() {
                return None;
            }
        }
        let entry_id = self.push_card(TranscriptEntryKind::Process(ProcessCardSnapshot::new(
            process_id.to_owned(),
        )));
        self.process_entries.insert(process_id.to_owned(), entry_id);
        self.process_order.push_back(process_id.to_owned());
        self.process_index_bytes = self.process_index_bytes.saturating_add(process_id.len());
        Some(entry_id)
    }

    fn evict_oldest_terminal_process(&mut self) -> bool {
        let candidates = self.process_order.len();
        for _ in 0..candidates {
            let Some(process_id) = self.process_order.pop_front() else {
                return false;
            };
            let Some(entry_id) = self.process_entries.get(&process_id).copied() else {
                continue;
            };
            let has_unresolved_call = self.call_entries.values().any(|binding| {
                binding.entry_id == entry_id
                    && matches!(binding.kind, ToolBindingKind::Process(_))
                    && !binding.resolved
            });
            let terminal = self
                .entry(entry_id)
                .and_then(TranscriptEntry::process_card)
                .is_some_and(|card| card.display_state.terminal());
            if terminal && !has_unresolved_call {
                self.process_entries.remove(&process_id);
                self.process_index_bytes =
                    self.process_index_bytes.saturating_sub(process_id.len());
                return true;
            }
            self.process_order.push_back(process_id);
        }
        false
    }

    fn prepare_non_message_entry(&mut self) {
        self.finish_active_response();
    }

    fn response_for_turn(&mut self, turn: u64) -> TranscriptEntryId {
        match self.active_response {
            Some((active_turn, entry_id)) if active_turn == turn => entry_id,
            Some(_) => {
                self.finish_active_response();
                self.allocate_response(turn)
            }
            None => self.allocate_response(turn),
        }
    }

    fn allocate_response(&mut self, turn: u64) -> TranscriptEntryId {
        let entry_id = self.pending_response.take().unwrap_or_else(|| {
            self.push_message(
                TranscriptRole::Assistant,
                String::new(),
                TranscriptEntryState::Pending,
            )
        });
        self.started_turn = None;
        self.active_response = Some((turn, entry_id));
        self.set_entry_state(entry_id, TranscriptEntryState::Streaming);
        entry_id
    }

    fn push_message(
        &mut self,
        role: TranscriptRole,
        content: String,
        state: TranscriptEntryState,
    ) -> TranscriptEntryId {
        self.push_entry(TranscriptEntry {
            id: TranscriptEntryId(0),
            role,
            content,
            state,
            kind: TranscriptEntryKind::Message,
            revision: 0,
            layout_epoch: 0,
        })
    }

    fn push_card(&mut self, kind: TranscriptEntryKind) -> TranscriptEntryId {
        let state = match &kind {
            TranscriptEntryKind::Tool(card) if card.status.terminal() => {
                TranscriptEntryState::Complete
            }
            TranscriptEntryKind::Process(card) if card.display_state.terminal() => {
                TranscriptEntryState::Complete
            }
            TranscriptEntryKind::Message => unreachable!("card kind expected"),
            TranscriptEntryKind::Tool(_) | TranscriptEntryKind::Process(_) => {
                TranscriptEntryState::Streaming
            }
        };
        self.push_entry(TranscriptEntry {
            id: TranscriptEntryId(0),
            role: TranscriptRole::Tool,
            content: String::new(),
            state,
            kind,
            revision: 0,
            layout_epoch: 0,
        })
    }

    fn push_entry(&mut self, mut entry: TranscriptEntry) -> TranscriptEntryId {
        if let Some(previous) = self.entries.last_mut() {
            Self::bump_revision(previous);
        }
        let id = TranscriptEntryId(self.next_entry_id);
        self.next_entry_id = self
            .next_entry_id
            .checked_add(1)
            .expect("transcript entry identifiers exhausted");
        entry.id = id;
        self.entries.push(entry);
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

    fn bump_card(&mut self, entry_id: TranscriptEntryId) {
        let entry = self.entry_mut(entry_id);
        entry.state = match &entry.kind {
            TranscriptEntryKind::Tool(card) if card.status.terminal() => {
                TranscriptEntryState::Complete
            }
            TranscriptEntryKind::Process(card) if card.display_state.terminal() => {
                TranscriptEntryState::Complete
            }
            TranscriptEntryKind::Tool(_) | TranscriptEntryKind::Process(_) => {
                TranscriptEntryState::Streaming
            }
            TranscriptEntryKind::Message => unreachable!("card entry expected"),
        };
        entry.layout_epoch = entry
            .layout_epoch
            .checked_add(1)
            .expect("transcript layout epoch exhausted");
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
    use crate::tool_cards::ProcessDisplayState;
    use serde_json::Value;

    fn call(call_id: &str, name: &str, arguments: Value) -> ToolCallInput {
        ToolCallInput {
            call_id: call_id.into(),
            name: name.into(),
            arguments,
        }
    }

    fn result(call_id: &str, output: &str) -> ToolResultInput {
        ToolResultInput {
            call_id: call_id.into(),
            name: "read".into(),
            output: output.into(),
            output_source_bytes: output.len() as u64,
            is_error: false,
            failure_code: None,
            retryable: false,
            recovery_hint: None,
            exit_code: None,
            output_has_exit_status: false,
            before_text: None,
            created: false,
            summary: None,
            truncated: false,
            process_id: None,
            process_state: None,
            process_error: None,
            stdout: None,
            stdout_source_bytes: 0,
            stderr: None,
            stderr_source_bytes: 0,
            stdout_truncated: false,
            stderr_truncated: false,
            stdout_dropped_bytes: 0,
            stderr_dropped_bytes: 0,
        }
    }

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
    fn lazy_runtime_messages_preserve_tool_interleaving() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("hello".into());
        transcript.begin_message(1);
        assert_eq!(transcript.complete_message(1, String::new()), None);
        let tool = transcript.observe_tool_call(call(
            "call-1",
            "read",
            serde_json::json!({"path": "README.md"}),
        ));
        transcript.observe_tool_result(result("call-1", "contents"));
        transcript.begin_message(2);
        let assistant = transcript
            .complete_message(2, "done".into())
            .expect("non-empty completion creates a row");

        assert_eq!(transcript.entries().len(), 3);
        assert_eq!(transcript.entries()[0].role, TranscriptRole::User);
        assert_eq!(transcript.entries()[1].id, tool);
        assert_eq!(transcript.entries()[2].id, assistant);
    }

    #[test]
    fn approval_before_call_enriches_one_stable_card() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("read".into());
        let request = call("call-1", "read", serde_json::json!({"path": "README.md"}));
        let card_id = transcript.observe_approval_requested(request.clone());
        assert_eq!(
            transcript
                .entry(card_id)
                .unwrap()
                .tool_card()
                .unwrap()
                .status,
            ToolStatus::AwaitingApproval
        );

        assert_eq!(transcript.observe_tool_call(request), card_id);
        transcript.observe_approval_resolved("call-1", true, None);
        transcript.observe_tool_result(result("call-1", "contents"));

        assert_eq!(transcript.entries().len(), 2);
        let card = transcript.entry(card_id).unwrap().tool_card().unwrap();
        assert_eq!(card.status, ToolStatus::Done);
        assert_eq!(card.action_arguments, "README.md");
    }

    #[test]
    fn parallel_calls_resolve_in_place_out_of_order() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("parallel".into());
        let first =
            transcript.observe_tool_call(call("first", "read", serde_json::json!({"path": "a"})));
        let second =
            transcript.observe_tool_call(call("second", "read", serde_json::json!({"path": "b"})));
        transcript.observe_tool_result(result("second", "B"));
        transcript.observe_tool_result(result("first", "A"));

        assert!(
            transcript
                .entry(first)
                .unwrap()
                .tool_card()
                .unwrap()
                .status
                .terminal()
        );
        assert!(
            transcript
                .entry(second)
                .unwrap()
                .tool_card()
                .unwrap()
                .status
                .terminal()
        );
        assert_eq!(transcript.entries()[1].id, first);
        assert_eq!(transcript.entries()[2].id, second);
    }

    #[test]
    fn process_poll_calls_share_one_entry() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("poll".into());
        let first = transcript.observe_tool_call(call(
            "poll-1",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        ));
        let mut first_result = result("poll-1", "");
        first_result.name = "bash".into();
        first_result.process_id = Some("process-1".into());
        first_result.process_state = Some("running".into());
        first_result.stdout = Some("one".into());
        transcript.observe_tool_result(first_result);

        let second = transcript.observe_tool_call(call(
            "poll-2",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        ));
        assert_eq!(first, second);
        let card = transcript.entry(first).unwrap().process_card().unwrap();
        assert_eq!(card.call_count, 2);
        assert_eq!(card.poll_count, 2);
    }

    #[test]
    fn cancelled_poll_result_interrupts_only_the_poll_operation() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("poll".into());
        let card_id = transcript.observe_tool_call(call(
            "poll-1",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        ));
        let mut interrupted = result("poll-1", INTERRUPTED_TOOL_RESULT_TEXT);
        interrupted.name = "bash".into();
        interrupted.is_error = true;
        interrupted.failure_code = Some("internal_error".into());
        interrupted.retryable = true;
        interrupted.process_state = Some("cancelled".into());

        transcript.observe_tool_result(interrupted);

        assert_eq!(
            transcript
                .entry(card_id)
                .unwrap()
                .process_card()
                .unwrap()
                .display_state,
            ProcessDisplayState::PollInterrupted
        );
    }

    #[test]
    fn mismatched_process_result_does_not_retain_foreign_output() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("poll".into());
        let card_id = transcript.observe_tool_call(call(
            "poll-1",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-a"}),
        ));
        let mut mismatched = result("poll-1", "foreign fallback");
        mismatched.name = "bash".into();
        mismatched.process_id = Some("process-b".into());
        mismatched.process_state = Some("running".into());
        mismatched.stdout = Some("foreign stdout".into());
        mismatched.stderr = Some("foreign stderr".into());
        mismatched.truncated = true;
        mismatched.stdout_truncated = true;
        mismatched.stdout_dropped_bytes = 99;
        mismatched.recovery_hint = Some("trust the foreign process".into());

        transcript.observe_tool_result(mismatched);

        let card = transcript.entry(card_id).unwrap().process_card().unwrap();
        assert_eq!(card.display_state, ProcessDisplayState::PollFailed);
        assert!(card.retained_output.text.contains("different process"));
        assert!(!card.retained_output.text.contains("foreign"));
        assert!(
            !card
                .retained_output
                .text
                .contains("trust the foreign process")
        );
        assert!(!card.backend_truncated);
        assert_eq!(card.backend_dropped_bytes, 0);
    }

    #[test]
    fn missing_process_identity_does_not_retain_unattributed_output() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("poll".into());
        let card_id = transcript.observe_tool_call(call(
            "poll-1",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-a"}),
        ));
        let mut missing = result("poll-1", "unattributed fallback");
        missing.name = "bash".into();
        missing.process_state = Some("running".into());
        missing.stdout = Some("unattributed stdout".into());
        missing.stdout_source_bytes = "unattributed stdout".len() as u64;
        missing.truncated = true;
        missing.stdout_truncated = true;
        missing.stdout_dropped_bytes = 99;
        missing.recovery_hint = Some("trust unattributed metadata".into());

        transcript.observe_tool_result(missing);

        let card = transcript.entry(card_id).unwrap().process_card().unwrap();
        assert_eq!(card.display_state, ProcessDisplayState::PollFailed);
        assert!(card.retained_output.text.contains("did not identify"));
        assert!(!card.retained_output.text.contains("unattributed"));
        assert!(
            !card
                .retained_output
                .text
                .contains("trust unattributed metadata")
        );
        assert!(!card.backend_truncated);
        assert_eq!(card.backend_dropped_bytes, 0);
    }

    #[test]
    fn missing_process_identity_preserves_bound_tool_error_context() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("poll".into());
        let card_id = transcript.observe_tool_call(call(
            "poll-1",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "expired-process"}),
        ));
        let mut failed = result("poll-1", "process expired before it could be polled");
        failed.name = "bash".into();
        failed.is_error = true;
        failed.failure_code = Some("tool_error".into());
        failed.recovery_hint = Some("start the command again".into());

        transcript.observe_tool_result(failed);

        let card = transcript.entry(card_id).unwrap().process_card().unwrap();
        assert_eq!(card.display_state, ProcessDisplayState::PollFailed);
        assert!(card.retained_output.text.contains("process expired"));
        assert!(
            card.retained_output
                .text
                .contains("start the command again")
        );
        assert!(!card.retained_output.text.contains("did not identify"));
    }

    #[test]
    fn unresolved_process_operations_settle_in_observation_order() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("process".into());
        let card_id = transcript.observe_tool_call(call(
            "poll-1",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        ));
        transcript.observe_tool_call(call(
            "cancel-1",
            "bash",
            serde_json::json!({"operation": "cancel", "process_id": "process-1"}),
        ));

        transcript.settle_unresolved_tools("closed");

        assert_eq!(
            transcript
                .entry(card_id)
                .unwrap()
                .process_card()
                .unwrap()
                .display_state,
            ProcessDisplayState::CancelInterrupted
        );
    }

    #[test]
    fn duplicate_results_do_not_mutate_or_append() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("read".into());
        let card = transcript.observe_tool_call(call("call-1", "read", serde_json::json!({})));
        let completed = result("call-1", "contents");
        transcript.observe_tool_result(completed.clone());
        let revision = transcript.entry(card).unwrap().revision();
        transcript.observe_tool_result(completed);

        assert_eq!(transcript.entries().len(), 2);
        assert_eq!(transcript.entry(card).unwrap().revision(), revision);
    }

    #[test]
    fn overlapping_process_results_retain_older_output_without_regressing_state() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("process".into());
        let card_id = transcript.observe_tool_call(call(
            "poll-1",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        ));
        transcript.observe_tool_call(call(
            "poll-2",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        ));
        let mut older = result("poll-1", "");
        older.name = "bash".into();
        older.process_id = Some("process-1".into());
        older.process_state = Some("completed".into());
        older.stdout = Some("unique output from first poll".into());
        older.stdout_source_bytes = 64;
        older.stdout_dropped_bytes = 7;
        older.stdout_truncated = true;
        transcript.observe_tool_result(older);

        let card = transcript.entry(card_id).unwrap().process_card().unwrap();
        assert_eq!(card.display_state, ProcessDisplayState::Polling);
        assert!(
            card.retained_output
                .text
                .contains("unique output from first poll")
        );
        assert!(card.backend_truncated);
        assert!(card.backend_dropped_bytes >= 7);
    }

    #[test]
    fn stale_process_results_and_settlement_cannot_overwrite_newer_operations() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("process".into());
        let card_id = transcript.observe_tool_call(call(
            "poll-1",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        ));
        transcript.observe_tool_call(call(
            "cancel-1",
            "bash",
            serde_json::json!({"operation": "cancel", "process_id": "process-1"}),
        ));
        let mut cancelled = result("cancel-1", "cancelled");
        cancelled.name = "bash".into();
        cancelled.process_id = Some("process-1".into());
        cancelled.process_state = Some("cancelled".into());
        transcript.observe_tool_result(cancelled);

        transcript.settle_unresolved_tools("closed");

        assert_eq!(
            transcript
                .entry(card_id)
                .unwrap()
                .process_card()
                .unwrap()
                .display_state,
            ProcessDisplayState::Cancelled
        );
    }

    #[test]
    fn resolved_process_call_index_is_bounded() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("poll".into());
        for index in 0..MAX_CALL_INDEX_ENTRIES {
            let call_id = format!("poll-{index}");
            transcript.observe_tool_call(call(
                &call_id,
                "bash",
                serde_json::json!({"operation": "poll", "process_id": "process-1"}),
            ));
            let mut completed = result(&call_id, "");
            completed.name = "bash".into();
            completed.process_id = Some("process-1".into());
            completed.process_state = Some("running".into());
            transcript.observe_tool_result(completed);
        }

        transcript.observe_tool_result(result("poll-0", "duplicate"));
        let extra = "poll-extra";
        transcript.observe_tool_call(call(
            extra,
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        ));
        let mut completed = result(extra, "");
        completed.name = "bash".into();
        completed.process_id = Some("process-1".into());
        completed.process_state = Some("running".into());
        transcript.observe_tool_result(completed);

        assert!(transcript.call_entries.len() <= MAX_CALL_INDEX_ENTRIES);
        assert!(transcript.call_index_bytes <= MAX_CALL_INDEX_BYTES);
        assert!(transcript.call_entries.contains_key("poll-0"));
        assert!(!transcript.call_entries.contains_key("poll-1"));
        assert_eq!(transcript.process_entries.len(), 1);
    }

    #[test]
    fn reused_call_id_is_reported_as_ambiguous_and_late_results_are_ignored() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("reuse".into());
        let first = transcript.observe_tool_call(call("reused", "read", serde_json::json!({})));
        transcript.observe_tool_result(result("reused", "first"));
        let conflict = transcript.observe_tool_call(call("reused", "read", serde_json::json!({})));
        transcript.observe_tool_result(result("reused", "late duplicate"));
        transcript.observe_tool_result(result("reused", "second lifecycle"));

        assert_ne!(first, conflict);
        assert_eq!(
            transcript
                .entry(first)
                .unwrap()
                .tool_card()
                .unwrap()
                .preview(),
            "first"
        );
        let conflict = transcript.entry(conflict).unwrap().tool_card().unwrap();
        assert_eq!(
            transcript.resolved_call_order.back().map(String::as_str),
            Some("reused")
        );
        assert_eq!(conflict.status, ToolStatus::Cancelled);
        assert!(conflict.detail.contains("correlation is ambiguous"));
        assert!(!conflict.preview().contains("late duplicate"));
        assert!(!conflict.preview().contains("second lifecycle"));
    }

    #[test]
    fn unresolved_call_index_limits_are_hard_bounds() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("parallel".into());
        let mut overflow = None;
        for index in 0..=MAX_CALL_INDEX_ENTRIES {
            let entry_id = transcript.observe_tool_call(call(
                &format!("call-{index}"),
                "read",
                serde_json::json!({}),
            ));
            overflow = Some(entry_id);
        }

        assert_eq!(transcript.call_entries.len(), MAX_CALL_INDEX_ENTRIES);
        assert!(transcript.call_index_bytes <= MAX_CALL_INDEX_BYTES);
        assert_eq!(
            transcript
                .entry(overflow.unwrap())
                .unwrap()
                .tool_card()
                .unwrap()
                .status,
            ToolStatus::Cancelled
        );
    }

    #[test]
    fn process_index_evicts_old_identities_without_disabling_new_cards() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("processes".into());
        let mut newest = None;
        for index in 0..(MAX_PROCESS_INDEX_ENTRIES + 10) {
            let process_id = format!("process-{index}");
            let call_id = format!("poll-{index}");
            let entry_id = transcript.observe_tool_call(call(
                &call_id,
                "bash",
                serde_json::json!({"operation": "poll", "process_id": process_id}),
            ));
            let mut completed = result(&call_id, "");
            completed.name = "bash".into();
            completed.process_id = Some(process_id);
            completed.process_state = Some("completed".into());
            transcript.observe_tool_result(completed);
            newest = Some(entry_id);
        }

        assert_eq!(transcript.process_entries.len(), MAX_PROCESS_INDEX_ENTRIES);
        assert!(transcript.process_index_bytes <= MAX_PROCESS_INDEX_BYTES);
        assert!(
            transcript
                .entry(newest.unwrap())
                .unwrap()
                .process_card()
                .is_some()
        );
        assert!(transcript.process_entries.contains_key("process-137"));
        assert!(!transcript.process_entries.contains_key("process-0"));
    }

    #[test]
    fn process_index_never_evicts_running_lifecycles() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("running processes".into());
        let mut first_entry = None;
        for index in 0..MAX_PROCESS_INDEX_ENTRIES {
            let process_id = format!("process-{index}");
            let call_id = format!("poll-{index}");
            let entry_id = transcript.observe_tool_call(call(
                &call_id,
                "bash",
                serde_json::json!({"operation": "poll", "process_id": process_id}),
            ));
            let mut running = result(&call_id, "");
            running.name = "bash".into();
            running.process_id = Some(process_id);
            running.process_state = Some("running".into());
            transcript.observe_tool_result(running);
            first_entry.get_or_insert(entry_id);
        }
        let overflow = transcript.observe_tool_call(call(
            "overflow",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-overflow"}),
        ));
        assert!(transcript.entry(overflow).unwrap().tool_card().is_some());
        let reused = transcript.observe_tool_call(call(
            "poll-first-again",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-0"}),
        ));

        assert_eq!(reused, first_entry.unwrap());
        assert_eq!(transcript.process_entries.len(), MAX_PROCESS_INDEX_ENTRIES);
        assert!(!transcript.process_entries.contains_key("process-overflow"));
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

    #[test]
    fn command_failure_settles_every_unresolved_card() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("tools".into());
        let tool = transcript.observe_tool_call(call("tool", "read", serde_json::json!({})));
        let process = transcript.observe_tool_call(call(
            "poll",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process"}),
        ));

        transcript.settle_unresolved_tools("command failed");

        assert_eq!(
            transcript.entry(tool).unwrap().tool_card().unwrap().status,
            ToolStatus::Cancelled
        );
        assert_eq!(
            transcript
                .entry(process)
                .unwrap()
                .process_card()
                .unwrap()
                .display_state,
            ProcessDisplayState::PollInterrupted
        );
    }
}
