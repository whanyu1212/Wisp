//! The Rust event decoder accepts exactly the events Python accepts.
//!
//! `tests/fixtures/event_decoding_conformance.json` is generated from Python by
//! `tests/rpc/test_event_decoding_conformance.py`: every canonical v9 event with
//! one field dropped (at any depth) or one unknown field added, plus whether
//! Python's decoder accepts it. Each case must get the same verdict here.

use std::collections::BTreeMap;

use serde::Deserialize;
use serde_json::Value;
use wisp_protocol::events;

#[derive(Deserialize)]
struct Fixture {
    version: u32,
    unknown_field: String,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    event: String,
    operation: String,
    path: Vec<Value>,
    accepted: bool,
}

/// Fields Python fills with a value computed at decode time, which a JSON schema
/// default cannot express: Rust keeps them required. Every backend emits them.
/// A new entry here means a new decode-time mismatch, so keep the list exact.
fn computed_default_field(path: &[Value]) -> bool {
    let keys: Vec<&str> = path.iter().filter_map(Value::as_str).collect();
    matches!(
        keys.as_slice(),
        // `utc_now()` at construction.
        ["timestamp"]
        // `SessionCostSummary()`, a default_factory.
        | ["stats", "cost"]
        // Derived from the estimate and observation by a model validator.
        | [.., "effective_tokens"]
    )
}

fn canonical_events() -> BTreeMap<String, Value> {
    let schema: Value = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../schemas/live-rpc/events.schema.json"
    )))
    .unwrap();
    serde_json::from_value(schema["x-wisp-conformance-fixtures"].clone()).unwrap()
}

/// Return the object at ``path`` inside ``value``, following keys and indexes.
fn object_at<'a>(value: &'a mut Value, path: &[Value]) -> &'a mut serde_json::Map<String, Value> {
    let target = path.iter().fold(value, |target, step| match step {
        Value::String(key) => &mut target[key.as_str()],
        Value::Number(index) => &mut target[index.as_u64().unwrap() as usize],
        _ => panic!("fixture path steps are keys or indexes"),
    });
    target.as_object_mut().expect("fixture paths name objects")
}

fn variant(event: &Value, case: &Case, unknown_field: &str) -> Value {
    let mut variant = event.clone();
    match case.operation.as_str() {
        "remove" => {
            let (key, parent) = case.path.split_last().expect("removals name a field");
            object_at(&mut variant, parent).remove(key.as_str().unwrap());
        }
        "add_unknown" => {
            object_at(&mut variant, &case.path).insert(unknown_field.to_owned(), Value::Null);
        }
        operation => panic!("unknown fixture operation: {operation}"),
    }
    variant
}

#[test]
fn every_event_variant_gets_the_python_verdict() {
    let fixture: Fixture = serde_json::from_str(include_str!(concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/fixtures/event_decoding_conformance.json"
    )))
    .unwrap();
    assert_eq!(fixture.version, 1);
    let canonical = canonical_events();
    assert!(!fixture.cases.is_empty());

    let mut exceptions = 0;
    let mismatches: Vec<String> = fixture
        .cases
        .iter()
        .filter_map(|case| {
            let event = &canonical[&case.event];
            let accepted =
                events::deserialize(variant(event, case, &fixture.unknown_field)).is_ok();
            if case.operation == "remove" && computed_default_field(&case.path) {
                // Python accepts the omission; Rust must still reject it.
                assert!(
                    case.accepted && !accepted,
                    "{} {:?} is no longer a computed default",
                    case.event,
                    case.path
                );
                exceptions += 1;
                return None;
            }
            (accepted != case.accepted).then(|| {
                format!(
                    "{} {} {:?}: python {}, rust {}",
                    case.event, case.operation, case.path, case.accepted, accepted
                )
            })
        })
        .collect();
    assert!(
        mismatches.is_empty(),
        "{} of {} cases disagree:\n{}",
        mismatches.len(),
        fixture.cases.len(),
        mismatches.join("\n")
    );
    // Every canonical event carries a timestamp; the other exceptions are rarer.
    assert!(exceptions >= canonical.len());
}
