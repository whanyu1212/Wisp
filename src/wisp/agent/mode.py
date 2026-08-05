"""Agent operating modes shared by core and frontends."""

from __future__ import annotations

from typing import Literal

type AgentMode = Literal["build", "plan"]

DEFAULT_AGENT_MODE: AgentMode = "build"
PLAN_MODE_SYSTEM_PROMPT = """You are in plan mode. Inspect the project using available read-only
tools.
Do not modify files or execute shell commands. Produce a concrete implementation plan covering
relevant files, implementation steps, tests, risks, and unresolved decisions. Do not claim that
changes were made."""


def is_agent_mode(value: object) -> bool:
    """Return whether *value* is a supported agent mode."""

    return value in {"build", "plan"}
