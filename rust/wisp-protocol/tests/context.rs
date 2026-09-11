use serde_json::{Value, json};
use wisp_protocol::{
    commands::WispTypedClientRpcCommands,
    events::{self, CompactionOutcome},
};

fn fixture(kind: &str) -> Value {
    serde_json::from_str::<Value>(include_str!(
        "../../../tests/fixtures/rust_tui_context.json"
    ))
    .unwrap()[kind]
        .clone()
}

#[test]
fn projects_python_context_and_compaction_events_without_losing_cost_or_outcome() {
    let stats = events::deserialize(fixture("session.stats")).unwrap();
    assert!(stats.session_stats("other").is_none());
    let stats = stats.session_stats("stats-1").unwrap();
    assert_eq!(stats.cost.known_usd, "0.0123");
    assert!(!stats.cost.complete);
    assert_eq!(stats.context.context_window, Some(10000));
    let context = events::deserialize(fixture("context.estimated"))
        .unwrap()
        .context_estimated()
        .unwrap();
    assert_eq!(context.budget, stats.context);
    let started = events::deserialize(fixture("compaction.started"))
        .unwrap()
        .compaction_started()
        .unwrap();
    assert_eq!(started.trigger_budget, Some(stats.context));
    let completed = events::deserialize(fixture("compaction.completed"))
        .unwrap()
        .compaction_completed()
        .unwrap();
    assert_eq!(completed.outcome, CompactionOutcome::Completed);
    assert!(!completed.will_retry);
    assert_eq!(completed.error.as_deref(), Some("Retry setup failed"));
}

#[test]
fn malformed_statistics_and_contradictory_compaction_fail_validation() {
    let mut stats = fixture("session.stats");
    stats["stats"]["context"]["context_window"] = json!(0);
    assert!(events::deserialize(stats).is_err());
    let mut stats = fixture("session.stats");
    stats["stats"]["cost"]
        .as_object_mut()
        .unwrap()
        .remove("known_usd");
    assert!(events::deserialize(stats).is_err());
    let mut completed = fixture("compaction.completed");
    completed["error"] = Value::Null;
    assert!(events::deserialize(completed).is_err());
    let mut completed = fixture("compaction.completed");
    completed["outcome"] = json!("failed");
    completed["will_retry"] = json!(true);
    assert!(events::deserialize(completed).is_err());
}

#[test]
fn commands_use_existing_compaction_and_process_local_configure_contracts() {
    let command = WispTypedClientRpcCommands::compact("compact-1", Some("Keep the constraints"))
        .unwrap()
        .to_value()
        .unwrap();
    assert_eq!(command["instructions"], "Keep the constraints");
    assert!(WispTypedClientRpcCommands::compact("compact-2", None).is_ok());
    let command = WispTypedClientRpcCommands::configure_auto_compaction("config-1", false)
        .unwrap()
        .to_value()
        .unwrap();
    assert_eq!(command["auto_compaction_enabled"], false);
    assert_eq!(command["clear_effort"], false);
    assert_eq!(command["persist_model_selection"], false);
    assert!(command.get("mode").is_none_or(Value::is_null));
}
