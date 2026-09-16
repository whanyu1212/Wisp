"""Keep the RC2 renderer decision synchronized across architecture and user docs."""

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


def test_architecture_records_rc2_trial_and_maintained_textual_fallback() -> None:
    assert "RC2 Rust-default trial" in _ARCHITECTURE
    assert "#470" in _ARCHITECTURE
    assert "supersedes the default hold" in _ARCHITECTURE
    for document in (_ARCHITECTURE, _ARCHITECTURE_INDEX):
        assert "maintained Python" in document
        assert "Pure/source installs" in document
        assert "macOS arm64/x86_64 and Linux glibc 2.28+ x86_64" in document


def test_architecture_describes_a_frontend_over_the_python_runtime() -> None:
    assert "diagnostic transport scaffold" not in _ARCHITECTURE
    assert "does not accept prompts" not in _ARCHITECTURE
    assert "prompts" in _ARCHITECTURE
    assert "approvals" in _ARCHITECTURE
    assert "Python decides what is allowed" in _ARCHITECTURE


def test_cli_and_environment_docs_explain_automatic_selection() -> None:
    for document in (_CLI, _ENVIRONMENT):
        assert "transport scaffold" not in document
        assert "`auto`" in document
        assert "`WISP_TUI_RENDERER`" in document
        assert "Explicit CLI selection takes precedence" in document


def test_docs_preserve_explicit_textual_recovery_without_silent_fallback() -> None:
    for document in (_ARCHITECTURE, _TUI_GUIDE, _CLI, _ENVIRONMENT, _ARCHITECTURE_INDEX):
        normalized = " ".join(document.split())
        assert "they never silently switch frontends" in normalized
        assert "wisp tui --renderer textual" in normalized


def test_rc2_decision_keeps_publication_and_stable_promotion_separate() -> None:
    release = (_REPOSITORY_ROOT / "site/contributing/rc2-release.md").read_text(encoding="utf-8")
    architecture = " ".join(_ARCHITECTURE.split())
    assert "this candidate" in architecture
    assert "stable promotion require separate decisions" in architecture
    assert "does not create a" in release
    assert "Before stable promotion" in release
    assert "postpublication acceptance" in release
