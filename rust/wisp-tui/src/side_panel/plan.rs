//! The Plan section: the current plan or to-do list extracted from the latest
//! checklist in the transcript, with checkmarks and a progress count (`3 of 5`).

use pulldown_cmark::{Event, Options, Parser, Tag, TagEnd};
use serde_json::Value;

use super::*;
use crate::transcript::TranscriptRole;

/// Plan items shown before the rest are counted on one muted row.
const MAX_PLAN_ROWS: usize = 8;

/// The state of a single item in the current plan.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(super) enum PlanItemStatus {
    Done,
    InProgress,
    Pending,
}

/// One step in the current plan or to-do list.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct PlanItem {
    pub(super) text: String,
    pub(super) status: PlanItemStatus,
}

/// The latest plan or to-do checklist found in the transcript.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(super) struct Plan {
    pub(super) items: Vec<PlanItem>,
}

impl Plan {
    pub(super) fn done_count(&self) -> usize {
        self.items
            .iter()
            .filter(|item| item.status == PlanItemStatus::Done)
            .count()
    }

    pub(super) fn total(&self) -> usize {
        self.items.len()
    }
}

/// Find the latest non-empty plan or to-do list in the transcript.
///
/// Checks entries from newest to oldest, reading structured todo/plan tool calls
/// and Markdown task lists (`- [x] ...`, `- [ ] ...`) in assistant replies.
pub(super) fn extract(state: &UiState) -> Option<Plan> {
    for entry in state.transcript.entries().iter().rev() {
        let items = match entry.role {
            TranscriptRole::Tool => entry.tool_card().and_then(extract_from_tool),
            TranscriptRole::Assistant => extract_from_markdown(entry.display_content()),
            _ => None,
        };
        if let Some(items) = items.filter(|items| !items.is_empty()) {
            return Some(Plan { items });
        }
    }
    None
}

/// Render the `Plan` section, or `None` when the session has no active checklist.
pub(super) fn plan_lines(
    state: &UiState,
    width: usize,
    palette: Palette,
) -> Option<Vec<Line<'static>>> {
    let plan = extract(state)?;
    let muted = Style::default().fg(palette.muted);
    let progress = format!("{} of {}", plan.done_count(), plan.total());
    let mut lines = vec![spread(
        vec![heading("Plan", palette)],
        vec![Span::styled(progress, muted)],
        width,
    )];
    let text_room = width.saturating_sub(2);
    for item in plan.items.iter().take(MAX_PLAN_ROWS) {
        let (mark, mark_style, text_style) = match item.status {
            PlanItemStatus::Done => ("✓ ", Style::default().fg(palette.success), muted),
            PlanItemStatus::InProgress => (
                "● ",
                Style::default().fg(palette.secondary),
                Style::default(),
            ),
            PlanItemStatus::Pending => ("○ ", muted, Style::default()),
        };
        lines.push(Line::from(vec![
            Span::styled(mark, mark_style),
            Span::styled(ui::clip_columns(&item.text, text_room), text_style),
        ]));
    }
    if plan.items.len() > MAX_PLAN_ROWS {
        lines.push(Line::styled(
            format!("+{} more", plan.items.len() - MAX_PLAN_ROWS),
            muted,
        ));
    }
    Some(lines)
}

/// Extract structured or Markdown todo/plan items from a tool call whose name or
/// output carries a plan or checklist.
fn extract_from_tool(card: &crate::tool_cards::ToolCardSnapshot) -> Option<Vec<PlanItem>> {
    let lower = card.name.to_ascii_lowercase();
    if !lower.contains("todo") && !lower.contains("plan") {
        return None;
    }
    for text in [card.retained_output.text.as_str(), card.detail.as_str()] {
        if let Ok(value) = serde_json::from_str::<Value>(text) {
            if let Some(items) = extract_from_json(&value) {
                return Some(items);
            }
        }
        if let Some(items) = extract_from_markdown(text) {
            return Some(items);
        }
    }
    None
}

fn extract_from_json(value: &Value) -> Option<Vec<PlanItem>> {
    let list = value.as_array().or_else(|| {
        value.as_object().and_then(|obj| {
            obj.get("todos")
                .or_else(|| obj.get("plan"))
                .or_else(|| obj.get("tasks"))
                .and_then(Value::as_array)
        })
    })?;
    let mut items = Vec::new();
    for entry in list {
        let Some(object) = entry.as_object() else {
            continue;
        };
        let Some(raw_text) = object
            .get("content")
            .or_else(|| object.get("step"))
            .or_else(|| object.get("title"))
            .or_else(|| object.get("text"))
            .and_then(Value::as_str)
        else {
            continue;
        };
        let text = sanitize_for_terminal(raw_text)
            .split_whitespace()
            .collect::<Vec<_>>()
            .join(" ");
        if text.is_empty() {
            continue;
        }
        let status = parse_tool_item_status(object);
        items.push(PlanItem { text, status });
    }
    (!items.is_empty()).then_some(items)
}

fn parse_tool_item_status(object: &serde_json::Map<String, Value>) -> PlanItemStatus {
    if let Some(status) = object.get("status").and_then(Value::as_str) {
        return match status.to_ascii_lowercase().as_str() {
            "completed" | "done" => PlanItemStatus::Done,
            "in_progress" | "in-progress" | "active" | "running" => PlanItemStatus::InProgress,
            _ => PlanItemStatus::Pending,
        };
    }
    if object
        .get("completed")
        .or_else(|| object.get("done"))
        .and_then(Value::as_bool)
        == Some(true)
    {
        return PlanItemStatus::Done;
    }
    PlanItemStatus::Pending
}

/// Extract Markdown task list items (`- [x] ...`, `- [ ] ...`) from an assistant reply.
/// Code blocks are ignored by the Markdown parser, and nested task items stay in document order.
fn extract_from_markdown(markdown: &str) -> Option<Vec<PlanItem>> {
    let mut options = Options::empty();
    options.insert(Options::ENABLE_TASKLISTS);
    let parser = Parser::new_ext(markdown, options);

    let mut slots: Vec<Option<PlanItem>> = Vec::new();
    // Stack of open list items: `Some((slot_index, status, text))` for task items.
    let mut stack: Vec<Option<(usize, PlanItemStatus, String)>> = Vec::new();

    for event in parser {
        match event {
            Event::Start(Tag::Item) => {
                stack.push(None);
            }
            Event::TaskListMarker(checked) => {
                if let Some(slot) = stack.last_mut() {
                    let status = if checked {
                        PlanItemStatus::Done
                    } else {
                        PlanItemStatus::Pending
                    };
                    let index = slots.len();
                    slots.push(None);
                    *slot = Some((index, status, String::new()));
                }
            }
            Event::Text(text) | Event::Code(text) => {
                if let Some(Some((_, _, buffer))) = stack.last_mut() {
                    buffer.push_str(&text);
                }
            }
            Event::SoftBreak | Event::HardBreak => {
                if let Some(Some((_, _, buffer))) = stack.last_mut() {
                    if !buffer.ends_with(' ') {
                        buffer.push(' ');
                    }
                }
            }
            Event::End(TagEnd::Item) => {
                if let Some(Some((index, status, raw))) = stack.pop() {
                    let text = sanitize_for_terminal(&raw)
                        .split_whitespace()
                        .collect::<Vec<_>>()
                        .join(" ");
                    if !text.is_empty() {
                        slots[index] = Some(PlanItem { text, status });
                    }
                }
            }
            _ => {}
        }
    }
    let items: Vec<PlanItem> = slots.into_iter().flatten().collect();
    (!items.is_empty()).then_some(items)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn extracts_markdown_task_list_and_ignores_code_blocks() {
        let md = "\
Here is the plan:

```markdown
- [ ] Not a real task
```

- [x] Inspect `side_panel.rs`
- [x] Add plan extraction
- [ ] Wire running subagents
";
        let items = extract_from_markdown(md).expect("plan items");
        assert_eq!(
            items,
            vec![
                PlanItem {
                    text: "Inspect side_panel.rs".into(),
                    status: PlanItemStatus::Done,
                },
                PlanItem {
                    text: "Add plan extraction".into(),
                    status: PlanItemStatus::Done,
                },
                PlanItem {
                    text: "Wire running subagents".into(),
                    status: PlanItemStatus::Pending,
                },
            ]
        );
    }

    #[test]
    fn plain_lists_do_not_produce_a_plan() {
        assert_eq!(extract_from_markdown("- one\n- two\n"), None);
    }

    #[test]
    fn extracts_structured_tool_todos() {
        let mut state = UiState::new("openai".into(), None, None);
        let output = serde_json::json!({
            "todos": [
                {"content": "Read config", "status": "completed"},
                {"content": "Update layout", "status": "in_progress"},
                {"content": "Run tests", "status": "pending"}
            ]
        })
        .to_string();
        let mut ids = crate::SequentialCommandIds::default();
        for event in [
            serde_json::json!({
                "type": "tool.call",
                "call_id": "todo-1",
                "name": "todo_write",
                "arguments": {}
            }),
            serde_json::json!({
                "type": "tool.result",
                "call_id": "todo-1",
                "name": "todo_write",
                "output": output,
                "is_error": false
            }),
        ] {
            let event = crate::reducer::BackendEvent::from_projection_value(&event).unwrap();
            crate::reducer::reduce(
                &mut state,
                crate::reducer::UiAction::BackendEvent(event),
                &mut ids,
            )
            .unwrap();
        }
        let plan = extract(&state).expect("plan");
        assert_eq!(plan.done_count(), 1);
        assert_eq!(plan.total(), 3);
        assert_eq!(plan.items[1].status, PlanItemStatus::InProgress);
    }
}
