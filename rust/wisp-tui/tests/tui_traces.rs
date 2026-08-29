use serde::{Deserialize, Serialize};
use serde_json::Value;
use sha2::{Digest, Sha256};
use std::collections::BTreeMap;
use std::fmt::Write as _;
use std::fs;
use std::path::{Path, PathBuf};
use wisp_protocol::commands::ApprovalScope;
use wisp_tui::reducer::{
    ActiveCommand, ActiveCommandType, AgentMode, BackendEvent, CommandIdSource, CommandKind,
    InteractionStatus, PendingApproval, UiAction, UiEffect, UiState, ViewStatus, reduce,
};

const MAX_JSON_DEPTH: usize = 8;
const MAX_JSON_NODES: usize = 1024;

#[derive(Debug, Deserialize)]
struct TraceFile {
    #[serde(rename = "schema_version")]
    _schema_version: u32,
    name: String,
    #[serde(rename = "description")]
    _description: String,
    initial: TraceInitial,
    inputs: Vec<TraceInput>,
    expected: TraceExpected,
}

#[derive(Debug, Deserialize)]
struct TraceInitial {
    provider: String,
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    effort: Option<String>,
    #[serde(default)]
    view: Option<TraceInitialView>,
    #[serde(default)]
    interaction: Option<TraceInteraction>,
}

#[derive(Debug, Deserialize)]
struct TraceInitialView {
    #[serde(default = "default_true")]
    input_ready: bool,
    #[serde(default = "default_mode")]
    mode: String,
    #[serde(default)]
    last_session: Option<String>,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type")]
enum TraceInput {
    #[serde(rename = "local.submit")]
    Submit { content: String, clock_ms: u64 },
    #[serde(rename = "local.slash")]
    Slash {
        #[serde(rename = "command")]
        _command: String,
        #[serde(default, rename = "args")]
        _args: Vec<Value>,
        clock_ms: u64,
    },
    #[serde(rename = "local.cancel")]
    Cancel { clock_ms: u64 },
    #[serde(rename = "local.approve")]
    Approve {
        call_id: String,
        approved: bool,
        #[serde(default)]
        reason: Option<String>,
        #[serde(default)]
        scope: Option<String>,
        clock_ms: u64,
    },
    #[serde(rename = "local.trust")]
    Trust {
        request_id: String,
        trusted: bool,
        #[serde(default)]
        transient: Option<bool>,
        clock_ms: u64,
    },
    #[serde(rename = "rpc.event")]
    RpcEvent { event: Value, clock_ms: u64 },
    #[serde(rename = "rpc.closed")]
    RpcClosed {
        #[serde(default)]
        error: Option<String>,
        clock_ms: u64,
    },
}

impl TraceInput {
    fn clock_ms(&self) -> u64 {
        match self {
            Self::Submit { clock_ms, .. }
            | Self::Slash { clock_ms, .. }
            | Self::Cancel { clock_ms }
            | Self::Approve { clock_ms, .. }
            | Self::Trust { clock_ms, .. }
            | Self::RpcEvent { clock_ms, .. }
            | Self::RpcClosed { clock_ms, .. } => *clock_ms,
        }
    }
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
struct TraceView {
    status: String,
    input_mode: String,
    input_ready: bool,
    queued_steering: usize,
    queued_follow_ups: usize,
    #[serde(default)]
    provider: Option<String>,
    #[serde(default)]
    model: Option<String>,
    #[serde(default)]
    effort: Option<String>,
    #[serde(default = "default_mode")]
    mode: String,
    #[serde(default)]
    last_session: Option<String>,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
struct TraceInteraction {
    status: String,
    #[serde(default)]
    current_command_id: Option<String>,
    #[serde(default)]
    current_command_type: Option<String>,
    #[serde(default)]
    pending_approval_call_id: Option<String>,
    #[serde(default)]
    pending_trust_request_id: Option<String>,
    #[serde(default)]
    cancel_requested: bool,
    #[serde(default)]
    exit_requested: bool,
}

#[derive(Clone, Debug, Deserialize, PartialEq, Serialize)]
struct TraceToolCard {
    call_id: String,
    name: String,
    status: String,
    arguments_available: bool,
}

#[derive(Debug, Deserialize)]
struct TraceExpected {
    commands: Vec<Value>,
    view: TraceView,
    interaction: TraceInteraction,
    #[serde(default)]
    retained_text: Option<String>,
    #[serde(default)]
    tool_cards: Option<Vec<TraceToolCard>>,
}

#[derive(Clone, Debug, PartialEq)]
struct ReplayOutput {
    commands: Vec<Value>,
    view: TraceView,
    interaction: TraceInteraction,
    retained_text: Option<String>,
    tool_cards: Vec<TraceToolCard>,
}

#[derive(Default)]
struct DeterministicIds(BTreeMap<&'static str, usize>);

impl CommandIdSource for DeterministicIds {
    fn next_id(&mut self, kind: CommandKind) -> String {
        let count = self.0.entry(kind.prefix()).or_default();
        *count += 1;
        format!("{}-{count}", kind.prefix())
    }
}

#[test]
fn every_shared_tui_trace_matches_the_rust_reducer() {
    let fixture_paths = fixture_paths();
    assert!(!fixture_paths.is_empty(), "no shared TUI traces found");

    let schema: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../schemas/tui-traces/v1/trace.schema.json"
    )))
    .unwrap();
    let validator = jsonschema::draft202012::new(&schema).unwrap();

    for path in fixture_paths {
        let raw: Value = serde_json::from_str(&fs::read_to_string(&path).unwrap()).unwrap();
        validator.validate(&raw).unwrap_or_else(|error| {
            panic!("{} violates the trace schema: {error}", path.display())
        });
        validate_custom_bounds(&raw)
            .unwrap_or_else(|error| panic!("{} is not bounded: {error}", path.display()));
        let trace: TraceFile = serde_json::from_value(raw).unwrap();
        validate_monotonic_clock(&trace).unwrap();

        let expected = replay(&trace).unwrap_or_else(|error| panic!("{}: {error}", trace.name));
        assert_expected(&trace, &expected);
        for _ in 1..20 {
            assert_eq!(
                replay(&trace).unwrap_or_else(|error| panic!("{}: {error}", trace.name)),
                expected,
                "trace {} was nondeterministic",
                trace.name
            );
        }
    }
}

#[test]
fn trace_runner_rejects_trailing_input_after_exit() {
    let trace: TraceFile = serde_json::from_value(serde_json::json!({
        "schema_version": 1,
        "name": "trailing_input",
        "description": "runner termination regression",
        "initial": {"provider": "fake"},
        "inputs": [
            {"type": "rpc.closed", "error": null, "clock_ms": 0},
            {"type": "local.submit", "content": "late", "clock_ms": 1}
        ],
        "expected": {
            "commands": [],
            "view": {
                "status": "error",
                "input_mode": "idle",
                "input_ready": true,
                "queued_steering": 0,
                "queued_follow_ups": 0
            },
            "interaction": {"status": "idle"}
        }
    }))
    .unwrap();
    assert_eq!(
        replay(&trace).unwrap_err(),
        "trace contains input after an exit effect"
    );
}

#[test]
fn trace_runner_rejects_unsupported_actions_and_unbounded_values() {
    let unsupported: TraceFile = serde_json::from_value(serde_json::json!({
        "schema_version": 1,
        "name": "unsupported",
        "description": "unsupported action regression",
        "initial": {"provider": "fake"},
        "inputs": [{"type": "local.slash", "command": "help", "clock_ms": 0}],
        "expected": {
            "commands": [],
            "view": {
                "status": "idle",
                "input_mode": "idle",
                "input_ready": true,
                "queued_steering": 0,
                "queued_follow_ups": 0
            },
            "interaction": {"status": "idle"}
        }
    }))
    .unwrap();
    assert_eq!(
        replay(&unsupported).unwrap_err(),
        "unsupported trace action local.slash"
    );

    let too_deep = serde_json::json!([[[[[[[[["too deep"]]]]]]]]]);
    assert!(validate_json_structure(&too_deep, 1, &mut 0).is_err());
}

#[test]
fn trace_runner_rejects_a_clock_that_moves_backwards() {
    let trace: TraceFile = serde_json::from_value(serde_json::json!({
        "schema_version": 1,
        "name": "backwards_clock",
        "description": "clock regression",
        "initial": {"provider": "fake"},
        "inputs": [
            {"type": "local.submit", "content": "first", "clock_ms": 2},
            {"type": "local.submit", "content": "second", "clock_ms": 1}
        ],
        "expected": {
            "commands": [],
            "view": {
                "status": "idle",
                "input_mode": "idle",
                "input_ready": true,
                "queued_steering": 0,
                "queued_follow_ups": 0
            },
            "interaction": {"status": "idle"}
        }
    }))
    .unwrap();
    assert!(validate_monotonic_clock(&trace).is_err());
}

fn fixture_paths() -> Vec<PathBuf> {
    let directory = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../tests/fixtures/tui_traces");
    let mut paths: Vec<_> = fs::read_dir(directory)
        .unwrap()
        .map(|entry| entry.unwrap().path())
        .filter(|path| {
            path.extension()
                .is_some_and(|extension| extension == "json")
        })
        .collect();
    paths.sort();
    paths
}

fn replay(trace: &TraceFile) -> Result<ReplayOutput, String> {
    let mut state = initial_state(&trace.initial)?;
    let mut ids = DeterministicIds::default();
    let mut commands = Vec::new();
    let mut exited = false;

    for input in &trace.inputs {
        if exited {
            return Err("trace contains input after an exit effect".into());
        }
        let action = trace_action(input)?;
        for effect in reduce(&mut state, action, &mut ids).map_err(|error| error.to_string())? {
            match effect {
                UiEffect::SendCommand(command) => {
                    let mut value = command.to_value().map_err(|error| error.to_string())?;
                    normalize_trace_command(&mut value);
                    commands.push(value);
                }
                UiEffect::RequestRender => {}
                UiEffect::Exit => exited = true,
            }
        }
    }

    Ok(ReplayOutput {
        commands,
        view: view_projection(&state),
        interaction: interaction_projection(&state),
        retained_text: state
            .latest_assistant_text()
            .filter(|content| !content.is_empty())
            .map(str::to_owned),
        tool_cards: state
            .transcript
            .entries()
            .iter()
            .filter_map(|entry| {
                entry.tool_card().map(|card| TraceToolCard {
                    call_id: trace_card_id(&card.call_id),
                    name: card.name.clone(),
                    status: card.status.as_str().into(),
                    arguments_available: card.arguments_available,
                })
            })
            .collect(),
    })
}

fn trace_card_id(value: &str) -> String {
    let schema_safe = !value.is_empty()
        && value.len() <= 128
        && value.bytes().enumerate().all(|(index, byte)| {
            byte.is_ascii_lowercase()
                || byte.is_ascii_digit()
                || (index > 0 && matches!(byte, b'.' | b'_' | b'-'))
        });
    if schema_safe {
        return value.to_owned();
    }
    let digest = Sha256::digest(value.as_bytes());
    let mut encoded = String::with_capacity(66);
    encoded.push_str("h-");
    for byte in digest {
        write!(&mut encoded, "{byte:02x}").expect("writing to a string cannot fail");
    }
    encoded
}

fn initial_state(initial: &TraceInitial) -> Result<UiState, String> {
    let mut state = UiState::new(
        initial.provider.clone(),
        initial.model.clone(),
        initial.effort.clone(),
    );
    if let Some(view) = &initial.view {
        state.input_ready = view.input_ready;
        state.mode = match view.mode.as_str() {
            "build" => AgentMode::Build,
            "plan" => AgentMode::Plan,
            other => return Err(format!("unsupported initial mode {other:?}")),
        };
        state.last_session = view.last_session.clone();
    }
    if let Some(interaction) = &initial.interaction {
        state.interaction_status = parse_interaction_status(&interaction.status)?;
        state.view_status = view_status_for_interaction(state.interaction_status);
        state.cancel_requested = interaction.cancel_requested;
        state.exit_requested = interaction.exit_requested;
        state.pending_trust_request_id = interaction.pending_trust_request_id.clone();
        state.pending_trust_project_path = None;
        if let Some(call_id) = &interaction.pending_approval_call_id {
            state.pending_approval = Some(PendingApproval {
                call_id: call_id.clone(),
                name: "trace-pending-tool".into(),
                arguments: serde_json::json!({}),
                safety: "unknown".into(),
            });
        }
        match (
            interaction.current_command_id.as_ref(),
            interaction.current_command_type.as_deref(),
        ) {
            (None, None) => {}
            (Some(id), Some(command_type)) => {
                state.current_command = Some(ActiveCommand {
                    id: id.clone(),
                    command_type: parse_active_command_type(command_type)?,
                });
            }
            _ => return Err("initial command id and type must be provided together".into()),
        }
    }
    Ok(state)
}

fn trace_action(input: &TraceInput) -> Result<UiAction, String> {
    match input {
        TraceInput::Submit { content, .. } => Ok(UiAction::Submit(content.clone())),
        TraceInput::Approve {
            call_id,
            approved,
            reason,
            scope,
            ..
        } => Ok(UiAction::ApprovalDecision {
            call_id: call_id.clone(),
            approved: *approved,
            reason: reason.clone(),
            scope: scope.as_deref().map(parse_approval_scope).transpose()?,
        }),
        TraceInput::RpcEvent { event, .. } => BackendEvent::from_projection_value(event)
            .map(UiAction::BackendEvent)
            .map_err(|error| error.to_string()),
        TraceInput::RpcClosed { error, .. } => Ok(UiAction::TransportClosed {
            error: error.clone(),
        }),
        TraceInput::Cancel { .. } => Ok(UiAction::Cancel),
        TraceInput::Trust {
            request_id,
            trusted,
            transient,
            ..
        } => Ok(UiAction::TrustDecision {
            request_id: request_id.clone(),
            trusted: *trusted,
            reason: (!trusted).then(|| "Denied from trace".into()),
            transient: *transient,
        }),
        TraceInput::Slash { .. } => Err("unsupported trace action local.slash".into()),
    }
}

fn view_projection(state: &UiState) -> TraceView {
    TraceView {
        status: state.view_status.as_str().into(),
        input_mode: state.input_mode().into(),
        input_ready: state.input_ready,
        queued_steering: state.queued_steering,
        queued_follow_ups: state.queued_follow_ups,
        provider: state.provider.clone(),
        model: state.model.clone(),
        effort: state.effort.clone(),
        mode: state.mode.as_str().into(),
        last_session: state.last_session.clone(),
    }
}

fn interaction_projection(state: &UiState) -> TraceInteraction {
    TraceInteraction {
        status: state.interaction_status.as_str().into(),
        current_command_id: state
            .current_command
            .as_ref()
            .map(|command| command.id.clone()),
        current_command_type: state
            .current_command
            .as_ref()
            .map(|command| command.command_type.as_str().into()),
        pending_approval_call_id: state
            .pending_approval
            .as_ref()
            .map(|approval| approval.call_id.clone()),
        pending_trust_request_id: state.pending_trust_request_id.clone(),
        cancel_requested: state.cancel_requested,
        exit_requested: state.exit_requested,
    }
}

fn assert_expected(trace: &TraceFile, actual: &ReplayOutput) {
    assert_eq!(
        actual.commands.len(),
        trace.expected.commands.len(),
        "trace {} emitted the wrong command count",
        trace.name
    );
    for (index, (actual, expected)) in actual
        .commands
        .iter()
        .zip(&trace.expected.commands)
        .enumerate()
    {
        assert_value_subset(expected, actual, &format!("commands[{index}]"));
    }
    assert_eq!(
        actual.view, trace.expected.view,
        "trace {} view",
        trace.name
    );
    assert_eq!(
        actual.interaction, trace.expected.interaction,
        "trace {} interaction",
        trace.name
    );
    assert_eq!(
        actual.retained_text, trace.expected.retained_text,
        "trace {} retained text",
        trace.name
    );
    if let Some(expected) = &trace.expected.tool_cards {
        assert_eq!(
            &actual.tool_cards, expected,
            "trace {} tool cards",
            trace.name
        );
    }
}

fn assert_value_subset(expected: &Value, actual: &Value, path: &str) {
    match (expected, actual) {
        (Value::Object(expected), Value::Object(actual)) => {
            for (key, expected_value) in expected {
                let actual_value = actual
                    .get(key)
                    .unwrap_or_else(|| panic!("{path}.{key} is missing"));
                assert_value_subset(expected_value, actual_value, &format!("{path}.{key}"));
            }
        }
        _ => assert_eq!(actual, expected, "{path}"),
    }
}

fn normalize_trace_command(value: &mut Value) {
    match value.get("type").and_then(Value::as_str) {
        Some("approval") => {
            let object = value.as_object_mut().unwrap();
            object.entry("reason").or_insert(Value::Null);
            object.entry("scope").or_insert(Value::Null);
        }
        Some("trust") => {
            let object = value.as_object_mut().unwrap();
            object.entry("reason").or_insert(Value::Null);
            object.entry("transient").or_insert(Value::Bool(false));
        }
        _ => {}
    }
}

fn validate_monotonic_clock(trace: &TraceFile) -> Result<(), String> {
    for pair in trace.inputs.windows(2) {
        if pair[0].clock_ms() > pair[1].clock_ms() {
            return Err(format!("trace {} clock moved backwards", trace.name));
        }
    }
    Ok(())
}

fn validate_custom_bounds(trace: &Value) -> Result<(), String> {
    let Some(inputs) = trace.get("inputs").and_then(Value::as_array) else {
        return Err("trace inputs are missing".into());
    };
    for input in inputs {
        if let Some(event) = input.get("event") {
            validate_json_structure(event, 1, &mut 0)?;
        }
    }
    let Some(commands) = trace
        .pointer("/expected/commands")
        .and_then(Value::as_array)
    else {
        return Err("expected commands are missing".into());
    };
    for command in commands {
        validate_json_structure(command, 1, &mut 0)?;
    }
    Ok(())
}

fn validate_json_structure(value: &Value, depth: usize, nodes: &mut usize) -> Result<(), String> {
    *nodes += 1;
    if depth > MAX_JSON_DEPTH {
        return Err(format!("JSON depth exceeds {MAX_JSON_DEPTH}"));
    }
    if *nodes > MAX_JSON_NODES {
        return Err(format!("JSON node count exceeds {MAX_JSON_NODES}"));
    }
    match value {
        Value::Array(values) => {
            for value in values {
                validate_json_structure(value, depth + 1, nodes)?;
            }
        }
        Value::Object(values) => {
            for value in values.values() {
                validate_json_structure(value, depth + 1, nodes)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn parse_approval_scope(value: &str) -> Result<ApprovalScope, String> {
    match value {
        "once" => Ok(ApprovalScope::Once),
        "tool_session" => Ok(ApprovalScope::ToolSession),
        "all_session" => Ok(ApprovalScope::AllSession),
        other => Err(format!("unsupported approval scope {other:?}")),
    }
}

fn parse_interaction_status(value: &str) -> Result<InteractionStatus, String> {
    match value {
        "idle" => Ok(InteractionStatus::Idle),
        "running" => Ok(InteractionStatus::Running),
        "compacting" => Ok(InteractionStatus::Compacting),
        "waiting_for_approval" => Ok(InteractionStatus::WaitingForApproval),
        "waiting_for_trust" => Ok(InteractionStatus::WaitingForTrust),
        "exiting" => Ok(InteractionStatus::Exiting),
        other => Err(format!("unsupported interaction status {other:?}")),
    }
}

fn parse_active_command_type(value: &str) -> Result<ActiveCommandType, String> {
    match value {
        "prompt" => Ok(ActiveCommandType::Prompt),
        "init" => Ok(ActiveCommandType::Init),
        "compact" => Ok(ActiveCommandType::Compact),
        other => Err(format!("unsupported active command type {other:?}")),
    }
}

fn view_status_for_interaction(status: InteractionStatus) -> ViewStatus {
    match status {
        InteractionStatus::Idle => ViewStatus::Idle,
        InteractionStatus::Running | InteractionStatus::Compacting => ViewStatus::Running,
        InteractionStatus::WaitingForApproval => ViewStatus::WaitingForApproval,
        InteractionStatus::WaitingForTrust => ViewStatus::WaitingForTrust,
        InteractionStatus::Exiting => ViewStatus::Idle,
    }
}

fn default_true() -> bool {
    true
}

fn default_mode() -> String {
    "build".into()
}
