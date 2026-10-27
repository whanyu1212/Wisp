"""Keep compatibility policy and schema history synchronized with runtime contracts."""

from __future__ import annotations

import re
import tomllib
from typing import get_args

import pytest
import yaml

from tests.support.paths import DOCS_SRC_DIR, REPO_ROOT
from wisp import __version__
from wisp.events import WispEvent
from wisp.messages import CompactionRecord
from wisp.sessions import (
    PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION,
    SESSION_ENTRY_SCHEMA_VERSION,
)

_REPOSITORY_ROOT = REPO_ROOT
_CHANGELOG = (_REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
_POLICY = (DOCS_SRC_DIR / "reference" / "compatibility.md").read_text(encoding="utf-8")


def test_runtime_and_project_package_versions_match() -> None:
    project = tomllib.loads((_REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == __version__
    lock = tomllib.loads((_REPOSITORY_ROOT / "uv.lock").read_text(encoding="utf-8"))
    locked = next(package for package in lock["package"] if package["name"] == "wisp-ai")
    assert locked["version"] == __version__


def test_changelog_covers_every_historical_event_schema() -> None:
    """Per-event schemas v2–v39 stay documented; protocol v9 replaced the counter."""

    documented = {
        int(version)
        for version in re.findall(r"^## Schema v(\d+)(?:\s|$)", _CHANGELOG, flags=re.MULTILINE)
    }

    assert documented == set(range(2, 40))
    assert "schema v1" in _CHANGELOG
    assert "there was no merged schema v1" in _CHANGELOG
    assert not re.findall(r"^## Schema v(\d+) — current$", _CHANGELOG, flags=re.MULTILINE)
    assert not re.findall(r"— current$", _CHANGELOG, flags=re.MULTILINE)


def test_rpc_message_snapshot_history_distinguishes_calls_and_results() -> None:
    schema_v22 = _CHANGELOG.split("## Schema v22", maxsplit=1)[1].split(
        "## Schema v21", maxsplit=1
    )[0]
    schema_v17 = _CHANGELOG.split("## Schema v17", maxsplit=1)[1].split(
        "## Schema v16", maxsplit=1
    )[0]

    assert "tool-result" in schema_v22
    assert "tool-call" not in schema_v22
    assert "`tool_calls`" in schema_v17


@pytest.mark.parametrize(
    ("version", "entries"),
    [
        pytest.param(24, ("`output_has_exit_status`",), id="v24-wire-addition"),
        pytest.param(5, ("optional `effort`", "`project.config.applied`"), id="v5-wire-additions"),
        pytest.param(
            4,
            tuple(
                f"`{field}`"
                for field in ("exit_code", "before_text", "created", "summary", "truncated")
            ),
            id="v4-wire-additions",
        ),
        pytest.param(
            32, ("`context_observation`", "`message.completed`"), id="v32-context-observations"
        ),
        pytest.param(
            31, ("`utf8_bytes_div_4_v2`", "`ContextEstimate.method`"), id="v31-unicode-estimator"
        ),
    ],
)
def test_changelog_schema_sections_record_changes(version: int, entries: tuple[str, ...]) -> None:
    section = _CHANGELOG.split(f"## Schema v{version}", maxsplit=1)[1].split(
        f"## Schema v{version - 1}", maxsplit=1
    )[0]

    for entry in entries:
        assert entry in section


def test_changelog_records_the_documented_public_deprecation() -> None:
    assert "`wisp.agent.messages.SessionEntry(...)` as deprecated" in _CHANGELOG
    assert "`MessageSessionEntry`, `EventSessionEntry`, or `CompactionSessionEntry`" in _CHANGELOG
    assert "emits `DeprecationWarning`" in _CHANGELOG


def test_events_carry_no_per_event_version_and_policy_says_so() -> None:
    assert "schema_version" not in WispEvent.model_fields
    assert "carry no per-event version" in _POLICY
    assert "must be the same Wisp release" in _POLICY


def test_policy_documents_current_persistence_versions() -> None:
    compaction_versions = get_args(CompactionRecord.model_fields["schema_version"].annotation)

    assert f"| Session entry | v{SESSION_ENTRY_SCHEMA_VERSION} |" in _POLICY
    assert f"| Persisted event envelope | v{PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION} |" in _POLICY
    assert "| Event payload inside the envelope | unversioned (current event shape) |" in _POLICY
    assert f"| Compaction record | v{max(compaction_versions)} |" in _POLICY


def test_compatibility_reference_is_linked_from_sdk_and_navigation() -> None:
    sdk = (DOCS_SRC_DIR / "reference" / "sdk.md").read_text(encoding="utf-8")
    reference_index = (DOCS_SRC_DIR / "reference" / "index.md").read_text(encoding="utf-8")
    sessions = (DOCS_SRC_DIR / "guide" / "sessions.md").read_text(encoding="utf-8")
    metadata = yaml.safe_load(_POLICY.split("---", 2)[1])

    assert "[Compatibility & versioning](compatibility.md)" in sdk
    assert "being documented separately as part of" not in sdk
    assert "[Compatibility & versioning](compatibility.md)" in reference_index
    assert "[Compatibility & versioning](../reference/compatibility.md)" in sessions
    assert metadata["listed"] is True
    assert metadata["section"] == "reference"
