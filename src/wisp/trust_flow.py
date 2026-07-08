"""Trust resolution: decide (and, when needed, prompt for) project trust.

This sits between the persistent :mod:`wisp.trust` store and the entrypoints. It
answers "is the current project trusted?" — consulting the store first, prompting
on a first run, and defaulting to *untrusted* when no decision exists and no prompt
is possible (non-interactive runs). Untrusted is a safe default: the agent still
runs, it just does not load project-local resources.
"""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from pathlib import Path

from wisp.trust import is_trusted, record_trust

# A prompter shows the trust question and returns the user's yes/no answer. Returns
# None when no answer can be obtained (e.g. a non-interactive stream), which is
# treated as "untrusted for this run" without persisting a decision.
TrustPrompter = Callable[[Path], bool | None]


@dataclass(frozen=True)
class TrustDecision:
    """The resolved trust state for a project directory."""

    project_path: Path
    trusted: bool
    # True when this run's decision came from a fresh prompt (vs. a stored record
    # or a non-interactive default), so callers can log/render it differently.
    newly_decided: bool = False


def resolve_trust(
    project_path: Path,
    *,
    prompter: TrustPrompter | None = None,
    trust_path: Path | None = None,
) -> TrustDecision:
    """Resolve trust for ``project_path``.

    Precedence:

    1. A stored decision (trusted/untrusted) is honored without prompting.
    2. Otherwise, if a ``prompter`` is given and returns an answer, that answer is
       recorded and used.
    3. Otherwise (no record, no prompt, or a prompt that yielded no answer) the
       project is treated as **untrusted** for this run, and nothing is persisted
       — so a later interactive run still gets the first-run prompt.
    """

    stored = is_trusted(project_path, trust_path=trust_path)
    if stored is not None:
        return TrustDecision(project_path=project_path, trusted=stored)

    if prompter is not None:
        answer = prompter(project_path)
        if answer is not None:
            record_trust(project_path, answer, trust_path=trust_path)
            return TrustDecision(project_path=project_path, trusted=answer, newly_decided=True)

    return TrustDecision(project_path=project_path, trusted=False)
