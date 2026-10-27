"""Keep the published command and key tables aligned with the Rust frontend."""

import re

from tests.support.paths import DOCS_SRC_DIR, REPO_ROOT

_TUI_SRC = REPO_ROOT / "rust" / "wisp-tui" / "src"
_TUI_DOCS = DOCS_SRC_DIR / "guide" / "tui"


def test_slash_command_table_matches_rust_usage() -> None:
    source = (_TUI_SRC / "commands.rs").read_text(encoding="utf-8")
    usage = source.split("fn usage(", 1)[1].split("pub(crate) fn classify", 1)[0]
    registered = re.findall(r'"\w+" => "(/[^"\n]+)"', usage)
    assert registered, "Rust usage declarations must be found before comparing docs"

    document = (_TUI_DOCS / "commands.md").read_text(encoding="utf-8")
    table = document.split("## Slash commands", 1)[1].split("```text\n", 1)[1].split("```", 1)[0]
    documented = [re.split(r"\s{2,}", line.strip())[0] for line in table.splitlines()]
    assert sorted(documented) == sorted(registered)


def test_key_table_covers_visible_default_bindings() -> None:
    source = (_TUI_SRC / "keybindings.rs").read_text(encoding="utf-8")
    defaults = source.split("impl Default for Bindings", 1)[1].split("impl Bindings", 1)[0]
    # Visible chords carry a display label; hidden compatibility aliases do not.
    labels = re.findall(r'"([^"\n]+)"', defaults)
    assert labels, "Rust default key labels must be found before comparing docs"

    document = (_TUI_DOCS / "keybindings.md").read_text(encoding="utf-8")
    table = document.split("\n## Keybindings\n", 1)[1]
    keys = {
        key
        for line in table.splitlines()
        if line.startswith("| ")
        for key in re.findall(r"`([^`]+)`", line.split("|", 2)[1])
    }
    # The short labels in the live UI use PgUp/PgDn; the guide spells them out.
    aliases = {"PgUp": "PageUp", "PgDn": "PageDown"}
    assert {aliases.get(label, label) for label in labels} <= keys
