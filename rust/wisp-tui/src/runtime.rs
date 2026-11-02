//! External resources used by the startup and shutdown orchestration.

use std::{io, process::ExitStatus, time::Duration};

use crossterm::event::{self, Event};
use ratatui::Terminal;

use crate::{
    Error,
    process::{BackendProcess, CleanupOutcome},
    terminal::{FrameBackend, TerminalGuard},
};

pub(super) struct BackendIo<P, W, R, E> {
    pub process: P,
    pub stdin: W,
    pub stdout: R,
    pub stderr: E,
}

pub(super) trait Process {
    fn try_wait(&mut self) -> Result<Option<ExitStatus>, Error>;
    async fn wait_gracefully(&mut self, timeout: Duration) -> Result<bool, Error>;
    async fn terminate_then_kill(&mut self) -> Result<CleanupOutcome, Error>;
}

impl Process for BackendProcess {
    fn try_wait(&mut self) -> Result<Option<ExitStatus>, Error> {
        self.try_wait()
    }

    async fn wait_gracefully(&mut self, timeout: Duration) -> Result<bool, Error> {
        self.wait_gracefully(timeout).await
    }

    async fn terminate_then_kill(&mut self) -> Result<CleanupOutcome, Error> {
        self.terminate_then_kill().await
    }
}

/// Owns terminal restoration until the input worker has stopped.
pub(super) trait TerminalSession {
    type Backend: FrameBackend;
    fn terminal(&mut self) -> &mut Terminal<Self::Backend>;
}

impl TerminalSession for TerminalGuard {
    type Backend = ratatui::backend::CrosstermBackend<io::Stdout>;

    fn terminal(&mut self) -> &mut Terminal<Self::Backend> {
        self.terminal()
    }
}

pub(super) trait EventSource: Send + 'static {
    fn poll(&mut self, timeout: Duration) -> io::Result<bool>;
    fn read(&mut self) -> io::Result<Event>;
}

pub(super) struct TerminalEvents;

impl EventSource for TerminalEvents {
    fn poll(&mut self, timeout: Duration) -> io::Result<bool> {
        event::poll(timeout)
    }

    fn read(&mut self) -> io::Result<Event> {
        event::read()
    }
}
