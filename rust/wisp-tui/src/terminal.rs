use crossterm::cursor::{Hide, Show};
use crossterm::event::{DisableBracketedPaste, EnableBracketedPaste};
use crossterm::execute;
use crossterm::terminal::{
    EnterAlternateScreen, LeaveAlternateScreen, disable_raw_mode, enable_raw_mode,
};
use ratatui::Terminal;
use ratatui::backend::CrosstermBackend;
use std::io::{self, Stdout};
use std::panic::{self, PanicHookInfo};
use std::sync::Arc;

use crate::Error;

type Hook = dyn for<'a, 'b> Fn(&'a PanicHookInfo<'b>) + Send + Sync + 'static;

pub struct TerminalGuard {
    terminal: Terminal<CrosstermBackend<Stdout>>,
}

impl TerminalGuard {
    pub fn enter() -> Result<Self, Error> {
        enable_raw_mode()?;
        let mut stdout = io::stdout();
        if let Err(error) = execute!(stdout, EnterAlternateScreen, EnableBracketedPaste, Hide) {
            restore_terminal();
            return Err(Error::Io(error));
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
    let _ = execute!(
        io::stdout(),
        Show,
        DisableBracketedPaste,
        LeaveAlternateScreen
    );
}
