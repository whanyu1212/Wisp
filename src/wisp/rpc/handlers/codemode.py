"""Codemode setting inspection and changes for the RPC frontend."""

from __future__ import annotations

from wisp.coding.session import CodingSession, codemode_available
from wisp.config.runtime import codemode_from_env
from wisp.config.settings import try_persist_user_codemode
from wisp.events import CodemodeSetting, CodemodeState, ErrorEvent, RpcCodemodeReported
from wisp.rpc.commands import GetCodemodeCommand, SetCodemodeCommand
from wisp.rpc.lifecycle import RpcCommandLifecycle, RpcEventWriter

# The session and the settings file hold the setting as an optional boolean.
_CODEMODE_FOR_SETTING: dict[CodemodeSetting, bool | None] = {
    "automatic": None,
    "always": True,
    "never": False,
}
_SETTING_FOR_CODEMODE: dict[bool | None, CodemodeSetting] = {
    codemode: setting for setting, codemode in _CODEMODE_FOR_SETTING.items()
}


def handle_rpc_codemode_command(
    command: GetCodemodeCommand | SetCodemodeCommand,
    *,
    agent: CodingSession,
    write_event: RpcEventWriter,
) -> None:
    """Report the codemode setting, first applying and saving a new one.

    A new setting applies to the session's next run and is saved as the user
    setting. A failed save leaves it applied for this session and reports an error
    before the snapshot.

    Args:
        command (GetCodemodeCommand | SetCodemodeCommand): The inspection or change.
        agent (CodingSession): The session whose setting is read or changed.
        write_event (RpcEventWriter): Writes the snapshot and lifecycle events.
    """

    lifecycle = RpcCommandLifecycle.for_command(command, write_event=write_event)
    if isinstance(command, SetCodemodeCommand):
        agent.codemode = _CODEMODE_FOR_SETTING[command.setting]
        if not try_persist_user_codemode(agent.codemode):
            write_event(
                ErrorEvent(message="Codemode setting applied for this session; could not save it.")
            )
    write_event(RpcCodemodeReported(command_id=lifecycle.command_id, codemode=_snapshot(agent)))
    lifecycle.finish()


def _snapshot(agent: CodingSession) -> CodemodeState:
    env_codemode = codemode_from_env()
    return CodemodeState(
        setting=_SETTING_FOR_CODEMODE[agent.codemode],
        available=codemode_available(),
        offered=agent.codemode_offered(),
        env_setting=None if env_codemode is None else _SETTING_FOR_CODEMODE[env_codemode],
    )
