use serde::Deserialize;
use std::fs;
use wisp_process_text::{Drain, PendingText};

#[derive(Debug, Deserialize)]
struct Fixture {
    version: u32,
    cases: Vec<Case>,
}

#[derive(Debug, Deserialize)]
struct Case {
    name: String,
    max_bytes: usize,
    max_lines: usize,
    actions: Vec<Action>,
}

#[derive(Debug, Deserialize)]
#[serde(tag = "type", rename_all = "snake_case")]
enum Action {
    Append {
        hex: String,
        #[serde(default, rename = "final")]
        final_: bool,
    },
    Drain {
        expected: ExpectedDrain,
    },
}

#[derive(Debug, Deserialize)]
struct ExpectedDrain {
    text: String,
    dropped_bytes: usize,
    retained_source_bytes: usize,
    source_byte_lengths: Vec<usize>,
}

#[test]
fn shared_pending_text_fixture_matches() {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/fixtures/pending_text_conformance.json"
    );
    let fixture: Fixture = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
    assert_eq!(fixture.version, 1);

    for case in fixture.cases {
        let mut pending = PendingText::new(case.max_bytes, case.max_lines);
        for action in case.actions {
            match action {
                Action::Append { hex, final_ } => {
                    pending.append_bytes(&decode_hex(&hex), final_);
                }
                Action::Drain { expected } => {
                    let actual = pending.drain();
                    assert_drain(&case.name, actual, expected);
                }
            }
        }
    }
}

fn decode_hex(value: &str) -> Vec<u8> {
    assert_eq!(value.len() % 2, 0, "hex input has odd length");
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| {
            let text = std::str::from_utf8(pair).unwrap();
            u8::from_str_radix(text, 16).unwrap()
        })
        .collect()
}

fn assert_drain(case: &str, actual: Drain, expected: ExpectedDrain) {
    assert_eq!(actual.text, expected.text, "{case}: text");
    assert_eq!(
        actual.dropped_bytes, expected.dropped_bytes,
        "{case}: dropped_bytes"
    );
    assert_eq!(
        actual.retained_source_bytes, expected.retained_source_bytes,
        "{case}: retained_source_bytes"
    );
    assert_eq!(
        actual.source_byte_lengths, expected.source_byte_lengths,
        "{case}: source_byte_lengths"
    );
}
