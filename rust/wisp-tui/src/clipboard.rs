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
    #[error("the system clipboard does not contain an image")]
    NoImage,
}

/// A clipboard bitmap: 8-bit RGBA rows, top to bottom.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct ClipboardImage {
    pub width: usize,
    pub height: usize,
    pub rgba: Vec<u8>,
}

pub(crate) trait ClipboardBackend: Send {
    fn copy(&mut self, text: &str) -> Result<(), ClipboardError>;
    fn paste(&mut self) -> Result<String, ClipboardError>;
    /// Read an image from the clipboard; `NoImage` when it holds none.
    fn paste_image(&mut self) -> Result<ClipboardImage, ClipboardError>;
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
        copy_with_osc52(native_succeeded, &mut io::stdout().lock(), text)
    }

    fn paste(&mut self) -> Result<String, ClipboardError> {
        self.native()
            .and_then(arboard::Clipboard::get_text)
            .map_err(|_| ClipboardError::PasteFailed)
    }

    fn paste_image(&mut self) -> Result<ClipboardImage, ClipboardError> {
        let image = self
            .native()
            .and_then(arboard::Clipboard::get_image)
            .map_err(|_| ClipboardError::NoImage)?;
        Ok(ClipboardImage {
            width: image.width,
            height: image.height,
            rgba: image.bytes.into_owned(),
        })
    }
}

/// Emit OSC 52 alongside a native write, succeeding whenever either path succeeds so
/// copying works both locally and over SSH where no desktop clipboard exists.
fn copy_with_osc52(
    native_succeeded: bool,
    writer: &mut impl Write,
    text: &str,
) -> Result<(), ClipboardError> {
    let terminal_succeeded = write_osc52(writer, text).is_ok();
    if native_succeeded || terminal_succeeded {
        Ok(())
    } else {
        Err(ClipboardError::CopyFailed)
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
    /// Images handed out by `paste_image`; empty means the clipboard holds none.
    pub image_pastes: std::collections::VecDeque<ClipboardImage>,
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

    fn paste_image(&mut self) -> Result<ClipboardImage, ClipboardError> {
        self.state
            .lock()
            .expect("test clipboard lock")
            .image_pastes
            .pop_front()
            .ok_or(ClipboardError::NoImage)
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
    fn osc52_fallback_succeeds_when_native_clipboard_is_unavailable() {
        struct FailingWriter;
        impl Write for FailingWriter {
            fn write(&mut self, _buf: &[u8]) -> io::Result<usize> {
                Err(io::Error::new(io::ErrorKind::BrokenPipe, "closed"))
            }
            fn flush(&mut self) -> io::Result<()> {
                Ok(())
            }
        }

        let mut output = Vec::new();
        copy_with_osc52(false, &mut output, "hello over ssh").unwrap();
        assert_eq!(output, b"\x1b]52;c;aGVsbG8gb3ZlciBzc2g=\x07");

        assert_eq!(
            copy_with_osc52(false, &mut FailingWriter, "hello"),
            Err(ClipboardError::CopyFailed)
        );
        assert_eq!(copy_with_osc52(true, &mut FailingWriter, "hello"), Ok(()));
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
