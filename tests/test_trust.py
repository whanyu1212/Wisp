"""Tests for the project trust store and resolution flow."""

from __future__ import annotations

import json
from pathlib import Path

from pytest import CaptureFixture

from wisp.trust import is_trusted, record_trust
from wisp.trust_flow import resolve_trust


def _trust_file(tmp_path: Path) -> Path:
    return tmp_path / "trust.json"


# --- store: is_trusted / record_trust ---


def test_unknown_project_is_undecided(tmp_path: Path) -> None:
    assert is_trusted(tmp_path / "proj", trust_path=_trust_file(tmp_path)) is None


def test_record_and_read_back_trusted(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    record_trust(project, True, trust_path=trust_file)

    assert is_trusted(project, trust_path=trust_file) is True


def test_record_and_read_back_untrusted(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    record_trust(project, False, trust_path=trust_file)

    assert is_trusted(project, trust_path=trust_file) is False


def test_record_overwrites_prior_decision(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    record_trust(project, True, trust_path=trust_file)
    record_trust(project, False, trust_path=trust_file)

    assert is_trusted(project, trust_path=trust_file) is False


def test_path_variants_map_to_one_key(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    record_trust(project, True, trust_path=trust_file)

    # "./" form and a trailing-dot form resolve to the same canonical key.
    assert is_trusted(project / ".", trust_path=trust_file) is True
    assert is_trusted(Path(str(project) + "/"), trust_path=trust_file) is True


def test_symlinked_project_shares_decision(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    real = tmp_path / "real"
    real.mkdir()
    link = tmp_path / "link"
    link.symlink_to(real)

    record_trust(real, True, trust_path=trust_file)

    assert is_trusted(link, trust_path=trust_file) is True


def test_malformed_trust_file_is_ignored(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    trust_file = _trust_file(tmp_path)
    trust_file.write_text("{not json", encoding="utf-8")

    assert is_trusted(tmp_path / "proj", trust_path=trust_file) is None
    assert "malformed trust file" in capsys.readouterr().err


def test_non_object_trust_file_is_ignored(tmp_path: Path, capsys: CaptureFixture[str]) -> None:
    trust_file = _trust_file(tmp_path)
    trust_file.write_text("[1, 2, 3]", encoding="utf-8")

    assert is_trusted(tmp_path / "proj", trust_path=trust_file) is None
    assert "expected a JSON object" in capsys.readouterr().err


def test_record_creates_parent_directory(tmp_path: Path) -> None:
    trust_file = tmp_path / "nested" / "dir" / "trust.json"
    project = tmp_path / "proj"
    project.mkdir()

    record_trust(project, True, trust_path=trust_file)

    assert trust_file.is_file()
    stored = json.loads(trust_file.read_text(encoding="utf-8"))
    assert len(stored) == 1
    (entry,) = stored.values()
    assert entry["trusted"] is True
    assert "decided_at" in entry


def test_records_persist_across_multiple_projects(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()

    record_trust(a, True, trust_path=trust_file)
    record_trust(b, False, trust_path=trust_file)

    assert is_trusted(a, trust_path=trust_file) is True
    assert is_trusted(b, trust_path=trust_file) is False


# --- resolve_trust decision matrix ---


def test_resolve_honors_stored_decision_without_prompting(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    record_trust(project, True, trust_path=trust_file)

    def _fail_prompter(_path: Path) -> bool | None:
        raise AssertionError("prompter must not run when a decision is stored")

    decision = resolve_trust(project, prompter=_fail_prompter, trust_path=trust_file)

    assert decision.trusted is True
    assert decision.newly_decided is False


def test_resolve_prompts_and_records_when_undecided(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    decision = resolve_trust(project, prompter=lambda _p: True, trust_path=trust_file)

    assert decision.trusted is True
    assert decision.newly_decided is True
    # The fresh decision is persisted for next time.
    assert is_trusted(project, trust_path=trust_file) is True


def test_resolve_defaults_untrusted_without_prompter(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    decision = resolve_trust(project, prompter=None, trust_path=trust_file)

    assert decision.trusted is False
    # A non-interactive default is NOT persisted, so a later run still prompts.
    assert is_trusted(project, trust_path=trust_file) is None


def test_resolve_defaults_untrusted_when_prompter_declines_to_answer(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    decision = resolve_trust(project, prompter=lambda _p: None, trust_path=trust_file)

    assert decision.trusted is False
    assert is_trusted(project, trust_path=trust_file) is None
