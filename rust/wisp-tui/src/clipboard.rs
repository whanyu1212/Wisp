//! Composer clipboard integration with native read access and OSC52 copy fallback.

use base64::Engine;
use crossterm::event::{KeyCode, KeyEvent, KeyModifiers};
use std::io::{self, Write};

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub(crate) enum ClipboardAction {
    Copy,
    Cut,
    Paste,
}

impl ClipboardAction {
    pub(crate) fn for_key(key: KeyEvent) -> Option<Self> {
        let control = key.modifiers == KeyModifiers::CONTROL
            || key.modifiers == (KeyModifiers::CONTROL | KeyModifiers::SHIFT);
        match key.code {
            KeyCode::Char(character) if character.eq_ignore_ascii_case(&'c') && control => {
                Some(Self::Copy)
            }
            KeyCode::Char(character) if character.eq_ignore_ascii_case(&'x') && control => {
                Some(Self::Cut)
            }
            KeyCode::Char(character) if character.eq_ignore_ascii_case(&'v') && control => {
                Some(Self::Paste)
            }
            KeyCode::Insert if key.modifiers == KeyModifiers::CONTROL => Some(Self::Copy),
            KeyCode::Insert if key.modifiers == KeyModifiers::SHIFT => Some(Self::Paste),
            KeyCode::Delete if key.modifiers == KeyModifiers::SHIFT => Some(Self::Cut),
            _ => None,
        }
    }
}

#[derive(Clone, Copy, Debug, Eq, PartialEq, thiserror::Error)]
pub(crate) enum ClipboardError {
    #[error("could not write to the system clipboard")]
    CopyFailed,
    #[error("the system clipboard does not contain readable text")]
    PasteFailed,
}

pub(crate) trait ClipboardBackend: Send {
    fn copy(&mut self, text: &str) -> Result<(), ClipboardError>;
    fn paste(&mut self) -> Result<String, ClipboardError>;
}

#[derive(Default)]
pub(crate) struct SystemClipboard {
    native: Option<arboard::Clipboard>,
}

impl SystemClipboard {
    fn native(&mut self) -> Result<&mut arboard::Clipboard, arboard::Error> {
        if self.native.is_none() {
            self.native = Some(arboard::Clipboard::new()?);
        }
        Ok(self.native.as_mut().expect("initialized clipboard"))
    }
}

impl ClipboardBackend for SystemClipboard {
    fn copy(&mut self, text: &str) -> Result<(), ClipboardError> {
        let native_succeeded = self
            .native()
            .and_then(|clipboard| clipboard.set_text(text))
            .is_ok();
        // OSC52 keeps copy useful in remote terminals where a local desktop
        // clipboard is unavailable. Like Textual, emit it even after a native
        // write so the terminal hosting an SSH session can also receive it.
        let terminal_succeeded = write_osc52(&mut io::stdout().lock(), text).is_ok();
        if native_succeeded || terminal_succeeded {
            Ok(())
        } else {
            Err(ClipboardError::CopyFailed)
        }
    }

    fn paste(&mut self) -> Result<String, ClipboardError> {
        self.native()
            .and_then(arboard::Clipboard::get_text)
            .map_err(|_| ClipboardError::PasteFailed)
    }
}

fn write_osc52(writer: &mut impl Write, text: &str) -> io::Result<()> {
    let encoded = base64::engine::general_purpose::STANDARD.encode(text);
    writer.write_all(b"\x1b]52;c;")?;
    writer.write_all(encoded.as_bytes())?;
    writer.write_all(b"\x07")?;
    writer.flush()
}

#[cfg(test)]
#[derive(Default)]
pub(crate) struct TestClipboardState {
    pub copied: Vec<String>,
    pub copy_error: Option<ClipboardError>,
    pub pastes: std::collections::VecDeque<Result<String, ClipboardError>>,
}

#[cfg(test)]
struct TestClipboard {
    state: std::sync::Arc<std::sync::Mutex<TestClipboardState>>,
}

#[cfg(test)]
impl ClipboardBackend for TestClipboard {
    fn copy(&mut self, text: &str) -> Result<(), ClipboardError> {
        let mut state = self.state.lock().expect("test clipboard lock");
        if let Some(error) = state.copy_error {
            return Err(error);
        }
        state.copied.push(text.to_owned());
        Ok(())
    }

    fn paste(&mut self) -> Result<String, ClipboardError> {
        self.state
            .lock()
            .expect("test clipboard lock")
            .pastes
            .pop_front()
            .unwrap_or(Err(ClipboardError::PasteFailed))
    }
}

#[cfg(test)]
pub(crate) fn test_clipboard() -> (
    Box<dyn ClipboardBackend>,
    std::sync::Arc<std::sync::Mutex<TestClipboardState>>,
) {
    let state = std::sync::Arc::new(std::sync::Mutex::new(TestClipboardState::default()));
    (
        Box::new(TestClipboard {
            state: std::sync::Arc::clone(&state),
        }),
        state,
    )
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn osc52_copy_base64_encodes_text_and_flushes_the_sequence() {
        let mut output = Vec::new();
        write_osc52(&mut output, "Wisp 🙂\n").unwrap();
        assert_eq!(output, b"\x1b]52;c;V2lzcCDwn5mCCg==\x07");
    }

    #[test]
    fn fixed_clipboard_chords_are_exact_for_insert_and_delete() {
        assert_eq!(
            ClipboardAction::for_key(KeyEvent::new(KeyCode::Insert, KeyModifiers::CONTROL)),
            Some(ClipboardAction::Copy)
        );
        assert_eq!(
            ClipboardAction::for_key(KeyEvent::new(KeyCode::Insert, KeyModifiers::SHIFT)),
            Some(ClipboardAction::Paste)
        );
        assert_eq!(
            ClipboardAction::for_key(KeyEvent::new(KeyCode::Delete, KeyModifiers::SHIFT)),
            Some(ClipboardAction::Cut)
        );
        assert_eq!(
            ClipboardAction::for_key(KeyEvent::new(
                KeyCode::Insert,
                KeyModifiers::CONTROL | KeyModifiers::SHIFT,
            )),
            None
        );
    }
}
