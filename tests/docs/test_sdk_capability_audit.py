"""Keep the pinned SDK capability audit reproducible and navigable."""

from __future__ import annotations

import re

import yaml

from tests.support.paths import DOCS_SRC_DIR, REPO_ROOT

_REPOSITORY_ROOT = REPO_ROOT
_AUDIT_PATH = DOCS_SRC_DIR / "architecture" / "sdk-capability-audit.md"
_AUDIT = _AUDIT_PATH.read_text(encoding="utf-8")
_PI_RELEASE = "v0.84.2"
_PI_COMMIT = "914cf1472e715297caa30db4b9535d534a9eb718"
_WISP_BASE = "f98260a4d4cf98a4c8dbb4aaafcf813bb0bae567"
_EXPECTED_CAPABILITIES = (
    "In-process startup",
    "Awaitable command completion and event fan-out",
    "Direct state and settled lifecycle",
    "Typed lifecycle events",
    "Steering, follow-up, cancellation, and compaction",
    "Tool selection and caller-owned composition",
    "Prompt, skill, context, and template overrides",
    "Persistent sessions and tree operations",
    "In-memory sessions and generalized session replacement",
    "Model, authentication, and settings management",
    "Cleanup, health, restart, and recovery",
    "Process-isolated integration",
    "Trust and unsafe-tool approval",
    "Distribution boundary",
    "Guide and executable examples",
)


def test_audit_pins_immutable_pi_and_wisp_references() -> None:
    assert f"[`{_PI_RELEASE}`]" in _AUDIT
    assert f"[`{_PI_COMMIT}`]" in _AUDIT
    assert f"/blob/{_PI_COMMIT}/packages/coding-agent/docs/sdk.md" in _AUDIT
    assert f"/tree/{_PI_COMMIT}/packages/coding-agent/examples/sdk" in _AUDIT
    assert f"[`{_WISP_BASE}`]" in _AUDIT
    assert re.search(r"source, binary, wire, or behavioral\s+compatibility with Pi", _AUDIT)


def test_audit_covers_the_selected_capability_set_once() -> None:
    rows = re.findall(r"^\| ([^|]+?) \| .* \| \*\*(\w+(?: \w+)*)\*\* \|", _AUDIT, re.MULTILINE)

    assert tuple(capability for capability, _disposition in rows) == _EXPECTED_CAPABILITIES
    assert {disposition for _capability, disposition in rows} == {
        "Intentional difference",
        "Open decision",
        "Partial",
        "Planned",
        "Shipped",
    }


def test_every_open_sdk_roadmap_owner_is_linked() -> None:
    for issue_number in (*range(400, 408), 409):
        assert f"https://github.com/whanyu1212/Wisp/issues/{issue_number}" in _AUDIT, issue_number


def test_every_local_audit_link_has_a_markdown_target() -> None:
    local_targets = re.findall(r"\[[^]]+\]\((\.?\.?/[^)]+)\)", _AUDIT)

    assert local_targets
    for target in local_targets:
        relative_path = target.split("#", maxsplit=1)[0]
        resolved = (_AUDIT_PATH.parent / relative_path).with_suffix(".md").resolve()
        assert resolved.is_file(), target


def test_audit_is_linked_as_design_evidence_not_api_reference() -> None:
    guide = (DOCS_SRC_DIR / "guide" / "sdk.md").read_text(encoding="utf-8")
    reference_index = (DOCS_SRC_DIR / "reference" / "index.md").read_text(encoding="utf-8")
    architecture_index = (DOCS_SRC_DIR / "architecture" / "index.md").read_text(encoding="utf-8")
    metadata = yaml.safe_load(_AUDIT.split("---", 2)[1])
    changelog = (_REPOSITORY_ROOT / "CHANGELOG.md").read_text(encoding="utf-8")

    assert "[SDK capability audit](../architecture/sdk-capability-audit.md)" in guide
    assert "sdk-capability-audit" not in reference_index
    assert "[SDK capability audit](sdk-capability-audit.md)" in architecture_index
    assert metadata["section"] == "inside"
    assert metadata["listed"] is True
    assert metadata["route"] == "architecture/sdk-capability-audit.html"
    assert "Historical design evidence" in _AUDIT
    assert f"capability audit pinned to Pi SDK {_PI_RELEASE}" in changelog
