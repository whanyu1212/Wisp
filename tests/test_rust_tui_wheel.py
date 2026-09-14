from __future__ import annotations

import base64
import csv
import hashlib
import zipfile
from pathlib import Path

import pytest

from scripts.verify_rust_tui_wheel import (
    cargo_version,
    python_version,
    verify_versions,
    verify_wheel,
)

_ROOT = Path(__file__).resolve().parents[1]
_VERSION = verify_versions(_ROOT)
_DIST_INFO = f"wisp_ai-{_VERSION}.dist-info"


def _wheel(
    tmp_path: Path,
    *,
    executable: bool = True,
    tag: str = "py3-none-test_platform",
    native: bool = True,
    package_init: bytes | None = None,
) -> Path:
    tmp_path.mkdir(parents=True, exist_ok=True)
    wheel = tmp_path / f"wisp_ai-{_VERSION}-{tag}.whl"
    files = {
        "wisp/__init__.py": (
            f'__version__ = "{_VERSION}"\n'.encode() if package_init is None else package_init
        ),
        "wisp/py.typed": b"",
        f"{_DIST_INFO}/METADATA": (
            f"Metadata-Version: 2.4\nName: wisp-ai\nVersion: {_VERSION}\n".encode()
        ),
        f"{_DIST_INFO}/WHEEL": (
            f"Wheel-Version: 1.0\nRoot-Is-Purelib: {str(not native).lower()}\nTag: {tag}\n".encode()
        ),
    }
    if native:
        files[f"wisp_ai-{_VERSION}.data/scripts/wisp-tui"] = b"binary"
    record = f"{_DIST_INFO}/RECORD"
    rows = []
    for name, content in files.items():
        digest = base64.urlsafe_b64encode(hashlib.sha256(content).digest()).rstrip(b"=").decode()
        rows.append(f"{name},sha256={digest},{len(content)}\n")
    rows.append(f"{record},,\n")
    files[record] = "".join(rows).encode()
    with zipfile.ZipFile(wheel, "w") as archive:
        for name, content in files.items():
            info = zipfile.ZipInfo(name)
            info.external_attr = (
                0o100755 if executable and name.endswith("wisp-tui") else 0o100644
            ) << 16
            archive.writestr(info, content)
    return wheel


def test_cargo_prerelease_conversion_is_exact() -> None:
    assert python_version("0.2.0-rc.1") == "0.2.0rc1"
    assert python_version("1.2.3") == "1.2.3"
    assert python_version("0.2.0-dev.1") == "0.2.0-dev.1"


def test_repository_versions_are_lockstep() -> None:
    assert verify_versions(_ROOT) == python_version(cargo_version(_ROOT))


def test_candidate_wheel_contract(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    wheel = _wheel(tmp_path)
    verify_wheel(wheel, expected_tag="py3-none-test_platform", root=root)

    nonexecutable = _wheel(tmp_path / "nonexec", executable=False)
    with pytest.raises(ValueError, match="not executable"):
        verify_wheel(nonexecutable, expected_tag="py3-none-test_platform", root=root)


def test_candidate_matches_reference_python_package(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    candidate = _wheel(tmp_path / "candidate")
    reference = _wheel(
        tmp_path / "reference",
        tag="py3-none-any",
        native=False,
    )
    verify_wheel(
        candidate,
        expected_tag="py3-none-test_platform",
        root=root,
        reference_wheel=reference,
    )

    with zipfile.ZipFile(reference, "a") as archive:
        archive.writestr("wisp/missing.py", b"")
    with pytest.raises(ValueError, match="differs from reference"):
        verify_wheel(
            candidate,
            expected_tag="py3-none-test_platform",
            root=root,
            reference_wheel=reference,
        )

    changed_reference = _wheel(
        tmp_path / "changed-reference",
        tag="py3-none-any",
        native=False,
        package_init=b"changed",
    )
    with pytest.raises(ValueError, match="content differs from reference"):
        verify_wheel(
            candidate,
            expected_tag="py3-none-test_platform",
            root=root,
            reference_wheel=changed_reference,
        )


def test_record_covers_candidate_files(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    wheel = _wheel(tmp_path)
    rewritten = tmp_path / "broken" / wheel.name
    rewritten.parent.mkdir()
    with zipfile.ZipFile(wheel) as source, zipfile.ZipFile(rewritten, "w") as target:
        for info in source.infolist():
            content = source.read(info.filename)
            if info.filename.endswith("RECORD"):
                rows = list(csv.reader(content.decode().splitlines()))
                content = "\n".join(",".join(row) for row in rows[:-2]).encode()
            target.writestr(info, content)
    with pytest.raises(ValueError, match="missing from RECORD"):
        verify_wheel(rewritten, expected_tag="py3-none-test_platform", root=root)


def test_wheel_fixture_is_deterministic(tmp_path: Path) -> None:
    first = _wheel(tmp_path / "first").read_bytes()
    second = _wheel(tmp_path / "second").read_bytes()
    assert hashlib.sha256(first).digest() == hashlib.sha256(second).digest()
