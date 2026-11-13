//! Signals that reach the user outside the TUI: the terminal title and a notification
//! when a run finishes or waits for them while the terminal is in the background.
//!
//! [`Attention`] decides what to signal from the run state alone; the bytes are written
//! through [`crate::terminal::FrameBackend::write_control`] after a frame is drawn.

use crate::reducer::{InteractionStatus, UiState, ViewStatus};

/// The variable that chooses notifications: `on` (the default), `bell` or `off`.
pub(crate) const NOTIFY_VARIABLE: &str = "WISP_TUI_NOTIFY";
/// The variable that turns off motion such as the approval pulse: `off`.
pub(crate) const MOTION_VARIABLE: &str = "WISP_TUI_MOTION";

/// What a run is doing, as far as the user outside the TUI needs to know.
#[derive(Clone, Copy, Debug, PartialEq, Eq)]
pub(crate) enum RunState {
    Ready,
    Working,
    NeedsApproval,
    NeedsTrust,
    Failed,
}

impl RunState {
    /// A failed command (prompt, `/compact` or `/init`) leaves the TUI idle with the
    /// failure kept until the next command starts, so that, not only the
    /// transport-closed `Error` status, makes the state failed.
    pub(crate) fn of(state: &UiState) -> Self {
        match state.view_status {
            ViewStatus::WaitingForApproval => Self::NeedsApproval,
            ViewStatus::WaitingForTrust => Self::NeedsTrust,
            ViewStatus::Error => Self::Failed,
            ViewStatus::Running => Self::Working,
            ViewStatus::Idle if state.interaction_status == InteractionStatus::Compacting => {
                Self::Working
            }
            ViewStatus::Idle if state.last_command_failed => Self::Failed,
            ViewStatus::Idle => Self::Ready,
        }
    }

    /// The state's word in the title; a ready TUI shows none.
    fn label(self) -> Option<&'static str> {
        match self {
            Self::Ready => None,
            Self::Working => Some("working"),
            Self::NeedsApproval => Some("needs approval"),
            Self::NeedsTrust => Some("needs trust"),
            Self::Failed => Some("error"),
        }
    }
}

/// How a notification reaches the user.
#[derive(Clone, Copy, Debug, Default, PartialEq, Eq)]
pub(crate) enum NotifyMode {
    /// A desktop notification through OSC 9.
    #[default]
    Desktop,
    /// The terminal bell, which most terminals turn into a badge or a bounce.
    Bell,
    Off,
}

impl NotifyMode {
    /// Reads `WISP_TUI_NOTIFY`: `bell` and `off` choose those modes; anything else,
    /// including no value, is a desktop notification.
    pub(crate) fn from_value(value: Option<&str>) -> Self {
        match value
            .map(|value| value.trim().to_ascii_lowercase())
            .as_deref()
        {
            Some("bell") => Self::Bell,
            Some("off" | "0" | "false") => Self::Off,
            _ => Self::Desktop,
        }
    }
}

/// Whether motion is allowed: on unless `WISP_TUI_MOTION` is `off`, `0` or `false`.
pub(crate) fn motion_allowed(value: Option<&str>) -> bool {
    !matches!(
        value
            .map(|value| value.trim().to_ascii_lowercase())
            .as_deref(),
        Some("off" | "0" | "false")
    )
}

/// Tracks the run state and focus between frames and turns changes into terminal output.
#[derive(Debug, Default)]
pub(crate) struct Attention {
    mode: NotifyMode,
    /// Whether the terminal has focus. `None` until it first reports a change, which is
    /// also all a terminal without focus reporting ever says; Wisp then assumes the user
    /// is watching and does not notify.
    focused: Option<bool>,
    last_state: Option<RunState>,
    last_title: Option<String>,
    /// Background-process exits already accounted for; `None` until first observed.
    seen_exits: Option<u64>,
}

impl Attention {
    pub(crate) fn new(mode: NotifyMode) -> Self {
        Self {
            mode,
            ..Self::default()
        }
    }

    pub(crate) fn set_focus(&mut self, focused: bool) {
        self.focused = Some(focused);
    }

    /// Makes the next [`Attention::observe`] write the title again, after something
    /// else, such as an external editor, may have changed it.
    pub(crate) fn forget_title(&mut self) {
        self.last_title = None;
    }

    /// Whether the terminal has reported that it is in the background.
    pub(crate) fn focus_lost(&self) -> bool {
        self.focused == Some(false)
    }

    /// Records the current run state and returns the terminal output it calls for: a new
    /// title when the title text changed, and a notification when the run just finished,
    /// failed or started waiting while the terminal is known to be in the background.
    ///
    /// `folder` is the workspace path; only its last component is shown.
    pub(crate) fn observe(&mut self, state: RunState, folder: Option<&str>) -> Vec<u8> {
        let mut output = Vec::new();
        let folder = folder.and_then(folder_name);
        let title = title(state, folder.as_deref());
        if self.last_title.as_deref() != Some(title.as_str()) {
            output.extend_from_slice(format!("\x1b]0;{title}\x07").as_bytes());
            self.last_title = Some(title);
        }
        let previous = self.last_state.replace(state);
        let message = previous.and_then(|previous| notification(previous, state));
        if let Some(message) = message.filter(|_| self.focus_lost()) {
            self.notify(&mut output, message, folder.as_deref());
        }
        output
    }

    /// Records how many background processes have stopped on their own and returns a
    /// notification for those that stopped since the last call, if the terminal is
    /// in the background. Exits seen while focused are accounted for without one, so
    /// losing focus later does not announce them.
    ///
    /// `latest` describes the newest exit; it is model-influenced text and is made
    /// safe here before it goes into the notification.
    pub(crate) fn observe_exits(
        &mut self,
        exits: u64,
        latest: Option<&str>,
        folder: Option<&str>,
    ) -> Vec<u8> {
        let mut output = Vec::new();
        let Some(previous) = self.seen_exits.replace(exits) else {
            return output;
        };
        let new = exits.saturating_sub(previous);
        if new == 0 || !self.focus_lost() {
            return output;
        }
        let message = match latest.map(notice_text) {
            Some(latest) if new == 1 && !latest.is_empty() => latest,
            _ => format!("{new} background processes stopped"),
        };
        let folder = folder.and_then(folder_name);
        self.notify(&mut output, &message, folder.as_deref());
        output
    }

    fn notify(&self, output: &mut Vec<u8>, message: &str, folder: Option<&str>) {
        match self.mode {
            NotifyMode::Desktop => {
                let text = match folder {
                    Some(folder) => format!("{message} · {folder}"),
                    None => message.to_owned(),
                };
                output.extend_from_slice(format!("\x1b]9;{text}\x07").as_bytes());
            }
            NotifyMode::Bell => output.push(0x07),
            NotifyMode::Off => {}
        }
    }
}

/// Text made safe to place inside an OSC string, on one line.
fn notice_text(text: &str) -> String {
    crate::ui::sanitize_for_terminal(text)
        .replace('\n', " ")
        .trim()
        .to_owned()
}

fn title(state: RunState, folder: Option<&str>) -> String {
    ["Wisp"]
        .into_iter()
        .chain(state.label())
        .chain(folder)
        .collect::<Vec<_>>()
        .join(" · ")
}

/// The message for a change worth interrupting the user for, if this is one.
fn notification(previous: RunState, current: RunState) -> Option<&'static str> {
    if previous == current {
        return None;
    }
    match (previous, current) {
        (_, RunState::NeedsApproval) => Some("Wisp needs approval"),
        (_, RunState::NeedsTrust) => Some("Wisp needs a trust decision"),
        (RunState::Working, RunState::Failed) => Some("Wisp run failed"),
        // The backend stopped with no run going.
        (_, RunState::Failed) => Some("Wisp stopped with an error"),
        (RunState::Working, RunState::Ready) => Some("Wisp finished"),
        _ => None,
    }
}

/// The last component of a workspace path, made safe to place inside an OSC string:
/// control characters could end the sequence early or start another one.
fn folder_name(path: &str) -> Option<String> {
    let name = path.rsplit('/').find(|part| !part.is_empty())?;
    let safe = notice_text(name);
    (!safe.is_empty()).then_some(safe)
}

#[cfg(test)]
mod tests {
    use super::*;

    fn text(bytes: Vec<u8>) -> String {
        String::from_utf8(bytes).unwrap()
    }

    #[test]
    fn title_follows_state() {
        let mut attention = Attention::new(NotifyMode::Desktop);
        assert_eq!(
            text(attention.observe(RunState::Ready, Some("/srv/demo-app"))),
            "\x1b]0;Wisp · demo-app\x07"
        );
        assert_eq!(
            text(attention.observe(RunState::Ready, Some("/srv/demo-app"))),
            ""
        );
        assert_eq!(
            text(attention.observe(RunState::Working, Some("/srv/demo-app/"))),
            "\x1b]0;Wisp · working · demo-app\x07"
        );
        assert_eq!(
            text(attention.observe(RunState::NeedsApproval, None)),
            "\x1b]0;Wisp · needs approval\x07"
        );
    }

    #[test]
    fn notifies_only_when_unfocused() {
        let run = |focus: Option<bool>| {
            let mut attention = Attention::new(NotifyMode::Desktop);
            if let Some(focused) = focus {
                attention.set_focus(focused);
            }
            attention.observe(RunState::Working, Some("/srv/app"));
            text(attention.observe(RunState::Ready, Some("/srv/app")))
        };
        assert_eq!(
            run(Some(false)),
            "\x1b]0;Wisp · app\x07\x1b]9;Wisp finished · app\x07"
        );
        assert_eq!(run(Some(true)), "\x1b]0;Wisp · app\x07");
        assert_eq!(run(None), "\x1b]0;Wisp · app\x07");
    }

    #[test]
    fn exit_notified_once_unfocused() {
        let mut attention = Attention::new(NotifyMode::Desktop);
        attention.observe_exits(0, None, Some("/srv/app"));
        attention.set_focus(false);

        let first = text(attention.observe_exits(
            1,
            Some("Background process finished: make"),
            Some("/srv/app"),
        ));
        let again = text(attention.observe_exits(
            1,
            Some("Background process finished: make"),
            Some("/srv/app"),
        ));
        let burst = text(attention.observe_exits(3, Some("Background process failed: x"), None));

        assert_eq!(first, "\x1b]9;Background process finished: make · app\x07");
        assert_eq!(again, "");
        assert_eq!(burst, "\x1b]9;2 background processes stopped\x07");
    }

    #[test]
    fn focused_exits_not_replayed() {
        let mut attention = Attention::new(NotifyMode::Desktop);
        attention.observe_exits(0, None, None);
        attention.set_focus(true);
        assert_eq!(text(attention.observe_exits(2, Some("done"), None)), "");

        attention.set_focus(false);

        assert_eq!(text(attention.observe_exits(2, Some("done"), None)), "");
        assert_eq!(text(attention.observe_exits(0, None, None)), "");
    }

    #[test]
    fn exit_notice_sanitized() {
        let mut attention = Attention::new(NotifyMode::Desktop);
        attention.observe_exits(0, None, None);
        attention.set_focus(false);

        let output = text(attention.observe_exits(
            1,
            Some("Background process finished: x\x07\x1b]0;owned\x07\nnext"),
            None,
        ));

        assert!(
            output.starts_with("\x1b]9;Background process finished: x"),
            "{output:?}"
        );
        assert_eq!(output.matches('\x07').count(), 1, "{output:?}");
        assert_eq!(output.matches('\x1b').count(), 1, "{output:?}");
        assert!(!output.contains('\n'), "{output:?}");
    }

    #[test]
    fn notification_transitions() {
        use RunState::*;
        for (previous, current, expected) in [
            (Working, NeedsApproval, Some("Wisp needs approval")),
            (Ready, NeedsTrust, Some("Wisp needs a trust decision")),
            (Working, Failed, Some("Wisp run failed")),
            (Ready, Failed, Some("Wisp stopped with an error")),
            (Working, Ready, Some("Wisp finished")),
            // Denying or answering a decision happens while the user is there.
            (NeedsApproval, Ready, None),
            (NeedsApproval, Working, None),
            (Ready, Working, None),
            (Failed, Failed, None),
            (NeedsApproval, NeedsApproval, None),
        ] {
            assert_eq!(
                notification(previous, current),
                expected,
                "{previous:?} → {current:?}"
            );
        }
    }

    #[test]
    fn run_state_of_ui() {
        let mut state = UiState::unconfigured();
        state.view_status = ViewStatus::Running;
        assert_eq!(RunState::of(&state), RunState::Working);
        // A failed prompt goes back to idle but keeps its error until the next submit.
        state.view_status = ViewStatus::Idle;
        state.last_command_failed = true;
        assert_eq!(RunState::of(&state), RunState::Failed);
        state.last_command_failed = false;
        assert_eq!(RunState::of(&state), RunState::Ready);
        state.interaction_status = InteractionStatus::Compacting;
        assert_eq!(RunState::of(&state), RunState::Working);
        state.interaction_status = InteractionStatus::Idle;
        for (status, expected) in [
            (ViewStatus::WaitingForApproval, RunState::NeedsApproval),
            (ViewStatus::WaitingForTrust, RunState::NeedsTrust),
            (ViewStatus::Error, RunState::Failed),
        ] {
            state.view_status = status;
            assert_eq!(RunState::of(&state), expected, "{status:?}");
        }
    }

    #[test]
    fn first_state_never_notifies() {
        let mut attention = Attention::new(NotifyMode::Desktop);
        attention.set_focus(false);
        assert_eq!(
            text(attention.observe(RunState::NeedsTrust, None)),
            "\x1b]0;Wisp · needs trust\x07"
        );
    }

    #[test]
    fn notify_modes() {
        let finished = |mode: NotifyMode| {
            let mut attention = Attention::new(mode);
            attention.set_focus(false);
            attention.observe(RunState::Working, None);
            attention.observe(RunState::Ready, None)
        };
        assert!(text(finished(NotifyMode::Desktop)).ends_with("\x1b]9;Wisp finished\x07"));
        assert!(finished(NotifyMode::Bell).ends_with(b"\x07"));
        assert!(!text(finished(NotifyMode::Bell)).contains("]9;"));
        assert_eq!(text(finished(NotifyMode::Off)), "\x1b]0;Wisp\x07");

        for (value, expected) in [
            (None, NotifyMode::Desktop),
            (Some("on"), NotifyMode::Desktop),
            (Some(" Bell "), NotifyMode::Bell),
            (Some("off"), NotifyMode::Off),
            (Some("0"), NotifyMode::Off),
        ] {
            assert_eq!(NotifyMode::from_value(value), expected, "{value:?}");
        }
        assert!(motion_allowed(None) && motion_allowed(Some("on")));
        assert!(!motion_allowed(Some("off")) && !motion_allowed(Some("FALSE")));
    }

    #[test]
    fn folder_cannot_escape() {
        let mut attention = Attention::new(NotifyMode::Desktop);
        let output = text(attention.observe(RunState::Ready, Some("/tmp/a\x07b\x1b]9;x\nc")));
        assert_eq!(output.matches('\x07').count(), 1, "{output:?}");
        assert_eq!(output.matches('\x1b').count(), 1, "{output:?}");
        assert!(!output.contains('\n'), "{output:?}");
    }
}
