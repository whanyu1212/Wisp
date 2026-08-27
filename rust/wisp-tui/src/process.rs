use nix::errno::Errno;
use nix::sys::signal::{Signal, kill};
use nix::unistd::Pid;
use std::ffi::OsString;
use std::process::Stdio;
use std::time::Duration;
use tokio::process::{Child, ChildStderr, ChildStdin, ChildStdout, Command};
use tokio::time::{Instant, sleep};

use crate::Error;

const EXIT_POLL_INTERVAL: Duration = Duration::from_millis(25);
const CLEANUP_STAGE_TIMEOUT: Duration = Duration::from_secs(2);

#[derive(Clone, Copy, Debug, Eq, PartialEq)]
pub enum CleanupOutcome {
    AlreadyExited,
    Terminated,
    Killed,
}

pub struct BackendProcess {
    child: Child,
    backend_pid: Pid,
}

impl BackendProcess {
    pub fn spawn(argv: &[OsString]) -> Result<(Self, ChildStdin, ChildStdout, ChildStderr), Error> {
        let (program, arguments) = argv.split_first().ok_or(Error::MissingBackendCommand)?;
        let mut command = Command::new(program);
        command
            .args(arguments)
            .stdin(Stdio::piped())
            .stdout(Stdio::piped())
            .stderr(Stdio::piped())
            .kill_on_drop(true);
        let mut child = command.spawn().map_err(|source| Error::Spawn {
            program: program.clone(),
            source,
        })?;
        let raw_pid = child.id().ok_or(Error::MissingProcessId)?;
        let pid = i32::try_from(raw_pid).map_err(|_| Error::MissingProcessId)?;
        let stdin = child
            .stdin
            .take()
            .ok_or(Error::MissingProcessPipe("stdin"))?;
        let stdout = child
            .stdout
            .take()
            .ok_or(Error::MissingProcessPipe("stdout"))?;
        let stderr = child
            .stderr
            .take()
            .ok_or(Error::MissingProcessPipe("stderr"))?;
        Ok((
            Self {
                child,
                backend_pid: Pid::from_raw(pid),
            },
            stdin,
            stdout,
            stderr,
        ))
    }

    pub fn try_wait(&mut self) -> Result<Option<std::process::ExitStatus>, Error> {
        self.child.try_wait().map_err(Error::Io)
    }

    pub async fn wait_gracefully(&mut self, timeout: Duration) -> Result<bool, Error> {
        let deadline = Instant::now() + timeout;
        loop {
            if self.try_wait()?.is_some() {
                return Ok(true);
            }
            if Instant::now() >= deadline {
                return Ok(false);
            }
            sleep(EXIT_POLL_INTERVAL).await;
        }
    }

    pub async fn terminate_then_kill(&mut self) -> Result<CleanupOutcome, Error> {
        if self.try_wait()?.is_some() {
            return Ok(CleanupOutcome::AlreadyExited);
        }
        self.signal_backend(Signal::SIGTERM)?;
        if self.wait_gracefully(CLEANUP_STAGE_TIMEOUT).await? {
            return Ok(CleanupOutcome::Terminated);
        }
        self.signal_backend(Signal::SIGKILL)?;
        if self.wait_gracefully(CLEANUP_STAGE_TIMEOUT).await? {
            return Ok(CleanupOutcome::Killed);
        }
        Err(Error::CleanupTimeout)
    }

    fn signal_backend(&self, signal: Signal) -> Result<(), Error> {
        match kill(self.backend_pid, signal) {
            Ok(()) | Err(Errno::ESRCH) => Ok(()),
            Err(source) => Err(Error::Signal { signal, source }),
        }
    }
}

impl Drop for BackendProcess {
    fn drop(&mut self) {
        if self.child.try_wait().ok().flatten().is_none() {
            let _ = self.signal_backend(Signal::SIGKILL);
            let _ = self.child.start_kill();
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[tokio::test]
    async fn terminate_stage_signals_only_the_backend_pid() {
        let argv = [OsString::from("/bin/sleep"), OsString::from("30")];
        let (mut backend, stdin, stdout, stderr) = BackendProcess::spawn(&argv).unwrap();
        drop((stdin, stdout, stderr));
        assert_eq!(
            backend.terminate_then_kill().await.unwrap(),
            CleanupOutcome::Terminated
        );
        assert!(backend.try_wait().unwrap().is_some());
    }

    #[tokio::test]
    async fn backend_inherits_the_rust_process_group() {
        use nix::unistd::getpgrp;
        use tokio::io::AsyncReadExt;

        let argv = [
            OsString::from("/bin/sh"),
            OsString::from("-c"),
            OsString::from("ps -o pgid= -p $$"),
        ];
        let (mut backend, stdin, mut stdout, stderr) = BackendProcess::spawn(&argv).unwrap();
        drop((stdin, stderr));
        let mut output = String::new();
        stdout.read_to_string(&mut output).await.unwrap();
        assert!(
            backend
                .wait_gracefully(Duration::from_secs(1))
                .await
                .unwrap()
        );
        assert_eq!(output.trim().parse::<i32>().unwrap(), getpgrp().as_raw());
    }
}
