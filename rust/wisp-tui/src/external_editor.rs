//! Running the user's own editor: on the composer draft, or on a file a tool card names.
//!
//! The draft is written to a private temporary file, the editor runs on the real
//! terminal while the TUI steps aside, and the saved text comes back as the new draft.
//! A file is opened in place, at a line.
//! [`InputGate`] keeps the input task off the terminal meanwhile, so the editor
//! receives every keystroke, and the editor runs as the terminal's foreground job, so
//! the keys that signal a job (Ctrl+C, Ctrl+Z) reach it rather than Wisp.

use std::ffi::OsString;
use std::fmt;
use std::fs::{self, File, OpenOptions};
use std::io::{self, Read, Write};
use std::os::unix::fs::OpenOptionsExt;
use std::os::unix::process::{CommandExt, ExitStatusExt};
use std::path::{Path, PathBuf};
use std::process::{Command, ExitStatus};
use std::sync::atomic::{AtomicU64, Ordering};
use std::sync::{Condvar, Mutex, MutexGuard};
use std::time::{Duration, Instant, SystemTime, UNIX_EPOCH};

use crate::prompt_editor::MAX_PROMPT_BYTES;

/// The editor when neither `$VISUAL` nor `$EDITOR` names one.
const FALLBACK_EDITOR: &str = "vi";

/// Resolves the editor command: `$VISUAL`, then `$EDITOR`, then `vi`. A blank value
/// counts as unset. The command may carry arguments, such as `code --wait`.
pub(crate) fn editor_command(variable: impl Fn(&str) -> Option<String>) -> String {
    ["VISUAL", "EDITOR"]
        .into_iter()
        .filter_map(variable)
        .map(|value| value.trim().to_owned())
        .find(|value| !value.is_empty())
        .unwrap_or_else(|| FALLBACK_EDITOR.to_owned())
}

/// Why an edit did not produce a new draft.
#[derive(Debug)]
pub(crate) enum EditFailure {
    /// The temporary file could not be written or read back.
    File(io::Error),
    /// The editor could not be started.
    Spawn(io::Error),
    /// The editor exited unsuccessfully, so its result is not trusted.
    Exit(ExitStatus),
    /// The saved text is larger than a draft may be.
    TooLarge,
    /// The saved text is not UTF-8.
    NotText,
}

impl fmt::Display for EditFailure {
    fn fmt(&self, f: &mut fmt::Formatter<'_>) -> fmt::Result {
        match self {
            Self::File(error) => write!(f, "could not use a temporary file: {error}"),
            Self::Spawn(error) => write!(f, "could not start the editor: {error}"),
            Self::Exit(status) => write!(f, "the editor exited with {status}"),
            Self::TooLarge => write!(f, "the saved text is over {} MiB", MAX_PROMPT_BYTES >> 20),
            Self::NotText => write!(f, "the saved text is not UTF-8"),
        }
    }
}

/// What the editor key asked to open.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) enum EditorRequest {
    /// The composer draft, which comes back as the new draft.
    Draft,
    /// A file a tool card names, opened in place.
    File(FileTarget),
}

/// What running the editor for an [`EditorRequest`] returned.
#[derive(Debug)]
pub(crate) enum EditorOutcome {
    Draft(Result<String, EditFailure>),
    File(FileTarget, Result<(), EditFailure>),
}

/// Runs `command` for `request`, blocking until the editor exits.
pub(crate) fn run(command: &str, request: EditorRequest, draft: &str) -> EditorOutcome {
    match request {
        EditorRequest::Draft => EditorOutcome::Draft(edit(command, draft)),
        EditorRequest::File(target) => {
            let opened = open_file(command, &target);
            EditorOutcome::File(target, opened)
        }
    }
}

/// Opens `draft` in `command` and returns the text the user saved, or why there is none.
///
/// Blocks until the editor exits, so the caller hands it the terminal first. The
/// command runs through `sh` with the file as `$1`, so it may carry arguments while the
/// path is never parsed by the shell. The file holds the draft plus a newline, and one
/// trailing newline, which most editors add on save, is removed on the way back, so an
/// unchanged file returns the draft exactly. The temporary file is removed either way.
pub(crate) fn edit(command: &str, draft: &str) -> Result<String, EditFailure> {
    let file = DraftFile::create(&format!("{draft}\n")).map_err(EditFailure::File)?;
    let mut shell = Command::new("sh");
    shell
        .arg("-c")
        .arg(format!("{command} \"$1\""))
        .arg("sh")
        .arg(&file.path);
    let status = run_in_foreground(shell).map_err(EditFailure::Spawn)?;
    if !status.success() {
        return Err(EditFailure::Exit(status));
    }
    let saved = file.read()?;
    let saved = saved
        .strip_suffix("\r\n")
        .or_else(|| saved.strip_suffix('\n'))
        .unwrap_or(&saved);
    Ok(saved.to_owned())
}

/// A file to open in the editor, at a line.
#[derive(Clone, Debug, Eq, PartialEq)]
pub(crate) struct FileTarget {
    /// Always absolute, so it can never be read as an editor option such as vim's
    /// `+cmd`, which runs a command.
    path: PathBuf,
    /// 1-based.
    line: u64,
}

impl FileTarget {
    /// A target for `path` at `line`, or `None` unless `path` is absolute and names a
    /// regular file, so a model-chosen relative path like `+:!cmd` never reaches the
    /// editor's arguments.
    pub(crate) fn resolve(path: &Path, line: u64) -> Option<Self> {
        (path.is_absolute() && path.is_file()).then(|| Self {
            path: path.to_owned(),
            line: line.max(1),
        })
    }

    pub(crate) fn path(&self) -> &Path {
        &self.path
    }
}

/// Opens `target` in `command` at its line and waits for the editor to exit.
///
/// As with [`edit`], the command runs through `sh` and the arguments are passed as
/// `"$@"`, never parsed by the shell.
pub(crate) fn open_file(command: &str, target: &FileTarget) -> Result<(), EditFailure> {
    let mut shell = Command::new("sh");
    shell
        .arg("-c")
        .arg(format!("{command} \"$@\""))
        .arg("sh")
        .args(line_arguments(command, target));
    let status = run_in_foreground(shell).map_err(EditFailure::Spawn)?;
    if !status.success() {
        return Err(EditFailure::Exit(status));
    }
    Ok(())
}

/// The arguments that open `target` at its line, in the form the editor understands.
///
/// Most terminal editors take `+LINE path`, the vi convention. VS Code and its forks
/// take `--goto path:line`; Zed, Helix and Sublime Text take `path:line`, and would
/// open a file named `+LINE` instead.
fn line_arguments(command: &str, target: &FileTarget) -> Vec<OsString> {
    let program = command
        .split_whitespace()
        .next()
        .map(|word| word.rsplit('/').next().unwrap_or(word))
        .unwrap_or_default();
    let mut located = target.path.as_os_str().to_owned();
    located.push(format!(":{}", target.line));
    match program {
        "code" | "code-insiders" | "codium" | "cursor" | "windsurf" => {
            vec!["--goto".into(), located]
        }
        "zed" | "zeditor" | "hx" | "helix" | "subl" => vec![located],
        _ => vec![
            format!("+{}", target.line).into(),
            target.path.as_os_str().to_owned(),
        ],
    }
}

/// Runs `command` as the terminal's foreground job and waits for it to exit.
///
/// The child gets a process group of its own and the terminal's foreground, as a shell
/// gives a job: Ctrl+C and Ctrl+Z then signal only the editor, not Wisp and its backend,
/// which share Wisp's group. A child that stops, from Ctrl+Z or from reading the
/// terminal before it became the foreground, is given the foreground again and
/// continued, so suspending does nothing here. The foreground then returns to Wisp.
/// When Wisp is not the terminal's foreground job, or there is no terminal, the child
/// just runs.
fn run_in_foreground(mut command: Command) -> io::Result<ExitStatus> {
    use nix::sys::signal::{Signal, killpg};
    use nix::sys::wait::{WaitPidFlag, WaitStatus, waitpid};
    use nix::unistd::{Pid, getpgrp, tcgetpgrp};

    // Taking the foreground is only safe from the foreground: a background group that
    // tried would be stopped by the terminal.
    let foreground = getpgrp();
    let Some(terminal) = File::options()
        .read(true)
        .write(true)
        .open("/dev/tty")
        .ok()
        .filter(|terminal| tcgetpgrp(terminal).is_ok_and(|group| group == foreground))
    else {
        return command.status();
    };
    command
        .stdin(terminal.try_clone()?)
        .stdout(terminal.try_clone()?)
        .stderr(terminal.try_clone()?)
        .process_group(0);
    // The child is reaped below with waitpid, never through this handle.
    let child = command.spawn()?;
    let job = Pid::from_raw(i32::try_from(child.id()).expect("process ids fit i32"));
    set_foreground(&terminal, job);
    let waited = loop {
        match waitpid(job, Some(WaitPidFlag::WUNTRACED)) {
            Ok(WaitStatus::Stopped(..)) => {
                set_foreground(&terminal, job);
                let _ = killpg(job, Signal::SIGCONT);
            }
            Ok(WaitStatus::Exited(_, code)) => break Ok(ExitStatus::from_raw(code << 8)),
            Ok(WaitStatus::Signaled(_, signal, _)) => {
                break Ok(ExitStatus::from_raw(signal as i32));
            }
            Ok(_) | Err(nix::errno::Errno::EINTR) => {}
            Err(error) => break Err(io::Error::from(error)),
        }
    };
    set_foreground(&terminal, foreground);
    waited
}

/// Makes `group` the terminal's foreground job. Once the child has the foreground, Wisp
/// is a background job, and the terminal answers a background job that sets the
/// foreground with SIGTTOU, which would stop Wisp and its backend. This thread blocks
/// SIGTTOU around the call, as a shell does.
fn set_foreground(terminal: &File, group: nix::unistd::Pid) {
    use nix::sys::signal::{SigSet, SigmaskHow, Signal, pthread_sigmask};
    let mut blocked = SigSet::empty();
    blocked.add(Signal::SIGTTOU);
    let mut previous = SigSet::empty();
    let masked = pthread_sigmask(SigmaskHow::SIG_BLOCK, Some(&blocked), Some(&mut previous));
    let _ = nix::unistd::tcsetpgrp(terminal, group);
    if masked.is_ok() {
        let _ = pthread_sigmask(SigmaskHow::SIG_SETMASK, Some(&previous), None);
    }
}

/// A temporary file only the user can read, removed when dropped.
struct DraftFile {
    path: PathBuf,
}

impl DraftFile {
    fn create(draft: &str) -> io::Result<Self> {
        // The clock alone can repeat between two drafts in one process, since some
        // clocks tick in microseconds, so a counter keeps each name unique.
        static CREATED: AtomicU64 = AtomicU64::new(0);
        let sequence = CREATED.fetch_add(1, Ordering::Relaxed);
        let nanos = SystemTime::now()
            .duration_since(UNIX_EPOCH)
            .map_or(0, |elapsed| elapsed.subsec_nanos());
        // `.md` lets the editor highlight the prompt as Markdown.
        let name = format!("wisp-prompt-{}-{sequence}-{nanos}.md", std::process::id());
        let path = std::env::temp_dir().join(name);
        let mut file = OpenOptions::new()
            .write(true)
            .create_new(true)
            .mode(0o600)
            .open(&path)?;
        let file_guard = Self { path };
        file.write_all(draft.as_bytes())?;
        file.sync_all()?;
        Ok(file_guard)
    }

    fn read(&self) -> Result<String, EditFailure> {
        read_bounded(&self.path)
    }
}

impl Drop for DraftFile {
    fn drop(&mut self) {
        let _ = fs::remove_file(&self.path);
    }
}

/// Reads at most one byte more than a draft may hold, so an oversized file is refused
/// without loading all of it.
fn read_bounded(path: &Path) -> Result<String, EditFailure> {
    let mut bytes = Vec::new();
    File::open(path)
        .and_then(|file| {
            file.take(MAX_PROMPT_BYTES as u64 + 1)
                .read_to_end(&mut bytes)
        })
        .map_err(EditFailure::File)?;
    if bytes.len() > MAX_PROMPT_BYTES {
        return Err(EditFailure::TooLarge);
    }
    String::from_utf8(bytes).map_err(|_| EditFailure::NotText)
}

/// Lets the event loop park the input task while a child process owns the terminal.
///
/// The input task calls [`InputGate::hold`] before each poll of the terminal. Between
/// [`InputGate::pause`] and [`InputGate::resume`] it waits there instead of reading,
/// so nothing it reads can take keystrokes meant for the child.
#[derive(Debug, Default)]
pub(crate) struct InputGate {
    state: Mutex<GateState>,
    changed: Condvar,
}

#[derive(Debug, Default)]
struct GateState {
    pause_requested: bool,
    /// Whether the input task is parked at the gate.
    holding: bool,
}

/// How long the event loop waits for the input task to park. A task that does not
/// park in time is not reading the terminal either: it has ended, or it is blocked
/// handing over an input the event loop has not taken.
const PAUSE_TIMEOUT: Duration = Duration::from_secs(1);
/// How often a parked input task checks whether it should stop.
const HOLD_CHECK: Duration = Duration::from_millis(50);

impl InputGate {
    fn lock(&self) -> MutexGuard<'_, GateState> {
        // The state is two flags with no invariant a panic could break midway.
        self.state
            .lock()
            .unwrap_or_else(|poisoned| poisoned.into_inner())
    }

    /// Asks the input task to park and waits until it does, or until the timeout.
    /// Returns whether it parked.
    pub(crate) fn pause(&self) -> bool {
        let deadline = Instant::now() + PAUSE_TIMEOUT;
        let mut state = self.lock();
        state.pause_requested = true;
        while !state.holding {
            let Some(remaining) = deadline.checked_duration_since(Instant::now()) else {
                return false;
            };
            state = self
                .changed
                .wait_timeout(state, remaining)
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .0;
        }
        true
    }

    /// Lets a parked input task read the terminal again.
    pub(crate) fn resume(&self) {
        self.lock().pause_requested = false;
        self.changed.notify_all();
    }

    /// Called by the input task before it reads: parks while a pause is requested,
    /// returning early once `stopping` reports that the task should end.
    pub(crate) fn hold(&self, mut stopping: impl FnMut() -> bool) {
        let mut state = self.lock();
        while state.pause_requested && !stopping() {
            state.holding = true;
            self.changed.notify_all();
            state = self
                .changed
                .wait_timeout(state, HOLD_CHECK)
                .unwrap_or_else(|poisoned| poisoned.into_inner())
                .0;
        }
        state.holding = false;
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use std::sync::Arc;
    use std::sync::atomic::{AtomicBool, Ordering};

    fn lookup<'a>(pairs: &'a [(&'a str, &'a str)]) -> impl Fn(&str) -> Option<String> + 'a {
        move |name| {
            pairs
                .iter()
                .find(|(key, _)| *key == name)
                .map(|(_, value)| (*value).to_owned())
        }
    }

    #[test]
    fn editor_resolution() {
        assert_eq!(editor_command(lookup(&[])), "vi");
        assert_eq!(editor_command(lookup(&[("EDITOR", "nano")])), "nano");
        assert_eq!(
            editor_command(lookup(&[("VISUAL", "code --wait"), ("EDITOR", "nano")])),
            "code --wait"
        );
        assert_eq!(
            editor_command(lookup(&[("VISUAL", "  "), ("EDITOR", "nano")])),
            "nano"
        );
    }

    // `edit` appends the file as `"$1"`; the fake editors below end in `true` so it
    // lands harmlessly there.
    #[test]
    fn edit_returns_saved_text() {
        // The fake editor checks it got the draft, then saves new text with a newline.
        let command = r#"grep -qx 'old draft' "$1" && printf 'new draft\n' >"$1" && true"#;
        assert_eq!(edit(command, "old draft").unwrap(), "new draft");
        // Only one trailing newline is an editor's; more belong to the draft.
        let command = r#"printf 'a\n\n' >"$1" && true"#;
        assert_eq!(edit(command, "").unwrap(), "a\n");
        // Quitting without saving keeps the draft exactly, trailing newlines included.
        for draft in ["kept", "kept\n", "", "\n\n"] {
            assert_eq!(edit("true", draft).unwrap(), draft, "{draft:?}");
        }
    }

    #[test]
    fn edit_failures() {
        assert!(matches!(
            edit("exit 3; true", "draft"),
            Err(EditFailure::Exit(_))
        ));
        let oversized = format!("head -c {} /dev/zero | tr '\\0' x >", MAX_PROMPT_BYTES + 1);
        assert!(matches!(edit(&oversized, ""), Err(EditFailure::TooLarge)));
        assert!(matches!(
            edit(r#"printf '\377' >"$1" && true"#, ""),
            Err(EditFailure::NotText)
        ));
    }

    #[test]
    fn draft_file_is_private_and_removed() {
        use std::os::unix::fs::PermissionsExt;
        let file = DraftFile::create("secret").unwrap();
        let path = file.path.clone();
        let mode = fs::metadata(&path).unwrap().permissions().mode() & 0o777;
        assert_eq!(mode, 0o600);
        assert_eq!(fs::read_to_string(&path).unwrap(), "secret");
        drop(file);
        assert!(!path.exists());

        // The file is gone after a failed edit too. The fake editor records the path
        // it was given, then fails.
        let record = std::env::temp_dir().join(format!(
            "wisp-editor-path-{}-{:?}",
            std::process::id(),
            std::thread::current().id()
        ));
        let command = format!(r#"printf '%s' "$1" >'{}'; exit 1; true"#, record.display());
        assert!(matches!(edit(&command, "draft"), Err(EditFailure::Exit(_))));
        let edited = PathBuf::from(fs::read_to_string(&record).unwrap());
        fs::remove_file(&record).unwrap();
        assert!(edited.starts_with(std::env::temp_dir()), "{edited:?}");
        assert!(!edited.exists());
    }

    #[test]
    fn line_argument_forms() {
        let target = FileTarget {
            path: PathBuf::from("/work/src/main.rs"),
            line: 12,
        };
        let cases: [(&str, &[&str]); 5] = [
            ("vim", &["+12", "/work/src/main.rs"]),
            ("/usr/bin/nvim -p", &["+12", "/work/src/main.rs"]),
            ("code --wait", &["--goto", "/work/src/main.rs:12"]),
            ("/opt/bin/cursor -w", &["--goto", "/work/src/main.rs:12"]),
            ("zed --wait", &["/work/src/main.rs:12"]),
        ];
        for (command, expected) in cases {
            assert_eq!(line_arguments(command, &target), expected, "{command}");
        }
    }

    #[test]
    fn resolve_needs_existing_file() {
        let dir = std::env::temp_dir()
            .canonicalize()
            .unwrap()
            .join(format!("wisp-resolve-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        fs::write(dir.join("present.rs"), "").unwrap();
        let target = FileTarget::resolve(&dir.join("present.rs"), 0).unwrap();
        assert_eq!(target.path(), dir.join("present.rs"));
        // Line 0 is not a line; the top of the file is.
        assert_eq!(target.line, 1);
        assert_eq!(FileTarget::resolve(&dir.join("absent.rs"), 1), None);
        assert_eq!(FileTarget::resolve(&dir, 1), None);
        // A relative path, where `+cmd` would be an option, is refused.
        assert_eq!(FileTarget::resolve(Path::new("+:!cmd"), 1), None);
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn open_file_passes_arguments_unparsed() {
        // A path the shell would expand or split reaches the editor as one argument.
        let dir = std::env::temp_dir()
            .canonicalize()
            .unwrap()
            .join(format!("wisp-open-{}", std::process::id()));
        fs::create_dir_all(&dir).unwrap();
        let name = "a $(touch pwned) b;`x`.rs";
        fs::write(dir.join(name), "").unwrap();
        let target = FileTarget::resolve(&dir.join(name), 5).unwrap();
        let record = dir.join("args");
        let command = format!(r#"printf '%s\n' >'{}'"#, record.display());
        open_file(&command, &target).unwrap();
        assert_eq!(
            fs::read_to_string(&record).unwrap(),
            format!("+5\n{}\n", dir.join(name).display())
        );
        assert!(!dir.join("pwned").exists());
        assert!(matches!(
            open_file("exit 4; true", &target),
            Err(EditFailure::Exit(_))
        ));
        fs::remove_dir_all(&dir).unwrap();
    }

    #[test]
    fn gate_parks_input_until_resumed() {
        let gate = Arc::new(InputGate::default());
        let reads = Arc::new(Mutex::new(0_u32));
        let stop = Arc::new(AtomicBool::new(false));
        let task = {
            let (gate, reads, stop) = (Arc::clone(&gate), Arc::clone(&reads), Arc::clone(&stop));
            std::thread::spawn(move || {
                while !stop.load(Ordering::SeqCst) {
                    gate.hold(|| stop.load(Ordering::SeqCst));
                    *reads.lock().unwrap() += 1;
                    std::thread::sleep(Duration::from_millis(1));
                }
            })
        };
        assert!(gate.pause());
        let parked = *reads.lock().unwrap();
        std::thread::sleep(Duration::from_millis(80));
        assert_eq!(
            *reads.lock().unwrap(),
            parked,
            "a parked task must not read"
        );
        gate.resume();
        std::thread::sleep(Duration::from_millis(30));
        assert!(
            *reads.lock().unwrap() > parked,
            "a resumed task reads again"
        );

        // A stop request releases a parked task.
        assert!(gate.pause());
        stop.store(true, Ordering::SeqCst);
        task.join().unwrap();
    }

    #[test]
    fn pause_gives_up_without_input_task() {
        let gate = InputGate::default();
        let started = Instant::now();
        assert!(!gate.pause());
        assert!(started.elapsed() >= PAUSE_TIMEOUT);
        gate.resume();
    }
}
