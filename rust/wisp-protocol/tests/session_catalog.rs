use serde_json::json;
use wisp_protocol::commands::WispTypedClientRpcCommands;

#[test]
fn older_v9_catalog_reports_without_discovery_fields_still_decode() {
    let schema: serde_json::Value = serde_json::from_str(include_str!(
        "../../../schemas/live-rpc/v9/events.schema.json"
    ))
    .unwrap();
    let mut report = schema["x-wisp-conformance-fixtures"]["rpc.sessions"].clone();
    for field in ["query", "next_cursor", "previous_cursor"] {
        report.as_object_mut().unwrap().remove(field);
    }
    assert!(wisp_protocol::events::deserialize(report).is_ok());
}

#[test]
fn search_and_cursor_are_optional_bounded_wire_fields() {
    let command = WispTypedClientRpcCommands::search_sessions("catalog", "Straße", Some("opaque"))
        .unwrap()
        .into_value()
        .unwrap();
    assert_eq!(
        command,
        json!({"type":"get_sessions", "id":"catalog", "limit":50,
        "query":"Straße", "cursor":"opaque"})
    );
    assert!(
        WispTypedClientRpcCommands::search_sessions("catalog", &"界".repeat(342), None).is_err()
    );
    assert!(
        WispTypedClientRpcCommands::search_sessions("catalog", "", Some(&"x".repeat(4097)))
            .is_err()
    );
    assert_eq!(
        WispTypedClientRpcCommands::get_sessions("catalog")
            .unwrap()
            .into_value()
            .unwrap(),
        json!({"type":"get_sessions", "id":"catalog", "limit":50})
    );
}
