//! The native walk on real directories. `tests/tools/search/test_native_walk_conformance.py`
//! compares it with the Python walk; these cover the Rust API directly.

#![cfg(unix)]

use std::fs;
use std::os::fd::AsRawFd;
use std::os::unix::fs::symlink;
use std::path::{Path, PathBuf};

use wisp_search::walk::{ENTRIES_PER_BATCH, Walk, WalkConfig, WalkError};

struct TempTree(PathBuf);

impl TempTree {
    fn new(name: &str) -> Self {
        let root = std::env::temp_dir().join(format!("wisp-walk-{}-{name}", std::process::id()));
        let _ = fs::remove_dir_all(&root);
        fs::create_dir_all(&root).unwrap();
        Self(root)
    }

    fn file(&self, relative: &str, content: &str) {
        let path = self.0.join(relative);
        fs::create_dir_all(path.parent().unwrap()).unwrap();
        fs::write(path, content).unwrap();
    }
}

impl Drop for TempTree {
    fn drop(&mut self) {
        let _ = fs::remove_dir_all(&self.0);
    }
}

fn config() -> WalkConfig {
    WalkConfig {
        ancestors: Vec::new(),
        in_git_repository: false,
        include_ignored: false,
        max_directory_entries: 100,
        max_ignore_file_bytes: 1_000,
        max_ignore_file_patterns: 100,
        max_git_marker_bytes: 4_096,
        max_depth: 100,
    }
}

fn walk(root: &Path, config: WalkConfig) -> Result<Vec<(String, bool)>, WalkError> {
    let directory = fs::File::open(root).unwrap();
    let mut walk = Walk::new(directory.as_raw_fd(), config).unwrap();
    let mut files = Vec::new();
    while let Some(batch) = walk.next_batch()? {
        for file in batch.files {
            let mut path = batch.directory.clone();
            if !path.is_empty() {
                path.push(b'/');
            }
            path.extend_from_slice(&file.name);
            files.push((String::from_utf8(path).unwrap(), file.hidden_or_ignored));
        }
    }
    Ok(files)
}

fn names(files: Vec<(String, bool)>) -> Vec<String> {
    files.into_iter().map(|(name, _)| name).collect()
}

#[test]
fn walks_subdirectories_in_place() {
    let tree = TempTree::new("order");
    for path in ["a.txt", "b/inner.txt", "b/deeper/x.txt", "c.txt"] {
        tree.file(path, "");
    }

    assert_eq!(
        names(walk(&tree.0, config()).unwrap()),
        ["a.txt", "b/deeper/x.txt", "b/inner.txt", "c.txt"]
    );
}

#[test]
fn skips_links_and_hidden_entries() {
    let tree = TempTree::new("links");
    tree.file("real/x.txt", "");
    tree.file(".hidden/y.txt", "");
    tree.file(".dotfile", "");
    symlink(tree.0.join("real"), tree.0.join("linked")).unwrap();
    symlink(tree.0.join("real/x.txt"), tree.0.join("link.txt")).unwrap();

    assert_eq!(names(walk(&tree.0, config()).unwrap()), ["real/x.txt"]);
}

#[test]
fn applies_ignore_files_and_reports_overrides() {
    let tree = TempTree::new("ignore");
    tree.file(".ignore", "*.log\nbuild/\n");
    tree.file("a.log", "");
    tree.file("build/b.txt", "");
    tree.file("c.txt", "");

    assert_eq!(names(walk(&tree.0, config()).unwrap()), ["c.txt"]);
    let mut including = config();
    including.include_ignored = true;
    assert_eq!(
        walk(&tree.0, including).unwrap(),
        [
            (".ignore".to_owned(), true),
            ("a.log".to_owned(), true),
            ("build/b.txt".to_owned(), true),
            ("c.txt".to_owned(), false),
        ]
    );
}

#[test]
fn returns_between_ignored_entries() {
    let tree = TempTree::new("budget");
    tree.file(".ignore", "*.o\n");
    for index in 0..ENTRIES_PER_BATCH {
        tree.file(&format!("{index:04}.o"), "");
    }
    tree.file("z.txt", "");
    let directory = fs::File::open(&tree.0).unwrap();
    let mut limits = config();
    limits.max_directory_entries = 2 * ENTRIES_PER_BATCH;
    let mut walk = Walk::new(directory.as_raw_fd(), limits).unwrap();

    let first = walk.next_batch().unwrap().unwrap();
    let second = walk.next_batch().unwrap().unwrap();

    assert!(first.files.is_empty());
    assert_eq!(second.files[0].name, b"z.txt");
}

#[test]
fn gitignore_needs_a_repository() {
    let tree = TempTree::new("repository");
    tree.file(".gitignore", "a.txt\n");
    tree.file("a.txt", "");

    assert_eq!(names(walk(&tree.0, config()).unwrap()), ["a.txt"]);
    fs::create_dir(tree.0.join(".git")).unwrap();
    assert!(walk(&tree.0, config()).unwrap().is_empty());
}

#[test]
fn reports_limits() {
    let tree = TempTree::new("limits");
    tree.file("nested/a", "");
    tree.file("nested/b", "");
    tree.file(".ignore", "one\ntwo\n");

    let mut entries = config();
    entries.max_directory_entries = 1;
    assert!(matches!(
        walk(&tree.0, entries),
        Err(WalkError::DirectoryEntryLimit { directory }) if directory.is_empty()
    ));
    let mut patterns = config();
    patterns.max_ignore_file_patterns = 1;
    assert!(matches!(
        walk(&tree.0, patterns),
        Err(WalkError::TooManyIgnorePatterns { file }) if file == b".ignore"
    ));
}

#[test]
fn skips_directories_past_max_depth() {
    let tree = TempTree::new("depth");
    for path in [
        "top.txt",
        "a/one.txt",
        "a/b/two.txt",
        "a/b/c/three.txt",
        "x/one.txt",
    ] {
        tree.file(path, "");
    }
    let directory = fs::File::open(&tree.0).unwrap();
    let mut walk = Walk::new(
        directory.as_raw_fd(),
        WalkConfig {
            max_depth: 2,
            ..config()
        },
    )
    .unwrap();
    let mut files = Vec::new();
    while let Some(batch) = walk.next_batch().unwrap() {
        for file in batch.files {
            let directory = String::from_utf8(batch.directory.clone()).unwrap();
            let name = String::from_utf8(file.name).unwrap();
            files.push(if directory.is_empty() {
                name
            } else {
                format!("{directory}/{name}")
            });
        }
    }

    // a/b/c is three levels down, so only it is skipped.
    assert_eq!(files, ["a/b/two.txt", "a/one.txt", "top.txt", "x/one.txt"]);
    assert_eq!(walk.deep_directories_skipped(), 1);
}

#[test]
fn skips_directories_it_cannot_search() {
    use std::os::unix::fs::PermissionsExt;

    let tree = TempTree::new("list-only");
    tree.file("open/a.txt", "");
    tree.file("list-only/b.txt", "");
    let list_only = tree.0.join("list-only");
    // Readable but not searchable: listing works, but stat of its entries fails.
    fs::set_permissions(&list_only, fs::Permissions::from_mode(0o444)).unwrap();
    // Root ignores permissions, so the case only arises for other users.
    let enforced = fs::metadata(list_only.join("b.txt")).is_err();

    let walked = walk(&tree.0, config());
    fs::set_permissions(&list_only, fs::Permissions::from_mode(0o755)).unwrap();

    if enforced {
        assert_eq!(names(walked.unwrap()), ["open/a.txt"]);
    }
}
