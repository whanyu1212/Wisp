//! Bounded FIFO turns: transport outcomes, eight events, one input, one due paint.

use crate::{
    BackendEvent, ConnectionInfo, Error, FRAME_INTERVAL, Input, LiveUi, LoopControl, OverlayKind,
    QueuedEvent, ReaderTermination, RenderedDecisionContext, UiAction, WriterMessage, is_ctrl_c,
    is_escape, mouse, printable_char, receive_reader_outcome,
};
use ratatui::{Terminal, backend::Backend};
use tokio::{
    sync::{mpsc, oneshot},
    time::Instant,
};

const EVENTS_PER_TURN: usize = 8;
type ReaderOutcome = Option<oneshot::Receiver<Result<ReaderTermination, Error>>>;
type WriterOutcome = Option<oneshot::Receiver<Result<(), Error>>>;

pub(super) enum Exit {
    User,
    Eof,
}

pub(super) struct Sources<'a> {
    pub events: &'a mut mpsc::Receiver<QueuedEvent>,
    pub ready_event: &'a mut Option<QueuedEvent>,
    pub inputs: &'a mut mpsc::Receiver<Input>,
    pub reader: &'a mut ReaderOutcome,
    pub writer: &'a mut WriterOutcome,
}

fn captured_event_prefix(sources: &Sources<'_>) -> usize {
    // Queue capacity is consumed at reserve(), before the reader decodes the
    // frame. len() alone would let input overtake that admitted event.
    sources.events.max_capacity() - sources.events.capacity()
        + usize::from(sources.ready_event.is_some())
}

// No input is processed between capture and apply. A workflow revision therefore
// identifies its selection as well as its owner, including remove/recreate cycles.
// Never snapshot the connection panel or its secret input buffer.
#[derive(PartialEq)]
struct ActivationTarget {
    revision: u64,
    decision: Option<RenderedDecisionContext>,
    overlay: Option<OverlayKind>,
    help_owner: Option<crate::key_help::Owner>,
    editor_revision: u64,
    editor_editable: bool,
    browse_entry: Option<crate::TranscriptEntryId>,
    detail_entry: Option<crate::TranscriptEntryId>,
    rendered_decision: Option<RenderedDecisionContext>,
    rendered_overlay: Option<OverlayKind>,
    rendered_model: bool,
    rendered_file: Option<usize>,
    rendered_completion: Option<String>,
    rendered_theme: Option<usize>,
    rendered_history: Option<u64>,
    rendered_skill: Option<String>,
    mouse_frame: Option<mouse::Frame>,
}

impl ActivationTarget {
    fn capture(ui: &LiveUi, input: &Input) -> Self {
        Self {
            revision: ui.activation_revision,
            decision: ui.current_decision_context(),
            overlay: ui.active_overlay(),
            help_owner: ui.key_help.as_ref().map(|help| help.owner.clone()),
            editor_revision: ui.editor.revision(),
            editor_editable: ui.editor_editable(),
            browse_entry: ui.browse_selected,
            detail_entry: ui.detail_view.selected_entry(),
            rendered_decision: ui.rendered_decision_context.clone(),
            rendered_overlay: ui.rendered_overlay,
            rendered_model: ui.rendered_model_picker,
            rendered_file: ui.file_picker.rendered_selection(),
            rendered_completion: ui.completion.rendered_selection().map(str::to_owned),
            rendered_theme: ui
                .theme_picker
                .as_ref()
                .and_then(|view| view.rendered_selection()),
            rendered_history: ui
                .prompt_history_view
                .as_ref()
                .and_then(|view| view.rendered_selection()),
            rendered_skill: ui
                .discovery_view
                .as_ref()
                .and_then(|view| view.rendered_selection())
                .map(str::to_owned),
            mouse_frame: matches!(input, Input::Mouse(_))
                .then(|| ui.mouse_frame.clone())
                .flatten(),
        }
    }
}

pub(super) struct PendingInput {
    input: Input,
    target: ActivationTarget,
    remaining: usize,
    editor_input: bool,
}

impl PendingInput {
    pub fn capture(input: Input, ui: &LiveUi, remaining: usize) -> Self {
        let target = ActivationTarget::capture(ui, &input);
        // Ordinary draft editing must survive tool output and status changes.
        // Submission and rendered selections retain the stronger activation guard.
        let editor_input = target.help_owner.is_none()
            && target.overlay.is_none()
            && target.decision.is_none()
            && target.browse_entry.is_none()
            && match &input {
                Input::Paste(_) => true,
                Input::Key(key) => {
                    let activates = matches!(
                        ui.bindings.action(*key),
                        Some(
                            crate::KeyAction::Submit
                                | crate::KeyAction::AlternateSubmit
                                | crate::KeyAction::RestoreQueue
                                | crate::KeyAction::Browse
                        )
                    ) || matches!(
                        key.code,
                        crossterm::event::KeyCode::Enter | crossterm::event::KeyCode::Tab
                    ) || (ui.file_picker.is_open()
                        && key.code == crossterm::event::KeyCode::Right);
                    !activates
                }
                _ => false,
            };
        Self {
            input,
            target,
            remaining,
            editor_input,
        }
    }

    pub async fn apply(
        self,
        ui: &mut LiveUi,
        writer: &mpsc::Sender<WriterMessage>,
        limit: usize,
    ) -> Result<LoopControl, Error> {
        let current = ActivationTarget::capture(ui, &self.input);
        // A negative decision may deny a replacement decision, but must never
        // become literal text after the waiting workflow has finished.
        let recovery = matches!(&self.input, Input::Key(key) if is_ctrl_c(*key) || is_escape(*key)
            || (self.target.decision.is_some() && current.decision.is_some()
                && printable_char(*key).is_some_and(|c| c.eq_ignore_ascii_case(&'n'))));
        let same_editor = self.editor_input
            && self.target.help_owner == current.help_owner
            && self.target.overlay == current.overlay
            && self.target.decision == current.decision
            && self.target.browse_entry == current.browse_entry
            && self.target.editor_revision == current.editor_revision
            && self.target.editor_editable == current.editor_editable;
        if recovery
            || matches!(self.input, Input::Redraw | Input::Error(_))
            || same_editor
            || self.target == current
        {
            ui.handle_input(self.input, writer, limit).await
        } else {
            ui.render_pending = true;
            Ok(LoopControl::Continue)
        }
    }
}

/// Events that can replace a workflow, its catalog/selection, or draft policy.
/// Text streaming and telemetry leave those targets intact and must not starve input.
pub(super) fn changes_activation_target(action: &UiAction) -> bool {
    match action {
        UiAction::BackendEvent(event) => !matches!(
            event,
            BackendEvent::MessageStarted { .. }
                | BackendEvent::MessageDelta { .. }
                | BackendEvent::Diagnostic(_)
                | BackendEvent::ContextEstimated(_)
                | BackendEvent::SessionStatsReported { .. }
                | BackendEvent::DeviceCodeProgress { .. }
                | BackendEvent::Other { .. }
        ),
        _ => false,
    }
}

pub(super) async fn finish_writer(outcome: &mut WriterOutcome) -> Result<(), Error> {
    match outcome.take() {
        Some(outcome) => outcome.await.map_err(|_| Error::WriterStopped)?,
        None => Ok(()),
    }
}

async fn receive_writer(outcome: &mut WriterOutcome) -> Result<(), Error> {
    match outcome {
        Some(outcome) => outcome.await.map_err(|_| Error::WriterStopped)?,
        None => std::future::pending().await,
    }
}

trait InterruptSource {
    async fn receive(&mut self) -> Result<(), Error>;
}

impl InterruptSource for tokio::signal::unix::Signal {
    async fn receive(&mut self) -> Result<(), Error> {
        self.recv()
            .await
            .ok_or_else(|| Error::Io(std::io::Error::other("terminal signal stream ended")))
    }
}

fn poll_outcomes(sources: &mut Sources<'_>, eof: &mut bool) -> Result<(), Error> {
    use oneshot::error::TryRecvError;
    if let Some(writer) = sources.writer.as_mut() {
        match writer.try_recv() {
            Ok(result) => {
                *sources.writer = None;
                result?;
                return Err(Error::WriterStopped);
            }
            Err(TryRecvError::Closed) => {
                *sources.writer = None;
                return Err(Error::WriterStopped);
            }
            Err(TryRecvError::Empty) => {}
        }
    }
    if let Some(reader) = sources.reader.as_mut() {
        match reader.try_recv() {
            Ok(result) => {
                *sources.reader = None;
                result?;
                *eof = true;
            }
            Err(TryRecvError::Closed) => {
                *sources.reader = None;
                return Err(Error::ReaderStopped);
            }
            Err(TryRecvError::Empty) => {}
        }
    }
    Ok(())
}

pub(super) async fn run<B: Backend>(
    ui: &mut LiveUi,
    terminal: &mut Terminal<B>,
    connection: &ConnectionInfo,
    sources: Sources<'_>,
    writer: &mpsc::Sender<WriterMessage>,
    limit: usize,
) -> Result<Exit, Error> {
    // Keep the registration alive while dispatch awaits writes. Recreating a
    // ctrl_c() future leaves a listener gap until its next poll.
    let mut signal = tokio::signal::unix::signal(tokio::signal::unix::SignalKind::interrupt())?;
    run_with_interrupts(
        ui,
        terminal,
        connection,
        sources,
        writer,
        limit,
        &mut signal,
    )
    .await
}

async fn run_with_interrupts<B: Backend>(
    ui: &mut LiveUi,
    terminal: &mut Terminal<B>,
    connection: &ConnectionInfo,
    mut sources: Sources<'_>,
    writer: &mpsc::Sender<WriterMessage>,
    limit: usize,
    signal: &mut impl InterruptSource,
) -> Result<Exit, Error> {
    let mut pending_input: Option<PendingInput> = None;
    let mut pending_interrupt: Option<usize> = None;
    let mut eof = false;
    let mut events_open = true;
    let mut next_paint = Instant::now();
    loop {
        poll_outcomes(&mut sources, &mut eof)?;
        let mut progressed = false;
        if pending_interrupt.is_none()
            && tokio::select! {
                biased;
                result = signal.receive() => { result?; true }
                _ = std::future::ready(()) => false,
            }
        {
            pending_interrupt = Some(captured_event_prefix(&sources));
            progressed = true;
        }
        let handled_signal = pending_interrupt.is_some();
        if pending_input.is_none() && !eof && !handled_signal {
            match sources.inputs.try_recv() {
                Ok(input) => {
                    pending_input = Some(PendingInput::capture(
                        input,
                        ui,
                        captured_event_prefix(&sources),
                    ))
                }
                Err(mpsc::error::TryRecvError::Disconnected) => return Ok(Exit::User),
                Err(mpsc::error::TryRecvError::Empty) => {}
            }
        }
        for _ in 0..EVENTS_PER_TURN {
            poll_outcomes(&mut sources, &mut eof)?;
            // Stop at the captured prefix boundary. Later output belongs after
            // this input even if it was admitted while dispatch awaited a write.
            if !eof
                && pending_interrupt.or_else(|| pending_input.as_ref().map(|input| input.remaining))
                    == Some(0)
            {
                break;
            }
            let event = sources
                .ready_event
                .take()
                .or_else(|| sources.events.try_recv().ok());
            let Some(event) = event else { break };
            if let Some(input) = &mut pending_input {
                input.remaining = input.remaining.saturating_sub(1);
            }
            if let Some(remaining) = &mut pending_interrupt {
                *remaining = remaining.saturating_sub(1);
            }
            progressed = true;
            if ui
                .dispatch(UiAction::BackendEvent(event.event), writer, limit)
                .await?
                == LoopControl::Exit
            {
                return Ok(Exit::User);
            }
            // The remaining field retains the wire permit through dispatch.
        }
        poll_outcomes(&mut sources, &mut eof)?;
        if !eof && pending_interrupt == Some(0) {
            pending_interrupt = None;
            progressed = true;
            if ui.interrupt(writer, limit, true).await? == LoopControl::Exit {
                return Ok(Exit::User);
            }
        }
        if !eof
            && !handled_signal
            && pending_input
                .as_ref()
                .is_some_and(|input| input.remaining == 0)
        {
            let input = pending_input.take().expect("ready input");
            progressed = true;
            if input.apply(ui, writer, limit).await? == LoopControl::Exit {
                return Ok(Exit::User);
            }
        }
        if Instant::now() >= next_paint {
            if ui.render_pending {
                ui.draw(terminal, connection)?;
            }
            next_paint = Instant::now() + FRAME_INTERVAL;
        }
        if eof && sources.ready_event.is_none() && sources.events.is_empty() {
            return Ok(Exit::Eof);
        }
        if progressed {
            tokio::task::yield_now().await;
            continue;
        }
        tokio::select! {
            biased;
            result = receive_writer(sources.writer) => {
                *sources.writer = None;
                result?;
                return Err(Error::WriterStopped);
            }
            result = receive_reader_outcome(sources.reader) => {
                *sources.reader = None;
                result?;
                eof = true;
            }
            result = signal.receive(), if pending_interrupt.is_none() => {
                result?;
                pending_interrupt = Some(captured_event_prefix(&sources));
            }
            input = sources.inputs.recv(), if pending_input.is_none() && pending_interrupt.is_none() && !eof => {
                match input {
                    Some(input) => pending_input = Some(PendingInput::capture(input, ui, captured_event_prefix(&sources))),
                    None => return Ok(Exit::User),
                }
            }
            event = sources.events.recv(), if events_open => {
                match event { Some(event) => *sources.ready_event = Some(event), None => events_open = false }
            }
            _ = tokio::time::sleep_until(next_paint) => {}
        }
    }
}

#[cfg(test)]
mod tests;
