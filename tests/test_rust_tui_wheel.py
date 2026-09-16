from __future__ import annotations

import base64
import csv
import hashlib
import json
import zipfile
from pathlib import Path

import pytest

from scripts.verify_rust_tui_wheel import (
    cargo_version,
    python_version,
    verify_release_set,
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
    tag: str = "cp312-abi3-test_platform",
    native: bool = True,
    package_init: bytes | None = None,
    include_extension: bool | None = None,
    extra_package_file: bool = False,
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
    if include_extension is None:
        include_extension = native
    if include_extension:
        files["wisp/_native.abi3.so"] = b"extension"
    if extra_package_file:
        files["wisp/native_helper.py"] = b""
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
    assert python_version("0.2.0-alpha.1") == "0.2.0a1"
    assert python_version("0.2.0-beta.2") == "0.2.0b2"
    assert python_version("0.2.0-rc.3") == "0.2.0rc3"
    assert python_version("1.2.3") == "1.2.3"
    assert python_version("0.2.0-dev.1") == "0.2.0-dev.1"
    assert python_version("0.2.0-a.1") == "0.2.0-a.1"


def test_repository_versions_are_lockstep() -> None:
    assert verify_versions(_ROOT) == python_version(cargo_version(_ROOT))


def test_candidate_wheel_contract(tmp_path: Path) -> None:
    root = Path(__file__).resolve().parents[1]
    wheel = _wheel(tmp_path)
    verify_wheel(wheel, expected_tag="cp312-abi3-test_platform", root=root)

    nonexecutable = _wheel(tmp_path / "nonexec", executable=False)
    with pytest.raises(ValueError, match="not executable"):
        verify_wheel(nonexecutable, expected_tag="cp312-abi3-test_platform", root=root)

    missing_extension = _wheel(tmp_path / "missing-extension", include_extension=False)
    with pytest.raises(ValueError, match="must contain exactly one wisp/_native.abi3.so"):
        verify_wheel(
            missing_extension,
            expected_tag="cp312-abi3-test_platform",
            root=root,
        )


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
        expected_tag="cp312-abi3-test_platform",
        root=root,
        reference_wheel=reference,
    )

    with zipfile.ZipFile(reference, "a") as archive:
        archive.writestr("wisp/missing.py", b"")
    with pytest.raises(ValueError, match="differs from reference"):
        verify_wheel(
            candidate,
            expected_tag="cp312-abi3-test_platform",
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
            expected_tag="cp312-abi3-test_platform",
            root=root,
            reference_wheel=changed_reference,
        )

    extra_candidate = _wheel(tmp_path / "extra-candidate", extra_package_file=True)
    with pytest.raises(ValueError, match="differs from reference"):
        verify_wheel(
            extra_candidate,
            expected_tag="cp312-abi3-test_platform",
            root=root,
            reference_wheel=_wheel(
                tmp_path / "clean-reference",
                tag="py3-none-any",
                native=False,
            ),
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
        verify_wheel(rewritten, expected_tag="cp312-abi3-test_platform", root=root)


def test_release_set_requires_exact_supported_matrix(tmp_path: Path) -> None:
    distributions = tmp_path / "dist"
    assets = tmp_path / "assets"
    distributions.mkdir()
    assets.mkdir()
    (distributions / f"wisp_ai-{_VERSION}.tar.gz").write_bytes(b"sdist")
    pure = _wheel(distributions, tag="py3-none-any", native=False)
    cargo = cargo_version(_ROOT)
    targets = {
        "manylinux-x86-64": "cp312-abi3-manylinux_2_28_x86_64",
        "macos-arm64": "cp312-abi3-macosx_11_0_arm64",
    }
    for target, tag in targets.items():
        wheel = _wheel(distributions, tag=tag)
        digest = hashlib.sha256(wheel.read_bytes()).hexdigest()
        (assets / f"wisp-tui-{target}.sha256").write_text(
            f"{digest}  {wheel.name}\n",
            encoding="utf-8",
        )
        (assets / f"wisp-tui-{target}.cdx.json").write_text(
            '{"bomFormat":"CycloneDX","specVersion":"1.5","metadata":'
            f'{{"component":{{"name":"wisp-tui","version":"{cargo}"}}}}}}',
            encoding="utf-8",
        )
        (assets / f"wisp-python-{target}.cdx.json").write_text(
            '{"bomFormat":"CycloneDX","specVersion":"1.5","metadata":'
            f'{{"component":{{"name":"wisp-python","version":"{cargo}"}}}}}}',
            encoding="utf-8",
        )
        (assets / f"wisp-tui-{target}-install.json").write_text(
            json.dumps(
                {
                    "wheel_bytes": wheel.stat().st_size,
                    "binary_bytes": 1,
                    "binary_startup_seconds": 0.1,
                    "extension_bytes": 1,
                    "max_rss_bytes": 1,
                    "ready_frame_seconds": 0.2,
                    "total_seconds": 0.3,
                }
            ),
            encoding="utf-8",
        )

    verify_release_set(distributions, assets, root=_ROOT)

    extra = distributions / f"wisp_ai-{_VERSION}-cp312-abi3-macosx_11_0_x86_64.whl"
    extra.write_bytes(pure.read_bytes())
    with pytest.raises(ValueError, match="differs from supported matrix"):
        verify_release_set(distributions, assets, root=_ROOT)


def test_release_set_rejects_bad_checksum_and_sbom(tmp_path: Path) -> None:
    distributions = tmp_path / "dist"
    assets = tmp_path / "assets"
    distributions.mkdir()
    assets.mkdir()
    (distributions / f"wisp_ai-{_VERSION}.tar.gz").write_bytes(b"sdist")
    _wheel(distributions, tag="py3-none-any", native=False)
    targets = {
        "manylinux-x86-64": "cp312-abi3-manylinux_2_28_x86_64",
        "macos-arm64": "cp312-abi3-macosx_11_0_arm64",
    }
    for target, tag in targets.items():
        wheel = _wheel(distributions, tag=tag)
        (assets / f"wisp-tui-{target}.sha256").write_text(
            f"{'0' * 64}  {wheel.name}\n",
            encoding="utf-8",
        )
        (assets / f"wisp-tui-{target}.cdx.json").write_text("{}", encoding="utf-8")
        (assets / f"wisp-python-{target}.cdx.json").write_text("{}", encoding="utf-8")
        (assets / f"wisp-tui-{target}-install.json").write_text("{}", encoding="utf-8")

    with pytest.raises(ValueError, match="checksum mismatch"):
        verify_release_set(distributions, assets, root=_ROOT)


def test_wheel_fixture_is_deterministic(tmp_path: Path) -> None:
    first = _wheel(tmp_path / "first").read_bytes()
    second = _wheel(tmp_path / "second").read_bytes()
    assert hashlib.sha256(first).digest() == hashlib.sha256(second).digest()
