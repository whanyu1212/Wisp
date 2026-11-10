//! Folder names for `/cd`, completed from the local file system.
//!
//! The TUI runs on the same machine as the backend it launched, so it lists folders
//! itself rather than asking the backend. The backend still resolves and checks the
//! folder `/cd` names; a listing here only saves typing.

use std::path::{Path, PathBuf};

/// The most folders a listing offers, so a large folder keeps the popup short.
const MAX_CANDIDATES: usize = 64;
/// The most matching names a listing checks, so a huge folder cannot stall a keystroke.
const MAX_CHECKED_NAMES: usize = 4096;

/// Folders that complete a partly typed `/cd` path.
#[derive(Clone, Debug, Default, Eq, PartialEq)]
pub(crate) struct DirectoryListing {
    /// Each candidate as the whole argument it would become, such as `src/ui/`, sorted.
    pub candidates: Vec<String>,
    /// The typed path already names a folder, so submitting it is a complete command.
    pub names_directory: bool,
}

/// Lists the folders that complete `partial`, the text typed after `/cd `.
///
/// `partial` splits at its last `/` into the folder to list and the name prefix to
/// match. The folder is relative to `cwd` unless it is absolute or starts with `~`,
/// which `home` replaces. Names holding control characters are left out, and so are
/// hidden folders unless the prefix starts with `.`.
pub(crate) fn list(partial: &str, cwd: Option<&Path>, home: Option<&Path>) -> DirectoryListing {
    let (folder_text, prefix) = match partial.rfind('/') {
        Some(slash) => partial.split_at(slash + 1),
        None if partial == "~" => ("~/", ""),
        None => ("", partial),
    };
    let Some(folder) = resolve(folder_text, cwd, home) else {
        return DirectoryListing::default();
    };
    let names_directory =
        !partial.is_empty() && resolve(partial, cwd, home).is_some_and(|path| path.is_dir());
    let Ok(entries) = std::fs::read_dir(&folder) else {
        return DirectoryListing {
            candidates: Vec::new(),
            names_directory,
        };
    };
    let mut names: Vec<String> = entries
        .filter_map(Result::ok)
        .filter_map(|entry| entry.file_name().into_string().ok())
        .filter(|name| name.starts_with(prefix))
        .filter(|name| prefix.starts_with('.') || !name.starts_with('.'))
        .filter(|name| !name.chars().any(char::is_control))
        // Reading names is cheap; checking each is a folder costs a `stat`.
        .take(MAX_CHECKED_NAMES)
        // `metadata` follows links, so a link to a folder counts as one.
        .filter(|name| std::fs::metadata(folder.join(name)).is_ok_and(|meta| meta.is_dir()))
        .collect();
    names.sort();
    names.truncate(MAX_CANDIDATES);
    DirectoryListing {
        candidates: names
            .into_iter()
            .map(|name| format!("{folder_text}{name}/"))
            .collect(),
        names_directory,
    }
}

/// The folder `text` names, as the backend would resolve it.
fn resolve(text: &str, cwd: Option<&Path>, home: Option<&Path>) -> Option<PathBuf> {
    if text == "~" || text.starts_with("~/") {
        return Some(home?.join(text.trim_start_matches('~').trim_start_matches('/')));
    }
    if text.starts_with('~') {
        // `~user` names another account's home; the backend expands it, but completion
        // does not look accounts up.
        return None;
    }
    let path = Path::new(text);
    if path.is_absolute() {
        return Some(path.to_path_buf());
    }
    Some(cwd?.join(path))
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Scratch(PathBuf);

    impl Scratch {
        fn new(name: &str) -> Self {
            let path = std::env::temp_dir().join(format!("wisp-cd-{name}-{}", std::process::id()));
            let _ = std::fs::remove_dir_all(&path);
            for folder in ["src/ui", "scripts", "site", ".git", "tests"] {
                std::fs::create_dir_all(path.join(folder)).unwrap();
            }
            std::fs::write(path.join("setup.py"), "").unwrap();
            Self(path)
        }
    }

    impl Drop for Scratch {
        fn drop(&mut self) {
            let _ = std::fs::remove_dir_all(&self.0);
        }
    }

    fn candidates(partial: &str, scratch: &Scratch) -> Vec<String> {
        list(partial, Some(&scratch.0), None).candidates
    }

    #[test]
    fn lists_matching_folders() {
        let scratch = Scratch::new("matching");
        assert_eq!(candidates("s", &scratch), ["scripts/", "site/", "src/"]);
        assert_eq!(candidates("src/", &scratch), ["src/ui/"]);
        // Files never complete, and hidden folders only for a leading dot.
        assert_eq!(
            candidates("", &scratch),
            ["scripts/", "site/", "src/", "tests/"]
        );
        assert_eq!(candidates(".", &scratch), [".git/"]);
    }

    #[test]
    fn expands_home_and_absolute() {
        let scratch = Scratch::new("home");
        let home = list("~/sr", None, Some(&scratch.0));
        assert_eq!(home.candidates, ["~/src/"]);
        let root = format!("{}/t", scratch.0.display());
        assert_eq!(
            list(&root, None, None).candidates,
            [format!("{}/tests/", scratch.0.display())]
        );
        assert!(
            list("~other/", None, Some(&scratch.0))
                .candidates
                .is_empty()
        );
    }

    #[test]
    fn reports_typed_folder() {
        let scratch = Scratch::new("typed");
        assert!(list("src", Some(&scratch.0), None).names_directory);
        assert!(list("src/", Some(&scratch.0), None).names_directory);
        assert!(!list("sr", Some(&scratch.0), None).names_directory);
        assert!(!list("setup.py", Some(&scratch.0), None).names_directory);
        assert!(!list("", Some(&scratch.0), None).names_directory);
    }

    #[cfg(unix)]
    #[test]
    fn follows_folder_links() {
        let scratch = Scratch::new("links");
        std::os::unix::fs::symlink(scratch.0.join("src"), scratch.0.join("linked")).unwrap();
        assert_eq!(candidates("l", &scratch), ["linked/"]);
    }
}
