//! Bounded, presentation-only state compatible with Textual's ~/.wisp/tui.json.

use crate::theme::{self, Theme};
use serde_json::value::{RawValue, to_raw_value};
use std::{
    collections::BTreeMap,
    fs::{self, File, OpenOptions},
    io::{self, Read, Write},
    os::unix::fs::OpenOptionsExt,
    path::PathBuf,
    sync::atomic::{AtomicU64, Ordering},
};

const MAX_PREFERENCE_BYTES: usize = 64 * 1024;

#[derive(Clone, Copy, Debug)]
pub(crate) struct ThemeSelection {
    pub active: &'static Theme,
    pub last_dark: &'static Theme,
}

impl Default for ThemeSelection {
    fn default() -> Self {
        Self {
            active: theme::default_theme(),
            last_dark: theme::default_theme(),
        }
    }
}

impl ThemeSelection {
    pub fn select(&mut self, theme: &'static Theme) {
        self.active = theme;
        if theme.dark {
            self.last_dark = theme;
        }
    }

    pub fn toggle(&mut self) {
        self.select(if self.active.dark {
            theme::paper_theme()
        } else {
            self.last_dark
        });
    }
}

#[derive(Debug)]
pub(crate) struct ThemePreferences {
    path: PathBuf,
}

impl ThemePreferences {
    pub fn from_environment() -> Option<Self> {
        let home = PathBuf::from(std::env::var_os("HOME")?);
        // Never interpret a missing/relative home as project-local preferences.
        home.is_absolute()
            .then(|| Self::at(home.join(".wisp/tui.json")))
    }

    pub fn at(path: PathBuf) -> Self {
        Self { path }
    }

    pub fn load(&self) -> ThemeSelection {
        let Ok(document) = self.read_document() else {
            return ThemeSelection::default();
        };
        let active = document
            .get("theme")
            .and_then(|value| serde_json::from_str::<String>(value.get()).ok())
            .and_then(|name| theme::named(&name))
            .unwrap_or_else(theme::default_theme);
        let last_dark = document
            .get("last_dark_theme")
            .and_then(|value| serde_json::from_str::<String>(value.get()).ok())
            .and_then(|name| theme::named(&name))
            .filter(|theme| theme.dark)
            .unwrap_or_else(|| {
                if active.dark {
                    active
                } else {
                    theme::default_theme()
                }
            });
        ThemeSelection { active, last_dark }
    }

    /// Missing or malformed JSON is repairable, but unreadable/non-UTF-8/oversized
    /// documents are never replaced: their unrelated preferences are unknown.
    /// Unowned values stay raw so Python-sized integers and decimal precision
    /// survive without passing through serde_json::Value's numeric representation.
    fn read_document(&self) -> io::Result<BTreeMap<String, Box<RawValue>>> {
        let file = match OpenOptions::new()
            .read(true)
            .custom_flags(nix::libc::O_NONBLOCK)
            .open(&self.path)
        {
            Ok(file) => file,
            Err(error) if error.kind() == io::ErrorKind::NotFound => return Ok(BTreeMap::new()),
            Err(error) => return Err(error),
        };
        if !file.metadata()?.is_file() {
            return Err(io::Error::other("theme preferences are not a regular file"));
        }
        let mut bytes = Vec::new();
        file.take((MAX_PREFERENCE_BYTES + 1) as u64)
            .read_to_end(&mut bytes)?;
        if bytes.len() > MAX_PREFERENCE_BYTES {
            return Err(io::Error::other("theme preferences exceed the read limit"));
        }
        let text = std::str::from_utf8(&bytes)
            .map_err(|_| io::Error::other("theme preferences are not UTF-8"))?;
        Ok(serde_json::from_str(text).unwrap_or_default())
    }

    pub fn save(&self, selection: ThemeSelection) -> io::Result<()> {
        let mut document = self.read_document()?;
        document.insert("theme".into(), to_raw_value(&selection.active.name)?);
        document.insert(
            "last_dark_theme".into(),
            to_raw_value(&selection.last_dark.name)?,
        );
        let mut bytes = serde_json::to_vec_pretty(&document)?;
        bytes.push(b'\n');
        if bytes.len() > MAX_PREFERENCE_BYTES {
            return Err(io::Error::other("theme preferences exceed the write limit"));
        }
        let parent = self
            .path
            .parent()
            .ok_or_else(|| io::Error::other("missing preference directory"))?;
        fs::create_dir_all(parent)?;
        let mut staged = StagedPreference::create(parent.to_path_buf())?;
        staged.file.write_all(&bytes)?;
        staged.file.sync_all()?;
        fs::rename(&staged.path, &self.path)?;
        Ok(())
    }
}

struct StagedPreference {
    path: PathBuf,
    file: File,
}

impl StagedPreference {
    fn create(parent: PathBuf) -> io::Result<Self> {
        static NEXT: AtomicU64 = AtomicU64::new(0);
        for _ in 0..16 {
            let path = parent.join(format!(
                ".tui.json.{}.{}.tmp",
                std::process::id(),
                NEXT.fetch_add(1, Ordering::Relaxed)
            ));
            match OpenOptions::new()
                .write(true)
                .create_new(true)
                .mode(0o600)
                .open(&path)
            {
                Ok(file) => return Ok(Self { path, file }),
                Err(error) if error.kind() == io::ErrorKind::AlreadyExists => continue,
                Err(error) => return Err(error),
            }
        }
        Err(io::Error::other("could not stage theme preferences"))
    }
}

impl Drop for StagedPreference {
    fn drop(&mut self) {
        // Only our create_new-owned sibling is removed, never the existing document.
        let _ = fs::remove_file(&self.path);
    }
}
