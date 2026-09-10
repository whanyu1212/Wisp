//! Correlated catalog reads and explicit model configuration for one frontend.

use super::*;

#[derive(Clone, Debug, PartialEq)]
pub(super) enum ModelOperation {
    Catalog {
        id: String,
        report: Option<ModelCatalogSnapshot>,
        invalidated: bool,
    },
    Configure {
        id: String,
        report: Option<ModelCatalogSnapshot>,
        invalidated: bool,
    },
}

impl ModelOperation {
    fn identity(&self) -> (&str, &str) {
        match self {
            Self::Catalog { id, .. } => (id, "get_model_catalog"),
            Self::Configure { id, .. } => (id, "configure"),
        }
    }
}

impl UiState {
    pub(crate) fn model_configuration_active(&self) -> bool {
        matches!(self.model_operation, Some(ModelOperation::Configure { .. }))
    }

    pub(crate) fn can_select_model(&self) -> bool {
        self.input_ready
            && !self.exit_requested
            && self.view_status == ViewStatus::Idle
            && self.current_command.is_none()
            && self.session_operation.is_none()
            && self.connection_operation.is_none()
            && self.history_request.is_none()
            && !self.model_configuration_active()
    }
}

pub(super) fn load(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if state.model_operation.is_some() || state.exit_requested {
        return Ok(Vec::new());
    }
    let id = ids.next_id(CommandKind::GetModelCatalog);
    let command = WispTypedClientRpcCommands::get_model_catalog(&id)?;
    state.model_operation = Some(ModelOperation::Catalog {
        id,
        report: None,
        invalidated: false,
    });
    Ok(vec![
        UiEffect::InvalidateModelCatalog,
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

pub(super) fn open(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if !state.can_select_model() {
        return Ok(busy());
    }
    let mut effects = vec![UiEffect::ShowModelPicker];
    effects.extend(load(state, ids)?);
    Ok(effects)
}

pub(super) fn configure(
    state: &mut UiState,
    configuration: ModelConfiguration,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    if !state.can_select_model() || state.model_operation.is_some() {
        return Ok(busy());
    }
    let id = ids.next_id(CommandKind::Configure);
    let command = WispTypedClientRpcCommands::configure_model(&id, &configuration)?;
    state.model_operation = Some(ModelOperation::Configure {
        id,
        report: None,
        invalidated: false,
    });
    Ok(vec![
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}

fn busy() -> Vec<UiEffect> {
    vec![
        UiEffect::Notice("Wait for the current operation before changing models.".into()),
        UiEffect::RequestRender,
    ]
}

pub(super) fn invalidate(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
) -> Result<Vec<UiEffect>, ProtocolDecodeError> {
    state.model_catalog = None;
    state.model_selection_stale = false; // The configuration event itself supplies current selection.
    if let Some(operation) = &mut state.model_operation {
        match operation {
            ModelOperation::Catalog { invalidated, .. }
            | ModelOperation::Configure { invalidated, .. } => *invalidated = true,
        }
    }
    let mut effects = vec![UiEffect::InvalidateModelCatalog];
    effects.extend(load(state, ids)?);
    Ok(effects)
}

pub(super) fn reject(state: &mut UiState, command_id: &str, error: String) -> Vec<UiEffect> {
    if state
        .model_operation
        .as_ref()
        .is_none_or(|op| op.identity().0 != command_id)
    {
        return Vec::new();
    }
    let configuring = state.model_configuration_active();
    state.model_operation = None;
    let mut effects = vec![UiEffect::Diagnostic(error), UiEffect::RequestRender];
    if !configuring {
        effects.push(UiEffect::ModelCatalogUnavailable);
    }
    effects
}

pub(super) fn observe(
    state: &mut UiState,
    event: &BackendEvent,
    ids: &mut impl CommandIdSource,
) -> Result<Option<Vec<UiEffect>>, ProtocolDecodeError> {
    let Some(operation) = state.model_operation.as_mut() else {
        return Ok(None);
    };
    let (id, kind) = operation.identity();
    match event {
        BackendEvent::ModelCatalogReported {
            command_id,
            catalog,
        } if command_id == id => {
            match operation {
                ModelOperation::Catalog {
                    report,
                    invalidated,
                    ..
                }
                | ModelOperation::Configure {
                    report,
                    invalidated,
                    ..
                } => {
                    if !*invalidated && report.is_none() {
                        *report = Some(catalog.clone());
                    }
                }
            }
            Ok(Some(Vec::new()))
        }
        BackendEvent::CommandFinished {
            command_id,
            command_type,
            ok,
            error,
        } if command_id == id && command_type == kind => {
            // Terminal order is part of the wire contract. A missing report must
            // release the UI, not wait forever for an event that may never arrive.
            let operation = state.model_operation.take().expect("matched operation");
            let configuring = matches!(operation, ModelOperation::Configure { .. });
            let (applied, report, invalidated) = match operation {
                ModelOperation::Configure {
                    report,
                    invalidated,
                    ..
                } => (*ok, report, invalidated),
                ModelOperation::Catalog {
                    report,
                    invalidated,
                    ..
                } => (false, report, invalidated),
            };
            let mut effects = Vec::new();
            if applied {
                effects.push(UiEffect::ModelConfigurationApplied);
            }
            if invalidated {
                effects.extend(load(state, ids)?);
            } else if *ok {
                if let Some(catalog) = report {
                    state.provider = Some(catalog.selection.provider.clone());
                    state.model = catalog.selection.model.clone();
                    state.effort = catalog.selection.effort.clone();
                    state.model_selection_stale = false;
                    state.model_catalog = Some(catalog.clone());
                    effects.push(UiEffect::ModelCatalogUpdated(catalog));
                } else if applied {
                    state.model_catalog = None;
                    state.model_selection_stale = true;
                    effects.extend(load(state, ids)?);
                } else {
                    effects.push(UiEffect::ModelCatalogUnavailable);
                    effects.push(UiEffect::Diagnostic(
                        "Model catalog unavailable. Use /model <model> or press r to retry.".into(),
                    ));
                }
            } else {
                if !configuring {
                    effects.push(UiEffect::ModelCatalogUnavailable);
                }
                effects.push(UiEffect::Diagnostic(bounded_session_text(
                    &format!(
                        "Model request failed: {}",
                        error.as_deref().unwrap_or("backend reported failure")
                    ),
                    SESSION_NOTICE_MAX_BYTES,
                )));
            }
            effects.push(UiEffect::RequestRender);
            Ok(Some(effects))
        }
        _ => Ok(None),
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::SequentialCommandIds;
    use crate::model_picker::tests::catalog;

    fn request(effects: &[UiEffect]) -> Value {
        effects
            .iter()
            .find_map(|effect| match effect {
                UiEffect::SendCommand(command) => Some(command.to_value().unwrap()),
                _ => None,
            })
            .expect("one command")
    }

    fn finished(id: &str, kind: &str, ok: bool) -> BackendEvent {
        BackendEvent::CommandFinished {
            command_id: id.into(),
            command_type: kind.into(),
            ok,
            error: (!ok).then(|| "rejected".into()),
        }
    }

    fn configure_request(state: &mut UiState, ids: &mut SequentialCommandIds) -> String {
        let effects = configure(
            state,
            ModelConfiguration {
                model: Some("alias".into()),
                persist_model_selection: true,
                ..Default::default()
            },
            ids,
        )
        .unwrap();
        request(&effects)["id"].as_str().unwrap().into()
    }

    #[test]
    fn report_and_matching_success_are_both_required_to_adopt_selection() {
        let mut state = UiState::new("old".into(), None, None);
        let mut ids = SequentialCommandIds::default();
        let id = configure_request(&mut state, &mut ids);
        assert!(!state.editor_editable());
        assert!(
            configure(&mut state, ModelConfiguration::default(), &mut ids)
                .unwrap()
                .iter()
                .all(|effect| !matches!(effect, UiEffect::SendCommand(_)))
        );
        let stale = BackendEvent::ModelCatalogReported {
            command_id: "retired".into(),
            catalog: catalog(),
        };
        assert!(observe(&mut state, &stale, &mut ids).unwrap().is_none());
        let report = BackendEvent::ModelCatalogReported {
            command_id: id.clone(),
            catalog: catalog(),
        };
        observe(&mut state, &report, &mut ids).unwrap();
        assert_eq!(state.provider.as_deref(), Some("old"));
        assert!(
            observe(
                &mut state,
                &finished(&id, "get_model_catalog", true),
                &mut ids
            )
            .unwrap()
            .is_none()
        );
        assert!(state.model_configuration_active());
        let effects = observe(&mut state, &finished(&id, "configure", true), &mut ids)
            .unwrap()
            .unwrap();
        assert!(state.editor_editable());
        assert_eq!(state.provider.as_deref(), Some("alpha"));
        assert_eq!(state.model.as_deref(), Some("alias")); // configured alias, not canonical ID
        assert_eq!(
            state
                .model_catalog
                .as_ref()
                .unwrap()
                .selection
                .effective_model
                .as_deref(),
            Some("one")
        );
        assert!(
            !effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::SendCommand(_)))
        );
        assert!(observe(&mut state, &report, &mut ids).unwrap().is_none());
    }

    #[test]
    fn failed_configuration_preserves_confirmed_selection_and_picker() {
        let mut state = UiState::new("old".into(), None, None);
        let mut ids = SequentialCommandIds::default();
        let id = configure_request(&mut state, &mut ids);
        observe(
            &mut state,
            &BackendEvent::ModelCatalogReported {
                command_id: id.clone(),
                catalog: catalog(),
            },
            &mut ids,
        )
        .unwrap();
        let effects = observe(&mut state, &finished(&id, "configure", false), &mut ids)
            .unwrap()
            .unwrap();
        assert_eq!(state.provider.as_deref(), Some("old"));
        assert!(state.editor_editable());
        assert!(!effects.iter().any(|effect| matches!(
            effect,
            UiEffect::ModelConfigurationApplied
                | UiEffect::ModelCatalogUpdated(_)
                | UiEffect::ModelCatalogUnavailable
        )));
    }

    #[test]
    fn missing_report_releases_input_and_attempts_only_one_recovery_read() {
        let mut state = UiState::new("old".into(), None, None);
        let mut ids = SequentialCommandIds::default();
        let id = configure_request(&mut state, &mut ids);
        let effects = observe(&mut state, &finished(&id, "configure", true), &mut ids)
            .unwrap()
            .unwrap();
        assert!(state.editor_editable());
        assert!(state.model_selection_stale);
        let recovery = request(&effects);
        assert_eq!(recovery["type"], "get_model_catalog");
        let recovery_id = recovery["id"].as_str().unwrap();
        assert!(
            observe(
                &mut state,
                &BackendEvent::ModelCatalogReported {
                    command_id: id,
                    catalog: catalog()
                },
                &mut ids
            )
            .unwrap()
            .is_none()
        );
        let effects = observe(
            &mut state,
            &finished(recovery_id, "get_model_catalog", true),
            &mut ids,
        )
        .unwrap()
        .unwrap();
        assert!(state.model_operation.is_none());
        assert!(state.model_selection_stale);
        assert!(
            !effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::SendCommand(_)))
        );
        assert!(
            effects
                .iter()
                .any(|effect| matches!(effect, UiEffect::ModelCatalogUnavailable))
        );
    }

    #[test]
    fn configuration_invalidation_discards_in_flight_snapshot_before_reloading() {
        let mut state = UiState::new("old".into(), None, None);
        let mut ids = SequentialCommandIds::default();
        let initial = request(&load(&mut state, &mut ids).unwrap());
        let id = initial["id"].as_str().unwrap();
        observe(
            &mut state,
            &BackendEvent::ModelCatalogReported {
                command_id: id.into(),
                catalog: catalog(),
            },
            &mut ids,
        )
        .unwrap();
        assert!(
            !invalidate(&mut state, &mut ids)
                .unwrap()
                .iter()
                .any(|effect| matches!(effect, UiEffect::SendCommand(_)))
        );
        let effects = observe(
            &mut state,
            &finished(id, "get_model_catalog", true),
            &mut ids,
        )
        .unwrap()
        .unwrap();
        let fresh = request(&effects);
        assert_ne!(fresh["id"], initial["id"]);
        assert_eq!(state.provider.as_deref(), Some("old"));
        assert!(state.model_catalog.is_none());
        let id = fresh["id"].as_str().unwrap();
        observe(
            &mut state,
            &BackendEvent::ModelCatalogReported {
                command_id: id.into(),
                catalog: catalog(),
            },
            &mut ids,
        )
        .unwrap();
        observe(
            &mut state,
            &finished(id, "get_model_catalog", true),
            &mut ids,
        )
        .unwrap();
        assert_eq!(state.provider.as_deref(), Some("alpha"));
        assert!(state.model_operation.is_none());
    }
}
