//! Session identities and incremental recovery of evicted live presentation.

use super::*;

#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct LiveHistoryGap {
    pub oldest: String,
    pub newest: String,
    pub cursor: Option<String>,
    pub generation: u64,
    pub has_historical_prefix: bool,
    pub until_start: bool,
    newest_successor: Option<TranscriptEntryId>,
    oldest_sequence: u64,
    newest_sequence: u64,
    insert_before: Option<TranscriptEntryId>,
}

impl Transcript {
    #[cfg(test)]
    pub(super) fn prune_live_origins(&mut self) {
        let retained = self
            .entries
            .iter()
            .flat_map(|entry| entry.live_message_origins.iter().cloned())
            .collect::<HashSet<_>>();
        self.live_origins.retain(|origin, _| {
            retained.contains(origin)
                || self
                    .pending_call_origins
                    .values()
                    .any(|pending| pending == origin)
        });
    }

    pub(crate) fn live_history_gap(&self) -> Option<&LiveHistoryGap> {
        self.live_history_gap.as_ref()
    }

    pub(crate) fn is_live_history_marker(&self, id: TranscriptEntryId) -> bool {
        self.live_history_gap.is_some()
            && self
                .entry(id)
                .is_some_and(|entry| entry.live_retention_omission)
    }

    pub(crate) fn live_gap_successor_origin(&self) -> Option<String> {
        let anchor = self.live_history_gap.as_ref()?.insert_before?;
        let index = self.entries.iter().position(|entry| entry.id == anchor)?;
        self.entries[index..]
            .iter()
            .find_map(|entry| entry.durable_entry_ids.first().cloned())
    }

    pub(crate) fn latest_user_entry(&self) -> Option<TranscriptEntryId> {
        self.entries
            .iter()
            .rev()
            .find(|entry| entry.role == TranscriptRole::User)
            .map(|entry| entry.id)
    }

    pub(crate) fn add_live_origin(&mut self, target: TranscriptEntryId, origin: &str) {
        if !self.live_origins.contains_key(origin) {
            self.live_origin_sequence = self
                .live_origin_sequence
                .checked_add(1)
                .expect("origin sequence exhausted");
            self.live_origins
                .insert(origin.to_owned(), self.live_origin_sequence);
        }
        let entry = self.entry_mut(target);
        if !entry.live_message_origins.iter().any(|id| id == origin) {
            entry.live_message_origins.push(origin.to_owned());
        }
    }

    pub(crate) fn associate_tool_origins(&mut self, origin: &str, calls: &[String]) {
        // One assistant completion owns all of its calls, including calls whose
        // presentation has not been emitted yet. Bound abandoned bindings too.
        self.pending_call_origins.clear();
        for call in calls.iter().take(MAX_CALL_INDEX_ENTRIES) {
            self.pending_call_origins
                .insert(call.clone(), origin.to_owned());
            if let Some(binding) = self.call_entries.get(call) {
                self.attach_tool_origin(binding.entry_id, call);
            }
        }
    }

    pub(crate) fn attach_tool_origin(&mut self, target: TranscriptEntryId, call: &str) {
        if let Some(origin) = self.pending_call_origins.remove(call) {
            self.add_live_origin(target, &origin);
            self.record_history_call(target, call);
        }
    }

    #[cfg(test)]
    pub(super) fn record_live_eviction(&mut self, index: usize) {
        let entry = &self.entries[index];
        let mut origins = entry
            .live_message_origins
            .iter()
            .filter_map(|origin| {
                self.live_origins
                    .get(origin)
                    .map(|sequence| (*sequence, origin.clone()))
            })
            .collect::<Vec<_>>();
        // A historical process can acquire new live output. Its older fragments
        // predate the live sequence, so recover to the durable beginning rather
        // than guessing their position relative to other retained cards.
        let until_start = !entry.durable_entry_ids.is_empty();
        if origins.is_empty() {
            origins.extend(entry.durable_entry_ids.iter().map(|id| (0, id.clone())));
        }
        origins.sort_by_key(|(sequence, _)| *sequence);
        let (Some((oldest_sequence, oldest)), Some((newest_sequence, newest))) =
            (origins.first(), origins.last())
        else {
            return;
        };
        let successor = self.entries.get(index + 1).map(|entry| entry.id);
        match &mut self.live_history_gap {
            Some(gap) => {
                gap.generation = gap.generation.wrapping_add(1);
                gap.until_start |= until_start;
                if gap.newest_successor == Some(entry.id) {
                    gap.newest_successor = successor;
                }
                if *oldest_sequence < gap.oldest_sequence {
                    gap.oldest_sequence = *oldest_sequence;
                    gap.oldest = oldest.clone();
                }
                if *newest_sequence >= gap.newest_sequence {
                    gap.newest_sequence = *newest_sequence;
                    gap.newest = newest.clone();
                    gap.newest_successor = successor;
                }
                // Even an interior protected row may disappear behind the cursor.
                // Restart from the newest boundary; identity merging is idempotent.
                gap.cursor = None;
                gap.insert_before = gap.newest_successor;
            }
            None => {
                self.live_history_gap = Some(LiveHistoryGap {
                    oldest: oldest.clone(),
                    newest: newest.clone(),
                    cursor: None,
                    generation: 1,
                    has_historical_prefix: self.entries[..index]
                        .iter()
                        .any(|entry| entry.history_group.is_some()),
                    until_start,
                    newest_successor: successor,
                    oldest_sequence: *oldest_sequence,
                    newest_sequence: *newest_sequence,
                    insert_before: successor,
                })
            }
        }
    }

    /// Reproject only missing process fragments when a page overlaps a retained
    /// grouped card. A merged card cannot subtract an already displayed poll.
    pub(crate) fn prepare_live_recovery_page(
        &self,
        page: &Transcript,
        messages: &[serde_json::Value],
    ) -> Result<Transcript, crate::history::HistoryProjectionError> {
        let mut prepared = page.clone();
        let mut entries = Vec::new();
        for source in &page.entries {
            let Some(process) = source.process_card() else {
                entries.push(source.clone());
                continue;
            };
            let Some(existing) = self.entries.iter().find(|entry| {
                entry
                    .process_card()
                    .is_some_and(|card| card.process_id == process.process_id)
            }) else {
                entries.push(source.clone());
                continue;
            };
            let missing = source
                .durable_entry_ids
                .iter()
                .filter(|id| {
                    !existing.durable_entry_ids.contains(id)
                        && !existing.live_message_origins.contains(id)
                })
                .collect::<HashSet<_>>();
            if missing.is_empty() || missing.len() == source.durable_entry_ids.len() {
                entries.push(source.clone());
                continue;
            }
            let mut fragments = Vec::new();
            for message in messages {
                if !message["entry_id"]
                    .as_str()
                    .is_some_and(|id| missing.iter().any(|origin| origin.as_str() == id))
                {
                    continue;
                }
                let mut message = message.clone();
                if message["role"] == "assistant" {
                    // A shared assistant message may also own text and calls for
                    // other cards; those keep their ordinary source projection.
                    message["content"] = serde_json::json!("");
                    if let Some(calls) = message["tool_calls"].as_array_mut() {
                        calls.retain(|call| {
                            crate::tool_cards::process_call_identity(
                                call["name"].as_str().unwrap_or(""),
                                &crate::tool_cards::bounded_tool_arguments(
                                    "bash",
                                    &call["arguments"],
                                ),
                            )
                            .is_some_and(|identity| identity.process_id == process.process_id)
                        });
                        if calls.is_empty() {
                            continue;
                        }
                        let count = calls.len();
                        message["tool_calls_original_count"] = serde_json::json!(count);
                    }
                }
                fragments.push(message);
            }
            if fragments.is_empty() {
                return Err(crate::history::HistoryProjectionError::InvalidField {
                    index: 0,
                    field: "recovery process fragments",
                });
            }
            let fragment =
                crate::history::project_rpc_message_page_with_origins(&fragments, false)?;
            entries.extend(fragment.transcript.entries().iter().cloned());
        }
        for (index, entry) in entries.iter_mut().enumerate() {
            entry.id = TranscriptEntryId(index as u64);
        }
        prepared.entries = entries;
        prepared.rebuild_entry_indexes();
        Ok(prepared)
    }

    /// Insert a durable page at the gap while preserving every surviving local ID.
    ///
    /// Message origins distinguish identical text. Cards additionally distinguish
    /// individual calls within one assistant message; process identity preserves
    /// the single live owner of grouped Bash updates.
    pub(crate) fn recover_live_page(
        &mut self,
        page: &Transcript,
        origins: &[String],
        next_cursor: Option<String>,
    ) -> Option<(TranscriptEntryId, TranscriptEntryId)> {
        let gap = self.live_history_gap.as_ref()?.clone();
        let marker = self
            .entries
            .iter()
            .position(|entry| entry.live_retention_omission);
        let old_anchor = marker
            .and_then(|index| Some((self.entries[index].id, self.entries.get(index + 1)?.id)));
        let mut before = gap.insert_before.filter(|id| self.entry(*id).is_some());
        let mut inserted_ids = HashSet::new();
        let mut groups = HashMap::new();
        for source in page.entries.iter().rev() {
            let matching = self
                .entries
                .iter()
                .position(|entry| same_recovery_entry(entry, source));
            let merge_process = matching.is_some_and(|index| {
                matches!(
                    (&self.entries[index].kind, &source.kind),
                    (
                        TranscriptEntryKind::Process(_),
                        TranscriptEntryKind::Process(_)
                    )
                ) && source.durable_entry_ids.iter().any(|id| {
                    !self.entries[index].durable_entry_ids.contains(id)
                        && !self.entries[index].live_message_origins.contains(id)
                })
            });
            if merge_process {
                before = matching.map(|index| self.entries[index].id);
            }
            let pending_result = matching.is_some_and(|index| {
                source
                    .history_pending_result
                    .as_ref()
                    .is_some_and(|result| {
                        self.entries[index]
                            .history_calls
                            .iter()
                            .any(|call| call.call_id == result.call_id)
                    })
            });
            if pending_result {
                before =
                    matching.and_then(|index| self.entries.get(index + 1).map(|entry| entry.id));
            }
            // A result crossing the page boundary still has to update its call.
            // Inserting the fragment lets the normal reconciler apply it once.
            let existing = matching.filter(|_| !merge_process && !pending_result);
            if let Some(index) = existing {
                let target = self.entries[index].id;
                // An exact result page can arrive before its call page. Enrich
                // that existing row through the normal boundary reconciler,
                // then transplant the result without changing its local ID.
                if self.entries[index].history_pending_result.is_some()
                    && !source.history_calls.is_empty()
                {
                    let mut pair = Transcript::default();
                    let mut call = source.clone();
                    call.id = TranscriptEntryId(0);
                    let mut result = self.entries[index].clone();
                    result.id = TranscriptEntryId(1);
                    pair.entries = vec![call, result];
                    pair.reconcile_history_boundaries(&HashSet::from([TranscriptEntryId(0)]));
                    if pair.entries.len() == 1 {
                        let mut enriched = pair.entries.remove(0);
                        enriched.id = target;
                        enriched.history_group = self.entries[index].history_group;
                        enriched.layout_epoch = self.entries[index].layout_epoch.saturating_add(1);
                        enriched.revision = self.entries[index].revision.saturating_add(1);
                        self.entries[index] = enriched;
                    }
                }
                for origin in &source.durable_entry_ids {
                    if !self.entries[index].durable_entry_ids.contains(origin)
                        && !self.entries[index].live_message_origins.contains(origin)
                    {
                        self.entries[index].durable_entry_ids.push(origin.clone());
                    }
                }
                before = Some(target);
                continue;
            }
            let mut entry = source.clone();
            entry.id = TranscriptEntryId(self.next_entry_id);
            self.next_entry_id = self
                .next_entry_id
                .checked_add(1)
                .expect("transcript identifiers exhausted");
            entry.history_group = Some(*groups.entry(source.history_group).or_insert_with(|| {
                let group = self.next_history_group;
                self.next_history_group += 1;
                group
            }));
            let index = before
                .and_then(|id| self.entries.iter().position(|entry| entry.id == id))
                .unwrap_or(self.entries.len());
            before = Some(entry.id);
            inserted_ids.insert(entry.id);
            self.entries.insert(index, entry);
        }
        self.reconcile_history_boundaries(&inserted_ids);
        self.rebuild_entry_indexes();
        self.index_historical_process_entries();
        before = page
            .entries
            .first()
            .and_then(|source| {
                self.entries
                    .iter()
                    .find(|entry| {
                        same_recovery_entry(entry, source)
                            || entry
                                .durable_entry_ids
                                .iter()
                                .any(|id| source.durable_entry_ids.contains(id))
                    })
                    .map(|entry| entry.id)
            })
            .or_else(|| before.filter(|id| self.entry(*id).is_some()));
        let complete = if gap.until_start {
            gap.cursor.is_some() && next_cursor.is_none()
        } else {
            origins.contains(&gap.oldest)
        };
        if complete {
            self.live_history_gap = None;
            self.entries.retain(|entry| !entry.live_retention_omission);
        } else if let Some(gap) = &mut self.live_history_gap {
            gap.cursor = next_cursor;
            gap.insert_before = before;
            if let Some(index) = self
                .entries
                .iter()
                .position(|entry| entry.live_retention_omission)
            {
                let marker = self.entries.remove(index);
                let index = before
                    .and_then(|id| self.entries.iter().position(|entry| entry.id == id))
                    .unwrap_or(self.entries.len());
                self.entries.insert(index, marker);
            }
        }
        self.rebuild_entry_indexes();
        self.bump_generation();
        old_anchor.filter(|(_, target)| self.entry(*target).is_some())
    }
}

fn same_recovery_entry(live: &TranscriptEntry, source: &TranscriptEntry) -> bool {
    if let Some(result) = &live.history_pending_result {
        if source
            .history_calls
            .iter()
            .any(|call| call.call_id == result.call_id)
        {
            return true;
        }
    }
    if source.history_pending_result.is_some()
        && source
            .durable_entry_ids
            .iter()
            .any(|id| live.live_message_origins.contains(id))
    {
        return true;
    }
    if let Some(result) = &source.history_pending_result {
        if live
            .history_calls
            .iter()
            .any(|call| call.call_id == result.call_id)
            && live.state == TranscriptEntryState::Complete
        {
            return true;
        }
    }
    match (&live.kind, &source.kind) {
        (TranscriptEntryKind::Message, TranscriptEntryKind::Message) => {
            live.role == source.role
                && !live.live_retention_omission
                && live
                    .durable_entry_ids
                    .iter()
                    .chain(&live.live_message_origins)
                    .any(|id| source.durable_entry_ids.contains(id))
        }
        (TranscriptEntryKind::Tool(left), TranscriptEntryKind::Tool(right)) => {
            left.call_id == right.call_id
        }
        (TranscriptEntryKind::Process(left), TranscriptEntryKind::Process(right)) => {
            left.process_id == right.process_id
        }
        _ => false,
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn messages(count: usize) -> (Transcript, Vec<TranscriptEntryId>) {
        let mut transcript = Transcript::default();
        let ids = (0..count)
            .map(|index| {
                let id = transcript.append_prompt("identical message".into());
                transcript.add_live_origin(id, &format!("message-{index}"));
                id
            })
            .collect();
        (transcript, ids)
    }

    fn page(range: std::ops::Range<usize>) -> (Transcript, Vec<String>) {
        let mut transcript = Transcript::default();
        let mut origins = Vec::new();
        for index in range {
            let start = transcript.entries.len();
            transcript.append_prompt("identical message".into());
            let origin = format!("message-{index}");
            transcript.mark_history_entries(start, &origin);
            origins.push(origin);
        }
        (transcript, origins)
    }

    #[test]
    fn recovery_distinguishes_repeated_text_and_preserves_protected_rows() {
        let (mut transcript, ids) = messages(LIVE_TRANSCRIPT_ENTRY_LIMIT + 3);
        assert!(transcript.enforce_live_retention(Some(ids[1])));
        let gap = transcript.live_history_gap().unwrap();
        assert_eq!(gap.oldest, "message-0");
        assert_eq!(gap.newest, "message-3");
        let (latest, origins) = page(3..4);
        transcript.recover_live_page(&latest, &origins, Some("message-3".into()));
        let recovered = transcript
            .entries
            .iter()
            .find(|entry| entry.durable_entry_ids == ["message-3"])
            .unwrap()
            .id;
        let (older, origins) = page(0..3);
        transcript.recover_live_page(&older, &origins, None);
        assert!(transcript.live_history_gap().is_none());
        assert_eq!(transcript.entries.len(), LIVE_TRANSCRIPT_ENTRY_LIMIT + 3);
        assert_eq!(transcript.entries[1].id, ids[1]);
        assert_eq!(transcript.entries[3].id, recovered);
        assert_eq!(transcript.entries[4].id, ids[4]);
        assert_eq!(transcript.entries[0].durable_entry_ids, ["message-0"]);
        assert_eq!(transcript.entries[2].durable_entry_ids, ["message-2"]);
        assert!(!transcript.enforce_live_retention(None));
    }

    #[test]
    fn recovery_restarts_when_a_protected_row_disappears_behind_its_cursor() {
        let (mut transcript, ids) = messages(LIVE_TRANSCRIPT_ENTRY_LIMIT + 10);
        transcript.enforce_live_retention(Some(ids[5]));
        let (latest, origins) = page(10..11);
        transcript.recover_live_page(&latest, &origins, Some("message-10".into()));
        let (older, origins) = page(4..10);
        transcript.recover_live_page(&older, &origins, Some("message-4".into()));
        let previous = transcript.live_history_gap().unwrap().clone();
        let target = transcript.append_prompt("new live message".into());
        transcript.add_live_origin(target, "message-new");
        transcript.enforce_live_retention(None);
        let gap = transcript.live_history_gap().unwrap();
        assert!(gap.generation > previous.generation);
        assert!(gap.cursor.is_none());
        assert_eq!(gap.newest, "message-10");
        let (latest, origins) = page(10..11);
        transcript.recover_live_page(&latest, &origins, Some("message-10".into()));
        let (older, origins) = page(0..10);
        transcript.recover_live_page(&older, &origins, None);
        assert!(transcript.live_history_gap().is_none());
        for index in 0..11 {
            assert_eq!(
                transcript.entries[index].durable_entry_ids,
                [format!("message-{index}")]
            );
        }
    }

    #[test]
    fn sequential_tool_results_keep_each_assistant_origin() {
        use crate::reducer::{BackendEvent, UiAction, UiState, reduce};
        use serde_json::json;
        let mut state = UiState::new("fake".into(), None, None);
        let mut ids = crate::SequentialCommandIds::default();
        for event in [
            json!({"type":"message.started", "turn":1}),
            json!({"type":"message.completed", "turn":1, "content":"", "message_entry_id":"assistant", "tool_calls":[{"call_id":"first"},{"call_id":"second"}]}),
            json!({"type":"tool.call", "call_id":"first", "name":"read", "arguments":{}}),
            json!({"type":"tool.execution.ended", "call_id":"first", "name":"read", "output":"one", "is_error":false, "message_entry_id":"result-1"}),
            json!({"type":"tool.result", "call_id":"first", "name":"read", "output":"one", "is_error":false}),
            json!({"type":"tool.call", "call_id":"second", "name":"read", "arguments":{}}),
            json!({"type":"tool.execution.ended", "call_id":"second", "name":"read", "output":"two", "is_error":false, "message_entry_id":"result-2"}),
            json!({"type":"tool.result", "call_id":"second", "name":"read", "output":"two", "is_error":false}),
        ] {
            let event = BackendEvent::from_projection_value(&event).unwrap();
            reduce(&mut state, UiAction::BackendEvent(event), &mut ids).unwrap();
        }
        for (call, result) in [("first", "result-1"), ("second", "result-2")] {
            let entry = state
                .transcript
                .entries()
                .iter()
                .find(|entry| entry.tool_card().is_some_and(|card| card.call_id == call))
                .unwrap();
            assert_eq!(entry.live_message_origins, ["assistant", result]);
        }
    }

    #[test]
    fn recovery_keeps_searching_for_inherited_historical_origins() {
        let (mut transcript, ids) = messages(LIVE_TRANSCRIPT_ENTRY_LIMIT + 1);
        transcript
            .entry_mut(ids[0])
            .durable_entry_ids
            .push("old-fragment".into());
        transcript.enforce_live_retention(None);
        let (latest, origins) = page(0..1);
        transcript.recover_live_page(&latest, &origins, Some("message-0".into()));
        assert!(transcript.live_history_gap().is_some());
        let mut older = Transcript::default();
        older.append_prompt("older fragment".into());
        older.mark_history_entries(0, "old-fragment");
        transcript.recover_live_page(
            &older,
            &["old-fragment".into()],
            Some("old-fragment".into()),
        );
        assert!(transcript.live_history_gap().is_some());
        transcript.recover_live_page(&Transcript::default(), &[], None);
        assert!(transcript.live_history_gap().is_none());
        assert_eq!(transcript.entries[0].durable_entry_ids, ["old-fragment"]);
    }

    #[test]
    fn byte_eviction_recovers_without_copying_the_large_live_payload() {
        let mut transcript = Transcript::default();
        let first = transcript.append_prompt("x".repeat(LIVE_TRANSCRIPT_BYTE_LIMIT + 1));
        transcript.add_live_origin(first, "large");
        let next = transcript.append_prompt("surviving draft echo".into());
        transcript.add_live_origin(next, "next");
        assert!(transcript.enforce_live_retention(None));
        let mut page = Transcript::default();
        page.append_prompt("bounded durable preview".into());
        page.mark_history_entries(0, "large");
        transcript.recover_live_page(&page, &["large".into()], None);
        assert_eq!(transcript.entries.len(), 2);
        assert_eq!(transcript.entries[0].content, "bounded durable preview");
        assert_eq!(transcript.entries[1].id, next);
    }

    #[test]
    fn recovery_keeps_the_existing_historical_prefix_in_order() {
        let (mut transcript, _) = messages(LIVE_TRANSCRIPT_ENTRY_LIMIT + 2);
        let (prefix, _) = page(10_000..10_002);
        transcript.prepend_history_page(&prefix);
        let prefix_ids = [transcript.entries[0].id, transcript.entries[1].id];
        transcript.enforce_live_retention(None);
        assert!(transcript.live_history_gap().unwrap().has_historical_prefix);
        let (latest, origins) = page(1..2);
        transcript.recover_live_page(&latest, &origins, Some("message-1".into()));
        let (older, origins) = page(0..1);
        transcript.recover_live_page(&older, &origins, None);
        assert_eq!(transcript.entries[0].id, prefix_ids[0]);
        assert_eq!(transcript.entries[1].id, prefix_ids[1]);
        assert_eq!(transcript.entries[2].durable_entry_ids, ["message-0"]);
        assert_eq!(transcript.entries[3].durable_entry_ids, ["message-1"]);
    }

    #[test]
    fn origin_metadata_counts_toward_retention_even_in_one_row() {
        let mut transcript = Transcript::default();
        let target = transcript.append_prompt("one grouped entry".into());
        for index in 0..=LIVE_TRANSCRIPT_ENTRY_LIMIT {
            transcript.add_live_origin(target, &format!("origin-{index}"));
        }
        assert!(transcript.enforce_live_retention(None));
        assert!(transcript.entry(target).is_none());
        assert!(transcript.live_origins.is_empty());
        assert_eq!(transcript.live_history_gap().unwrap().oldest, "origin-0");
    }

    #[test]
    fn recovery_does_not_repeat_partially_overlapping_process_output() {
        use crate::history::project_rpc_message_page_with_origins;
        use serde_json::json;
        let mut messages = Vec::new();
        for label in ["a", "b", "c"] {
            messages.push(json!({"entry_id":format!("call-{label}"), "role":"assistant", "content":"", "content_truncated":false,
                "tool_calls":[{"call_id":label, "name":"bash", "arguments":{"operation":"poll", "process_id":"process-1"}}]}));
            messages.push(json!({"entry_id":format!("result-{label}"), "role":"tool", "content":format!("Process process-1 is still running\nstdout:\noutput-{label}"), "content_truncated":false,
                "tool_call_id":label, "tool_name":"bash", "tool_result":{"status":"done"}}));
        }
        for (retained_end, fetched_end) in [(6, 4), (5, 6)] {
            let retained =
                project_rpc_message_page_with_origins(&messages[2..retained_end], false).unwrap();
            let mut transcript = (*retained.transcript).clone();
            let survivor = transcript.entries[0].id;
            transcript.live_history_gap = Some(LiveHistoryGap {
                oldest: "call-a".into(),
                newest: "result-b".into(),
                cursor: Some("call-c".into()),
                generation: 1,
                has_historical_prefix: false,
                until_start: false,
                oldest_sequence: 1,
                newest_sequence: 2,
                insert_before: Some(survivor),
                newest_successor: Some(survivor),
            });
            let source =
                project_rpc_message_page_with_origins(&messages[..fetched_end], false).unwrap();
            let page = transcript
                .prepare_live_recovery_page(&source.transcript, &messages[..fetched_end])
                .unwrap();
            transcript.recover_live_page(&page, &source.durable_entry_ids, None);
            assert_eq!(transcript.entries.len(), 1, "{:?}", transcript.entries);
            assert_eq!(transcript.entries[0].id, survivor);
            let card = transcript.entries[0].process_card().unwrap();
            assert_eq!(card.poll_count, 3);
            for label in ["a", "b", "c"] {
                assert_eq!(
                    card.retained_output
                        .text
                        .matches(&format!("output-{label}"))
                        .count(),
                    1
                );
            }
        }
    }

    #[test]
    fn recovery_merges_process_fragments_and_enriches_result_only_anchors() {
        use crate::history::project_rpc_message_page_with_origins;
        use serde_json::json;
        let messages = [
            json!({"entry_id":"call-old", "role":"assistant", "content":"", "content_truncated":false,
                "tool_calls":[{"call_id":"poll-old", "name":"bash", "arguments":{"operation":"poll", "process_id":"process-1"}}]}),
            json!({"entry_id":"result-old", "role":"tool", "content":"Process process-1 is still running\nstdout:\nolder output", "content_truncated":false,
                "tool_call_id":"poll-old", "tool_name":"bash", "tool_result":{"status":"done"}}),
            json!({"entry_id":"call-new", "role":"assistant", "content":"", "content_truncated":false,
                "tool_calls":[{"call_id":"poll-new", "name":"bash", "arguments":{"operation":"poll", "process_id":"process-1"}}]}),
            json!({"entry_id":"result-new", "role":"tool", "content":"Process process-1 completed with exit code 0\nstdout:\nnewer output", "content_truncated":false,
                "tool_call_id":"poll-new", "tool_name":"bash", "tool_result":{"status":"done"}}),
        ];
        for boundary in [2, 3] {
            let all = project_rpc_message_page_with_origins(&messages, false).unwrap();
            let mut transcript = (*all.transcript).clone();
            let target = transcript.entries[0].id;
            transcript.entries[0].history_group = None;
            transcript.entries[0].durable_entry_ids.clear();
            for origin in &all.durable_entry_ids {
                transcript.add_live_origin(target, origin);
            }
            for index in 0..LIVE_TRANSCRIPT_ENTRY_LIMIT {
                transcript.append_prompt(format!("tail {index}"));
            }
            transcript.enforce_live_retention(None);
            let latest =
                project_rpc_message_page_with_origins(&messages[boundary..], false).unwrap();
            transcript.recover_live_page(
                &latest.transcript,
                &latest.durable_entry_ids,
                Some(latest.durable_entry_ids[0].clone()),
            );
            let anchor = transcript
                .entries
                .iter()
                .find(|entry| !matches!(entry.kind, TranscriptEntryKind::Message))
                .unwrap()
                .id;
            let older =
                project_rpc_message_page_with_origins(&messages[..boundary], false).unwrap();
            transcript.recover_live_page(&older.transcript, &older.durable_entry_ids, None);
            let entries = transcript
                .entries
                .iter()
                .filter(|entry| entry.process_card().is_some())
                .collect::<Vec<_>>();
            assert_eq!(entries.len(), 1);
            assert_eq!(entries[0].id, anchor);
            let card = entries[0].process_card().unwrap();
            assert_eq!(card.poll_count, 2);
            assert!(
                card.retained_output.text.find("older output").unwrap()
                    < card.retained_output.text.find("newer output").unwrap()
            );
            assert!(transcript.live_history_gap().is_none());
        }
    }
}
