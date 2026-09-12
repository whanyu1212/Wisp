use crossterm::cursor::{Hide, Show};
use crossterm::event::{DisableBracketedPaste, EnableBracketedPaste};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use std::io::{self, Stdout, Write};
use std::panic::{self, PanicHookInfo};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};

use crate::Error;

type Hook = dyn for<'a, 'b> Fn(&'a PanicHookInfo<'b>) + Send + Sync + 'static;

// A panic may occur between requesting capture and constructing the terminal guard.
// Both restoration paths must observe ownership of that partially applied request.
static MOUSE_CAPTURE: MouseCapture = MouseCapture::new();

struct MouseCapture {
    requested: AtomicBool,
}

impl MouseCapture {
    const fn new() -> Self {
        Self {
            requested: AtomicBool::new(false),
        }
    }

    fn enable(&self, output: &mut impl Write) -> io::Result<()> {
        self.requested.store(true, Ordering::SeqCst);
        // Button/wheel + SGR only: do not request unneeded motion/drag traffic.
        output.write_all(b"\x1b[?1000h\x1b[?1006h")?;
        output.flush()
    }

    fn restore(&self, output: &mut impl Write) -> io::Result<()> {
        if self.requested.load(Ordering::SeqCst) {
            output.write_all(b"\x1b[?1000l\x1b[?1006l")?;
            output.flush()?;
            // Keep ownership after an I/O failure so another cleanup path can retry.
            self.requested.store(false, Ordering::SeqCst);
        }
        Ok(())
    }
}

pub struct TerminalGuard {
    terminal: Terminal<CrosstermBackend<Stdout>>,
}

impl TerminalGuard {
    pub fn enter(mouse_enabled: bool) -> Result<Self, Error> {
        // Wisp handles NO_COLOR in its palette (grayscale with readable contrast).
        // Crossterm's own suppression would erase those grayscale colors too.
        crossterm::style::force_color_output(true);
        enable_raw_mode()?;
        let mut stdout = io::stdout();
        if let Err(error) = execute!(stdout, EnterAlternateScreen, EnableBracketedPaste, Hide) {
            restore_terminal();
            return Err(Error::Io(error));
        }
        if mouse_enabled {
            if let Err(error) = MOUSE_CAPTURE.enable(&mut stdout) {
                restore_terminal();
                return Err(Error::Io(error));
            }
        }
        match Terminal::new(CrosstermBackend::new(stdout)) {
            Ok(terminal) => Ok(Self { terminal }),
            Err(error) => {
                restore_terminal();
                Err(Error::Io(error))
            }
        }
    }

    pub fn terminal(&mut self) -> &mut Terminal<CrosstermBackend<Stdout>> {
        &mut self.terminal
    }
}

impl Drop for TerminalGuard {
    fn drop(&mut self) {
        restore_terminal();
    }
}

pub struct PanicHookGuard {
    previous: Arc<Hook>,
}

impl PanicHookGuard {
    pub fn install() -> Self {
        let previous: Arc<Hook> = panic::take_hook().into();
        let chained = Arc::clone(&previous);
        panic::set_hook(Box::new(move |info| {
            restore_terminal();
            chained(info);
        }));
        Self { previous }
    }
}

impl Drop for PanicHookGuard {
    fn drop(&mut self) {
        let _ = panic::take_hook();
        let previous = Arc::clone(&self.previous);
        panic::set_hook(Box::new(move |info| previous(info)));
    }
}

fn restore_terminal() {
    let _ = disable_raw_mode();
    let _ = MOUSE_CAPTURE.restore(&mut io::stdout());
    let _ = execute!(
        io::stdout(),
        Show,
        DisableBracketedPaste,
        LeaveAlternateScreen
    );
}

#[cfg(test)]
mod tests {
    use super::*;

    struct Broken;
    impl Write for Broken {
        fn write(&mut self, _: &[u8]) -> io::Result<usize> {
            Err(io::ErrorKind::BrokenPipe.into())
        }
        fn flush(&mut self) -> io::Result<()> {
            Err(io::ErrorKind::BrokenPipe.into())
        }
    }

    #[test]
    fn mouse_cleanup_is_opt_in_idempotent_and_retries_failed_output() {
        let capture = MouseCapture::new();
        let mut output = Vec::new();
        capture.restore(&mut output).unwrap();
        assert!(
            output.is_empty(),
            "default mode must not change mouse protocols"
        );
        capture.enable(&mut output).unwrap();
        assert_eq!(output, b"\x1b[?1000h\x1b[?1006h");
        assert!(capture.restore(&mut Broken).is_err());
        output.clear();
        capture.restore(&mut output).unwrap();
        assert_eq!(output, b"\x1b[?1000l\x1b[?1006l");
        output.clear();
        capture.restore(&mut output).unwrap();
        assert!(output.is_empty());
    }

    #[test]
    fn failed_enable_is_still_owned_by_the_panic_and_error_cleanup_path() {
        let capture = MouseCapture::new();
        assert!(capture.enable(&mut Broken).is_err());
        let mut output = Vec::new();
        capture.restore(&mut output).unwrap();
        assert_eq!(output, b"\x1b[?1000l\x1b[?1006l");
    }
}
