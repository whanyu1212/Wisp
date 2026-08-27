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
            "/../../schemas/live-rpc/v2/commands.schema.json"
        ))),
        commands::deserialize,
    );
}

#[test]
fn every_python_event_fixture_round_trips_in_rust() {
    let fixtures = fixtures(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../schemas/live-rpc/v2/events.schema.json"
    )));
    assert_round_trips::<events::WispCurrentLiveEventOutput>(fixtures, events::deserialize);
}

#[test]
fn future_and_malformed_types_fail_closed() {
    assert!(commands::deserialize(serde_json::json!({"type": "future.command"})).is_err());
    assert!(
        events::deserialize(serde_json::json!({"schema_version": 34, "type": "future.event"}))
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
        "/../../schemas/live-rpc/v2/events.schema.json"
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
fn generated_handshake_types_preserve_the_v2_contract() {
    let request_value = serde_json::json!({
        "type": "rpc.handshake.request",
        "frontend_name": "wisp-rust-tui",
        "frontend_version": "0.1.0",
        "min_protocol_version": 2,
        "max_protocol_version": 2,
        "min_event_schema_version": 34,
        "max_event_schema_version": 34,
        "supported_capabilities": [],
        "required_capabilities": []
    });
    let accepted_value = serde_json::json!({
        "type": "rpc.handshake.accepted",
        "backend_package_version": "0.1.0",
        "protocol_version": 2,
        "event_schema_version": 34,
        "min_protocol_version": 2,
        "max_protocol_version": 2,
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
            "min_event_schema_version": 34,
            "max_event_schema_version": 34,
            "supported_capabilities": [],
            "required_capabilities": []
        }),
        serde_json::json!({
            "type": "rpc.handshake.request",
            "frontend_name": "fixture",
            "frontend_version": "0.1.0",
            "min_protocol_version": 2,
            "max_protocol_version": 2,
            "min_event_schema_version": 35,
            "max_event_schema_version": 34,
            "supported_capabilities": [],
            "required_capabilities": []
        }),
        serde_json::json!({
            "type": "rpc.handshake.request",
            "frontend_name": "fixture",
            "frontend_version": "0.1.0",
            "min_protocol_version": 2,
            "max_protocol_version": 2,
            "min_event_schema_version": 34,
            "max_event_schema_version": 34,
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
            "protocol_version": 3,
            "event_schema_version": 34,
            "min_protocol_version": 2,
            "max_protocol_version": 2,
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
            "event_schema_version": 34
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
    assert_eq!(request["min_protocol_version"], 2);
    assert_eq!(request["max_event_schema_version"], 34);

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
        "protocol_version": 2,
        "event_schema_version": 34,
        "min_protocol_version": 2,
        "max_protocol_version": 2,
        "capabilities": [],
        "limits": {"max_client_frame_bytes": 1024, "max_server_frame_bytes": 2048}
    }))
    .unwrap();
    assert_eq!(accepted.backend_package_version(), "0.1.0");
    assert_eq!(accepted.accepted_contract(), Some((2, 34, 1024, 2048)));
    assert!(accepted.rejection().is_none());

    let event = events::deserialize(serde_json::json!({
        "type": "rpc.command.finished",
        "schema_version": 34,
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
        "schema_version": 34,
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
