use crossterm::cursor::{Hide, Show};
use crossterm::event::{
    DisableBracketedPaste, DisableFocusChange, EnableBracketedPaste, EnableFocusChange,
};
use crossterm::terminal::{
    BeginSynchronizedUpdate, EndSynchronizedUpdate, EnterAlternateScreen, LeaveAlternateScreen,
    disable_raw_mode, enable_raw_mode,
};
use crossterm::{ExecutableCommand, QueueableCommand, execute};
use nix::sys::termios::{self, LocalFlags, SetArg, Termios};
use ratatui::Terminal;
use ratatui::backend::{Backend, CrosstermBackend};
use std::convert::Infallible;
use std::io::{self, Stdout, Write};
use std::os::fd::AsFd;
use std::panic::{self, PanicHookInfo};
use std::sync::{
    Arc, Mutex, PoisonError,
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
    /// Gives the terminal to a child process: leaves raw mode, the alternate screen and
    /// every input mode the TUI turned on. The saved window title stays saved, so the
    /// title Wisp restores on exit is still the one from before it started.
    fn suspend(&mut self) -> io::Result<Suspended>;
    /// Takes the terminal back after [`FrameBackend::suspend`]. The screen must then be
    /// cleared so the next frame repaints all of it.
    fn resume(&mut self, suspended: Suspended) -> io::Result<()>;
}

/// What [`FrameBackend::suspend`] turned off that resuming turns back on.
#[derive(Debug)]
pub(crate) struct Suspended {
    mouse_capture: bool,
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

    // The live terminal is always stdout, which the screen modes are written to, as
    // they are when the TUI starts and exits.
    fn suspend(&mut self) -> io::Result<Suspended> {
        Write::flush(self)?;
        let suspended = Suspended {
            mouse_capture: MOUSE_CAPTURE.requested.load(Ordering::SeqCst),
        };
        leave_screen();
        Ok(suspended)
    }

    fn resume(&mut self, suspended: Suspended) -> io::Result<()> {
        enter_screen(&mut io::stdout(), suspended.mouse_capture)
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

    fn suspend(&mut self) -> io::Result<Suspended> {
        Ok(Suspended {
            mouse_capture: false,
        })
    }

    fn resume(&mut self, _suspended: Suspended) -> io::Result<()> {
        Ok(())
    }
}

/// Draws one frame inside a synchronized update. After `render` fills the buffer,
/// `finish` gets the complete frame, then its colours are downconverted to `depth`,
/// before ratatui diffs and flushes it.
pub(crate) fn draw_frame<B: FrameBackend>(
    terminal: &mut Terminal<B>,
    depth: ColorDepth,
    render: impl FnOnce(&mut ratatui::Frame<'_>),
    finish: impl FnOnce(&mut ratatui::buffer::Buffer),
) -> io::Result<()> {
    terminal.backend_mut().begin_frame()?;
    let draw_result = terminal
        .draw(|frame| {
            render(frame);
            finish(frame.buffer_mut());
            depth.downconvert_buffer(frame.buffer_mut());
        })
        .map(drop)
        .map_err(B::into_io_error);
    let end_result = terminal.backend_mut().end_frame();
    draw_result?;
    end_result
}

/// Clears the screen and forgets the last frame, so the next frame repaints every cell,
/// at the terminal's current size.
///
/// `Terminal::clear` would do the same but first asks the terminal where its cursor is,
/// and the input task can take that reply as an ordinary event, which leaves the query
/// waiting until it times out. Resizing to the current size clears without asking.
pub(crate) fn repaint_all<B: FrameBackend>(terminal: &mut Terminal<B>) -> io::Result<()> {
    let size = terminal.size().map_err(B::into_io_error)?;
    terminal
        .resize(ratatui::layout::Rect::new(0, 0, size.width, size.height))
        .map_err(B::into_io_error)
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
    /// The terminal's attributes from before raw mode, when stdin is a terminal.
    cooked: Option<Termios>,
}

impl TerminalGuard {
    pub fn enter(mouse_enabled: bool, color_depth: ColorDepth) -> Result<Self, Error> {
        // Wisp handles NO_COLOR in its palette (grayscale with readable contrast).
        // Crossterm's own suppression would erase those grayscale colors too.
        crossterm::style::force_color_output(true);
        let mut stdout = io::stdout();
        let cooked = termios::tcgetattr(io::stdin()).ok();
        if let Err(error) =
            enter_screen(&mut stdout, mouse_enabled).and_then(|()| TITLE_STACK.enable(&mut stdout))
        {
            restore_terminal();
            return Err(Error::Io(error));
        }
        match Terminal::new(CrosstermBackend::new(BasicColorWriter::new(
            stdout,
            color_depth,
        ))) {
            Ok(terminal) => Ok(Self { terminal, cooked }),
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
        // The backend is still shutting down and shares this process group. Leaving raw
        // mode would let a second Ctrl+C reach it as SIGINT and end it mid-cleanup, so
        // go from raw mode to the earlier attributes without the signal keys in one
        // step. `disable_raw_mode` would turn the keys back on, so it is skipped.
        let silenced = self
            .cooked
            .as_ref()
            .is_some_and(|cooked| silence_signal_keys(io::stdin(), cooked).is_ok());
        if silenced {
            *signal_key_attributes() = self.cooked.take();
            leave_input_modes();
        } else {
            leave_screen();
        }
        let _ = TITLE_STACK.restore(&mut io::stdout());
    }
}

/// The attributes [`SignalKeyGuard`] puts back, saved when [`TerminalGuard`] drops.
static SIGNAL_KEY_ATTRIBUTES: Mutex<Option<Termios>> = Mutex::new(None);

fn signal_key_attributes() -> std::sync::MutexGuard<'static, Option<Termios>> {
    SIGNAL_KEY_ATTRIBUTES
        .lock()
        .unwrap_or_else(PoisonError::into_inner)
}

/// Lets Ctrl+C, Ctrl+Z and Ctrl+\ signal the process group again once the backend has
/// exited. Hold it across the run; [`TerminalGuard`] silences the keys when it drops.
pub struct SignalKeyGuard;

impl Drop for SignalKeyGuard {
    fn drop(&mut self) {
        if let Some(cooked) = signal_key_attributes().take() {
            let _ = restore_signal_keys(io::stdin(), &cooked);
        }
    }
}

/// Applies `cooked` except that the terminal does not turn its signal keys into signals.
fn silence_signal_keys(tty: impl AsFd, cooked: &Termios) -> nix::Result<()> {
    let mut silenced = cooked.clone();
    silenced.local_flags.remove(LocalFlags::ISIG);
    termios::tcsetattr(&tty, SetArg::TCSANOW, &silenced)
}

/// Undoes [`silence_signal_keys`]. Discards all input typed while the keys were silent,
/// so the shell does not read a stray Ctrl+C.
fn restore_signal_keys(tty: impl AsFd, cooked: &Termios) -> nix::Result<()> {
    termios::tcsetattr(&tty, SetArg::TCSAFLUSH, cooked)
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

/// Turns on raw mode, the alternate screen and the input modes the TUI reads with. Used
/// on entry and to take the terminal back from a child process.
fn enter_screen(stdout: &mut Stdout, mouse_capture: bool) -> io::Result<()> {
    enable_raw_mode()?;
    execute!(
        stdout,
        EnterAlternateScreen,
        EnableBracketedPaste,
        EnableFocusChange,
        Hide
    )?;
    KEYBOARD_PROTOCOL.enable(stdout)?;
    if mouse_capture {
        MOUSE_CAPTURE.enable(stdout)?;
    }
    Ok(())
}

/// Undoes [`enter_screen`]. Best effort: every step runs even if an earlier one fails.
fn leave_screen() {
    let _ = disable_raw_mode();
    leave_input_modes();
}

/// The part of [`leave_screen`] after raw mode: the alternate screen and input modes.
fn leave_input_modes() {
    let _ = MOUSE_CAPTURE.restore(&mut io::stdout());
    let _ = KEYBOARD_PROTOCOL.restore(&mut io::stdout());
    let _ = execute!(
        io::stdout(),
        EndSynchronizedUpdate,
        Show,
        DisableBracketedPaste,
        DisableFocusChange,
        LeaveAlternateScreen
    );
}

fn restore_terminal() {
    leave_screen();
    let _ = TITLE_STACK.restore(&mut io::stdout());
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn signal_keys_silent_until_restored() {
        let pty = nix::pty::openpty(None, None).unwrap();
        let local_flags = || termios::tcgetattr(&pty.slave).unwrap().local_flags;
        let cooked = termios::tcgetattr(&pty.slave).unwrap();
        let mut raw = cooked.clone();
        termios::cfmakeraw(&mut raw);
        termios::tcsetattr(&pty.slave, SetArg::TCSANOW, &raw).unwrap();

        silence_signal_keys(&pty.slave, &cooked).unwrap();
        assert!(local_flags().contains(LocalFlags::ICANON));
        assert!(!local_flags().contains(LocalFlags::ISIG));

        restore_signal_keys(&pty.slave, &cooked).unwrap();
        assert!(local_flags().contains(LocalFlags::ISIG));
    }

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
