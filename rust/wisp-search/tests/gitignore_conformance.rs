//! The Rust gitignore port reaches pathspec's verdict for every fixture case.
//!
//! `tests/fixtures/gitignore_conformance.json` is generated from Python by
//! `tests/tools/search/test_gitignore_conformance.py`: raw ignore file contents and
//! paths, with what pathspec's `check_file` returns for each path as a file and as a
//! directory, and whether `_may_reinclude_descendant` would descend into it.

use std::fs;

use serde::Deserialize;
use wisp_search::gitignore::{GitIgnore, may_reinclude_descendant, wtf8_from_raw};

#[derive(Deserialize)]
struct Fixture {
    version: u32,
    paths: Vec<String>,
    cases: Vec<Case>,
}

#[derive(Deserialize)]
struct Case {
    content: String,
    file: String,
    directory: String,
    reinclude: String,
}

#[test]
fn matches_pathspec_fixture() {
    let path = concat!(
        env!("CARGO_MANIFEST_DIR"),
        "/../../tests/fixtures/gitignore_conformance.json"
    );
    let fixture: Fixture = serde_json::from_str(&fs::read_to_string(path).unwrap()).unwrap();
    assert_eq!(fixture.version, 1);
    let paths: Vec<Vec<u8>> = fixture
        .paths
        .iter()
        .map(|path| wtf8_from_raw(&decode_hex(path)))
        .collect();

    let mut mismatches = Vec::new();
    for case in &fixture.cases {
        let content = decode_hex(&case.content);
        let gitignore = GitIgnore::parse(&content, usize::MAX).unwrap();
        let expected = case.file.chars().zip(case.directory.chars());
        let expected = expected.zip(case.reinclude.chars());
        for (path, ((file, directory), reinclude)) in paths.iter().zip(expected) {
            let mut directory_path = path.clone();
            directory_path.push(b'/');
            let actual = (
                verdict(&gitignore, path),
                verdict(&gitignore, &directory_path),
                if may_reinclude_descendant(&[(&gitignore, &directory_path)]) {
                    '1'
                } else {
                    '0'
                },
            );
            if actual != (file, directory, reinclude) {
                mismatches.push(format!(
                    "{:?} on {:?}: expected {:?}, got {actual:?}",
                    String::from_utf8_lossy(&content),
                    String::from_utf8_lossy(path),
                    (file, directory, reinclude),
                ));
            }
        }
    }
    assert!(mismatches.is_empty(), "{}", mismatches.join("\n"));
}

fn verdict(gitignore: &GitIgnore, path: &[u8]) -> char {
    match gitignore.check(path) {
        None => '-',
        Some(found) => {
            let base = if found.ignored { b'A' } else { b'a' };
            char::from(base + u8::try_from(found.index).unwrap())
        }
    }
}

fn decode_hex(value: &str) -> Vec<u8> {
    assert_eq!(value.len() % 2, 0, "hex input has odd length");
    value
        .as_bytes()
        .chunks_exact(2)
        .map(|pair| u8::from_str_radix(std::str::from_utf8(pair).unwrap(), 16).unwrap())
        .collect()
}
