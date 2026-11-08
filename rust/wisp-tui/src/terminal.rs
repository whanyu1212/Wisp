use crossterm::cursor::{Hide, Show};
use crossterm::event::{
    DisableBracketedPaste, DisableFocusChange, EnableBracketedPaste, EnableFocusChange,
};
use crossterm::terminal::{
    BeginSynchronizedUpdate, EndSynchronizedUpdate, EnterAlternateScreen, LeaveAlternateScreen,
    disable_raw_mode, enable_raw_mode,
};
use crossterm::{ExecutableCommand, QueueableCommand, execute};
use ratatui::Terminal;
use ratatui::backend::{Backend, CrosstermBackend};
use std::convert::Infallible;
use std::io::{self, Stdout, Write};
use std::panic::{self, PanicHookInfo};
use std::sync::{
    Arc,
    atomic::{AtomicBool, Ordering},
};

use crate::Error;
use crate::color_depth::{BasicColorWriter, ColorDepth};

type Hook = dyn for<'a, 'b> Fn(&'a PanicHookInfo<'b>) + Send + Sync + 'static;

pub(crate) trait FrameBackend: Backend {
    fn begin_frame(&mut self) -> io::Result<()>;
    fn end_frame(&mut self) -> io::Result<()>;
    /// Converts a draw failure into the I/O error the TUI reports.
    fn into_io_error(error: Self::Error) -> io::Error;
    /// Writes terminal control output that is not part of a frame, such as the window
    /// title or a notification.
    fn write_control(&mut self, bytes: &[u8]) -> io::Result<()>;
}

impl<W: Write> FrameBackend for CrosstermBackend<W> {
    fn begin_frame(&mut self) -> io::Result<()> {
        self.queue(BeginSynchronizedUpdate).map(drop)
    }

    fn end_frame(&mut self) -> io::Result<()> {
        self.execute(EndSynchronizedUpdate).map(drop)
    }

    fn write_control(&mut self, bytes: &[u8]) -> io::Result<()> {
        self.write_all(bytes)?;
        Write::flush(self)
    }

    fn into_io_error(error: io::Error) -> io::Error {
        error
    }
}

impl FrameBackend for ratatui::backend::TestBackend {
    fn begin_frame(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn end_frame(&mut self) -> io::Result<()> {
        Ok(())
    }

    fn into_io_error(error: Infallible) -> io::Error {
        match error {}
    }

    fn write_control(&mut self, _bytes: &[u8]) -> io::Result<()> {
        Ok(())
    }
}

/// Draws one frame inside a synchronized update, downconverting its colours to `depth`
/// after `render` fills the buffer and before ratatui diffs and flushes it.
pub(crate) fn draw_frame<B: FrameBackend>(
    terminal: &mut Terminal<B>,
    depth: ColorDepth,
    render: impl FnOnce(&mut ratatui::Frame<'_>),
) -> io::Result<()> {
    terminal.backend_mut().begin_frame()?;
    let draw_result = terminal
        .draw(|frame| {
            render(frame);
            depth.downconvert_buffer(frame.buffer_mut());
        })
        .map(drop)
        .map_err(B::into_io_error);
    let end_result = terminal.backend_mut().end_frame();
    draw_result?;
    end_result
}

// A panic may occur between requesting capture and constructing the terminal guard.
// Both restoration paths must observe ownership of that partially applied request.
static MOUSE_CAPTURE: MouseCapture = MouseCapture::new();
/// The kitty keyboard protocol's first level, which reports keys a legacy terminal
/// sends alike apart (Shift+Enter from Enter, Esc from an Alt chord) while printable
/// keys still arrive as text. Terminals without the protocol ignore the request.
/// Kitty keeps one setting per screen, so it is pushed on the alternate screen and
/// popped before leaving it.
static KEYBOARD_PROTOCOL: PushedSetting = PushedSetting::new(b"\x1b[>1u", b"\x1b[<1u");
/// The window title, saved with XTWINOPS so the one Wisp sets is undone on exit.
/// Terminals without the title stack ignore both; there the shell prompt resets it.
static TITLE_STACK: PushedSetting = PushedSetting::new(b"\x1b[22;0t", b"\x1b[23;0t");

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
        // Button/wheel, motion only while a button is held (drag selection), and SGR.
        // Plain pointer motion (?1003) is never requested.
        output.write_all(b"\x1b[?1000h\x1b[?1002h\x1b[?1006h")?;
        output.flush()
    }

    fn restore(&self, output: &mut impl Write) -> io::Result<()> {
        if self.requested.load(Ordering::SeqCst) {
            output.write_all(b"\x1b[?1000l\x1b[?1002l\x1b[?1006l")?;
            output.flush()?;
            // Keep ownership after an I/O failure so another cleanup path can retry.
            self.requested.store(false, Ordering::SeqCst);
        }
        Ok(())
    }
}

/// A terminal setting kept on a stack: pushed on entry and popped exactly once on exit.
struct PushedSetting {
    push: &'static [u8],
    pop: &'static [u8],
    requested: AtomicBool,
}

impl PushedSetting {
    const fn new(push: &'static [u8], pop: &'static [u8]) -> Self {
        Self {
            push,
            pop,
            requested: AtomicBool::new(false),
        }
    }

    /// Unlike mouse capture, ownership is taken only once the push is written: a pop
    /// without its push would remove a setting the program around Wisp pushed.
    fn enable(&self, output: &mut impl Write) -> io::Result<()> {
        output.write_all(self.push)?;
        output.flush()?;
        self.requested.store(true, Ordering::SeqCst);
        Ok(())
    }

    fn restore(&self, output: &mut impl Write) -> io::Result<()> {
        if self.requested.load(Ordering::SeqCst) {
            output.write_all(self.pop)?;
            output.flush()?;
            // Keep ownership after an I/O failure so another cleanup path can retry.
            self.requested.store(false, Ordering::SeqCst);
        }
        Ok(())
    }
}

/// The backend the live TUI draws to: crossterm over stdout, with 16-colour output encoded
/// as basic ANSI codes.
pub(crate) type LiveBackend = CrosstermBackend<BasicColorWriter<Stdout>>;

pub struct TerminalGuard {
    terminal: Terminal<LiveBackend>,
}

impl TerminalGuard {
    pub fn enter(mouse_enabled: bool, color_depth: ColorDepth) -> Result<Self, Error> {
        // Wisp handles NO_COLOR in its palette (grayscale with readable contrast).
        // Crossterm's own suppression would erase those grayscale colors too.
        crossterm::style::force_color_output(true);
        enable_raw_mode()?;
        let mut stdout = io::stdout();
        if let Err(error) = execute!(
            stdout,
            EnterAlternateScreen,
            EnableBracketedPaste,
            EnableFocusChange,
            Hide
        ) {
            restore_terminal();
            return Err(Error::Io(error));
        }
        for setting in [&KEYBOARD_PROTOCOL, &TITLE_STACK] {
            if let Err(error) = setting.enable(&mut stdout) {
                restore_terminal();
                return Err(Error::Io(error));
            }
        }
        if mouse_enabled {
            if let Err(error) = MOUSE_CAPTURE.enable(&mut stdout) {
                restore_terminal();
                return Err(Error::Io(error));
            }
        }
        match Terminal::new(CrosstermBackend::new(BasicColorWriter::new(
            stdout,
            color_depth,
        ))) {
            Ok(terminal) => Ok(Self { terminal }),
            Err(error) => {
                restore_terminal();
                Err(Error::Io(error))
            }
        }
    }

    pub fn terminal(&mut self) -> &mut Terminal<LiveBackend> {
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
    let _ = KEYBOARD_PROTOCOL.restore(&mut io::stdout());
    let _ = TITLE_STACK.restore(&mut io::stdout());
    let _ = execute!(
        io::stdout(),
        EndSynchronizedUpdate,
        Show,
        DisableBracketedPaste,
        DisableFocusChange,
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
    fn crossterm_frames_use_synchronized_updates_to_avoid_tearing() {
        let mut output = Vec::new();
        {
            let mut backend = CrosstermBackend::new(&mut output);
            backend.begin_frame().unwrap();
            backend.end_frame().unwrap();
        }

        assert_eq!(output, b"\x1b[?2026h\x1b[?2026l");
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
        assert_eq!(output, b"\x1b[?1000h\x1b[?1002h\x1b[?1006h");
        assert!(capture.restore(&mut Broken).is_err());
        output.clear();
        capture.restore(&mut output).unwrap();
        assert_eq!(output, b"\x1b[?1000l\x1b[?1002l\x1b[?1006l");
        output.clear();
        capture.restore(&mut output).unwrap();
        assert!(output.is_empty());
    }

    #[test]
    fn control_output_reaches_terminal() {
        let control = b"\x1b]0;Wisp \xc2\xb7 working\x07\x1b]9;Wisp finished\x07";
        for depth in [ColorDepth::TrueColor, ColorDepth::Ansi16] {
            let mut output = Vec::new();
            {
                let mut backend = CrosstermBackend::new(BasicColorWriter::new(&mut output, depth));
                backend.write_control(control).unwrap();
            }
            assert_eq!(output, control, "{depth:?}");
        }
    }

    #[test]
    fn keyboard_protocol_pops_once() {
        let protocol = PushedSetting::new(b"\x1b[>1u", b"\x1b[<1u");
        let mut output = Vec::new();
        protocol.restore(&mut output).unwrap();
        assert!(output.is_empty(), "nothing to pop before a push");
        protocol.enable(&mut output).unwrap();
        assert_eq!(output, b"\x1b[>1u");
        // A failed pop stays owned so the other cleanup path retries it.
        assert!(protocol.restore(&mut Broken).is_err());
        output.clear();
        protocol.restore(&mut output).unwrap();
        assert_eq!(output, b"\x1b[<1u");
        output.clear();
        protocol.restore(&mut output).unwrap();
        assert!(output.is_empty());
        // A push that never reached the terminal leaves nothing to pop.
        let failed = PushedSetting::new(b"\x1b[>1u", b"\x1b[<1u");
        assert!(failed.enable(&mut Broken).is_err());
        failed.restore(&mut output).unwrap();
        assert!(output.is_empty());
    }

    #[test]
    fn failed_enable_is_still_owned_by_the_panic_and_error_cleanup_path() {
        let capture = MouseCapture::new();
        assert!(capture.enable(&mut Broken).is_err());
        let mut output = Vec::new();
        capture.restore(&mut output).unwrap();
        assert_eq!(output, b"\x1b[?1000l\x1b[?1002l\x1b[?1006l");
    }
}
