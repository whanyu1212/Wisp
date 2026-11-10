//! `/cd`: moving the session to another folder, and another project.
//!
//! The backend resolves and checks the folder, saves the move, and reports what
//! changed before the command finishes: `session.directory_changed` for the
//! divider, `rpc.workspace` for the footer, and for a move into another project its
//! configuration, skills and permission mode.

use super::*;

pub(super) fn change(
    state: &mut UiState,
    path: String,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    // The backend would queue a move behind running work; like other configuration,
    // the TUI asks for it once the session is idle instead.
    if !state.can_select_model() || super::session_sync_pending(state) {
        return Ok(vec![
            UiEffect::Notice("Wait for the current operation before changing directory.".into()),
            UiEffect::RequestRender,
        ]);
    }
    let id = ids.next_id(CommandKind::ChangeDirectory);
    let command = WispTypedClientRpcCommands::change_directory(&id, &path)?;
    state.directory_change = Some(id);
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

/// Claims the reports addressed to a pending `/cd`.
pub(super) fn observe(state: &mut UiState, event: &BackendEvent) -> Option<Vec<UiEffect>> {
    let pending = state.directory_change.as_deref()?;
    match event {
        // A move into another project reports that project's permission mode for the
        // move's own command; it is the mode in effect from now on.
        BackendEvent::PermissionsReported {
            command_id,
            permissions,
        } if command_id == pending => {
            state.permissions.adopt(permissions);
            Some(vec![UiEffect::RequestRender])
        }
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } if command_type == "change_directory" && command_id == pending => {
            state.directory_change = None;
            // A move shows in the footer and as the transcript's divider; only a failure
            // needs a notice.
            if *ok {
                return Some(vec![UiEffect::RequestRender]);
            }
            let notice = bounded_session_text(
                error.as_deref().unwrap_or("Could not change directory."),
                SESSION_NOTICE_MAX_BYTES,
            );
            Some(vec![UiEffect::Notice(notice), UiEffect::RequestRender])
        }
        _ => None,
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use wisp_protocol::commands::PermissionMode;

    #[derive(Default)]
    struct Ids(usize);

    impl CommandIdSource for Ids {
        fn next_id(&mut self, kind: CommandKind) -> String {
            self.0 += 1;
            format!("{}-{}", kind.prefix(), self.0)
        }
    }

    /// Feeds `value` through the same decoding a live backend event takes.
    fn backend(state: &mut UiState, ids: &mut Ids, value: serde_json::Value) -> Vec<UiEffect> {
        let mut value = value;
        value["timestamp"] = "2026-01-02T03:04:05Z".into();
        let live = wisp_protocol::events::deserialize(value).unwrap();
        let event = BackendEvent::from_live(&live).unwrap();
        reduce(state, UiAction::BackendEvent(event), ids).unwrap()
    }

    fn finished(ok: bool, error: Option<&str>) -> serde_json::Value {
        serde_json::json!({
            "type": "rpc.command.finished", "command_id": "change_directory-1",
            "command_type": "change_directory", "ok": ok, "error": error,
        })
    }

    fn notices(effects: &[UiEffect]) -> Vec<&str> {
        effects
            .iter()
            .filter_map(|effect| match effect {
                UiEffect::Notice(text) => Some(text.as_str()),
                _ => None,
            })
            .collect()
    }

    fn change_directory(state: &mut UiState, ids: &mut Ids, path: &str) -> Vec<UiEffect> {
        reduce(state, UiAction::ChangeDirectory(path.into()), ids).unwrap()
    }

    #[test]
    fn sends_path_and_follows_folder() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        let effects = change_directory(&mut state, &mut ids, "~/work/app");
        let command = effects
            .iter()
            .find_map(|effect| match effect {
                UiEffect::SendCommand(command) => Some(serde_json::to_value(command).unwrap()),
                _ => None,
            })
            .unwrap();
        assert_eq!(command["type"], "change_directory");
        assert_eq!(command["path"], "~/work/app");
        // A second move waits for the first.
        assert!(state.configuration_active());
        let refused = change_directory(&mut state, &mut ids, "src");
        assert!(notices(&refused)[0].starts_with("Wait for the current operation"));

        backend(
            &mut state,
            &mut ids,
            serde_json::json!({"type": "rpc.workspace", "cwd": "/work/app", "git_branch": null}),
        );
        let effects = backend(&mut state, &mut ids, finished(true, None));
        assert!(notices(&effects).is_empty());
        assert_eq!(state.workspace_cwd.as_deref(), Some("/work/app"));
        assert!(!state.configuration_active());
    }

    #[test]
    fn failure_shows_backend_error() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        change_directory(&mut state, &mut ids, "missing");
        let effects = backend(
            &mut state,
            &mut ids,
            finished(false, Some("No such folder: /work/missing")),
        );
        assert_eq!(notices(&effects), ["No such folder: /work/missing"]);
        assert!(!state.configuration_active());
    }

    #[test]
    fn new_project_mode_applies() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        change_directory(&mut state, &mut ids, "/work/other");
        backend(
            &mut state,
            &mut ids,
            serde_json::json!({
                "type": "rpc.permissions", "command_id": "change_directory-1",
                "permissions": {"mode": "yolo", "saved_mode": "yolo",
                    "project_path": "/work/other", "session_only_yolo": false},
            }),
        );
        let permissions = state.permissions.snapshot.as_deref().unwrap();
        assert_eq!(permissions.mode, PermissionMode::Yolo);
        // A report for a command nobody sent is still ignored.
        backend(
            &mut state,
            &mut ids,
            serde_json::json!({
                "type": "rpc.permissions", "command_id": "elsewhere-1",
                "permissions": {"mode": "ask", "saved_mode": null,
                    "project_path": null, "session_only_yolo": false},
            }),
        );
        let permissions = state.permissions.snapshot.as_deref().unwrap();
        assert_eq!(permissions.mode, PermissionMode::Yolo);
    }
}
