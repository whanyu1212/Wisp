//! Incremental literal find over the current session's projected messages and tool output.

use ratatui::{
    Frame,
    layout::Rect,
    style::Style,
    text::{Line, Span},
    widgets::{Block, Paragraph, Wrap},
};

use crate::{
    theme::Palette,
    transcript::{Transcript, TranscriptEntry, TranscriptEntryId, TranscriptEntryKind},
    transcript_view::{CardPosition, CardSection, RowAnchor, RowPosition},
    ui::sanitize_for_terminal,
};

use crate::{
    Error, Input, LiveUi, LoopControl, UiAction, UiEffect, WriterMessage, is_ctrl_c, printable_char,
};
use crossterm::event::KeyCode;
use tokio::sync::mpsc;

impl LiveUi {
    pub(super) fn find_history_action(&self) -> Option<UiAction> {
        if self.state.transcript.live_history_gap().is_some() {
            Some(UiAction::RecoverLiveHistory)
        } else if self.state.history.oldest_cursor.is_some() {
            Some(UiAction::LoadOlderHistory)
        } else if self.state.history.tail_evicted {
            Some(UiAction::LoadNewerHistory)
        } else {
            None
        }
    }

    pub(super) fn next_find_page(&mut self, limit: usize) -> Result<Vec<UiEffect>, Error> {
        let Some(action) = self.find_history_action() else {
            return Ok(Vec::new());
        };
        if let Some(notice) =
            self.reduced_action_frame_limit_notice(&action, "history request", limit)?
        {
            return Ok(vec![UiEffect::Notice(notice)]);
        }
        Ok(crate::reducer::reduce(
            &mut self.state,
            action,
            &mut self.ids,
        )?)
    }

    pub(super) async fn handle_find_input(
        &mut self,
        input: Input,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        let find = self.transcript_find.as_mut().expect("active find");
        match input {
            Input::Paste(text) => find.append(&text),
            Input::Key(key) if key.code == KeyCode::Esc || is_ctrl_c(key) => {
                self.transcript_find = None
            }
            Input::Key(key) if key.code == KeyCode::Enter => {
                if let Some(anchor) = find.anchor(&self.state.transcript) {
                    self.transcript_viewport.reveal_match(
                        &self.state.transcript,
                        &mut self.transcript_row_cache,
                        anchor,
                    );
                    if let Some(hit) = find.selected
                        && let Some(card) = self
                            .state
                            .transcript
                            .entry(hit.entry)
                            .and_then(TranscriptEntry::tool_card)
                        && let crate::tool_detail::DetailAvailability::LiveRetained(detail) =
                            &card.structured_detail
                    {
                        self.detail_view
                            .open_match(hit.entry, detail, hit.part, hit.offset);
                    }
                    self.transcript_find = None;
                }
            }
            Input::Key(key) if matches!(key.code, KeyCode::Up | KeyCode::Down) => {
                find.step(&self.state.transcript, key.code == KeyCode::Up)
            }
            Input::Key(key) if key.code == KeyCode::Backspace => find.backspace(),
            Input::Key(key) => {
                if let Some(ch) = printable_char(key) {
                    find.append(&ch.to_string());
                }
            }
            _ => {}
        }
        self.render_pending = true;
        if self
            .transcript_find
            .as_ref()
            .is_some_and(TranscriptFind::has_query)
            && self.state.history_request_direction().is_none()
        {
            let effects = self.next_find_page(limit)?;
            return self.apply_effects(effects, writer, limit).await;
        }
        Ok(LoopControl::Continue)
    }
}

#[derive(Debug, Clone, Copy, PartialEq, Eq)]
struct Hit {
    entry: TranscriptEntryId,
    part: usize,
    offset: usize,
}

/// Structured tools (read, grep, edits) retain rows instead of raw output.
fn source(entry: &TranscriptEntry, part: usize) -> Option<(&str, &str, Option<u64>)> {
    match &entry.kind {
        TranscriptEntryKind::Message if part == 0 => Some((
            match entry.role {
                crate::transcript::TranscriptRole::User => "Prompt",
                crate::transcript::TranscriptRole::Assistant => "Reply",
                crate::transcript::TranscriptRole::Tool => "Tool",
            },
            entry.display_content(),
            None,
        )),
        TranscriptEntryKind::Tool(card) => {
            if let crate::tool_detail::DetailAvailability::LiveRetained(detail) =
                &card.structured_detail
            {
                return detail
                    .rows
                    .get(part)
                    .map(|row| (card.name.as_str(), row.text.as_str(), None));
            }
            (part == 0).then_some((
                &card.name,
                &card.retained_output.text,
                Some(card.retained_output.base_offset),
            ))
        }
        TranscriptEntryKind::Process(card) if part == 0 => Some((
            "Process",
            &card.retained_output.text,
            Some(card.retained_output.base_offset),
        )),
        _ => None,
    }
}

fn occurrences<'a>(entry: &'a TranscriptEntry, query: &'a str) -> impl Iterator<Item = Hit> + 'a {
    (0..)
        .map_while(move |part| source(entry, part).map(|(_, text, _)| (part, text)))
        .flat_map(move |(part, text)| {
            text.match_indices(query).map(move |(offset, _)| Hit {
                entry: entry.id,
                part,
                offset,
            })
        })
}

#[derive(Default)]
pub(super) struct TranscriptFind {
    query: String,
    selected: Option<Hit>,
    ordinal: usize,
    total: usize,
    generation: Option<u64>,
    counts: std::collections::HashMap<TranscriptEntryId, (u64, usize)>,
}

impl TranscriptFind {
    pub fn append(&mut self, text: &str) {
        for ch in text.chars().filter(|c| !c.is_control()).take(256) {
            if self.query.len() + ch.len_utf8() > 256 {
                break;
            }
            self.query.push(ch);
        }
        self.reset();
    }

    pub fn backspace(&mut self) {
        self.query.pop();
        self.reset();
    }

    fn reset(&mut self) {
        self.counts.clear();
        self.selected = None;
        self.generation = None;
    }

    pub fn has_query(&self) -> bool {
        !self.query.is_empty()
    }

    /// Keep only one identity, even if a common phrase occurs millions of times.
    /// Preserve it when older pages are prepended or a live reply grows.
    fn refresh(&mut self, transcript: &Transcript, target: Option<usize>) {
        if self.generation == Some(transcript.generation()) && target.is_none() {
            return;
        }
        self.generation = Some(transcript.generation());
        let previous = self.selected.take();
        self.total = 0;
        self.ordinal = 0;
        if self.query.is_empty() {
            return;
        }
        for entry in transcript.entries() {
            let revision = entry.revision();
            let count = match self.counts.get(&entry.id) {
                Some(&(cached, count)) if cached == revision => count,
                _ => {
                    let count = occurrences(entry, &self.query).count();
                    self.counts.insert(entry.id, (revision, count));
                    count
                }
            };
            let start = self.total;
            self.total += count;
            if count == 0 {
                continue;
            }
            let hits = || occurrences(entry, &self.query).enumerate();
            let candidate = if let Some(index) = target.filter(|i| (start..self.total).contains(i))
            {
                hits().nth(index - start)
            } else if target.is_none() && previous.is_some_and(|hit| hit.entry == entry.id) {
                hits().find(|(_, hit)| Some(*hit) == previous)
            } else {
                None
            };
            let fallback = || self.selected.is_none().then(|| hits().next()).flatten();
            if let Some((index, hit)) = candidate.or_else(fallback) {
                self.selected = Some(hit);
                self.ordinal = start + index;
            }
        }
    }

    pub fn step(&mut self, transcript: &Transcript, previous: bool) {
        self.refresh(transcript, None);
        if self.total > 1 {
            let delta = if previous { self.total - 1 } else { 1 };
            self.refresh(transcript, Some((self.ordinal + delta) % self.total));
        }
    }

    pub fn anchor(&self, transcript: &Transcript) -> Option<RowAnchor> {
        let hit = self.selected?;
        let entry = transcript.entry(hit.entry)?;
        let (_, text, base) = source(entry, hit.part)?;
        if !text.get(hit.offset..)?.starts_with(&self.query) {
            return None;
        }
        Some(RowAnchor {
            entry_id: hit.entry,
            position: base.map_or(
                if matches!(entry.kind, TranscriptEntryKind::Message) {
                    RowPosition::Content(hit.offset)
                } else {
                    RowPosition::Header
                },
                |base| {
                    RowPosition::Card(CardPosition {
                        section: CardSection::Detail,
                        absolute_byte_offset: (base as usize).saturating_add(hit.offset),
                    })
                },
            ),
        })
    }

    pub fn render(
        &mut self,
        frame: &mut Frame,
        area: Rect,
        transcript: &Transcript,
        history_status: Option<&str>,
        palette: Palette,
    ) {
        self.refresh(transcript, None);
        let mut lines = vec![
            Line::from(format!("Find: {}", sanitize_for_terminal(&self.query))),
            Line::default(),
        ];
        if let Some(hit) = self.selected {
            let (label, text, _) = source(
                transcript
                    .entry(hit.entry)
                    .expect("current search generation"),
                hit.part,
            )
            .expect("current search source");
            let end = hit.offset + self.query.len();
            let before = &text[..hit.offset];
            let start = before
                .char_indices()
                .rev()
                .take(40)
                .last()
                .map_or(hit.offset, |(i, _)| i);
            let after: String = text[end..].chars().take(80).collect();
            lines.push(Line::from(format!(
                "{} · match {} of {}",
                sanitize_for_terminal(label),
                self.ordinal + 1,
                self.total
            )));
            let snippet = |s: &str| sanitize_for_terminal(s).replace('\n', " ↵ ");
            lines.push(Line::from(vec![
                Span::raw(snippet(&before[start..])),
                Span::styled(snippet(&self.query), palette.selected_row(Style::default())),
                Span::raw(snippet(&after)),
            ]));
        } else {
            lines.push(Line::from(if self.query.is_empty() {
                "Type a phrase to find in this session."
            } else {
                "No matches in loaded history."
            }));
        }
        if let Some(status) = history_status {
            lines.push(Line::from(sanitize_for_terminal(status)));
        }
        lines.push(Line::default());
        lines.push(Line::from("↑ previous · ↓ next · Enter jump · Esc close"));
        frame.render_widget(
            Paragraph::new(lines)
                .block(Block::bordered().title(" Find in transcript "))
                .style(Style::default().fg(palette.foreground).bg(palette.panel))
                .wrap(Wrap { trim: false }),
            area,
        );
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    #[test]
    fn navigates_each_occurrence_in_restored_tool_output_and_wraps() {
        let transcript = crate::history::project_rpc_messages(&[
            json!({"entry_id":"call", "role":"assistant", "content":"", "content_truncated":false, "tool_calls":[{"call_id":"c", "name":"bash", "arguments":{"command":"check"}}]}),
            json!({"entry_id":"result", "role":"tool", "content":"αneedle first\nsecond needle", "content_truncated":false, "tool_call_id":"c", "tool_name":"bash", "tool_result":{"status":"done"}}),
        ]).unwrap();
        let mut find = TranscriptFind::default();
        find.append("need");
        find.refresh(&transcript, None);
        assert_eq!(find.total, 2);
        find.append("les");
        find.refresh(&transcript, None);
        assert!(find.selected.is_none());
        find.backspace();
        find.refresh(&transcript, None);
        let first = find.selected.unwrap();
        assert_eq!(first.offset, "α".len());
        find.step(&transcript, true);
        assert_eq!(
            find.selected.unwrap().offset,
            "αneedle first\nsecond ".len()
        );
        let mut cache = crate::transcript_view::TranscriptRowCache::default();
        let mut viewport = crate::transcript_view::TranscriptViewport::default();
        viewport.set_geometry(&transcript, &mut cache, 60, 5);
        viewport.reveal_match(&transcript, &mut cache, find.anchor(&transcript).unwrap());
        assert!(
            viewport.visible_rows(&transcript, &mut cache)[0]
                .plain_text()
                .contains("needle")
        );
        find.step(&transcript, false);
        assert_eq!(find.selected, Some(first));
    }
}
