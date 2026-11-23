use serde_json::{Value, json};
use wisp_protocol::{commands, commands::WispTypedClientRpcCommands, events};

#[test]
fn project_file_contract_round_trips_and_correlates() {
    let fixtures: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/rpc_project_files.json"
    ))
    .unwrap();
    let command = WispTypedClientRpcCommands::get_project_files("files-1").unwrap();
    assert_eq!(command.to_value().unwrap()["type"], "get_project_files");
    let report = events::deserialize(fixtures["report"].clone()).unwrap();
    assert!(report.project_files("different").is_none());
    let snapshot = report.project_files("files-1").unwrap();
    assert_eq!(snapshot.generation, 1);
    assert!(snapshot.truncated);
    assert_eq!(snapshot.entries[0].kind, events::ProjectFileKind::Directory);
    assert_eq!(snapshot.entries[1].path, "src/资料 \"example\".py");
    let invalidated = events::deserialize(fixtures["invalidated"].clone()).unwrap();
    assert_eq!(invalidated.project_files_invalidated(), Some(2));
    assert!(invalidated.project_files("files-1").is_none());
}

#[test]
fn discovery_requests_distinguish_folders_and_indexing() {
    let root = WispTypedClientRpcCommands::get_project_directory("root", "").unwrap();
    assert_eq!(root.to_value().unwrap()["directory"], "");
    let folder = WispTypedClientRpcCommands::get_project_directory("dir", "src/wisp").unwrap();
    assert_eq!(folder.to_value().unwrap()["directory"], "src/wisp");
    let index = WispTypedClientRpcCommands::get_project_file_index("index").unwrap();
    assert_eq!(index.to_value().unwrap()["progressive"], true);

    for path in ["/outside", "../outside", "a/../b", "a/./b", "a//b"] {
        assert!(WispTypedClientRpcCommands::get_project_directory("dir", path).is_err());
    }
    assert!(
        commands::deserialize(json!({
            "type": "get_project_files",
            "id": "dir",
            "directory": "",
            "progressive": true
        }))
        .is_err()
    );
}

#[test]
fn project_file_contract_rejects_unsafe_paths_and_unbounded_metadata() {
    let fixtures: Value = serde_json::from_str(include_str!(
        "../../../tests/fixtures/rpc_project_files.json"
    ))
    .unwrap();
    for path in [
        "/outside",
        "../outside",
        "a/../b",
        "a/./b",
        "a//b",
        "a\u{1b}[31m",
        "a\\b",
    ] {
        let mut report = fixtures["report"].clone();
        report["entries"][0]["path"] = json!(path);
        assert!(events::deserialize(report).is_err(), "accepted {path:?}");
    }
    let mut report = fixtures["report"].clone();
    report["entries"] = json!(vec![json!({"path":"a", "kind":"file"}); 10001]);
    assert!(events::deserialize(report).is_err());
}
