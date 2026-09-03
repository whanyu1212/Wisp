"""Keep the #470 renderer decision synchronized across architecture and user docs."""

from __future__ import annotations

from pathlib import Path

_REPOSITORY_ROOT = Path(__file__).resolve().parents[1]
_ARCHITECTURE = (_REPOSITORY_ROOT / "site" / "architecture" / "rust-tui-boundary.md").read_text(
    encoding="utf-8"
)
_ARCHITECTURE_INDEX = (_REPOSITORY_ROOT / "site" / "architecture" / "index.md").read_text(
    encoding="utf-8"
)
_TUI_GUIDE = (_REPOSITORY_ROOT / "site" / "guide" / "tui.md").read_text(encoding="utf-8")
_CLI = (_REPOSITORY_ROOT / "site" / "reference" / "cli.md").read_text(encoding="utf-8")
_ENVIRONMENT = (_REPOSITORY_ROOT / "site" / "reference" / "environment.md").read_text(
    encoding="utf-8"
)


def test_architecture_records_textual_default_and_rust_experimental_opt_in() -> None:
    assert "Textual remains the default and the supported product frontend." in _ARCHITECTURE
    assert "Rust is an experimental opt-in" in _ARCHITECTURE
    closed = "[#470](https://github.com/whanyu1212/Wisp/issues/470) is the closed renderer decision"
    assert closed in _ARCHITECTURE
    assert "Textual remains the default and supported frontend." in _ARCHITECTURE_INDEX
    assert "Rust is an experimental" in _ARCHITECTURE_INDEX


def test_architecture_does_not_describe_current_rust_tui_as_promptless_scaffold() -> None:
    assert "diagnostic transport scaffold" not in _ARCHITECTURE
    assert "does not accept prompts" not in _ARCHITECTURE
    assert "The current experimental frontend accepts" in _ARCHITECTURE
    assert "prompts, approvals, trust answers" in _ARCHITECTURE


def test_cli_and_environment_docs_do_not_call_rust_a_transport_scaffold() -> None:
    assert "transport-diagnostic scaffold" not in _CLI
    assert "transport scaffold" not in _CLI
    assert "transport scaffold" not in _ENVIRONMENT
    assert "experimental Rust frontend" in _CLI
    assert "experimental macOS/Linux Rust" in _ENVIRONMENT


def test_docs_record_that_rust_selection_does_not_fall_back_to_textual() -> None:
    assert "does not fall back to Textual" in _ARCHITECTURE
    assert "never falls back to Textual" in _TUI_GUIDE
    assert "never falls back to Textual" in _CLI
    assert "never falls back to Textual" in _ENVIRONMENT


def test_docs_name_stage_three_blockers_and_closed_decision() -> None:
    for document in (_ARCHITECTURE, _TUI_GUIDE, _CLI, _ENVIRONMENT, _ARCHITECTURE_INDEX):
        assert "#470" in document
        assert "#467" in document
        assert "#468" in document
        assert "#469" in document
        assert "stage 3" in document.lower() or "stage-3" in document
