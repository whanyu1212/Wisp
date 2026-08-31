//! Terminal-independent transcript state for the native TUI.

use std::collections::{HashMap, HashSet, VecDeque};
use std::ops::{Deref, DerefMut};
use std::sync::Arc;

const MAX_CALL_INDEX_ENTRIES: usize = 1_024;
const MAX_CALL_INDEX_BYTES: usize = 1024 * 1024;
const MAX_PROCESS_INDEX_ENTRIES: usize = 128;
const MAX_PROCESS_INDEX_BYTES: usize = 256 * 1024;
const MAX_TRACKED_PROCESS_ID_BYTES: usize = 4 * 1024;
const MAX_PENDING_DETAIL_SOURCES: usize = 128;
const MAX_PENDING_DETAIL_SOURCE_BYTES: usize = 1024 * 1024;
const HISTORY_OMISSION_MARKER: &str = "[earlier session history omitted]";

use crate::tool_cards::{
    INTERRUPTED_TOOL_RESULT_TEXT, ProcessCallIdentity, ProcessCardSnapshot, ProcessOperation,
    ToolCallInput, ToolCardSnapshot, ToolResultInput, ToolStatus, identity_for_display,
    process_call_identity,
};
use crate::tool_detail::{
    DetailAvailability, DetailUnavailableReason, ToolDetailPresentation, ToolDetailSource,
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

    #[cfg(test)]
    pub(crate) fn from_raw(value: u64) -> Self {
        Self(value)
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
    history_group: Option<u64>,
    history_omission: bool,
    durable_entry_ids: Vec<String>,
    history_detail_source: Option<ToolDetailSource>,
    history_result_projection_truncated: bool,
    history_calls: Vec<HistoricalCall>,
    history_pending_result: Option<Box<ToolResultInput>>,
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

#[derive(Clone, Debug, Eq, PartialEq)]
struct HistoricalCall {
    call_id: String,
    kind: ToolBindingKind,
    sequence: u64,
}

#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct Transcript {
    entries: Vec<TranscriptEntry>,
    entry_indexes: HashMap<TranscriptEntryId, usize>,
    next_entry_id: u64,
    next_history_group: u64,
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
    pending_detail_sources: HashMap<TranscriptEntryId, usize>,
    pending_detail_order: VecDeque<TranscriptEntryId>,
    pending_detail_source_bytes: usize,
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

    pub(crate) fn has_unresolved_tool_call(&self, call_id: &str) -> bool {
        self.call_entries
            .get(call_id)
            .is_some_and(|binding| !binding.resolved)
    }

    pub fn observe_tool_call(&mut self, input: ToolCallInput) -> TranscriptEntryId {
        let entry_id = self.ensure_tool_entry(&input, ToolStatus::Requested);
        self.update_pending_detail_tracking(entry_id);
        entry_id
    }

    pub fn observe_approval_requested(&mut self, input: ToolCallInput) -> TranscriptEntryId {
        let entry_id = self.ensure_tool_entry(&input, ToolStatus::AwaitingApproval);
        self.update_pending_detail_tracking(entry_id);
        let Some(binding) = self.call_entries.get(&input.call_id).copied() else {
            return entry_id;
        };
        if binding.resolved {
            self.touch_resolved_tool_binding(&input.call_id);
            return entry_id;
        }
        let changed = match self.entry_mut(entry_id).kind {
            TranscriptEntryKind::Tool(ref mut card) => card.approval_requested(),
            TranscriptEntryKind::Process(ref mut card) => {
                let ToolBindingKind::Process(operation) = binding.kind else {
                    unreachable!("process card binding must target a process operation")
                };
                card.approval_requested(operation, binding.sequence)
            }
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
                let entry = self.entry_mut(binding.entry_id);
                let TranscriptEntryKind::Process(card) = &mut entry.kind else {
                    unreachable!("process binding must target a process card")
                };
                if approved {
                    card.approve(operation, binding.sequence)
                } else {
                    card.deny(operation, reason, binding.sequence)
                }
            }
        };
        if changed {
            self.bump_card(binding.entry_id);
        }
        self.update_pending_detail_tracking(binding.entry_id);
        if !approved && changed {
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
                            output_source_lines: 0,
                            output_projection_cut_mid_line: false,
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
                    let promoted_recovery_hint = preserve_bound_error
                        && input.process_error.is_none()
                        && input.output.is_empty()
                        && input.recovery_hint.is_some();
                    let process_error = if preserve_bound_error {
                        input.process_error.clone().or_else(|| {
                            promoted_recovery_hint
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
                            output_source_lines: if preserve_bound_error {
                                input.output_source_lines
                            } else {
                                0
                            },
                            output_projection_cut_mid_line: preserve_bound_error
                                && input.output_projection_cut_mid_line,
                            process_state: None,
                            process_error,
                            recovery_hint: (preserve_bound_error && !promoted_recovery_hint)
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
        self.update_pending_detail_tracking(binding.entry_id);
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
            self.update_pending_detail_tracking(binding.entry_id);
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
                } else {
                    let ToolBindingKind::Process(operation) = binding.kind else {
                        unreachable!("non-tool binding must target a process operation")
                    };
                    let incoming = process_call_identity(&input.name, &input.arguments);
                    let (conflicts, changed) = {
                        let entry = self.entry_mut(binding.entry_id);
                        let TranscriptEntryKind::Process(card) = &mut entry.kind else {
                            unreachable!("process binding must target a process card")
                        };
                        let conflicts = incoming.as_ref().is_none_or(|identity| {
                            identity.operation != operation
                                || identity.process_id != card.process_id
                        });
                        let changed = conflicts && card.conflict(operation, binding.sequence);
                        (conflicts, changed)
                    };
                    if changed {
                        self.bump_card(binding.entry_id);
                    }
                    if conflicts {
                        self.mark_tool_binding_resolved(&input.call_id);
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

    fn update_pending_detail_tracking(&mut self, entry_id: TranscriptEntryId) {
        if let Some(previous) = self.pending_detail_sources.remove(&entry_id) {
            self.pending_detail_source_bytes =
                self.pending_detail_source_bytes.saturating_sub(previous);
            self.pending_detail_order
                .retain(|candidate| *candidate != entry_id);
        }
        let source_bytes = self
            .entry(entry_id)
            .and_then(TranscriptEntry::tool_card)
            .filter(|card| card.detail_source.is_pending_payload())
            .map_or(0, |card| card.detail_source.retained_bytes());
        if source_bytes > 0 {
            self.pending_detail_sources.insert(entry_id, source_bytes);
            self.pending_detail_order.push_back(entry_id);
            self.pending_detail_source_bytes = self
                .pending_detail_source_bytes
                .saturating_add(source_bytes);
        }

        while self.pending_detail_sources.len() > MAX_PENDING_DETAIL_SOURCES
            || self.pending_detail_source_bytes > MAX_PENDING_DETAIL_SOURCE_BYTES
        {
            let Some(oldest) = self.pending_detail_order.pop_front() else {
                break;
            };
            let Some(removed_bytes) = self.pending_detail_sources.remove(&oldest) else {
                continue;
            };
            self.pending_detail_source_bytes = self
                .pending_detail_source_bytes
                .saturating_sub(removed_bytes);
            {
                let entry = self.entry_mut(oldest);
                let TranscriptEntryKind::Tool(card) = &mut entry.kind else {
                    unreachable!("pending detail source must belong to a tool card")
                };
                card.detail_source =
                    ToolDetailSource::Unavailable(DetailUnavailableReason::RetentionPressure);
            }
            self.bump_card(oldest);
        }
        debug_assert!(self.pending_detail_sources.len() <= MAX_PENDING_DETAIL_SOURCES);
        debug_assert!(self.pending_detail_source_bytes <= MAX_PENDING_DETAIL_SOURCE_BYTES);
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
        if identity_for_display(process_id).len() > MAX_TRACKED_PROCESS_ID_BYTES {
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
                .is_some_and(|card| card.display_state.evictable());
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

    pub(crate) fn mark_history_entries(&mut self, start: usize, durable_entry_id: &str) {
        let group = self.next_history_group;
        self.next_history_group = self
            .next_history_group
            .checked_add(1)
            .expect("history group identifiers exhausted");
        for entry in &mut self.entries[start..] {
            entry.history_group = Some(group);
            if !entry
                .durable_entry_ids
                .iter()
                .any(|id| id == durable_entry_id)
            {
                entry.durable_entry_ids.push(durable_entry_id.to_owned());
            }
            if let Some(card) = entry.tool_card() {
                entry.history_detail_source = Some(card.detail_source.clone());
            }
        }
    }

    pub(crate) fn add_history_origin(
        &mut self,
        entry_id: TranscriptEntryId,
        durable_entry_id: &str,
    ) {
        let entry = self.entry_mut(entry_id);
        if !entry
            .durable_entry_ids
            .iter()
            .any(|id| id == durable_entry_id)
        {
            entry.durable_entry_ids.push(durable_entry_id.to_owned());
        }
    }

    pub(crate) fn mark_history_result_projection(
        &mut self,
        entry_id: TranscriptEntryId,
        truncated: bool,
    ) {
        self.entry_mut(entry_id).history_result_projection_truncated = truncated;
    }

    pub(crate) fn record_history_call(&mut self, entry_id: TranscriptEntryId, call_id: &str) {
        let Some(binding) = self.call_entries.get(call_id).copied() else {
            return;
        };
        if binding.entry_id != entry_id {
            return;
        }
        let entry = self.entry_mut(entry_id);
        if !entry
            .history_calls
            .iter()
            .any(|call| call.call_id == call_id)
        {
            entry.history_calls.push(HistoricalCall {
                call_id: call_id.to_owned(),
                kind: binding.kind,
                sequence: binding.sequence,
            });
        }
    }

    pub(crate) fn resolve_history_call(&mut self, entry_id: TranscriptEntryId, call_id: &str) {
        self.entry_mut(entry_id)
            .history_calls
            .retain(|call| call.call_id != call_id);
    }

    pub(crate) fn record_history_pending_result(
        &mut self,
        entry_id: TranscriptEntryId,
        result: ToolResultInput,
    ) {
        self.entry_mut(entry_id).history_pending_result = Some(Box::new(result));
    }

    pub(crate) fn complete_history_entries(&mut self) {
        for entry in &mut self.entries {
            if entry.history_group.is_some() {
                entry.state = TranscriptEntryState::Complete;
            }
        }
    }

    pub(crate) fn durable_entry_id(&self, entry_id: TranscriptEntryId) -> Option<&str> {
        self.entry(entry_id)
            .and_then(|entry| entry.durable_entry_ids.last())
            .map(String::as_str)
    }

    pub(crate) fn has_live_entries(&self) -> bool {
        self.entries
            .iter()
            .any(|entry| entry.history_group.is_none() && !entry.history_omission)
    }

    pub(crate) fn exact_historical_detail_target(
        &self,
        entry_id: TranscriptEntryId,
    ) -> Option<TranscriptEntryId> {
        let entry = self.entry(entry_id)?;
        entry.tool_card()?;
        let usable_source = matches!(
            entry.history_detail_source,
            Some(
                ToolDetailSource::Edit(_)
                    | ToolDetailSource::Write(_)
                    | ToolDetailSource::Read(_)
                    | ToolDetailSource::Grep(_)
                    | ToolDetailSource::Find
            )
        );
        (usable_source
            && entry.history_result_projection_truncated
            && entry.durable_entry_ids.len() > 1)
            .then_some(entry_id)
    }

    pub(crate) fn historical_durable_entry_ids(&self) -> std::collections::BTreeSet<String> {
        self.entries
            .iter()
            .filter(|entry| entry.history_group.is_some())
            .flat_map(|entry| entry.durable_entry_ids.iter().cloned())
            .collect()
    }

    pub(crate) fn exact_historical_detail(
        &self,
        entry_id: TranscriptEntryId,
        result: &ToolResultInput,
    ) -> Option<ToolDetailPresentation> {
        self.exact_historical_detail_target(entry_id)?;
        let entry = self.entry(entry_id)?;
        let mut card = entry.tool_card()?.clone();
        card.detail_source = entry.history_detail_source.clone()?;
        card.status = ToolStatus::Requested;
        card.apply_result(result);
        let DetailAvailability::LiveRetained(detail) = card.structured_detail else {
            return None;
        };
        Some(detail)
    }

    pub(crate) fn prepend_history_page(&mut self, page: &Transcript) -> bool {
        self.insert_history_page(page, 0, true)
    }

    pub(crate) fn append_history_page(&mut self, page: &Transcript) -> bool {
        let index = self
            .entries
            .iter()
            .rposition(|entry| entry.history_group.is_some())
            .map_or(0, |index| index + 1);
        self.insert_history_page(page, index, false)
    }

    pub(crate) fn replace_history_omission_marker(&mut self, omitted: bool) {
        let marker_index = self.entries.iter().position(|entry| entry.history_omission);
        let marker_count = self
            .entries
            .iter()
            .filter(|entry| entry.history_omission)
            .count();
        let oldest_history_index = self
            .entries
            .iter()
            .position(|entry| entry.history_group.is_some());
        let marker_is_at_oldest_edge = marker_index
            .zip(oldest_history_index)
            .is_some_and(|(marker, oldest)| marker.checked_add(1) == Some(oldest));
        if (!omitted && marker_count == 0)
            || (omitted && marker_count == 1 && marker_is_at_oldest_edge)
        {
            return;
        }
        self.entries.retain(|entry| !entry.history_omission);
        if omitted {
            if let Some(index) = self
                .entries
                .iter()
                .position(|entry| entry.history_group.is_some())
            {
                let id = TranscriptEntryId(self.next_entry_id);
                self.next_entry_id = self
                    .next_entry_id
                    .checked_add(1)
                    .expect("transcript entry identifiers exhausted");
                self.entries.insert(
                    index,
                    TranscriptEntry {
                        id,
                        role: TranscriptRole::Assistant,
                        content: HISTORY_OMISSION_MARKER.into(),
                        state: TranscriptEntryState::Complete,
                        kind: TranscriptEntryKind::Message,
                        revision: 0,
                        layout_epoch: 0,
                        history_group: None,
                        history_omission: true,
                        durable_entry_ids: Vec::new(),
                        history_detail_source: None,
                        history_result_projection_truncated: false,
                        history_calls: Vec::new(),
                        history_pending_result: None,
                    },
                );
            }
        }
        self.rebuild_entry_indexes();
        self.bump_generation();
    }

    #[cfg(test)]
    pub(crate) fn history_omission_count(&self) -> usize {
        self.entries
            .iter()
            .filter(|entry| entry.history_omission)
            .count()
    }

    fn insert_history_page(
        &mut self,
        page: &Transcript,
        index: usize,
        replace_marker: bool,
    ) -> bool {
        if page
            .entries
            .iter()
            .any(|entry| entry.state != TranscriptEntryState::Complete)
        {
            return false;
        }
        let omission_marker = replace_marker
            .then(|| {
                self.entries
                    .iter()
                    .position(|entry| entry.history_omission)
                    .map(|index| self.entries.remove(index))
            })
            .flatten();
        let mut groups = HashMap::new();
        let mut inserted = Vec::with_capacity(page.entries.len());
        for source in page.entries.iter().filter(|entry| !entry.history_omission) {
            let mut entry = source.clone();
            entry.id = TranscriptEntryId(self.next_entry_id);
            self.next_entry_id = self
                .next_entry_id
                .checked_add(1)
                .expect("transcript entry identifiers exhausted");
            if let Some(source_group) = entry.history_group {
                let group = *groups.entry(source_group).or_insert_with(|| {
                    let next = self.next_history_group;
                    self.next_history_group = self
                        .next_history_group
                        .checked_add(1)
                        .expect("history group identifiers exhausted");
                    next
                });
                entry.history_group = Some(group);
            }
            inserted.push(entry);
        }
        if inserted.is_empty() {
            if let Some(marker) = omission_marker {
                self.entries.insert(0, marker);
            }
            if replace_marker {
                self.rebuild_entry_indexes();
                self.bump_generation();
            }
            return true;
        }
        let inserted_ids = inserted
            .iter()
            .map(|entry| entry.id)
            .collect::<HashSet<_>>();
        let index = if replace_marker { 0 } else { index };
        self.entries.splice(index..index, inserted);
        if let Some(marker) = omission_marker {
            self.entries.insert(0, marker);
        }
        self.reconcile_history_boundaries(&inserted_ids);
        self.rebuild_entry_indexes();
        self.index_historical_process_entries();
        self.bump_generation();
        true
    }

    fn reconcile_history_boundaries(&mut self, inserted_ids: &HashSet<TranscriptEntryId>) {
        let pending = self
            .entries
            .iter()
            .enumerate()
            .filter_map(|(index, entry)| {
                entry
                    .history_pending_result
                    .as_ref()
                    .map(|result| (index, result.call_id.clone()))
            })
            .collect::<Vec<_>>();
        let mut removed = 0;
        for (original_result_index, call_id) in pending {
            let result_index = original_result_index.saturating_sub(removed);
            if result_index >= self.entries.len() {
                continue;
            }
            let Some((call_index, call)) = self.entries[..result_index]
                .iter()
                .enumerate()
                .rev()
                .find_map(|(index, entry)| {
                    entry
                        .history_calls
                        .iter()
                        .find(|call| call.call_id == call_id)
                        .cloned()
                        .map(|call| (index, call))
                })
            else {
                continue;
            };
            let result = self.entries[result_index]
                .history_pending_result
                .as_deref()
                .expect("pending result exists")
                .clone();
            let result_card = self.entries[result_index].tool_card().cloned();
            let durable_entry_ids = self.entries[result_index].durable_entry_ids.clone();
            let projection_truncated =
                self.entries[result_index].history_result_projection_truncated;
            let detail_source = self.entries[call_index]
                .history_detail_source
                .clone()
                .unwrap_or(ToolDetailSource::None);
            let reconciled = match (&mut self.entries[call_index].kind, call.kind) {
                (TranscriptEntryKind::Tool(card), ToolBindingKind::Tool) => {
                    let Some(result_card) = result_card.as_ref() else {
                        continue;
                    };
                    card.reconcile_historical_result(&result, result_card, detail_source)
                }
                (TranscriptEntryKind::Process(card), ToolBindingKind::Process(operation)) => {
                    let mut result = result;
                    let process_id = card.process_id.clone();
                    result.process_id = Some(process_id.clone());
                    crate::history::project_historical_process_result(&mut result, &process_id);
                    card.reconcile_historical_result(
                        operation,
                        &result,
                        call.sequence,
                        result_card
                            .as_ref()
                            .is_some_and(|card| card.status == ToolStatus::Denied),
                    )
                }
                (TranscriptEntryKind::Message, _)
                | (TranscriptEntryKind::Tool(_), ToolBindingKind::Process(_))
                | (TranscriptEntryKind::Process(_), ToolBindingKind::Tool) => false,
            };
            if !reconciled {
                continue;
            }
            {
                let entry = &mut self.entries[call_index];
                for durable_entry_id in durable_entry_ids {
                    if !entry
                        .durable_entry_ids
                        .iter()
                        .any(|existing| existing == &durable_entry_id)
                    {
                        entry.durable_entry_ids.push(durable_entry_id);
                    }
                }
                entry.history_result_projection_truncated |= projection_truncated;
                entry
                    .history_calls
                    .retain(|candidate| candidate.call_id != call.call_id);
                Self::bump_revision(entry);
            }
            self.entries.remove(result_index);
            removed += 1;
        }
        self.coalesce_historical_process_cards(inserted_ids);
    }

    fn coalesce_historical_process_cards(&mut self, inserted_ids: &HashSet<TranscriptEntryId>) {
        // ponytail: O(n²) is bounded by 1,200 retained rows; add an index if that limit grows.
        loop {
            let mut process_entries = HashMap::<String, usize>::new();
            let duplicate = self.entries.iter().enumerate().find_map(|(index, entry)| {
                let process_id = entry
                    .history_group
                    .and_then(|_| entry.process_card())?
                    .process_id
                    .clone();
                process_entries
                    .insert(process_id, index)
                    .map(|older_index| (older_index, index))
            });
            let Some((older_index, newer_index)) = duplicate else {
                return;
            };

            let older = self.entries[older_index].clone();
            let newer = self.entries[newer_index].clone();
            let mut merged_card = older
                .process_card()
                .expect("process entry checked above")
                .clone();
            merged_card
                .merge_historical_newer(newer.process_card().expect("process entry checked above"));

            let keep_newer = inserted_ids.contains(&older.id) && !inserted_ids.contains(&newer.id);
            let survivor_index = if keep_newer { newer_index } else { older_index };
            let removed_index = if keep_newer { older_index } else { newer_index };
            let mut merged = self.entries[survivor_index].clone();
            merged.kind = TranscriptEntryKind::Process(merged_card);
            merged.durable_entry_ids = older.durable_entry_ids;
            for durable_entry_id in newer.durable_entry_ids {
                if !merged
                    .durable_entry_ids
                    .iter()
                    .any(|candidate| candidate == &durable_entry_id)
                {
                    merged.durable_entry_ids.push(durable_entry_id);
                }
            }
            merged.history_result_projection_truncated = older.history_result_projection_truncated
                || newer.history_result_projection_truncated;
            merged.history_calls = older.history_calls;
            for call in newer.history_calls {
                if !merged
                    .history_calls
                    .iter()
                    .any(|candidate| candidate.call_id == call.call_id)
                {
                    merged.history_calls.push(call);
                }
            }
            merged.history_pending_result = older
                .history_pending_result
                .or(newer.history_pending_result);
            Self::bump_revision(&mut merged);
            self.entries[survivor_index] = merged;
            self.entries.remove(removed_index);
        }
    }

    #[cfg(test)]
    pub(crate) fn retain_historical_entries(
        &mut self,
        limit: usize,
        evict_newest: bool,
    ) -> Option<Vec<String>> {
        let mut seen = std::collections::BTreeSet::new();
        let durable_entry_order = self
            .entries
            .iter()
            .filter(|entry| entry.history_group.is_some())
            .flat_map(|entry| entry.durable_entry_ids.iter())
            .filter(|entry_id| seen.insert((*entry_id).clone()))
            .cloned()
            .collect::<Vec<_>>();
        self.retain_historical_entries_in_order(limit, evict_newest, &durable_entry_order)
    }

    pub(crate) fn retain_historical_entries_in_order(
        &mut self,
        limit: usize,
        evict_newest: bool,
        durable_entry_order: &[String],
    ) -> Option<Vec<String>> {
        let positions = durable_entry_order
            .iter()
            .enumerate()
            .map(|(index, entry_id)| (entry_id.as_str(), index))
            .collect::<HashMap<_, _>>();
        let mut removed = Vec::new();
        while {
            let historical_entries = self
                .entries
                .iter()
                .filter(|entry| entry.history_group.is_some());
            historical_entries.clone().count() > limit
                || historical_entries
                    .flat_map(|entry| entry.durable_entry_ids.iter())
                    .collect::<std::collections::BTreeSet<_>>()
                    .len()
                    > limit
        } {
            let mut ranges = std::collections::BTreeMap::<u64, (usize, usize)>::new();
            for entry in self
                .entries
                .iter()
                .filter(|entry| entry.history_group.is_some())
            {
                let group = entry.history_group.expect("filtered above");
                for durable_entry_id in &entry.durable_entry_ids {
                    let position = *positions.get(durable_entry_id.as_str())?;
                    ranges
                        .entry(group)
                        .and_modify(|range| {
                            range.0 = range.0.min(position);
                            range.1 = range.1.max(position);
                        })
                        .or_insert((position, position));
                }
            }
            let seed = if evict_newest {
                ranges
                    .iter()
                    .max_by_key(|(group, range)| (range.1, range.0, **group))
            } else {
                ranges
                    .iter()
                    .min_by_key(|(group, range)| (range.0, range.1, **group))
            }?;
            let groups = if evict_newest {
                let mut cutoff = seed.1.0;
                loop {
                    let expanded = ranges
                        .values()
                        .filter(|range| range.1 >= cutoff)
                        .map(|range| range.0)
                        .min()
                        .unwrap_or(cutoff);
                    if expanded == cutoff {
                        break;
                    }
                    cutoff = expanded;
                }
                ranges
                    .iter()
                    .filter_map(|(group, range)| (range.1 >= cutoff).then_some(*group))
                    .collect::<std::collections::BTreeSet<_>>()
            } else {
                let mut cutoff = seed.1.1;
                loop {
                    let expanded = ranges
                        .values()
                        .filter(|range| range.0 <= cutoff)
                        .map(|range| range.1)
                        .max()
                        .unwrap_or(cutoff);
                    if expanded == cutoff {
                        break;
                    }
                    cutoff = expanded;
                }
                ranges
                    .iter()
                    .filter_map(|(group, range)| (range.0 <= cutoff).then_some(*group))
                    .collect::<std::collections::BTreeSet<_>>()
            };
            for entry in self.entries.iter().filter(|entry| {
                entry
                    .history_group
                    .is_some_and(|group| groups.contains(&group))
            }) {
                for durable_entry_id in &entry.durable_entry_ids {
                    if !removed.iter().any(|id| id == durable_entry_id) {
                        removed.push(durable_entry_id.clone());
                    }
                }
            }
            self.entries.retain(|entry| {
                !entry
                    .history_group
                    .is_some_and(|group| groups.contains(&group))
            });
        }
        if !removed.is_empty() {
            self.rebuild_entry_indexes();
            self.bump_generation();
        }
        Some(removed)
    }

    fn index_historical_process_entries(&mut self) {
        let entries = self
            .entries
            .iter()
            .filter(|entry| entry.history_group.is_some())
            .filter_map(|entry| {
                entry
                    .process_card()
                    .map(|card| (card.process_id.clone(), entry.id))
            })
            .collect::<Vec<_>>();
        for (process_id, entry_id) in entries {
            if self.process_entries.contains_key(&process_id)
                || identity_for_display(&process_id).len() > MAX_TRACKED_PROCESS_ID_BYTES
            {
                continue;
            }
            while self.process_entries.len().saturating_add(1) > MAX_PROCESS_INDEX_ENTRIES
                || self.process_index_bytes.saturating_add(process_id.len())
                    > MAX_PROCESS_INDEX_BYTES
            {
                if !self.evict_oldest_terminal_process() {
                    break;
                }
            }
            if self.process_entries.len().saturating_add(1) > MAX_PROCESS_INDEX_ENTRIES
                || self.process_index_bytes.saturating_add(process_id.len())
                    > MAX_PROCESS_INDEX_BYTES
            {
                continue;
            }
            self.process_index_bytes = self.process_index_bytes.saturating_add(process_id.len());
            self.process_order.push_back(process_id.clone());
            self.process_entries.insert(process_id, entry_id);
        }
    }

    fn rebuild_entry_indexes(&mut self) {
        self.entry_indexes.clear();
        self.entry_indexes.extend(
            self.entries
                .iter()
                .enumerate()
                .map(|(index, entry)| (entry.id, index)),
        );
        self.call_entries
            .retain(|_, binding| self.entry_indexes.contains_key(&binding.entry_id));
        self.call_index_bytes = self.call_entries.keys().map(String::len).sum();
        self.resolved_call_order.retain(|call_id| {
            self.call_entries
                .get(call_id)
                .is_some_and(|binding| binding.resolved)
        });
        self.process_entries
            .retain(|_, entry_id| self.entry_indexes.contains_key(entry_id));
        self.process_index_bytes = self.process_entries.keys().map(String::len).sum();
        self.process_order
            .retain(|process_id| self.process_entries.contains_key(process_id));
        self.pending_detail_sources
            .retain(|entry_id, _| self.entry_indexes.contains_key(entry_id));
        self.pending_detail_source_bytes = self.pending_detail_sources.values().sum();
        self.pending_detail_order
            .retain(|entry_id| self.pending_detail_sources.contains_key(entry_id));
        self.pending_response = self
            .pending_response
            .filter(|entry_id| self.entry_indexes.contains_key(entry_id));
        self.active_response = self
            .active_response
            .filter(|(_, entry_id)| self.entry_indexes.contains_key(entry_id));
        if self.active_response.is_none() {
            self.started_turn = None;
        }
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
            history_group: None,
            history_omission: false,
            durable_entry_ids: Vec::new(),
            history_detail_source: None,
            history_result_projection_truncated: false,
            history_calls: Vec::new(),
            history_pending_result: None,
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
            history_group: None,
            history_omission: false,
            durable_entry_ids: Vec::new(),
            history_detail_source: None,
            history_result_projection_truncated: false,
            history_calls: Vec::new(),
            history_pending_result: None,
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
        self.entry_indexes.insert(id, self.entries.len() - 1);
        self.bump_generation();
        id
    }

    pub(crate) fn entry(&self, entry_id: TranscriptEntryId) -> Option<&TranscriptEntry> {
        self.entry_indexes
            .get(&entry_id)
            .and_then(|index| self.entries.get(*index))
    }

    pub(crate) fn entry_before(&self, entry_id: TranscriptEntryId) -> Option<&TranscriptEntry> {
        let index = *self.entry_indexes.get(&entry_id)?;
        self.entries.get(index.checked_sub(1)?)
    }

    pub(crate) fn entry_after(&self, entry_id: TranscriptEntryId) -> Option<&TranscriptEntry> {
        let index = *self.entry_indexes.get(&entry_id)?;
        self.entries.get(index.checked_add(1)?)
    }

    fn entry_mut(&mut self, entry_id: TranscriptEntryId) -> &mut TranscriptEntry {
        let index = *self
            .entry_indexes
            .get(&entry_id)
            .expect("active transcript entry must exist");
        self.entries
            .get_mut(index)
            .expect("active transcript entry index must exist")
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
            detail_source: crate::tool_detail::ToolDetailSource::None,
        }
    }

    fn detailed_call(call_id: &str, name: &str, arguments: Value) -> ToolCallInput {
        ToolCallInput {
            call_id: call_id.into(),
            name: name.into(),
            detail_source: crate::tool_detail::project_tool_detail_source(
                name,
                arguments.as_object().expect("test arguments are objects"),
            ),
            arguments: crate::tool_cards::bounded_tool_arguments(name, &arguments),
        }
    }

    fn result(call_id: &str, output: &str) -> ToolResultInput {
        ToolResultInput {
            call_id: call_id.into(),
            name: "read".into(),
            output: output.into(),
            output_tail: None,
            output_source_bytes: output.len() as u64,
            output_source_lines: crate::tool_cards::logical_line_count(output),
            output_projection_cut_mid_line: false,
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
    fn prepending_and_evicting_history_keeps_surviving_ids_stable() {
        let mut transcript = Transcript::default();
        let retained = transcript.append_prompt("retained".into());
        let mut older = Transcript::default();
        older.append_prompt("older".into());
        older.mark_history_entries(0, "older-entry");

        assert!(transcript.prepend_history_page(&older));
        assert_eq!(transcript.entry(retained).unwrap().content, "retained");
        assert_eq!(transcript.entry_before(retained).unwrap().content, "older");

        assert_eq!(
            transcript.retain_historical_entries(0, false),
            Some(vec!["older-entry".into()])
        );
        assert_eq!(transcript.entry(retained).unwrap().content, "retained");
        assert!(transcript.entry_before(retained).is_none());
    }

    #[test]
    fn historical_retention_is_a_hard_logical_entry_bound() {
        let mut transcript = Transcript::default();
        for index in 0..1_205 {
            let start = transcript.entries().len();
            transcript.append_prompt(format!("message-{index}"));
            transcript.mark_history_entries(start, &format!("entry-{index}"));
        }

        assert_eq!(
            transcript.retain_historical_entries(1_200, false),
            Some((0..5).map(|index| format!("entry-{index}")).collect())
        );
        assert_eq!(transcript.entries().len(), 1_200);
        let retained = transcript.historical_durable_entry_ids();
        assert_eq!(retained.len(), 1_200);
        assert!(!retained.contains("entry-4"));
        assert!(retained.contains("entry-5"));
        assert!(retained.contains("entry-1204"));
    }

    #[test]
    fn historical_retention_bounds_origins_coalesced_into_one_card() {
        let mut transcript = Transcript::default();
        let entry_id = transcript.observe_tool_call(call(
            "poll-0",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        ));
        transcript.mark_history_entries(0, "assistant-0");
        for index in 0..1_200 {
            transcript.add_history_origin(entry_id, &format!("result-{index}"));
        }

        let removed = transcript.retain_historical_entries(1_200, false).unwrap();

        assert_eq!(removed.len(), 1_201);
        assert!(transcript.entries().is_empty());
        assert!(transcript.historical_durable_entry_ids().is_empty());
    }

    #[test]
    fn historical_retention_keeps_durable_origins_contiguous() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("older".into());
        transcript.mark_history_entries(0, "older");
        let start = transcript.entries().len();
        let process = transcript.observe_tool_call(call(
            "poll-0",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        ));
        transcript.mark_history_entries(start, "process-call");
        transcript.add_history_origin(process, "process-result");
        let start = transcript.entries().len();
        transcript.append_prompt("interleaved".into());
        transcript.mark_history_entries(start, "interleaved");
        let start = transcript.entries().len();
        transcript.append_prompt("newer".into());
        transcript.mark_history_entries(start, "newer");
        let order = [
            "older".into(),
            "process-call".into(),
            "interleaved".into(),
            "process-result".into(),
            "newer".into(),
        ];

        let mut tail = transcript.clone();
        tail.retain_historical_entries_in_order(3, true, &order)
            .unwrap();
        assert_eq!(
            tail.historical_durable_entry_ids(),
            ["older".to_owned()].into_iter().collect()
        );

        transcript
            .retain_historical_entries_in_order(3, false, &order)
            .unwrap();
        assert_eq!(
            transcript.historical_durable_entry_ids(),
            ["newer".to_owned()].into_iter().collect()
        );
    }

    #[test]
    fn historical_eviction_prunes_tool_and_process_indexes() {
        let mut transcript = Transcript::default();
        let tool_id = transcript.observe_tool_call(call(
            "tool-1",
            "read",
            serde_json::json!({"path": "old.txt"}),
        ));
        transcript.mark_history_entries(0, "tool-entry");
        assert_eq!(
            transcript.retain_historical_entries(0, false),
            Some(vec!["tool-entry".into()])
        );
        assert!(transcript.entry(tool_id).is_none());
        assert_ne!(
            transcript.observe_tool_call(call(
                "tool-1",
                "read",
                serde_json::json!({"path": "new.txt"}),
            )),
            tool_id
        );

        let start = transcript.entries().len();
        let process_id = transcript.observe_tool_call(call(
            "poll-1",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-1"}),
        ));
        transcript.mark_history_entries(start, "process-entry");
        transcript.retain_historical_entries(0, true).unwrap();
        assert!(transcript.entry(process_id).is_none());
        assert_ne!(
            transcript.observe_tool_call(call(
                "poll-2",
                "bash",
                serde_json::json!({"operation": "poll", "process_id": "process-1"}),
            )),
            process_id
        );
    }

    #[test]
    fn historical_retention_evicts_whole_message_groups() {
        let mut transcript = Transcript::default();
        for index in 0..5 {
            transcript.append_prompt(format!("grouped-{index}"));
        }
        transcript.mark_history_entries(0, "large-group");
        let start = transcript.entries().len();
        transcript.append_prompt("survivor".into());
        transcript.mark_history_entries(start, "survivor");

        assert_eq!(
            transcript.retain_historical_entries(3, false),
            Some(vec!["large-group".into()])
        );
        assert_eq!(transcript.entries().len(), 1);
        assert_eq!(transcript.entries()[0].content, "survivor");
    }

    #[test]
    fn prepending_history_keeps_the_existing_omission_marker_identity() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("current".into());
        transcript.mark_history_entries(0, "current-entry");
        transcript.replace_history_omission_marker(true);
        let marker_id = transcript.entries()[0].id;
        let mut older = Transcript::default();
        older.append_prompt("older".into());
        older.mark_history_entries(0, "older-entry");

        assert!(transcript.prepend_history_page(&older));
        assert_eq!(transcript.entries()[0].id, marker_id);
        assert_eq!(transcript.entries()[1].content, "older");
        assert_eq!(transcript.entries()[2].content, "current");

        transcript.replace_history_omission_marker(false);
        assert_eq!(transcript.entries()[0].content, "older");
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
    fn process_approval_state_is_explicit_and_monotonic() {
        let mut transcript = Transcript::default();
        let request = call(
            "poll-approved",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process"}),
        );
        let card_id = transcript.observe_approval_requested(request.clone());
        let card = transcript.entry(card_id).unwrap().process_card().unwrap();
        assert_eq!(
            card.display_state,
            ProcessDisplayState::PollAwaitingApproval
        );
        assert_eq!(card.display_state.status(), ToolStatus::AwaitingApproval);

        transcript.observe_approval_resolved("poll-approved", true, None);
        assert_eq!(
            transcript
                .entry(card_id)
                .unwrap()
                .process_card()
                .unwrap()
                .display_state,
            ProcessDisplayState::Polling
        );
        transcript.observe_approval_resolved("poll-approved", false, Some("late duplicate"));
        assert_eq!(
            transcript
                .entry(card_id)
                .unwrap()
                .process_card()
                .unwrap()
                .display_state,
            ProcessDisplayState::Polling
        );
        transcript.observe_approval_requested(request);
        assert_eq!(
            transcript
                .entry(card_id)
                .unwrap()
                .process_card()
                .unwrap()
                .display_state,
            ProcessDisplayState::Polling
        );
        let mut completed = result("poll-approved", "done");
        completed.name = "bash".into();
        completed.process_id = Some("process".into());
        completed.process_state = Some("completed".into());
        transcript.observe_tool_result(completed);
        assert_eq!(
            transcript
                .entry(card_id)
                .unwrap()
                .process_card()
                .unwrap()
                .display_state,
            ProcessDisplayState::Completed
        );
    }

    #[test]
    fn structured_detail_survives_approval_and_releases_source_on_result() {
        let mut transcript = Transcript::default();
        let arguments = serde_json::json!({
            "path": "file.txt",
            "edits": [{"oldText": "old\n", "newText": "new\n"}]
        });
        let request = detailed_call("edit-detail", "edit", arguments);
        let card_id = transcript.observe_approval_requested(request.clone());
        assert_eq!(transcript.observe_tool_call(request), card_id);

        let mut completed = result("edit-detail", "Applied 1 edit");
        completed.name = "edit".into();
        transcript.observe_tool_result(completed);

        let card = transcript.entry(card_id).unwrap().tool_card().unwrap();
        assert!(card.has_retained_detail());
        assert_eq!(card.detail_source, ToolDetailSource::None);
        let crate::tool_detail::DetailAvailability::LiveRetained(detail) = &card.structured_detail
        else {
            panic!("structured detail expected");
        };
        assert_eq!((detail.additions, detail.deletions), (1, 1));
    }

    #[test]
    fn failed_or_conflicting_tools_never_show_proposed_detail() {
        let mut failed_transcript = Transcript::default();
        let failed_id = failed_transcript.observe_tool_call(detailed_call(
            "failed-edit",
            "edit",
            serde_json::json!({
                "path": "file.txt",
                "edits": [{"oldText": "old", "newText": "new"}]
            }),
        ));
        let mut failed = result("failed-edit", "edit failed");
        failed.name = "edit".into();
        failed.is_error = true;
        failed_transcript.observe_tool_result(failed);
        let failed_card = failed_transcript
            .entry(failed_id)
            .unwrap()
            .tool_card()
            .unwrap();
        assert_eq!(
            failed_card.structured_detail,
            crate::tool_detail::DetailAvailability::None
        );
        assert_eq!(failed_card.detail_source, ToolDetailSource::None);

        let mut conflict_transcript = Transcript::default();
        let first = detailed_call(
            "conflicting-edit",
            "edit",
            serde_json::json!({
                "path": "file.txt",
                "edits": [{"oldText": "old", "newText": "first"}]
            }),
        );
        let conflict_id = conflict_transcript.observe_tool_call(first);
        conflict_transcript.observe_tool_call(detailed_call(
            "conflicting-edit",
            "edit",
            serde_json::json!({
                "path": "file.txt",
                "edits": [{"oldText": "old", "newText": "second"}]
            }),
        ));
        let conflict = conflict_transcript
            .entry(conflict_id)
            .unwrap()
            .tool_card()
            .unwrap();
        assert_eq!(conflict.status, ToolStatus::Requested);
        assert_eq!(
            conflict.structured_detail,
            crate::tool_detail::DetailAvailability::None
        );
        assert_eq!(
            conflict.detail_source,
            ToolDetailSource::Unavailable(DetailUnavailableReason::ConflictingLifecycle)
        );

        let mut mismatched_transcript = Transcript::default();
        let mismatched_id = mismatched_transcript.observe_tool_call(detailed_call(
            "write-mismatch",
            "write",
            serde_json::json!({"path": "file.txt", "content": "proposed"}),
        ));
        let mut mismatched = result("write-mismatch", "done");
        mismatched.name = "read".into();
        mismatched.created = true;
        mismatched_transcript.observe_tool_result(mismatched);
        let mismatched_card = mismatched_transcript
            .entry(mismatched_id)
            .unwrap()
            .tool_card()
            .unwrap();
        assert_eq!(mismatched_card.status, ToolStatus::Done);
        assert_eq!(
            mismatched_card.structured_detail,
            crate::tool_detail::DetailAvailability::Unavailable(
                DetailUnavailableReason::ConflictingLifecycle
            )
        );
    }

    #[test]
    fn pending_write_detail_retention_is_hard_bounded() {
        let mut transcript = Transcript::default();
        for index in 0..=MAX_PENDING_DETAIL_SOURCES {
            transcript.observe_tool_call(detailed_call(
                &format!("write-{index}"),
                "write",
                serde_json::json!({
                    "path": format!("file-{index}.txt"),
                    "content": "x".repeat(8 * 1024),
                }),
            ));
        }

        let pending = transcript
            .entries()
            .iter()
            .filter_map(TranscriptEntry::tool_card)
            .filter(|card| card.detail_source.is_pending_payload())
            .collect::<Vec<_>>();
        assert!(pending.len() <= MAX_PENDING_DETAIL_SOURCES);
        assert!(
            pending
                .iter()
                .map(|card| card.detail_source.retained_bytes())
                .sum::<usize>()
                <= MAX_PENDING_DETAIL_SOURCE_BYTES
        );
        assert_eq!(transcript.pending_detail_sources.len(), pending.len());
        assert!(transcript.pending_detail_source_bytes <= MAX_PENDING_DETAIL_SOURCE_BYTES);
        let oldest = transcript.entries()[0].tool_card().unwrap();
        assert_eq!(
            oldest.detail_source,
            ToolDetailSource::Unavailable(DetailUnavailableReason::RetentionPressure)
        );

        let mut edit_transcript = Transcript::default();
        for index in 0..10 {
            edit_transcript.observe_tool_call(detailed_call(
                &format!("edit-{index}"),
                "edit",
                serde_json::json!({
                    "path": format!("file-{index}.txt"),
                    "edits": [{
                        "oldText": "o".repeat(60 * 1024),
                        "newText": "n".repeat(60 * 1024),
                    }],
                }),
            ));
        }
        assert!(edit_transcript.pending_detail_source_bytes <= MAX_PENDING_DETAIL_SOURCE_BYTES);
        assert_eq!(
            edit_transcript.entries()[0]
                .tool_card()
                .unwrap()
                .detail_source,
            ToolDetailSource::Unavailable(DetailUnavailableReason::RetentionPressure)
        );
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
    fn process_ids_at_the_raw_tracking_limit_remain_process_cards() {
        let mut transcript = Transcript::default();
        let raw_process_id = "p".repeat(MAX_TRACKED_PROCESS_ID_BYTES);
        let arguments = crate::tool_cards::bounded_tool_arguments(
            "bash",
            &serde_json::json!({"operation": "poll", "process_id": raw_process_id.clone()}),
        );

        let entry_id = transcript.observe_tool_call(call("poll-long", "bash", arguments));
        let card = transcript.entry(entry_id).unwrap().process_card().unwrap();
        assert_eq!(identity_for_display(&card.process_id), raw_process_id);
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

        let mut hint_only_transcript = Transcript::default();
        let hint_card_id = hint_only_transcript.observe_tool_call(call(
            "poll-hint",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "expired-process"}),
        ));
        let mut hint_only = result("poll-hint", "");
        hint_only.name = "bash".into();
        hint_only.is_error = true;
        hint_only.recovery_hint = Some("start the command again".into());
        hint_only_transcript.observe_tool_result(hint_only);

        let hint_card = hint_only_transcript
            .entry(hint_card_id)
            .unwrap()
            .process_card()
            .unwrap();
        assert_eq!(
            hint_card
                .retained_output
                .text
                .matches("start the command again")
                .count(),
            1
        );
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
    fn generic_call_conflicting_with_an_unresolved_process_is_explicit() {
        let mut transcript = Transcript::default();
        let process = transcript.observe_tool_call(call(
            "crossed",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process"}),
        ));

        let conflict = transcript.observe_tool_call(call(
            "crossed",
            "read",
            serde_json::json!({"path": "README.md"}),
        ));
        transcript.observe_tool_result(result("crossed", "must be ignored"));

        assert_eq!(process, conflict);
        let card = transcript.entry(process).unwrap().process_card().unwrap();
        assert_eq!(card.display_state, ProcessDisplayState::PollInterrupted);
        assert!(card.preview().text.contains("metadata changed"));
        assert!(!card.preview().text.contains("must be ignored"));
    }

    #[test]
    fn changed_process_metadata_on_an_unresolved_call_is_explicit() {
        for conflicting_arguments in [
            serde_json::json!({"operation": "poll", "process_id": "other"}),
            serde_json::json!({"operation": "cancel", "process_id": "process"}),
        ] {
            let mut transcript = Transcript::default();
            let process = transcript.observe_tool_call(call(
                "crossed",
                "bash",
                serde_json::json!({"operation": "poll", "process_id": "process"}),
            ));

            assert_eq!(
                transcript.observe_tool_call(call("crossed", "bash", conflicting_arguments)),
                process
            );
            transcript.observe_tool_result(result("crossed", "must be ignored"));

            let card = transcript.entry(process).unwrap().process_card().unwrap();
            assert_eq!(card.display_state, ProcessDisplayState::PollInterrupted);
            assert!(card.preview().text.contains("metadata changed"));
            assert!(!card.preview().text.contains("must be ignored"));
        }
    }

    #[test]
    fn changed_process_metadata_on_approval_stays_interrupted() {
        let mut transcript = Transcript::default();
        let process = transcript.observe_tool_call(call(
            "crossed-approval",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process"}),
        ));

        assert_eq!(
            transcript.observe_approval_requested(call(
                "crossed-approval",
                "bash",
                serde_json::json!({"operation": "cancel", "process_id": "process"}),
            )),
            process
        );

        let card = transcript.entry(process).unwrap().process_card().unwrap();
        assert_eq!(card.display_state, ProcessDisplayState::PollInterrupted);
        assert!(card.preview().text.contains("metadata changed"));
    }

    #[test]
    fn stale_process_metadata_conflict_still_resolves_the_older_binding() {
        let mut transcript = Transcript::default();
        let process = transcript.observe_tool_call(call(
            "older",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process"}),
        ));
        assert_eq!(
            transcript.observe_tool_call(call(
                "newer",
                "bash",
                serde_json::json!({"operation": "poll", "process_id": "process"}),
            )),
            process
        );

        assert_eq!(
            transcript.observe_tool_call(call(
                "older",
                "bash",
                serde_json::json!({"operation": "cancel", "process_id": "process"}),
            )),
            process
        );
        assert!(transcript.call_entries["older"].resolved);
        transcript.observe_tool_result(result("older", "must be ignored"));

        let card = transcript.entry(process).unwrap().process_card().unwrap();
        assert_eq!(card.display_state, ProcessDisplayState::Polling);
        assert!(card.preview().text.is_empty());
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

        let approval_overflow = transcript.observe_approval_requested(call(
            "approval-overflow",
            "read",
            serde_json::json!({}),
        ));
        assert_eq!(transcript.call_entries.len(), MAX_CALL_INDEX_ENTRIES);
        assert_eq!(
            transcript
                .entry(approval_overflow)
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
    fn process_index_evicts_resolved_operation_states() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("denied process operations".into());
        for index in 0..MAX_PROCESS_INDEX_ENTRIES {
            let process_id = format!("process-{index}");
            let call_id = format!("poll-{index}");
            transcript.observe_tool_call(call(
                &call_id,
                "bash",
                serde_json::json!({"operation": "poll", "process_id": process_id}),
            ));
            transcript.observe_approval_resolved(&call_id, false, Some("policy"));
        }

        let overflow = transcript.observe_tool_call(call(
            "poll-overflow",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-overflow"}),
        ));

        assert!(transcript.entry(overflow).unwrap().process_card().is_some());
        assert!(transcript.process_entries.contains_key("process-overflow"));
        assert!(!transcript.process_entries.contains_key("process-0"));
        assert_eq!(transcript.process_entries.len(), MAX_PROCESS_INDEX_ENTRIES);
    }

    #[test]
    fn process_index_evicts_resolved_observed_states() {
        let mut transcript = Transcript::default();
        transcript.append_prompt("observed process operations".into());
        for index in 0..MAX_PROCESS_INDEX_ENTRIES {
            let process_id = format!("process-{index}");
            let call_id = format!("poll-{index}");
            transcript.observe_tool_call(call(
                &call_id,
                "bash",
                serde_json::json!({"operation": "poll", "process_id": process_id}),
            ));
            let mut observed = result(&call_id, "observed");
            observed.name = "bash".into();
            observed.process_id = Some(process_id);
            transcript.observe_tool_result(observed);
        }

        let overflow = transcript.observe_tool_call(call(
            "poll-overflow",
            "bash",
            serde_json::json!({"operation": "poll", "process_id": "process-overflow"}),
        ));

        assert!(transcript.entry(overflow).unwrap().process_card().is_some());
        assert!(transcript.process_entries.contains_key("process-overflow"));
        assert!(!transcript.process_entries.contains_key("process-0"));
        assert_eq!(transcript.process_entries.len(), MAX_PROCESS_INDEX_ENTRIES);
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
