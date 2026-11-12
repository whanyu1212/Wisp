//! What a codemode script card knows: the script, and the calls it made.
//!
//! A codemode call runs a Python script that calls other tools. The script's own
//! arguments carry its code and an optional title; its calls arrive live as
//! `tool.nested.started` and `tool.nested.ended` while it runs, and its result's
//! `nested_calls` records them all. The result's list wins: live rows only fill the
//! card until then. Everything here is untrusted backend text, kept on one line
//! and bounded when it is stored.
//!
//! A `spawn_agent` call's card is the same card: its subagent's tool calls arrive
//! and are recorded exactly as a script's are, and its task stands where the
//! script would. [`NestedHost`] says which of the two a card is.

use serde_json::Value;

/// The tool name codemode calls carry.
pub(crate) const CODEMODE: &str = "codemode";
/// The tool name subagent calls carry.
pub(crate) const SPAWN_AGENT: &str = "spawn_agent";
/// How a call is recorded when its script or subagent ended before the call finished.
const ENDED_FIRST: [&str; 2] = [
    "The script ended before this call finished",
    "The subagent ended before this call finished",
];
/// The most of a script a card keeps; a longer one is shown up to here.
pub(crate) const SCRIPT_MAX_BYTES: usize = 16 * 1024;
/// The longest title shown; the model may write more.
const TITLE_MAX_CHARS: usize = 80;
/// The most calls a card keeps, as many as a script's result records.
pub(crate) const MAX_SCRIPT_CALLS: usize = 200;
/// Bounds on one call's stored text.
const CALL_NAME_MAX_CHARS: usize = 128;
const CALL_TEXT_MAX_CHARS: usize = 240;
/// Tools the tally names before it stops.
const TALLY_TOOLS: usize = 3;

/// What made a card's calls: a codemode script, or a subagent.
#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum NestedHost {
    Script,
    Subagent,
}

impl NestedHost {
    /// What an unfinished call's row says ended before it.
    pub fn ended_first_note(self) -> &'static str {
        match self {
            Self::Script => "script ended first",
            Self::Subagent => "subagent ended first",
        }
    }
}

/// A codemode script, or a subagent's task, and the calls it has made so far.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScriptCard {
    pub host: NestedHost,
    /// The script's own short description, if it gave one.
    pub title: Option<String>,
    /// The script, or the subagent's task, up to [`SCRIPT_MAX_BYTES`]; empty when
    /// its arguments did not carry it, as in a history page that clipped them.
    pub code: String,
    /// Its calls in the order it made them.
    pub calls: Vec<ScriptCall>,
    /// Calls it made that the record leaves out.
    pub omitted: u64,
}

/// One call a script made.
#[derive(Clone, Debug, Eq, PartialEq)]
pub struct ScriptCall {
    pub call_id: String,
    /// The tool that ran: a proxied `mcp_call`'s target, or the name it was called by.
    pub tool: String,
    /// The tool's arguments, already bounded for display; `Value::Null` when the
    /// backend left them out.
    pub arguments: Value,
    pub state: ScriptCallState,
    /// Why it failed, on one line.
    pub error: Option<String>,
    pub duration_ms: Option<u32>,
}

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum ScriptCallState {
    Running,
    Done,
    Failed,
    /// The script ended before the call finished.
    Unfinished,
}

/// The calls a script's result records, parsed from its event or history page.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub struct ScriptResult {
    pub calls: Vec<ScriptCall>,
    pub omitted: u64,
}

impl ScriptResult {
    /// Parse a result's `nested_calls` and `nested_calls_omitted` fields.
    pub fn from_fields(calls: Option<&Value>, omitted: Option<&Value>) -> Self {
        Self {
            calls: calls
                .and_then(Value::as_array)
                .into_iter()
                .flatten()
                .filter_map(ScriptCall::from_record)
                .take(MAX_SCRIPT_CALLS)
                .collect(),
            omitted: omitted.and_then(Value::as_u64).unwrap_or(0),
        }
    }

    fn is_empty(&self) -> bool {
        self.calls.is_empty() && self.omitted == 0
    }
}

impl ScriptCard {
    /// The card for a call of `name` with these (already bounded) arguments; None
    /// for a tool that makes no calls of its own.
    pub fn for_call(name: &str, arguments: &Value) -> Option<Self> {
        match name {
            CODEMODE => Some(Self::from_arguments(arguments)),
            SPAWN_AGENT => Some(Self::from_task(arguments)),
            _ => None,
        }
    }

    /// The card for a `spawn_agent` call with these (already bounded) arguments.
    pub fn from_task(arguments: &Value) -> Self {
        let task = arguments
            .get("task")
            .and_then(Value::as_str)
            .map(|task| head_bytes(task, SCRIPT_MAX_BYTES).to_owned())
            .unwrap_or_default();
        Self {
            host: NestedHost::Subagent,
            title: None,
            code: task,
            calls: Vec::new(),
            omitted: 0,
        }
    }

    /// The card for a codemode call with these (already bounded) arguments.
    pub fn from_arguments(arguments: &Value) -> Self {
        let title = arguments
            .get("title")
            .and_then(Value::as_str)
            .map(|title| clip(&one_line(title), TITLE_MAX_CHARS))
            .filter(|title| !title.is_empty());
        let code = arguments
            .get("code")
            .and_then(Value::as_str)
            .map(|code| head_bytes(code, SCRIPT_MAX_BYTES).to_owned())
            .unwrap_or_default();
        Self {
            host: NestedHost::Script,
            title,
            code,
            calls: Vec::new(),
            omitted: 0,
        }
    }

    /// What the card's action line says the script does: its title, or how long it
    /// is. A subagent's line shows the start of its task.
    pub fn subject(&self) -> String {
        if let Some(title) = &self.title {
            return title.clone();
        }
        if self.host == NestedHost::Subagent {
            let task = clip(&one_line(&self.code), TITLE_MAX_CHARS);
            return if task.is_empty() {
                "subagent task".to_owned()
            } else {
                task
            };
        }
        match self.code.lines().count() {
            0 => "Python script".to_owned(),
            1 => "Python script, 1 line".to_owned(),
            lines => format!("Python script, {lines} lines"),
        }
    }

    /// A call the script just made. Returns whether the card changed.
    pub fn call_started(&mut self, call_id: &str, tool: &str, arguments: Value) -> bool {
        let call_id = clip(call_id, CALL_NAME_MAX_CHARS);
        if self.calls.iter().any(|call| call.call_id == call_id) {
            return false;
        }
        if self.calls.len() >= MAX_SCRIPT_CALLS {
            self.omitted = self.omitted.saturating_add(1);
            return true;
        }
        self.calls.push(ScriptCall {
            call_id,
            tool: clip(&one_line(tool), CALL_NAME_MAX_CHARS),
            arguments,
            state: ScriptCallState::Running,
            error: None,
            duration_ms: None,
        });
        true
    }

    /// A call the script made finished. Returns whether the card changed.
    pub fn call_ended(&mut self, call: ScriptCall) -> bool {
        match self
            .calls
            .iter()
            .position(|known| known.call_id == call.call_id)
        {
            Some(index) if self.calls[index] == call => false,
            Some(index) => {
                self.calls[index] = call;
                true
            }
            None if self.calls.len() >= MAX_SCRIPT_CALLS => false,
            None => {
                self.calls.push(call);
                true
            }
        }
    }

    /// The script ended. Its result's record replaces the live rows; a result that
    /// records nothing, such as an interrupted run's, leaves them and marks the ones
    /// still running as cut off.
    pub fn settle(&mut self, result: &ScriptResult) {
        if !result.is_empty() {
            self.calls.clone_from(&result.calls);
            self.omitted = result.omitted;
            return;
        }
        self.close_running();
    }

    /// Mark every call still running as cut off, as when the card is cancelled.
    pub fn close_running(&mut self) {
        for call in &mut self.calls {
            if call.state == ScriptCallState::Running {
                call.state = ScriptCallState::Unfinished;
            }
        }
    }

    /// The calls so far, as the action line counts them: `9 calls · read ×7, grep ×2`,
    /// with how many failed. Empty before the first call.
    pub fn tally(&self) -> String {
        let total = u64::try_from(self.calls.len())
            .unwrap_or(u64::MAX)
            .saturating_add(self.omitted);
        if total == 0 {
            return String::new();
        }
        let mut tally = if total == 1 {
            "1 call".to_owned()
        } else {
            format!("{total} calls")
        };
        let failed = self
            .calls
            .iter()
            .filter(|call| call.state == ScriptCallState::Failed)
            .count();
        if failed > 0 {
            tally.push_str(&format!(" ({failed} failed)"));
        }
        let mut counts: Vec<(&str, usize)> = Vec::new();
        for call in &self.calls {
            let tool = short_tool_name(&call.tool);
            match counts.iter_mut().find(|(name, _)| *name == tool) {
                Some((_, count)) => *count += 1,
                None => counts.push((tool, 1)),
            }
        }
        // Most-called first; ties keep the order the script first called them.
        counts.sort_by(|left, right| right.1.cmp(&left.1));
        let named: Vec<String> = counts
            .iter()
            .take(TALLY_TOOLS)
            .map(|(tool, count)| {
                if *count == 1 {
                    (*tool).to_owned()
                } else {
                    format!("{tool} ×{count}")
                }
            })
            .collect();
        if !named.is_empty() {
            tally.push_str(" · ");
            tally.push_str(&named.join(", "));
            if counts.len() > TALLY_TOOLS {
                tally.push_str(", …");
            }
        }
        tally
    }
}

impl ScriptCall {
    /// One call from a `NestedToolCall` record, as `tool.nested.ended` and a result's
    /// `nested_calls` carry it.
    pub fn from_record(record: &Value) -> Option<Self> {
        let call_id = record.get("call_id")?.as_str()?;
        let name = record.get("name")?.as_str()?;
        let target = record.get("target").and_then(Value::as_str);
        let tool = target.unwrap_or(name);
        let is_error = record
            .get("is_error")
            .and_then(Value::as_bool)
            .unwrap_or(false);
        let error = record
            .get("error")
            .and_then(Value::as_str)
            .map(|error| clip(&one_line(error), CALL_TEXT_MAX_CHARS));
        let state = match (is_error, error.as_deref()) {
            (false, _) => ScriptCallState::Done,
            (true, Some(error)) if ENDED_FIRST.contains(&error) => ScriptCallState::Unfinished,
            (true, _) => ScriptCallState::Failed,
        };
        Some(Self {
            call_id: clip(call_id, CALL_NAME_MAX_CHARS),
            tool: clip(&one_line(tool), CALL_NAME_MAX_CHARS),
            arguments: call_arguments(name, target, record.get("arguments")),
            state,
            error: error.filter(|_| state == ScriptCallState::Failed),
            duration_ms: record
                .get("duration_ms")
                .and_then(Value::as_u64)
                .map(|duration| u32::try_from(duration).unwrap_or(u32::MAX)),
        })
    }
}

/// A call's arguments bounded as a direct call's would be. A proxied `mcp_call`
/// shows the arguments its target received.
pub fn call_arguments(name: &str, target: Option<&str>, arguments: Option<&Value>) -> Value {
    let Some(arguments) = arguments.filter(|arguments| arguments.is_object()) else {
        return Value::Null;
    };
    let (tool, arguments) = match target {
        Some(target) => (
            target,
            crate::tool_cards::proxied_arguments(name, arguments, target).unwrap_or(arguments),
        ),
        None => (name, arguments),
    };
    crate::tool_cards::bounded_tool_arguments(tool, arguments)
}

/// A tool as the tally names it: an MCP tool by its own name, without its server.
fn short_tool_name(tool: &str) -> &str {
    tool.strip_prefix("mcp__")
        .and_then(|rest| rest.split_once("__"))
        .map_or(tool, |(_, name)| name)
}

fn one_line(value: &str) -> String {
    value
        .split_whitespace()
        .collect::<Vec<_>>()
        .join(" ")
        .chars()
        .filter(|character| !character.is_control() && !crate::is_bidi_control(*character))
        .collect()
}

fn clip(value: &str, max_chars: usize) -> String {
    if value.chars().count() <= max_chars {
        return value.to_owned();
    }
    let mut clipped: String = value.chars().take(max_chars.saturating_sub(1)).collect();
    clipped.push('…');
    clipped
}

/// The longest start of `value` within `max_bytes`, cut on a character boundary.
fn head_bytes(value: &str, max_bytes: usize) -> &str {
    if value.len() <= max_bytes {
        return value;
    }
    let mut end = max_bytes;
    while !value.is_char_boundary(end) {
        end -= 1;
    }
    &value[..end]
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;

    fn call(id: &str, tool: &str, state: ScriptCallState) -> ScriptCall {
        ScriptCall {
            call_id: id.into(),
            tool: tool.into(),
            arguments: Value::Null,
            state,
            error: None,
            duration_ms: Some(5),
        }
    }

    #[test]
    fn subject_names_title_or_length() {
        let titled = ScriptCard::from_arguments(&json!({"title": " count\nTODOs ", "code": "x"}));
        let untitled = ScriptCard::from_arguments(&json!({"code": "a = 1\nb = 2\n"}));
        let empty = ScriptCard::from_arguments(&json!({}));

        assert_eq!(titled.subject(), "count TODOs");
        assert_eq!(untitled.subject(), "Python script, 2 lines");
        assert_eq!(empty.subject(), "Python script");
    }

    #[test]
    fn tally_counts_tools_and_failures() {
        let mut card = ScriptCard::from_arguments(&json!({}));
        for (index, tool) in ["read", "grep", "read", "mcp__db__query", "read"]
            .iter()
            .enumerate()
        {
            card.call_ended(call(&index.to_string(), tool, ScriptCallState::Done));
        }
        card.calls[1].state = ScriptCallState::Failed;

        assert_eq!(card.tally(), "5 calls (1 failed) · read ×3, grep, query");
    }

    #[test]
    fn result_replaces_live_rows() {
        let mut card = ScriptCard::from_arguments(&json!({}));
        card.call_started("1", "read", Value::Null);
        card.call_started("2", "read", Value::Null);
        let result = ScriptResult {
            calls: vec![call("1", "read", ScriptCallState::Done)],
            omitted: 0,
        };

        card.settle(&result);

        assert_eq!(card.calls, result.calls);
    }

    #[test]
    fn empty_result_closes_running_rows() {
        let mut card = ScriptCard::from_arguments(&json!({}));
        card.call_started("1", "read", Value::Null);
        card.call_ended(call("2", "grep", ScriptCallState::Done));

        card.settle(&ScriptResult::default());

        let states: Vec<_> = card.calls.iter().map(|call| call.state).collect();
        assert_eq!(states, [ScriptCallState::Unfinished, ScriptCallState::Done]);
    }

    #[test]
    fn record_parses_states_and_targets() {
        let records = json!([
            {"call_id": "a", "name": "read", "arguments": {"path": "x.rs"}, "is_error": false,
             "started_at": "2026-01-02T03:04:05Z", "duration_ms": 7},
            {"call_id": "b", "name": "mcp_call", "target": "mcp__db__drop",
             "arguments": {"name": "mcp__db__drop", "arguments": {"table": "t"}},
             "is_error": true, "error": "not\u{1b}[31m read-only",
             "started_at": "2026-01-02T03:04:05Z", "duration_ms": 1},
            {"call_id": "c", "name": "read", "arguments": null, "is_error": true,
             "error": ENDED_FIRST[0], "started_at": "2026-01-02T03:04:05Z", "duration_ms": 9},
        ]);

        let result = ScriptResult::from_fields(Some(&records), Some(&json!(2)));

        let summary: Vec<_> = result
            .calls
            .iter()
            .map(|call| (call.tool.as_str(), call.state, call.error.as_deref()))
            .collect();
        assert_eq!(
            summary,
            [
                ("read", ScriptCallState::Done, None),
                (
                    "mcp__db__drop",
                    ScriptCallState::Failed,
                    Some("not[31m read-only")
                ),
                ("read", ScriptCallState::Unfinished, None),
            ]
        );
        // A proxied call shows the arguments its target received, not the proxy's.
        let proxied = result.calls[1].arguments.to_string();
        assert!(
            proxied.contains("table") && !proxied.contains("mcp__db__drop"),
            "{proxied}"
        );
        assert_eq!(result.calls[2].arguments, Value::Null);
        assert_eq!(result.omitted, 2);
    }

    #[test]
    fn changed_script_is_a_conflict() {
        let call = |code: &str| crate::tool_cards::ToolCallInput {
            call_id: "s".into(),
            name: CODEMODE.into(),
            arguments: json!({"code": code, "title": "same"}),
            detail_source: crate::tool_detail::ToolDetailSource::None,
        };
        let mut card = crate::tool_cards::ToolCardSnapshot::requested(
            &call("a = 1"),
            crate::tool_cards::ToolStatus::Requested,
        );

        assert!(card.enrich_call(&call("a = 1")));
        assert_eq!(card.status, crate::tool_cards::ToolStatus::Requested);
        assert!(card.enrich_call(&call("a = 2")));
        assert_eq!(card.status, crate::tool_cards::ToolStatus::Error);
    }

    #[test]
    fn live_calls_stay_bounded() {
        let mut card = ScriptCard::from_arguments(&json!({}));
        for index in 0..MAX_SCRIPT_CALLS + 5 {
            card.call_started(&index.to_string(), "read", Value::Null);
        }

        assert_eq!((card.calls.len(), card.omitted), (MAX_SCRIPT_CALLS, 5));
    }
}
