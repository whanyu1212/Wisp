use serde::Serialize;
use serde::de::DeserializeOwned;
use serde_json::Value;
use std::collections::BTreeMap;
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
        assert!(commands::deserialize(command).is_err());
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

    let request: handshake_request::RpcHandshakeRequest =
        serde_json::from_value(request_value.clone()).expect("request must deserialize");
    let accepted: handshake_response::RpcHandshakeResponse =
        serde_json::from_value(accepted_value.clone()).expect("response must deserialize");
    assert_eq!(serde_json::to_value(request).unwrap(), request_value);
    assert_eq!(serde_json::to_value(accepted).unwrap(), accepted_value);
}
