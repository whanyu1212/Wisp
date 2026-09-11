//! Nonblocking command discovery and confirmed process-local mode changes.

use super::*;
use std::sync::Arc;
use wisp_protocol::events::CommandDescriptor;

#[derive(Clone, Debug, PartialEq)]
pub(super) struct PendingRead<T> {
    id: String,
    report: Option<T>,
}

#[derive(Clone, Debug, PartialEq)]
pub(super) struct PendingModeChange {
    id: String,
    mode: AgentMode,
}

impl UiState {
    pub(crate) fn configuration_active(&self) -> bool {
        self.mode_change.is_some() || self.model_configuration_active()
    }

    pub(crate) fn command_catalog_loading(&self) -> bool {
        self.command_catalog_read.is_some()
    }
}

pub(super) fn load_catalog(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if state.command_catalog_read.is_some() || state.exit_requested {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::GetCommands);
    let command = WispTypedClientRpcCommands::get_commands(&id)?;
    state.command_catalog_read = Some(PendingRead { id, report: None });
    state.command_catalog_error = None;
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

pub(super) fn load_mode(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if state.mode_read.is_some() || state.mode_change.is_some() || state.exit_requested {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::GetState);
    let command = WispTypedClientRpcCommands::get_state(&id)?;
    state.mode_read = Some(PendingRead { id, report: None });
    Ok(vec![UiEffect::SendCommand(command)])
}

pub(super) fn configure_mode(
    state: &mut UiState,
    mode: AgentMode,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if !state.can_select_model() || super::session_sync_pending(state) {
        return Ok(vec![
            UiEffect::Notice("Wait for the current operation before changing mode.".into()),
            UiEffect::RequestRender,
        ]);
    }
    let id = ids.next_id(CommandKind::Configure);
    let command = WispTypedClientRpcCommands::configure_mode(&id, mode)?;
    // A startup snapshot taken before this change must never undo its acknowledgement.
    state.mode_read = None;
    state.mode_change = Some(PendingModeChange { id, mode });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

pub(super) fn observe(
    state: &mut UiState,
    event: &BackendEvent,
    ids: &mut impl CommandIdSource,
) -> Result<Option<Vec<UiEffect>>, ProtocolDecodeError> {
    match event {
        BackendEvent::CommandCatalogReported {
            command_id,
            catalog,
        } => {
            if let Some(read) = &mut state.command_catalog_read {
                if read.id == *command_id {
                    read.report = Some(catalog.clone());
                }
            }
            return Ok(Some(Vec::new()));
        }
        BackendEvent::ModeReported { command_id, mode } => {
            if let Some(read) = &mut state.mode_read {
                if read.id == *command_id {
                    read.report = Some(*mode);
                }
            }
            return Ok(Some(Vec::new()));
        }
        _ => {}
    }
    let BackendEvent::CommandFinished {
        command_id,
        command_type,
        ok,
        error,
    } = event
    else {
        return Ok(None);
    };
    if command_type == "get_commands"
        && state
            .command_catalog_read
            .as_ref()
            .is_some_and(|read| read.id == *command_id)
    {
        let read = state
            .command_catalog_read
            .take()
            .expect("matched catalog read");
        if let Some(catalog) = read.report.filter(|_| *ok) {
            state.command_catalog = Some(catalog);
            state.command_catalog_error = None;
        } else {
            state.command_catalog_error = Some(bounded_session_text(
                error
                    .as_deref()
                    .unwrap_or("Command discovery did not return a catalog."),
                SESSION_NOTICE_MAX_BYTES,
            ));
        }
        return Ok(Some(vec![
            UiEffect::CommandCatalogChanged,
            UiEffect::RequestRender,
        ]));
    }
    if command_type == "get_state"
        && state
            .mode_read
            .as_ref()
            .is_some_and(|read| read.id == *command_id)
    {
        let read = state.mode_read.take().expect("matched mode read");
        if let Some(mode) = read.report.filter(|_| *ok) {
            state.mode = mode;
            state.mode_confirmed = true;
        }
        return Ok(Some(vec![UiEffect::RequestRender]));
    }
    if command_type == "configure"
        && state
            .mode_change
            .as_ref()
            .is_some_and(|change| change.id == *command_id)
    {
        let change = state.mode_change.take().expect("matched mode change");
        let mut effects = if *ok {
            state.mode = change.mode;
            state.mode_confirmed = true;
            vec![
                UiEffect::ModeConfigurationApplied,
                UiEffect::Notice(format!("{} mode enabled.", change.mode.as_str())),
            ]
        } else {
            vec![UiEffect::Notice(bounded_session_text(
                error
                    .as_deref()
                    .unwrap_or("Mode change failed; input was kept."),
                SESSION_NOTICE_MAX_BYTES,
            ))]
        };
        if !ok && !state.mode_confirmed {
            effects.extend(load_mode(state, ids)?);
        }
        effects.push(UiEffect::RequestRender);
        return Ok(Some(effects));
    }
    Ok(None)
}

pub(super) type CommandCatalog = Arc<[CommandDescriptor]>;

#[cfg(test)]
mod tests {
    use super::*;
    #[derive(Default)]
    struct Ids(usize);
    impl CommandIdSource for Ids {
        fn next_id(&mut self, kind: CommandKind) -> String {
            self.0 += 1;
            format!("{}-{}", kind.prefix(), self.0)
        }
    }
    fn finished(id: &str, kind: &str, ok: bool) -> BackendEvent {
        BackendEvent::CommandFinished {
            command_id: id.into(),
            command_type: kind.into(),
            ok,
            error: None,
        }
    }
    fn sends(effects: &[UiEffect]) -> bool {
        effects
            .iter()
            .any(|effect| matches!(effect, UiEffect::SendCommand(_)))
    }

    #[test]
    fn catalog_is_correlated_nonblocking_and_retryable() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        load_catalog(&mut state, &mut ids).unwrap();
        assert!(state.editor_editable());
        let catalog: CommandCatalog = crate::commands::tests::catalog().into();
        observe(
            &mut state,
            &BackendEvent::CommandCatalogReported {
                command_id: "wrong".into(),
                catalog: catalog.clone(),
            },
            &mut ids,
        )
        .unwrap();
        observe(
            &mut state,
            &finished("get_commands-1", "get_commands", true),
            &mut ids,
        )
        .unwrap();
        assert!(state.command_catalog.is_none());
        assert!(state.command_catalog_error.is_some());
        load_catalog(&mut state, &mut ids).unwrap();
        observe(
            &mut state,
            &BackendEvent::CommandCatalogReported {
                command_id: "get_commands-2".into(),
                catalog: catalog.clone(),
            },
            &mut ids,
        )
        .unwrap();
        assert!(state.command_catalog.is_none());
        observe(
            &mut state,
            &finished("get_commands-2", "configure", true),
            &mut ids,
        )
        .unwrap();
        assert!(state.command_catalog_loading());
        observe(
            &mut state,
            &finished("get_commands-2", "get_commands", true),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.command_catalog, Some(catalog.clone()));
        load_catalog(&mut state, &mut ids).unwrap();
        observe(
            &mut state,
            &finished("get_commands-3", "get_commands", false),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.command_catalog, Some(catalog));
        assert!(!state.command_catalog_loading());
    }

    #[test]
    fn refreshed_catalog_ignores_superseded_reports() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        load_catalog(&mut state, &mut ids).unwrap();
        reduce(
            &mut state,
            UiAction::BackendEvent(BackendEvent::ProjectConfigApplied {
                provider: "fake".into(),
                model: None,
                effort: None,
            }),
            &mut ids,
        )
        .unwrap();
        let new_id = state.command_catalog_read.as_ref().unwrap().id.clone();
        assert_ne!(new_id, "get_commands-1");
        observe(
            &mut state,
            &BackendEvent::CommandCatalogReported {
                command_id: "get_commands-1".into(),
                catalog: crate::commands::tests::catalog().into(),
            },
            &mut ids,
        )
        .unwrap();
        observe(
            &mut state,
            &finished("get_commands-1", "get_commands", true),
            &mut ids,
        )
        .unwrap();
        assert!(state.command_catalog.is_none());
        assert_eq!(state.command_catalog_read.as_ref().unwrap().id, new_id);
    }

    #[test]
    fn initial_mode_does_not_change_active_work_and_requires_success() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        load_mode(&mut state, &mut ids).unwrap();
        reduce(&mut state, UiAction::Submit("working".into()), &mut ids).unwrap();
        let active = state.current_command.clone();
        observe(
            &mut state,
            &BackendEvent::ModeReported {
                command_id: "get_state-1".into(),
                mode: AgentMode::Plan,
            },
            &mut ids,
        )
        .unwrap();
        assert!(!state.mode_confirmed);
        observe(
            &mut state,
            &finished("get_state-1", "get_state", true),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.mode, AgentMode::Plan);
        assert!(state.mode_confirmed);
        assert_eq!(state.current_command, active);
        assert_eq!(state.view_status, ViewStatus::Running);
    }

    #[test]
    fn mode_change_serializes_mutations_and_retires_stale_startup_read() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        load_mode(&mut state, &mut ids).unwrap();
        let effects = configure_mode(&mut state, AgentMode::Plan, &mut ids).unwrap();
        let UiEffect::SendCommand(command) = &effects[0] else {
            panic!("expected configure")
        };
        let wire = serde_json::to_value(command).unwrap();
        assert_eq!(wire["mode"], "plan");
        assert!(
            wire.get("persist_model_selection")
                .is_none_or(|value| value == false)
        );
        assert!(state.configuration_active());
        assert!(!state.editor_editable());
        for action in [
            UiAction::Submit("no".into()),
            UiAction::NewSession,
            UiAction::ConfigureMode(AgentMode::Build),
            UiAction::ConfigureModel(ModelConfiguration::default()),
        ] {
            assert!(!sends(&reduce(&mut state, action, &mut ids).unwrap()));
        }
        observe(
            &mut state,
            &finished("configure-2", "get_state", true),
            &mut ids,
        )
        .unwrap();
        assert!(state.configuration_active());
        observe(
            &mut state,
            &finished("configure-2", "configure", true),
            &mut ids,
        )
        .unwrap();
        observe(
            &mut state,
            &BackendEvent::ModeReported {
                command_id: "get_state-1".into(),
                mode: AgentMode::Build,
            },
            &mut ids,
        )
        .unwrap();
        observe(
            &mut state,
            &finished("get_state-1", "get_state", true),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.mode, AgentMode::Plan);
        assert!(state.mode_confirmed);
        assert!(!state.configuration_active());
    }

    #[test]
    fn failed_mode_preserves_confirmed_mode_and_retries_unknown_mode_once() {
        let mut state = UiState::unconfigured();
        let mut ids = Ids::default();
        configure_mode(&mut state, AgentMode::Plan, &mut ids).unwrap();
        let effects = observe(
            &mut state,
            &finished("configure-1", "configure", false),
            &mut ids,
        )
        .unwrap()
        .unwrap();
        assert!(sends(&effects));
        assert!(!state.mode_confirmed);
        let effects = observe(
            &mut state,
            &finished("get_state-2", "get_state", false),
            &mut ids,
        )
        .unwrap()
        .unwrap();
        assert!(!sends(&effects));
        state.mode_confirmed = true;
        configure_mode(&mut state, AgentMode::Plan, &mut ids).unwrap();
        let effects = observe(
            &mut state,
            &finished("configure-3", "configure", false),
            &mut ids,
        )
        .unwrap()
        .unwrap();
        assert!(!sends(&effects));
        assert_eq!(state.mode, AgentMode::Build);
        assert!(state.editor_editable());
    }
}
