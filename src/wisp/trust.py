"""Project trust boundary.

Wisp records, per project directory, whether the user has trusted it. Trusting a
project is the prerequisite for loading project-local or executable resources
(context files, project extensions) — an untrusted project is still fully usable,
it simply does not have its local configuration ingested.

The trust record is **global**: it lives at ``~/.wisp/trust.json`` and is keyed by
the canonical (resolved, absolute) project path. It is deliberately never read from
the project directory — otherwise an untrusted project could mark *itself* trusted
and defeat the boundary. This mirrors editor "workspace trust" models.

The store degrades gracefully: a missing file means "nothing trusted yet", and a
malformed file is treated the same way (with a warning) rather than crashing
startup. Writes are atomic (temp file + rename) so a crash mid-write cannot corrupt
the registry.
"""

from __future__ import annotations

import json
import os
import sys
from datetime import UTC, datetime
from pathlib import Path

GLOBAL_TRUST_PATH = Path("~/.wisp/trust.json")


def _default_trust_path() -> Path:
    """Return the location of the global trust store.

    Defaults to :data:`GLOBAL_TRUST_PATH` (``~/.wisp/trust.json``). ``WISP_TRUST_FILE``
    relocates it, but only when the override is **absolute**: a *relative* value (e.g.
    ``WISP_TRUST_FILE=.wisp/trust.json``) resolves against the current project
    directory, which would let a repo ship its own ``trust.json`` and mark itself
    trusted — the exact self-trust bypass this boundary exists to prevent. A relative
    override is rejected (with a warning) in favor of the global store.

    The chosen path is canonicalized (symlinks and ``..`` collapsed) so reads and
    writes always agree on one unambiguous location, regardless of how it was spelled.
    An absolute override is otherwise honored as-is: pointing it at a repo-controlled
    location is a deliberate user action, not something the project can trigger.
    """

    candidate = GLOBAL_TRUST_PATH
    if env_path := os.environ.get("WISP_TRUST_FILE"):
        override = Path(env_path)
        if override.is_absolute():
            candidate = override
        else:
            _warn(
                f"ignoring relative WISP_TRUST_FILE={env_path!r}: the trust store must be an "
                "absolute path so a project cannot supply its own; using the global store"
            )
    return candidate.expanduser().resolve(strict=False)


def _canonical_key(project_path: Path) -> str:
    """Return the canonical registry key for a project directory.

    Resolving collapses ``.``/``..`` and dereferences symlinks so that every way
    of naming the same directory maps to a single key.
    """

    return project_path.expanduser().resolve(strict=False).as_posix()


def is_trusted(project_path: Path, *, trust_path: Path | None = None) -> bool | None:
    """Return the recorded trust decision for ``project_path``.

    ``True``/``False`` is an explicit prior decision; ``None`` means undecided (no
    record yet), which callers surface as a first-run prompt.
    """

    records = _load_trust_records(trust_path if trust_path is not None else _default_trust_path())
    entry = records.get(_canonical_key(project_path))
    if not isinstance(entry, dict):
        return None
    trusted = entry.get("trusted")
    return trusted if isinstance(trusted, bool) else None


def record_trust(
    project_path: Path,
    trusted: bool,
    *,
    trust_path: Path | None = None,
) -> None:
    """Persist a trust decision for ``project_path`` to the global registry.

    The store is re-read immediately before writing and only this project's key is
    updated, so a concurrent process that recorded a *different* project's decision
    in the meantime is merged rather than clobbered. Trust decisions are rare,
    user-driven events, so this narrow read-merge-write is sufficient; the residual
    race (two processes deciding the *same* project simultaneously) simply resolves
    to one of the two identical-in-intent writes.
    """

    path = trust_path if trust_path is not None else _default_trust_path()
    records = _load_trust_records(path)
    records[_canonical_key(project_path)] = {
        "trusted": trusted,
        "decided_at": datetime.now(UTC).isoformat(),
    }
    _write_trust_records(path, records)


def _load_trust_records(path: Path) -> dict[str, object]:
    """Load the trust registry, returning an empty mapping on any problem.

    A missing file is normal. A file that exists but cannot be parsed is a
    corruption we surface (stderr warning) and then ignore, so a damaged registry
    never blocks startup — it just means nothing is trusted until re-decided.
    """

    try:
        raw = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as exc:
        _warn(f"could not read trust file {path}: {exc}")
        return {}

    try:
        data = json.loads(raw)
    except json.JSONDecodeError as exc:
        _warn(f"ignoring malformed trust file {path}: {exc}")
        return {}

    if not isinstance(data, dict):
        _warn(f"ignoring trust file {path}: expected a JSON object")
        return {}
    return data


def _write_trust_records(path: Path, records: dict[str, object]) -> None:
    """Atomically write the trust registry (temp file + rename)."""

    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(f"{path.name}.{os.getpid()}.tmp")
    tmp.write_text(json.dumps(records, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    tmp.replace(path)


def _warn(message: str) -> None:
    print(f"wisp: warning: {message}", file=sys.stderr)
