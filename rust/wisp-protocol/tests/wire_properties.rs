use std::{fs, path::Path};

use proptest::{
    collection::vec,
    prelude::*,
    test_runner::{Config, RngAlgorithm, TestRng, TestRunner},
};
use serde_json::Value;

#[path = "support/wire.rs"]
mod wire;

type Decoder = fn(&[u8]) -> bool;

fn families() -> [(&'static str, Decoder); 2] {
    [
        ("client_wire", wire::client_wire),
        ("server_wire", wire::server_wire),
    ]
}

#[test]
fn committed_wire_seeds_have_explicit_outcomes() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../fuzz/seeds");
    for (family, decode) in families() {
        for (outcome, expected) in [("valid", true), ("invalid", false)] {
            let mut paths: Vec<_> = fs::read_dir(root.join(family).join(outcome))
                .unwrap()
                .map(|entry| entry.unwrap().path())
                .collect();
            paths.sort();
            assert!(!paths.is_empty(), "missing {family}/{outcome} seeds");
            for path in paths {
                assert_eq!(
                    decode(&fs::read(&path).unwrap()),
                    expected,
                    "{}",
                    path.display()
                );
            }
        }
    }
}

#[test]
fn canonical_fixtures_use_the_strict_byte_decoder() {
    let root = Path::new(env!("CARGO_MANIFEST_DIR")).join("../../schemas/live-rpc/v6");
    for (schema, decode) in [
        ("commands.schema.json", wire::client_wire as Decoder),
        ("events.schema.json", wire::server_wire as Decoder),
    ] {
        let schema: Value = serde_json::from_slice(&fs::read(root.join(schema)).unwrap()).unwrap();
        let fixtures = schema["x-wisp-conformance-fixtures"].as_object().unwrap();
        assert!(!fixtures.is_empty());
        for (name, fixture) in fixtures {
            let bytes = serde_json::to_vec(fixture).unwrap();
            assert!(decode(&bytes), "fixture {name}");

            let mut missing_type = fixture.clone();
            missing_type.as_object_mut().unwrap().remove("type");
            assert!(!decode(&serde_json::to_vec(&missing_type).unwrap()));
            let mut unknown_type = fixture.clone();
            unknown_type["type"] = Value::String("unknown.future.wire.type".into());
            assert!(!decode(&serde_json::to_vec(&unknown_type).unwrap()));

            // Repeat the valid discriminator, so only strict duplicate rejection
            // (not a different semantic value) makes this input invalid.
            let duplicate = format!(
                "{{\"type\":{},{}",
                fixture["type"],
                &String::from_utf8(bytes.clone()).unwrap()[1..]
            );
            assert!(!decode(duplicate.as_bytes()), "duplicate type in {name}");
            assert!(!decode(&bytes[..bytes.len() - 1]), "truncated {name}");
        }
    }
}

#[test]
fn arbitrary_wire_bytes_never_panic_and_accepted_values_are_stable() {
    let mut runner = TestRunner::new_with_rng(
        Config {
            cases: 256,
            max_shrink_iters: 1024,
            failure_persistence: None,
            ..Config::default()
        },
        TestRng::from_seed(RngAlgorithm::ChaCha, &[46; 32]),
    );
    runner
        .run(&vec(any::<u8>(), 0..=8192), |bytes| {
            wire::client_wire(&bytes);
            wire::server_wire(&bytes);
            Ok(())
        })
        .unwrap();
}
