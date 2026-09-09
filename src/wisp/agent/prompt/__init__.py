"""Public prompt-building API; start with builder.build_prompt_messages."""

from .builder import (
    DEFAULT_TOOL_GUIDANCE_MAX_CHARS,
    build_prompt_messages,
)
from .instructions import (
    DEFAULT_SYSTEM_PROMPT,
    INSTRUCTION_BOUNDARY_SYSTEM_PROMPT,
)
from .project_context import (
    DEFAULT_CONTEXT_FILE_MAX_CHARS,
    DEFAULT_CONTEXT_MAX_CHARS,
    GIT_CONTEXT_TIMEOUT_SECONDS,
    MAX_GIT_STATUS_LINES,
    MAX_PROJECT_FILES,
    PROJECT_CONTEXT_FILE_CANDIDATES,
    PROJECT_FILE_CANDIDATES,
    build_project_context,
    build_untrusted_project_context,
    resolve_project_context_root,
)

__all__ = [
    "DEFAULT_TOOL_GUIDANCE_MAX_CHARS",
    "build_prompt_messages",
    "DEFAULT_SYSTEM_PROMPT",
    "INSTRUCTION_BOUNDARY_SYSTEM_PROMPT",
    "DEFAULT_CONTEXT_FILE_MAX_CHARS",
    "DEFAULT_CONTEXT_MAX_CHARS",
    "GIT_CONTEXT_TIMEOUT_SECONDS",
    "MAX_GIT_STATUS_LINES",
    "MAX_PROJECT_FILES",
    "PROJECT_CONTEXT_FILE_CANDIDATES",
    "PROJECT_FILE_CANDIDATES",
    "build_project_context",
    "build_untrusted_project_context",
    "resolve_project_context_root",
]
