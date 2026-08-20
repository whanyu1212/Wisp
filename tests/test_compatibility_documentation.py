"""Keep compatibility policy and schema history synchronized with runtime contracts."""

from __future__ import annotations

import json
import re
import tomllib
from pathlib import Path
from typing import get_args

from wisp import __version__
from wisp.agent.messages import CompactionRecord
from wisp.events import (
    EVENT_SCHEMA_VERSION,
    AgentStarted,
    WispEvent,
    wisp_event_from_json,
)
from wisp.sessions import (
    PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION,
    SESSION_ENTRY_SCHEMA_VERSION,
)

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_CHANGELOG = (_REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")
_POLICY = (_REPOSITORY_ROOT / "site" / "reference" / "compatibility.md").read_text(encoding="utf-8")


def test_runtime_and_project_package_versions_match() -> None:
    project = tomllib.loads((_REPOSITORY_ROOT / "pyproject.toml").read_text(encoding="utf-8"))

    assert project["project"]["version"] == __version__


def test_changelog_covers_every_explicit_event_schema() -> None:
    documented = {
        int(version)
        for version in re.findall(r"^## Schema v(\d+)(?:\s|$)", _CHANGELOG, flags=re.MULTILINE)
    }

    assert documented == set(range(2, EVENT_SCHEMA_VERSION + 1))
    assert "schema v1" in _CHANGELOG
    assert "there was no merged schema v1" in _CHANGELOG
    assert re.findall(r"^## Schema v(\d+) — current$", _CHANGELOG, flags=re.MULTILINE) == [
        str(EVENT_SCHEMA_VERSION)
    ]


def test_same_version_wire_additions_are_recorded() -> None:
    schema_v24 = _CHANGELOG.split("## Schema v24", maxsplit=1)[1].split(
        "## Schema v23", maxsplit=1
    )[0]
    schema_v5 = _CHANGELOG.split("## Schema v5", maxsplit=1)[1].split("## Schema v4", maxsplit=1)[0]

    assert "`output_has_exit_status`" in schema_v24
    assert "optional `effort`" in schema_v5
    assert "`project.config.applied`" in schema_v5


def test_schema_v32_records_message_context_observations() -> None:
    schema_v32 = _CHANGELOG.split("## Schema v32", maxsplit=1)[1].split(
        "## Schema v31", maxsplit=1
    )[0]

    assert "`context_observation`" in schema_v32
    assert "`message.completed`" in schema_v32


def test_schema_v31_records_the_unicode_context_estimator() -> None:
    schema_v31 = _CHANGELOG.split("## Schema v31", maxsplit=1)[1].split(
        "## Schema v30", maxsplit=1
    )[0]

    assert "`utf8_bytes_div_4_v2`" in schema_v31
    assert "`ContextEstimate.method`" in schema_v31


def test_changelog_records_the_documented_public_deprecation() -> None:
    assert "`wisp.agent.messages.SessionEntry(...)` as deprecated" in _CHANGELOG
    assert "`MessageSessionEntry`, `EventSessionEntry`, or `CompactionSessionEntry`" in _CHANGELOG
    assert "emits `DeprecationWarning`" in _CHANGELOG


def test_documented_readable_event_range_matches_runtime() -> None:
    readable_versions = get_args(WispEvent.model_fields["schema_version"].annotation)
    minimum = min(readable_versions)
    maximum = max(readable_versions)

    assert readable_versions == tuple(range(minimum, maximum + 1))
    assert maximum == EVENT_SCHEMA_VERSION
    assert f"Events at schema v{minimum} through v{maximum} remain readable." in _CHANGELOG
    assert f"read **v{minimum} through v{maximum}**" in _POLICY
    assert f"currently **v{EVENT_SCHEMA_VERSION}**" in _POLICY
    assert "not a complete historical-conformance checker" in _POLICY


def test_every_readable_event_version_round_trips_through_json() -> None:
    readable_versions = get_args(WispEvent.model_fields["schema_version"].annotation)

    for schema_version in readable_versions:
        event = AgentStarted.model_validate(
            {"session_id": "test-session", "schema_version": schema_version}
        )
        encoded = event.model_dump_json()
        decoded = wisp_event_from_json(encoded)

        assert json.loads(encoded)["schema_version"] == schema_version
        assert decoded == event


def test_policy_documents_current_persistence_versions() -> None:
    compaction_versions = get_args(CompactionRecord.model_fields["schema_version"].annotation)

    assert f"| Session entry | v{SESSION_ENTRY_SCHEMA_VERSION} |" in _POLICY
    assert f"| Persisted event envelope | v{PERSISTED_EVENT_ENVELOPE_SCHEMA_VERSION} |" in _POLICY
    assert f"| Event payload inside the envelope | v{EVENT_SCHEMA_VERSION} |" in _POLICY
    assert f"| Compaction record | v{max(compaction_versions)} |" in _POLICY


def test_compatibility_reference_is_linked_from_sdk_and_navigation() -> None:
    sdk = (_REPOSITORY_ROOT / "site" / "reference" / "sdk.md").read_text(encoding="utf-8")
    reference_index = (_REPOSITORY_ROOT / "site" / "reference" / "index.md").read_text(
        encoding="utf-8"
    )
    sessions = (_REPOSITORY_ROOT / "site" / "guide" / "sessions.md").read_text(encoding="utf-8")
    vitepress = (_REPOSITORY_ROOT / "site" / ".vitepress" / "config.ts").read_text(encoding="utf-8")

    assert "[Compatibility & versioning](./compatibility)" in sdk
    assert "being documented separately as part of" not in sdk
    assert "[Compatibility & versioning](./compatibility)" in reference_index
    assert "[Compatibility & versioning](../reference/compatibility)" in sessions
    assert "{ text: 'Compatibility & versioning', link: '/reference/compatibility' }" in vitepress
