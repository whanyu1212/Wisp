use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::Value;
use std::collections::BTreeMap;
use wisp_protocol::events::CommandFinishedOutcome;
use wisp_protocol::{commands, events, handshake_request, handshake_response};

fn fixtures(schema: &str) -> BTreeMap<String, Value> {
    let schema: Value = serde_json::from_str(schema).expect("schema must be JSON");
    serde_json::from_value(schema["x-wisp-conformance-fixtures"].clone())
        .expect("schema must contain fixture objects")
}

fn assert_round_trips<T>(
    fixtures: BTreeMap<String, Value>,
    deserialize: impl Fn(Value) -> Result<T, wisp_protocol::ProtocolDecodeError>,
) where
    T: DeserializeOwned + Serialize,
{
    assert!(!fixtures.is_empty());
    for (discriminator, fixture) in fixtures {
        assert_eq!(fixture["type"], discriminator);
        let typed: T = deserialize(fixture.clone())
            .unwrap_or_else(|error| panic!("{discriminator} did not deserialize: {error}"));
        let serialized = serde_json::to_value(typed)
            .unwrap_or_else(|error| panic!("{discriminator} did not serialize: {error}"));
        assert_eq!(serialized, fixture, "{discriminator} changed on round trip");
    }
}

#[test]
fn every_python_command_fixture_round_trips_in_rust() {
    assert_round_trips::<commands::WispTypedClientRpcCommands>(
        fixtures(include_str!(concat!(
            env!("CARGO_MANIFEST_DIR"),
            "/../../schemas/live-rpc/v3/commands.schema.json"
        ))),
        commands::deserialize,
    );
}

#[test]
fn tui_command_builders_preserve_the_canonical_wire_contract() {
    let catalog = commands::WispTypedClientRpcCommands::get_model_catalog("models-1")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        catalog,
        serde_json::json!({"type": "get_model_catalog", "id": "models-1"})
    );

    let prompt = commands::WispTypedClientRpcCommands::prompt("prompt-1", "hello")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        prompt,
        serde_json::json!({"type": "prompt", "id": "prompt-1", "prompt": "hello"})
    );

    let steer = commands::WispTypedClientRpcCommands::steer("steer-1", "change course")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        steer,
        serde_json::json!({"type": "steer", "id": "steer-1", "content": "change course"})
    );

    let follow_up = commands::WispTypedClientRpcCommands::follow_up("follow-up-1", "continue")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        follow_up,
        serde_json::json!({"type": "follow_up", "id": "follow-up-1", "content": "continue"})
    );

    let queue_state = commands::WispTypedClientRpcCommands::get_queue_state("queue-state-1")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        queue_state,
        serde_json::json!({"type": "get_queue_state", "id": "queue-state-1"})
    );

    let pop = commands::WispTypedClientRpcCommands::pop_queue(
        "queue-pop-1",
        commands::QueueKind::FollowUp,
    )
    .unwrap()
    .into_value()
    .unwrap();
    assert_eq!(
        pop,
        serde_json::json!({"type": "pop_queue", "id": "queue-pop-1", "kind": "follow_up"})
    );
    assert_eq!(commands::QueueKind::Steering.as_wire_value(), "steering");

    let approval = commands::WispTypedClientRpcCommands::approval(
        "approval-1",
        "call-1",
        true,
        None,
        Some(commands::ApprovalScope::ToolSession),
    )
    .unwrap()
    .into_value()
    .unwrap();
    assert_eq!(
        approval,
        serde_json::json!({
            "type": "approval",
            "id": "approval-1",
            "call_id": "call-1",
            "approved": true,
            "scope": "tool_session"
        })
    );

    let denial = commands::WispTypedClientRpcCommands::approval(
        "approval-2",
        "call-2",
        false,
        Some("Denied from TUI"),
        None,
    )
    .unwrap()
    .into_value()
    .unwrap();
    assert_eq!(
        denial,
        serde_json::json!({
            "type": "approval",
            "id": "approval-2",
            "call_id": "call-2",
            "approved": false,
            "reason": "Denied from TUI"
        })
    );

    let stats = commands::WispTypedClientRpcCommands::get_session_stats("stats-1")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        stats,
        serde_json::json!({"type": "get_session_stats", "id": "stats-1"})
    );

    let sessions = commands::WispTypedClientRpcCommands::get_sessions("sessions-1")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        sessions,
        serde_json::json!({"type": "get_sessions", "id": "sessions-1", "limit": 50})
    );

    let new_session = commands::WispTypedClientRpcCommands::new_session("new-1")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        new_session,
        serde_json::json!({"type": "new_session", "id": "new-1"})
    );

    let selected = commands::WispTypedClientRpcCommands::select_session("select-1", "session-1")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        selected,
        serde_json::json!({"type": "select_session", "id": "select-1", "session_id": "session-1"})
    );

    let current_messages = commands::WispTypedClientRpcCommands::get_messages("messages-1", None)
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        current_messages,
        serde_json::json!({
            "type": "get_messages", "id": "messages-1", "limit": 200,
            "complete_structure": true, "full_content": false
        })
    );

    let selected_messages =
        commands::WispTypedClientRpcCommands::get_messages("messages-2", Some("session-1"))
            .unwrap()
            .into_value()
            .unwrap();
    assert_eq!(
        selected_messages,
        serde_json::json!({
            "type": "get_messages", "id": "messages-2", "session_id": "session-1",
            "limit": 200, "complete_structure": true, "full_content": false
        })
    );

    let older = commands::WispTypedClientRpcCommands::get_messages_older(
        "messages-3",
        Some("session-1"),
        "entry-75",
    )
    .unwrap()
    .into_value()
    .unwrap();
    assert_eq!(
        older,
        serde_json::json!({
            "type": "get_messages", "id": "messages-3", "session_id": "session-1",
            "limit": 75, "before_entry_id": "entry-75", "complete_structure": true,
            "full_content": false, "allow_during_prompt": true
        })
    );

    let newer =
        commands::WispTypedClientRpcCommands::get_messages_newer("messages-4", None, "entry-75")
            .unwrap()
            .into_value()
            .unwrap();
    assert_eq!(
        newer,
        serde_json::json!({
            "type": "get_messages", "id": "messages-4", "limit": 75,
            "after_entry_id": "entry-75", "complete_structure": true,
            "full_content": false, "allow_during_prompt": true
        })
    );

    let detail = commands::WispTypedClientRpcCommands::get_message_detail(
        "messages-5",
        Some("session-1"),
        "entry-76",
    )
    .unwrap()
    .into_value()
    .unwrap();
    assert_eq!(
        detail,
        serde_json::json!({
            "type": "get_messages", "id": "messages-5", "session_id": "session-1",
            "limit": 1, "entry_ids": ["entry-76"], "complete_structure": true,
            "full_content": true, "allow_during_prompt": true
        })
    );

    let named = commands::WispTypedClientRpcCommands::set_session_name("name-1", "release work")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        named,
        serde_json::json!({"type": "set_session_name", "id": "name-1", "name": "release work"})
    );
    let cleared = commands::WispTypedClientRpcCommands::set_session_name("name-2", "")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        cleared,
        serde_json::json!({"type": "set_session_name", "id": "name-2", "name": ""})
    );

    let cloned = commands::WispTypedClientRpcCommands::clone_session("clone-1")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        cloned,
        serde_json::json!({"type": "clone_session", "id": "clone-1"})
    );

    let forked = commands::WispTypedClientRpcCommands::fork_session("fork-1", "entry-1")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        forked,
        serde_json::json!({"type": "fork_session", "id": "fork-1", "entry_id": "entry-1"})
    );

    let first_tree = commands::WispTypedClientRpcCommands::get_session_tree("tree-1", None)
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        first_tree,
        serde_json::json!({"type": "get_session_tree", "id": "tree-1", "limit": 200})
    );
    let next_tree =
        commands::WispTypedClientRpcCommands::get_session_tree("tree-2", Some("entry-200"))
            .unwrap()
            .into_value()
            .unwrap();
    assert_eq!(
        next_tree,
        serde_json::json!({
            "type": "get_session_tree", "id": "tree-2", "limit": 200,
            "after_entry_id": "entry-200"
        })
    );

    let navigated =
        commands::WispTypedClientRpcCommands::navigate_session_tree("navigate-1", "entry-2")
            .unwrap()
            .into_value()
            .unwrap();
    assert_eq!(
        navigated,
        serde_json::json!({
            "type": "navigate_session_tree", "id": "navigate-1", "entry_id": "entry-2"
        })
    );

    let unreverted = commands::WispTypedClientRpcCommands::unrevert_session_tree("unrevert-1")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        unreverted,
        serde_json::json!({"type": "unrevert_session_tree", "id": "unrevert-1"})
    );

    assert!(commands::WispTypedClientRpcCommands::set_session_name("", "name").is_err());
    assert!(commands::WispTypedClientRpcCommands::clone_session(&"x".repeat(257)).is_err());
    assert!(commands::WispTypedClientRpcCommands::fork_session("fork-empty", "").is_err());
    assert!(
        commands::WispTypedClientRpcCommands::navigate_session_tree("navigate-empty", "").is_err()
    );
    assert!(
        commands::WispTypedClientRpcCommands::get_session_tree("tree-empty", Some("")).is_err()
    );
    assert!(commands::WispTypedClientRpcCommands::unrevert_session_tree("").is_err());
    let oversized_entry_id = "x".repeat(8 * 1024);
    assert_eq!(
        commands::WispTypedClientRpcCommands::fork_session("fork-long", &oversized_entry_id)
            .unwrap()
            .into_value()
            .unwrap()["entry_id"],
        oversized_entry_id
    );

    let cancel = commands::WispTypedClientRpcCommands::cancel("cancel-1", "prompt-1")
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        cancel,
        serde_json::json!({"type": "cancel", "id": "cancel-1", "target_id": "prompt-1"})
    );

    let trusted = commands::WispTypedClientRpcCommands::trust(
        "trust-1",
        "trust-req-1",
        true,
        None,
        Some(false),
    )
    .unwrap()
    .into_value()
    .unwrap();
    assert_eq!(
        trusted,
        serde_json::json!({
            "type": "trust",
            "id": "trust-1",
            "request_id": "trust-req-1",
            "trusted": true,
            "transient": false
        })
    );

    let denied = commands::WispTypedClientRpcCommands::trust(
        "trust-2",
        "trust-req-2",
        false,
        Some("Trust prompt cancelled"),
        Some(true),
    )
    .unwrap()
    .into_value()
    .unwrap();
    assert_eq!(
        denied,
        serde_json::json!({
            "type": "trust",
            "id": "trust-2",
            "request_id": "trust-req-2",
            "trusted": false,
            "reason": "Trust prompt cancelled",
            "transient": true
        })
    );
}

#[test]
fn approval_builder_rejects_denied_scopes_and_invalid_ids() {
    assert!(
        commands::WispTypedClientRpcCommands::approval(
            "approval-1",
            "call-1",
            false,
            None,
            Some(commands::ApprovalScope::AllSession),
        )
        .is_err()
    );
    assert!(commands::WispTypedClientRpcCommands::prompt("", "hello").is_err());
    assert!(commands::WispTypedClientRpcCommands::steer("", "hello").is_err());
    assert!(commands::WispTypedClientRpcCommands::follow_up(&"x".repeat(257), "hello").is_err());
    assert!(commands::WispTypedClientRpcCommands::get_queue_state("").is_err());
    assert!(
        commands::WispTypedClientRpcCommands::pop_queue("", commands::QueueKind::Steering,)
            .is_err()
    );
    assert!(commands::WispTypedClientRpcCommands::get_sessions(&"x".repeat(257)).is_err());
    assert!(commands::WispTypedClientRpcCommands::select_session("select-1", "").is_err());
    assert!(commands::WispTypedClientRpcCommands::get_messages("messages-1", Some("")).is_err());
    assert!(
        commands::WispTypedClientRpcCommands::get_messages_older("messages-1", None, "").is_err()
    );
    assert!(
        commands::WispTypedClientRpcCommands::get_messages_newer("messages-1", None, "").is_err()
    );
    assert!(
        commands::WispTypedClientRpcCommands::get_message_detail("messages-1", None, "").is_err()
    );
    assert!(
        commands::WispTypedClientRpcCommands::approval(
            "approval-1",
            "",
            true,
            None,
            Some(commands::ApprovalScope::Once),
        )
        .is_err()
    );
}

#[test]
fn approval_builder_serializes_every_approved_scope() {
    for (scope, expected) in [
        (commands::ApprovalScope::Once, "once"),
        (commands::ApprovalScope::ToolSession, "tool_session"),
        (commands::ApprovalScope::AllSession, "all_session"),
    ] {
        let command = commands::WispTypedClientRpcCommands::approval(
            "approval-1",
            "call-1",
            true,
            None,
            Some(scope),
        )
        .unwrap()
        .into_value()
        .unwrap();
        assert_eq!(command["scope"], expected);
    }
}

#[test]
fn every_python_event_fixture_round_trips_in_rust() {
    let fixtures = fixtures(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../schemas/live-rpc/v3/events.schema.json"
    )));
    assert_round_trips::<events::WispCurrentLiveEventOutput>(fixtures, events::deserialize);
}

#[test]
fn model_catalog_projection_is_correlated_and_bounded() {
    let fixture = fixtures(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../schemas/live-rpc/v3/events.schema.json"
    )))
    .remove("rpc.model_catalog")
    .unwrap();
    let event = events::deserialize(fixture).unwrap();
    let catalog = event.model_catalog("command-1").unwrap();
    assert_eq!(catalog.selection.provider, "fixture");
    assert_eq!(
        catalog.providers[0].models[0].effort_levels,
        ["low", "high"]
    );
    assert!(event.model_catalog("stale-command").is_none());

    let models = (0..512)
        .map(|index| {
            serde_json::json!({
                "id": format!("model-{index}"),
                "lifecycle": null,
                "effort_levels": []
            })
        })
        .collect::<Vec<_>>();
    let providers = (0..9)
        .map(|index| {
            serde_json::json!({
                "name": format!("provider-{index}"),
                "display_name": format!("Provider {index}"),
                "default_model": "model-0",
                "available": true,
                "models": models
            })
        })
        .collect::<Vec<_>>();
    assert!(
        events::deserialize(serde_json::json!({
            "type": "rpc.model_catalog",
            "schema_version": 35,
            "timestamp": "2025-01-02T03:04:05Z",
            "command_id": "models-oversized",
            "catalog": {
                "selection": {
                    "provider": "provider-0",
                    "model": null,
                    "effective_model": "model-0",
                    "catalog_model": "model-0",
                    "effort": null
                },
                "providers": providers
            }
        }))
        .is_err()
    );
}

#[test]
fn future_and_malformed_types_fail_closed() {
    assert!(commands::deserialize(serde_json::json!({"type": "future.command"})).is_err());
    assert!(
        events::deserialize(serde_json::json!({"schema_version": 35, "type": "future.event"}))
            .is_err()
    );
    assert!(
        serde_json::from_str::<commands::WispTypedClientRpcCommands>(
            r#"{"type":"prompt","type":"prompt","prompt":"duplicate"}"#
        )
        .is_err()
    );
    assert!(serde_json::from_str::<events::WispCurrentLiveEventOutput>(r#"{"#).is_err());
}

#[test]
fn canonical_command_cross_field_constraints_fail_closed() {
    let invalid_commands = [
        serde_json::json!({
            "type": "approval",
            "call_id": "call-1",
            "approved": false,
            "scope": "all_session"
        }),
        serde_json::json!({
            "type": "get_messages",
            "before_entry_id": "before",
            "after_entry_id": "after"
        }),
        serde_json::json!({
            "type": "get_messages",
            "entry_ids": ["entry-1", "entry-1"]
        }),
        serde_json::json!({
            "type": "get_messages",
            "full_content": true
        }),
        serde_json::json!({
            "type": "configure",
            "clear_effort": false
        }),
        serde_json::json!({
            "type": "configure",
            "effort": "high",
            "clear_effort": true
        }),
    ];

    for command in invalid_commands {
        assert!(commands::deserialize(command.clone()).is_err());
        assert!(serde_json::from_value::<commands::WispTypedClientRpcCommands>(command).is_err());
    }
}

#[test]
fn canonical_event_cross_field_constraints_fail_closed() {
    let mut event_fixtures = fixtures(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../schemas/live-rpc/v3/events.schema.json"
    )));
    let mut invalid_events = Vec::new();

    let mut commands = event_fixtures.remove("rpc.commands").unwrap();
    commands["commands"] = serde_json::json!([{
        "name": "inspect",
        "title": "Inspect",
        "description": "Inspect state",
        "category": "general",
        "aliases": [],
        "slash_command": "/wrong",
        "slash_aliases": [],
        "arguments": [],
        "accepts_arguments": false,
        "prefill_on_partial_enter": false,
        "order": 1
    }]);
    invalid_events.push(commands);

    let mut cloned = event_fixtures.remove("rpc.session.cloned").unwrap();
    cloned["session_id"] = cloned["source_session_id"].clone();
    invalid_events.push(cloned);

    let mut navigated = event_fixtures.remove("rpc.session.tree.navigated").unwrap();
    navigated["changed"] = serde_json::json!(false);
    invalid_events.push(navigated);

    let mut unreverted = event_fixtures
        .remove("rpc.session.tree.unreverted")
        .unwrap();
    unreverted["active_leaf_id"] = unreverted["previous_active_leaf_id"].clone();
    invalid_events.push(unreverted);

    let tree_node = serde_json::json!({
        "entry_id": "entry-1",
        "parent_id": null,
        "operation_id": null,
        "created_at": "2026-01-02T03:04:05Z",
        "kind": "event",
        "role": null,
        "preview": "fixture",
        "preview_truncated": false
    });
    let tree_fixture = event_fixtures.remove("rpc.session.tree").unwrap();

    let mut oversized_page = tree_fixture.clone();
    oversized_page["session_id"] = serde_json::json!("session-1");
    oversized_page["session_path"] = serde_json::json!("fixture.jsonl");
    oversized_page["nodes"] = serde_json::json!([tree_node.clone()]);
    invalid_events.push(oversized_page);

    let mut duplicate_page = tree_fixture.clone();
    duplicate_page["session_id"] = serde_json::json!("session-1");
    duplicate_page["session_path"] = serde_json::json!("fixture.jsonl");
    duplicate_page["total_node_count"] = serde_json::json!(2);
    duplicate_page["nodes"] = serde_json::json!([tree_node.clone(), tree_node.clone()]);
    invalid_events.push(duplicate_page);

    let mut mismatched_cursor = tree_fixture;
    mismatched_cursor["session_id"] = serde_json::json!("session-1");
    mismatched_cursor["session_path"] = serde_json::json!("fixture.jsonl");
    mismatched_cursor["total_node_count"] = serde_json::json!(2);
    mismatched_cursor["nodes"] = serde_json::json!([tree_node]);
    mismatched_cursor["truncated"] = serde_json::json!(true);
    mismatched_cursor["next_after_entry_id"] = serde_json::json!("wrong-entry");
    invalid_events.push(mismatched_cursor);

    for event in invalid_events {
        assert!(events::deserialize(event).is_err());
    }
}

#[test]
fn generated_handshake_types_preserve_the_v3_contract() {
    let request_value = serde_json::json!({
        "type": "rpc.handshake.request",
        "frontend_name": "wisp-rust-tui",
        "frontend_version": "0.1.0",
        "min_protocol_version": 3,
        "max_protocol_version": 3,
        "min_event_schema_version": 35,
        "max_event_schema_version": 35,
        "supported_capabilities": [],
        "required_capabilities": []
    });
    let accepted_value = serde_json::json!({
        "type": "rpc.handshake.accepted",
        "backend_package_version": "0.1.0",
        "protocol_version": 3,
        "event_schema_version": 35,
        "min_protocol_version": 3,
        "max_protocol_version": 3,
        "capabilities": [],
        "limits": {"max_client_frame_bytes": 1024, "max_server_frame_bytes": 2048}
    });

    let request = handshake_request::RpcHandshakeRequest::try_from(request_value.clone())
        .expect("request must deserialize");
    let accepted: handshake_response::RpcHandshakeResponse =
        handshake_response::deserialize(accepted_value.clone()).expect("response must deserialize");
    assert_eq!(request.into_value().unwrap(), request_value);
    assert_eq!(serde_json::to_value(accepted).unwrap(), accepted_value);
}

#[test]
fn handshake_cross_field_invariants_fail_closed() {
    let invalid_requests = [
        serde_json::json!({
            "type": "rpc.handshake.request",
            "frontend_name": "fixture",
            "frontend_version": "0.1.0",
            "min_protocol_version": 3,
            "max_protocol_version": 2,
            "min_event_schema_version": 35,
            "max_event_schema_version": 35,
            "supported_capabilities": [],
            "required_capabilities": []
        }),
        serde_json::json!({
            "type": "rpc.handshake.request",
            "frontend_name": "fixture",
            "frontend_version": "0.1.0",
            "min_protocol_version": 3,
            "max_protocol_version": 3,
            "min_event_schema_version": 36,
            "max_event_schema_version": 35,
            "supported_capabilities": [],
            "required_capabilities": []
        }),
        serde_json::json!({
            "type": "rpc.handshake.request",
            "frontend_name": "fixture",
            "frontend_version": "0.1.0",
            "min_protocol_version": 3,
            "max_protocol_version": 3,
            "min_event_schema_version": 35,
            "max_event_schema_version": 35,
            "supported_capabilities": [],
            "required_capabilities": ["missing"]
        }),
    ];
    for request in invalid_requests {
        assert!(handshake_request::deserialize(request).is_err());
    }

    let invalid_responses = [
        serde_json::json!({
            "type": "rpc.handshake.accepted",
            "backend_package_version": "0.1.0",
            "protocol_version": 4,
            "event_schema_version": 35,
            "min_protocol_version": 3,
            "max_protocol_version": 3,
            "capabilities": [],
            "limits": {"max_client_frame_bytes": 1024, "max_server_frame_bytes": 2048}
        }),
        serde_json::json!({
            "type": "rpc.handshake.rejected",
            "code": "protocol_version_mismatch",
            "message": "No compatible protocol.",
            "backend_package_version": "0.1.0",
            "min_protocol_version": 3,
            "max_protocol_version": 2,
            "event_schema_version": 35
        }),
    ];
    for response in invalid_responses {
        assert!(handshake_response::deserialize(response).is_err());
    }
}

#[test]
fn current_helpers_match_the_embedded_manifest_and_wire_contract() {
    let manifest: Value =
        serde_json::from_str(wisp_protocol::LIVE_RPC_MANIFEST_JSON).expect("manifest is JSON");
    assert_eq!(
        manifest["live_protocol_version"],
        wisp_protocol::LIVE_RPC_PROTOCOL_VERSION
    );
    assert_eq!(
        manifest["event_schema_version"],
        wisp_protocol::EVENT_SCHEMA_VERSION
    );
    assert_eq!(
        manifest["fixed_handshake_frame_bytes"],
        wisp_protocol::HANDSHAKE_FRAME_BYTES
    );
    assert_eq!(
        manifest["maximum_application_frame_bytes"],
        wisp_protocol::MAX_APPLICATION_FRAME_BYTES
    );

    let request = handshake_request::RpcHandshakeRequest::current("wisp-rust-tui", "0.1.0")
        .expect("current request is valid")
        .into_value()
        .unwrap();
    assert_eq!(request["min_protocol_version"], 3);
    assert_eq!(request["max_event_schema_version"], 35);

    let shutdown = commands::WispTypedClientRpcCommands::shutdown("shutdown-1")
        .expect("shutdown command is valid")
        .into_value()
        .unwrap();
    assert_eq!(
        shutdown,
        serde_json::json!({"type": "shutdown", "id": "shutdown-1"})
    );
}

#[test]
fn response_and_event_accessors_use_validated_wire_values() {
    let accepted = handshake_response::deserialize(serde_json::json!({
        "type": "rpc.handshake.accepted",
        "backend_package_version": "0.1.0",
        "protocol_version": 3,
        "event_schema_version": 35,
        "min_protocol_version": 3,
        "max_protocol_version": 3,
        "capabilities": [],
        "limits": {"max_client_frame_bytes": 1024, "max_server_frame_bytes": 2048}
    }))
    .unwrap();
    assert_eq!(accepted.backend_package_version(), "0.1.0");
    assert_eq!(accepted.accepted_contract(), Some((3, 35, 1024, 2048)));
    assert!(accepted.rejection().is_none());

    let event = events::deserialize(serde_json::json!({
        "type": "rpc.command.finished",
        "schema_version": 35,
        "timestamp": "2026-01-02T03:04:05Z",
        "command_id": "shutdown-1",
        "command_type": "shutdown",
        "ok": true,
        "error": null
    }))
    .unwrap();
    assert_eq!(event.event_type(), "rpc.command.finished");
    assert!(event.successful_command_finished("shutdown-1", "shutdown"));

    let failed = events::deserialize(serde_json::json!({
        "type": "rpc.command.finished",
        "schema_version": 35,
        "timestamp": "2026-01-02T03:04:05Z",
        "command_id": "shutdown-1",
        "command_type": "shutdown",
        "ok": false,
        "error": "shutdown refused"
    }))
    .unwrap();
    assert_eq!(
        failed.command_finished_outcome("shutdown-1", "shutdown"),
        Some(CommandFinishedOutcome::Failed {
            error: Some("shutdown refused".into())
        })
    );
    assert_eq!(
        failed.command_finished_outcome("another-command", "shutdown"),
        None
    );
}
