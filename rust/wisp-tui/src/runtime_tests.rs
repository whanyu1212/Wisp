use super::*;
use crate::runtime::{BackendIo, EventSource, Process, TerminalSession};
use crossterm::event::{KeyEventState, MouseEventKind};
use ratatui::backend::TestBackend;
use serde_json::{Value, json};
use std::os::unix::process::ExitStatusExt;
use std::sync::{
    Mutex,
    atomic::{AtomicBool, Ordering},
};
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader, duplex};

struct ScriptedEvents {
    events: VecDeque<io::Result<Event>>,
    poll_error: Option<io::ErrorKind>,
}

impl ScriptedEvents {
    fn new(events: impl IntoIterator<Item = Event>) -> Self {
        Self {
            events: events.into_iter().map(Ok).collect(),
            poll_error: None,
        }
    }
}

impl EventSource for ScriptedEvents {
    fn poll(&mut self, _: Duration) -> io::Result<bool> {
        if let Some(kind) = self.poll_error.take() {
            return Err(kind.into());
        }
        Ok(!self.events.is_empty())
    }

    fn read(&mut self) -> io::Result<Event> {
        self.events
            .pop_front()
            .expect("read must follow a ready poll")
    }
}

fn inputs(source: ScriptedEvents, mouse_enabled: bool) -> (Result<(), Error>, Vec<Input>) {
    let (sender, mut receiver) = mpsc::channel(64);
    let (stop, stop_rx) = watch::channel(false);
    // An exhausted script returns a false poll, where a closed stop channel ends input.
    drop(stop);
    let result = input_task(sender, stop_rx, mouse_enabled, source);
    let mut received = Vec::new();
    while let Ok(input) = receiver.try_recv() {
        received.push(input);
    }
    (result, received)
}

fn wheel(kind: MouseEventKind) -> Event {
    Event::Mouse(MouseEvent {
        kind,
        column: 2,
        row: 3,
        modifiers: KeyModifiers::NONE,
    })
}

#[test]
fn input_preserves_supported_events_and_ignores_releases_and_focus() {
    let key = KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE);
    let repeat = KeyEvent {
        kind: KeyEventKind::Repeat,
        ..key
    };
    let release = KeyEvent {
        kind: KeyEventKind::Release,
        state: KeyEventState::NONE,
        ..key
    };
    let (result, received) = inputs(
        ScriptedEvents::new([
            Event::FocusGained,
            Event::Key(release),
            Event::Key(key),
            Event::Key(repeat),
            Event::Paste("paste\ntext".into()),
            Event::Resize(80, 24),
            Event::FocusLost,
        ]),
        false,
    );
    result.unwrap();
    assert!(
        matches!(&received[..], [Input::Key(a), Input::Key(b), Input::Paste(text), Input::Redraw]
        if *a == key && *b == repeat && text == "paste\ntext")
    );
}

#[test]
fn input_batches_scroll_without_losing_the_deferred_event() {
    let (result, received) = inputs(
        ScriptedEvents::new([
            wheel(MouseEventKind::ScrollDown),
            wheel(MouseEventKind::ScrollDown),
            Event::Paste("after scroll".into()),
            wheel(MouseEventKind::ScrollUp),
            wheel(MouseEventKind::Down(crossterm::event::MouseButton::Left)),
        ]),
        true,
    );
    result.unwrap();
    assert!(
        matches!(&received[..], [Input::Scroll(down), Input::Paste(text), Input::Scroll(up), Input::Mouse(_)]
        if down.lines == 2 && up.lines == -1 && text == "after scroll")
    );
    let (result, received) = inputs(
        ScriptedEvents::new([wheel(MouseEventKind::ScrollDown), Event::Resize(90, 30)]),
        false,
    );
    result.unwrap();
    assert!(matches!(&received[..], [Input::Redraw]));
}

#[test]
fn input_distinguishes_poll_failure_from_a_forwarded_read_failure() {
    let (result, received) = inputs(
        ScriptedEvents {
            events: VecDeque::new(),
            poll_error: Some(io::ErrorKind::Interrupted),
        },
        false,
    );
    assert!(matches!(result, Err(Error::Io(error)) if error.kind() == io::ErrorKind::Interrupted));
    assert!(received.is_empty());
    let (result, received) = inputs(
        ScriptedEvents {
            events: [Err(io::Error::other("read failed"))].into(),
            poll_error: None,
        },
        false,
    );
    result.unwrap();
    assert!(matches!(&received[..], [Input::Error(error)] if error.to_string() == "read failed"));
}

#[test]
fn stopped_or_disconnected_input_does_not_read_more_events() {
    let (sender, receiver) = mpsc::channel(1);
    let (_stop, stop_rx) = watch::channel(true);
    input_task(
        sender,
        stop_rx,
        false,
        ScriptedEvents {
            events: VecDeque::new(),
            poll_error: Some(io::ErrorKind::Other),
        },
    )
    .unwrap();
    drop(receiver);
    for event in [
        Event::Resize(80, 24),
        Event::Paste("x".into()),
        Event::Key(KeyEvent::new(KeyCode::Char('a'), KeyModifiers::NONE)),
        wheel(MouseEventKind::Down(crossterm::event::MouseButton::Left)),
    ] {
        let (sender, receiver) = mpsc::channel(1);
        drop(receiver);
        let (_stop, stop_rx) = watch::channel(false);
        input_task(
            sender,
            stop_rx,
            true,
            ScriptedEvents {
                events: [Ok(event), Err(io::Error::other("must not read"))].into(),
                poll_error: None,
            },
        )
        .unwrap();
    }
}

#[derive(Default)]
struct ProcessState {
    exited: AtomicBool,
    escalated: AtomicBool,
}

struct FakeProcess(Arc<ProcessState>);

impl Process for FakeProcess {
    fn try_wait(&mut self) -> Result<Option<std::process::ExitStatus>, Error> {
        Ok(self
            .0
            .exited
            .load(Ordering::SeqCst)
            .then(|| std::process::ExitStatus::from_raw(0)))
    }

    async fn wait_gracefully(&mut self, _: Duration) -> Result<bool, Error> {
        Ok(self.0.exited.load(Ordering::SeqCst))
    }

    async fn terminate_then_kill(&mut self) -> Result<CleanupOutcome, Error> {
        self.0.escalated.store(true, Ordering::SeqCst);
        Ok(CleanupOutcome::Killed)
    }
}

struct TestTerminal {
    terminal: Terminal<TestBackend>,
    restored: Arc<AtomicBool>,
    input_stopped: Arc<AtomicBool>,
}

impl TerminalSession for TestTerminal {
    type Backend = TestBackend;
    fn terminal(&mut self) -> &mut Terminal<TestBackend> {
        &mut self.terminal
    }
}

impl Drop for TestTerminal {
    fn drop(&mut self) {
        assert!(
            self.input_stopped.load(Ordering::SeqCst),
            "input must stop before terminal restoration"
        );
        self.restored.store(true, Ordering::SeqCst);
    }
}

// A blocking channel models a terminal poll without touching the real terminal.
struct ChannelEvents {
    receiver: std::sync::mpsc::Receiver<io::Result<Event>>,
    pending: Option<io::Result<Event>>,
    stopped: Arc<AtomicBool>,
}

impl EventSource for ChannelEvents {
    fn poll(&mut self, timeout: Duration) -> io::Result<bool> {
        self.pending = self.receiver.recv_timeout(timeout).ok();
        Ok(self.pending.is_some())
    }
    fn read(&mut self) -> io::Result<Event> {
        self.pending.take().unwrap()
    }
}

impl Drop for ChannelEvents {
    fn drop(&mut self) {
        self.stopped.store(true, Ordering::SeqCst);
    }
}

fn terminal_pair(
    receiver: std::sync::mpsc::Receiver<io::Result<Event>>,
    restored: Arc<AtomicBool>,
) -> (TestTerminal, ChannelEvents) {
    let stopped = Arc::new(AtomicBool::new(false));
    (
        TestTerminal {
            terminal: Terminal::new(TestBackend::new(80, 24)).unwrap(),
            restored,
            input_stopped: stopped.clone(),
        },
        ChannelEvents {
            receiver,
            pending: None,
            stopped,
        },
    )
}

fn cli() -> Cli {
    Cli {
        expected_backend_version: python_package_version(env!("CARGO_PKG_VERSION")),
        backend: vec![],
    }
}

fn accepted(version: &str) -> Value {
    json!({"type": "rpc.handshake.accepted", "backend_package_version": version,
        "capabilities": [], "limits": {"max_client_frame_bytes": 8192, "max_server_frame_bytes": 8192}})
}

#[tokio::test]
async fn startup_commands_and_graceful_shutdown_use_the_real_transport() {
    run_shutdown(true).await.unwrap();
}

#[tokio::test]
async fn zero_exit_without_shutdown_completion_is_not_success() {
    assert!(matches!(
        run_shutdown(false).await,
        Err(Error::ShutdownCompletionMissing)
    ));
}

async fn run_shutdown(send_completion: bool) -> Result<(), Error> {
    let (stdin, backend_input) = duplex(8192);
    let (mut backend_output, stdout) = duplex(8192);
    let state = Arc::new(ProcessState::default());
    let restored = Arc::new(AtomicBool::new(false));
    let (keys, receiver) = std::sync::mpsc::channel();
    let commands = Arc::new(Mutex::new(Vec::<Value>::new()));
    let backend_state = state.clone();
    let backend_restored = restored.clone();
    let recorded = commands.clone();
    let backend = tokio::spawn(async move {
        let mut lines = BufReader::new(backend_input).lines();
        let request: Value =
            serde_json::from_str(&lines.next_line().await.unwrap().unwrap()).unwrap();
        assert_eq!(request["type"], "rpc.handshake.request");
        backend_output
            .write_all(format!("{}\n", accepted(&cli().expected_backend_version)).as_bytes())
            .await
            .unwrap();
        keys.send(Ok(Event::Key(KeyEvent::new(
            KeyCode::Char('c'),
            KeyModifiers::CONTROL,
        ))))
        .unwrap();
        while let Some(line) = lines.next_line().await.unwrap() {
            let command: Value = serde_json::from_str(&line).unwrap();
            let shutdown = command["type"] == "shutdown";
            recorded.lock().unwrap().push(command);
            if shutdown {
                assert!(
                    backend_restored.load(Ordering::SeqCst),
                    "restore before shutdown"
                );
                let finished = json!({"type": "rpc.command.finished", "timestamp": "2026-01-02T03:04:05Z",
                    "command_id": SHUTDOWN_COMMAND_ID, "command_type": "shutdown", "ok": true, "error": null});
                if send_completion {
                    backend_output
                        .write_all(format!("{finished}\n").as_bytes())
                        .await
                        .unwrap();
                }
                backend_state.exited.store(true, Ordering::SeqCst);
                backend_output.shutdown().await.unwrap();
            }
        }
    });
    let terminal_restored = restored.clone();
    let result = run_with_io(
        cli(),
        Bindings::default(),
        None,
        BackendIo {
            process: FakeProcess(state.clone()),
            stdin,
            stdout,
            stderr: tokio::io::empty(),
        },
        |_| Ok(terminal_pair(receiver, terminal_restored)),
    )
    .await;
    backend.await.unwrap();
    assert!(restored.load(Ordering::SeqCst));
    assert!(!state.escalated.load(Ordering::SeqCst));
    let commands = commands.lock().unwrap();
    let kinds: Vec<_> = commands
        .iter()
        .map(|command| command["type"].as_str().unwrap())
        .collect();
    assert_eq!(
        kinds,
        [
            "get_messages",
            "get_connection_catalog",
            "get_model_catalog",
            "get_commands",
            "get_state",
            "get_skills",
            "shutdown"
        ]
    );
    result
}

#[tokio::test]
async fn handshake_failure_skips_terminal_and_preserves_the_original_error() {
    for response in [
        String::new(),
        format!("{}\n", accepted("wrong-version")),
        "not json\n".into(),
    ] {
        let state = Arc::new(ProcessState::default());
        let result = run_with_io(
            cli(),
            Bindings::default(),
            None,
            BackendIo {
                process: FakeProcess(state.clone()),
                stdin: tokio::io::sink(),
                stdout: std::io::Cursor::new(response.clone().into_bytes()),
                stderr: tokio::io::empty(),
            },
            |_| -> Result<(TestTerminal, ScriptedEvents), Error> {
                panic!("terminal must not open")
            },
        )
        .await;
        if response.is_empty() {
            assert!(matches!(result, Err(Error::HandshakeEof)));
        } else if response.starts_with('{') {
            assert!(
                matches!(result, Err(Error::BackendVersionMismatch { actual, .. }) if actual == "wrong-version")
            );
        } else {
            assert!(result.is_err());
            assert!(!matches!(result, Err(Error::CleanupEscalated { .. })));
        }
        assert!(
            state.escalated.load(Ordering::SeqCst),
            "startup errors still clean up the process"
        );
    }
}

#[tokio::test(start_paused = true)]
async fn silent_handshake_has_a_deadline_and_cleans_up() {
    let (_backend_output, stdout) = duplex(64);
    let state = Arc::new(ProcessState::default());
    let result = run_with_io(
        cli(),
        Bindings::default(),
        None,
        BackendIo {
            process: FakeProcess(state.clone()),
            stdin: tokio::io::sink(),
            stdout,
            stderr: tokio::io::empty(),
        },
        |_| -> Result<(TestTerminal, ScriptedEvents), Error> { panic!("terminal must not open") },
    )
    .await;
    assert!(matches!(result, Err(Error::HandshakeTimeout)));
    assert!(state.escalated.load(Ordering::SeqCst));
}

#[tokio::test]
async fn terminal_entry_failure_still_cleans_up_the_backend() {
    let state = Arc::new(ProcessState::default());
    let response = format!("{}\n", accepted(&cli().expected_backend_version));
    let result = run_with_io(
        cli(),
        Bindings::default(),
        None,
        BackendIo {
            process: FakeProcess(state.clone()),
            stdin: tokio::io::sink(),
            stdout: std::io::Cursor::new(response.into_bytes()),
            stderr: tokio::io::empty(),
        },
        |_| -> Result<(TestTerminal, ScriptedEvents), Error> {
            Err(io::Error::other("terminal entry failed").into())
        },
    )
    .await;
    assert!(
        matches!(result, Err(Error::Io(error)) if error.to_string() == "terminal entry failed")
    );
    assert!(state.escalated.load(Ordering::SeqCst));
}

#[tokio::test]
async fn unexpected_eof_restores_terminal_and_classifies_process_exit() {
    let state = Arc::new(ProcessState::default());
    state.exited.store(true, Ordering::SeqCst);
    let restored = Arc::new(AtomicBool::new(false));
    let terminal_restored = restored.clone();
    let (_keys, receiver) = std::sync::mpsc::channel();
    let response = format!("{}\n", accepted(&cli().expected_backend_version));
    let result = run_with_io(
        cli(),
        Bindings::default(),
        None,
        BackendIo {
            process: FakeProcess(state.clone()),
            stdin: tokio::io::sink(),
            stdout: std::io::Cursor::new(response.into_bytes()),
            stderr: tokio::io::empty(),
        },
        |_| Ok(terminal_pair(receiver, terminal_restored)),
    )
    .await;
    assert!(matches!(result, Err(Error::BackendExited(status)) if status.success()));
    assert!(restored.load(Ordering::SeqCst));
    assert!(!state.escalated.load(Ordering::SeqCst));
}

#[tokio::test]
async fn input_failure_restores_terminal_and_wins_over_cleanup_errors() {
    let state = Arc::new(ProcessState::default());
    let restored = Arc::new(AtomicBool::new(false));
    let terminal_restored = restored.clone();
    let (keys, receiver) = std::sync::mpsc::channel();
    keys.send(Err(io::Error::other("input read failed")))
        .unwrap();
    let (mut backend_output, stdout) = duplex(8192);
    backend_output
        .write_all(format!("{}\n", accepted(&cli().expected_backend_version)).as_bytes())
        .await
        .unwrap();
    let result = run_with_io(
        cli(),
        Bindings::default(),
        None,
        BackendIo {
            process: FakeProcess(state.clone()),
            stdin: tokio::io::sink(),
            stdout,
            stderr: tokio::io::empty(),
        },
        |_| Ok(terminal_pair(receiver, terminal_restored)),
    )
    .await;
    assert!(matches!(result, Err(Error::Io(error)) if error.to_string() == "input read failed"));
    assert!(restored.load(Ordering::SeqCst));
    assert!(state.escalated.load(Ordering::SeqCst));
}
