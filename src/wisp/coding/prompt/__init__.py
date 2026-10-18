"""Public prompt-building API; start with builder.build_prompt_messages."""

from .builder import (
    DEFAULT_TOOL_GUIDANCE_MAX_CHARS,
    build_prompt_messages,
    extend_custom_prompt_messages,
)
from .git import GIT_CONTEXT_TIMEOUT_SECONDS
from .instructions import (
    DEFAULT_SYSTEM_PROMPT,
    INSTRUCTION_BOUNDARY_SYSTEM_PROMPT,
    WEB_SEARCH_GUIDANCE,
    WEB_SEARCH_UNAVAILABLE_GUIDANCE,
)
from .project_context import (
    DEFAULT_CONTEXT_FILE_MAX_CHARS,
    DEFAULT_CONTEXT_MAX_CHARS,
    MAX_PROJECT_FILES,
    PROJECT_CONTEXT_HEADER,
    build_project_context,
    build_untrusted_project_context,
)
from .project_root import (
    PROJECT_CONTEXT_FILE_CANDIDATES,
    PROJECT_FILE_CANDIDATES,
    resolve_project_context_root,
)
from .repository_status import (
    MAX_GIT_STATUS_LINES,
    read_repository_status,
)

__all__ = [
    "DEFAULT_TOOL_GUIDANCE_MAX_CHARS",
    "build_prompt_messages",
    "extend_custom_prompt_messages",
    "DEFAULT_SYSTEM_PROMPT",
    "INSTRUCTION_BOUNDARY_SYSTEM_PROMPT",
    "WEB_SEARCH_GUIDANCE",
    "WEB_SEARCH_UNAVAILABLE_GUIDANCE",
    "DEFAULT_CONTEXT_FILE_MAX_CHARS",
    "DEFAULT_CONTEXT_MAX_CHARS",
    "GIT_CONTEXT_TIMEOUT_SECONDS",
    "MAX_GIT_STATUS_LINES",
    "MAX_PROJECT_FILES",
    "PROJECT_CONTEXT_HEADER",
    "PROJECT_CONTEXT_FILE_CANDIDATES",
    "PROJECT_FILE_CANDIDATES",
    "build_project_context",
    "build_untrusted_project_context",
    "read_repository_status",
    "resolve_project_context_root",
]
