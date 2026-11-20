//! Catalog reads use the existing serialized session-operation lifecycle.

use super::*;

pub(super) fn load(
    state: &mut UiState,
    ids: &mut impl CommandIdSource,
    query: &str,
    cursor: Option<&str>,
    read_only: bool,
) -> Result<Vec<UiEffect>, ReduceError> {
    if let Some(effects) = begin_session_operation(state)? {
        return Ok(effects);
    }
    let id = ids.next_id(CommandKind::GetSessions);
    let command = if read_only {
        WispTypedClientRpcCommands::search_saved_sessions(&id, query, cursor)?
    } else {
        WispTypedClientRpcCommands::search_sessions(&id, query, cursor)?
    };
    state.input_ready = false;
    state.session_operation = Some(SessionOperation::LoadingCatalog {
        command_id: id.clone(),
        navigation: CatalogNavigation::default(),
        sessions: None,
        selected_session_id: None,
        completion: None,
    });
    Ok(vec![
        UiEffect::SessionCatalogStarted(id),
        UiEffect::SendCommand(command),
        UiEffect::RequestRender,
    ])
}
