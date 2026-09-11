use serde_json::{Value, json};
use wisp_protocol::{
    commands::WispTypedClientRpcCommands,
    events::{self, McpServerStatus},
};

fn fixture(kind: &str) -> Value {
    serde_json::from_str::<Value>(include_str!(
        "../../../tests/fixtures/rust_tui_discovery.json"
    ))
    .unwrap()[kind]
        .clone()
}

#[test]
fn python_discovery_fixtures_preserve_sources_diagnostics_and_status() {
    let skills = events::deserialize(fixture("rpc.skills")).unwrap();
    assert!(skills.skill_catalog("wrong").is_none());
    let skills = skills.skill_catalog("skills-1").unwrap();
    assert!(skills.project_trusted);
    assert_eq!(
        skills
            .entries
            .iter()
            .map(|entry| entry.source.as_str())
            .collect::<Vec<_>>(),
        [
            "project:wisp",
            "project:agents",
            "user:wisp",
            "user:agents",
            "package:wisp"
        ]
    );
    assert_eq!(
        skills.diagnostics[0].path.as_deref(),
        Some("/skills/review/SKILL.md")
    );
    assert_eq!(skills.diagnostics[1].severity.as_str(), "error");
    let updated = events::deserialize(fixture("skill.catalog.updated")).unwrap();
    assert_eq!(updated.updated_skill_catalog(), Some(skills));
    assert!(updated.skill_catalog("skills-1").is_none());
    let mcp = events::deserialize(fixture("rpc.mcp")).unwrap();
    assert!(mcp.mcp_status("wrong").is_none());
    let status = mcp.mcp_status("mcp-1").unwrap();
    assert_eq!(
        status
            .servers
            .iter()
            .map(|server| server.status)
            .collect::<Vec<_>>(),
        [
            McpServerStatus::Connected,
            McpServerStatus::Disconnected,
            McpServerStatus::Unavailable
        ]
    );
    assert_eq!(status.servers[1].tool_names, ["mcp__files__read"]);
    assert_eq!(
        status.servers[2].error.as_deref(),
        Some("Server startup failed")
    );
}

#[test]
fn inspection_builders_use_existing_commands_and_status_validation() {
    assert_eq!(
        WispTypedClientRpcCommands::get_skills("s")
            .unwrap()
            .to_value()
            .unwrap(),
        json!({"type":"get_skills", "id":"s"})
    );
    assert_eq!(
        WispTypedClientRpcCommands::get_mcp_status("m")
            .unwrap()
            .to_value()
            .unwrap(),
        json!({"type":"get_mcp_status", "id":"m"})
    );
    let mut bad = fixture("rpc.mcp");
    bad["status"]["servers"][0]["status"] = json!("unknown");
    assert!(events::deserialize(bad).is_err());
}

#[test]
fn actionable_skill_names_match_the_backend_directive_grammar() {
    let mut entry = events::deserialize(fixture("rpc.skills"))
        .unwrap()
        .skill_catalog("skills-1")
        .unwrap()
        .entries
        .remove(0);
    for name in ["review", "a-2-b", &"a".repeat(64)] {
        entry.name = name.into();
        assert!(entry.is_invocable(), "{name}");
    }
    for name in [
        "",
        "Review",
        "a--b",
        "-a",
        "a-",
        "a b",
        "a\n/quit",
        "a\u{1b}",
        &"a".repeat(65),
    ] {
        entry.name = name.into();
        assert!(!entry.is_invocable(), "{name}");
    }
}
