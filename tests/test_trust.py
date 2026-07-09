"""Tests for the project trust store and resolution flow."""

from __future__ import annotations

import json
from pathlib import Path

from pytest import CaptureFixture, MonkeyPatch

from wisp.trust import (
    GLOBAL_TRUST_PATH,
    _default_trust_path,
    forget_trust,
    is_trusted,
    record_trust,
)
from wisp.trust_flow import resolve_trust


def _trust_file(tmp_path: Path) -> Path:
    return tmp_path / "trust.json"


# --- WISP_TRUST_FILE resolution: only an absolute override is honored ---


def test_trust_file_env_absolute_is_honored(tmp_path: Path, monkeypatch: MonkeyPatch) -> None:
    absolute = tmp_path / "custom-trust.json"
    monkeypatch.setenv("WISP_TRUST_FILE", str(absolute))

    # The path is canonicalized (symlinks/.. collapsed) but otherwise honored.
    assert _default_trust_path() == absolute.resolve()


def test_trust_file_env_relative_is_rejected(
    tmp_path: Path, monkeypatch: MonkeyPatch, capsys: CaptureFixture[str]
) -> None:
    # Security: a *relative* WISP_TRUST_FILE would resolve against the current project
    # directory, letting a repo ship its own trust.json and self-trust. It must be
    # ignored (with a warning) in favor of the global store.
    monkeypatch.chdir(tmp_path)
    monkeypatch.setenv("WISP_TRUST_FILE", ".wisp/trust.json")

    resolved = _default_trust_path()

    assert resolved == GLOBAL_TRUST_PATH.expanduser().resolve()
    # It did NOT resolve inside the project directory.
    assert str(tmp_path.resolve()) not in str(resolved)
    assert "relative WISP_TRUST_FILE" in capsys.readouterr().err


def test_trust_file_env_unset_uses_global(monkeypatch: MonkeyPatch) -> None:
    monkeypatch.delenv("WISP_TRUST_FILE", raising=False)

    assert _default_trust_path() == GLOBAL_TRUST_PATH.expanduser().resolve()


def test_project_cannot_self_trust_via_relative_trust_file(
    tmp_path: Path, monkeypatch: MonkeyPatch
) -> None:
    # End-to-end: a malicious repo ships its own .wisp/trust.json marking itself
    # trusted, and a relative WISP_TRUST_FILE tries to make Wisp read it. is_trusted()
    # must NOT honor the project-local file — the relative override is rejected and the
    # (empty, isolated) global store reports the project as undecided.
    project = tmp_path / "evilrepo"
    (project / ".wisp").mkdir(parents=True)
    key = project.expanduser().resolve(strict=False).as_posix()
    (project / ".wisp" / "trust.json").write_text(
        json.dumps({key: {"trusted": True}}), encoding="utf-8"
    )
    monkeypatch.chdir(project)
    monkeypatch.setenv("WISP_TRUST_FILE", ".wisp/trust.json")

    # No trust_path override here: exercise the real _default_trust_path resolution.
    assert is_trusted(project) is None


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


def test_forget_trust_removes_trusted_record(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    record_trust(project, True, trust_path=trust_file)

    assert forget_trust(project, trust_path=trust_file) is True
    assert is_trusted(project, trust_path=trust_file) is None


def test_forget_trust_removes_untrusted_record(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()
    record_trust(project, False, trust_path=trust_file)

    assert forget_trust(project, trust_path=trust_file) is True
    assert is_trusted(project, trust_path=trust_file) is None


def test_forget_trust_without_record_is_idempotent(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    project = tmp_path / "proj"
    project.mkdir()

    assert forget_trust(project, trust_path=trust_file) is False
    assert is_trusted(project, trust_path=trust_file) is None


def test_forget_trust_preserves_other_projects(tmp_path: Path) -> None:
    trust_file = _trust_file(tmp_path)
    a = tmp_path / "a"
    b = tmp_path / "b"
    a.mkdir()
    b.mkdir()
    record_trust(a, True, trust_path=trust_file)
    record_trust(b, False, trust_path=trust_file)

    assert forget_trust(a, trust_path=trust_file) is True

    assert is_trusted(a, trust_path=trust_file) is None
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
