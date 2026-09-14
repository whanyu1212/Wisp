"""Verify a candidate Wisp wheel containing the native Rust TUI."""

from __future__ import annotations

import argparse
import base64
import csv
import email.parser
import hashlib
import re
import tomllib
import zipfile
from pathlib import Path

_SCRIPT_SUFFIX = ".data/scripts/wisp-tui"
_CARGO_PRERELEASE = re.compile(r"^(?P<release>\d+\.\d+\.\d+)-(?P<kind>a|b|rc)\.(?P<number>\d+)$")


def python_version(cargo_version: str) -> str:
    """Convert Wisp's Cargo prerelease spelling to its exact Python spelling.

    Args:
        cargo_version: Version from the Rust TUI crate manifest.

    Returns:
        The equivalent PEP 440 spelling when Wisp's supported pattern matches.
    """

    match = _CARGO_PRERELEASE.fullmatch(cargo_version)
    if match is None:
        return cargo_version
    return f"{match['release']}{match['kind']}{match['number']}"


def project_versions(root: Path) -> tuple[str, str, str]:
    """Read the project, runtime, and Rust TUI versions from source files.

    Args:
        root: Repository root.

    Returns:
        Project, runtime, and normalized Rust TUI versions.

    Raises:
        ValueError: If the runtime version declaration cannot be parsed.
    """

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    cargo = tomllib.loads((root / "rust/wisp-tui/Cargo.toml").read_text(encoding="utf-8"))
    runtime_source = (root / "src/wisp/__init__.py").read_text(encoding="utf-8")
    runtime_match = re.search(r'^__version__ = "([^"]+)"$', runtime_source, re.MULTILINE)
    if runtime_match is None:
        raise ValueError("src/wisp/__init__.py has no static __version__ declaration")
    return (
        project["project"]["version"],
        runtime_match.group(1),
        python_version(cargo["package"]["version"]),
    )


def cargo_version(root: Path) -> str:
    """Read the exact Cargo version embedded in the Rust TUI binary.

    Args:
        root: Repository root.

    Returns:
        The Rust TUI crate version.
    """

    cargo = tomllib.loads((root / "rust/wisp-tui/Cargo.toml").read_text(encoding="utf-8"))
    return cargo["package"]["version"]


def verify_versions(root: Path) -> str:
    """Require project, runtime, and Rust TUI versions to match exactly.

    Args:
        root: Repository root.

    Returns:
        The shared Python package version.

    Raises:
        ValueError: If the three versions differ.
    """

    versions = project_versions(root)
    if len(set(versions)) != 1:
        raise ValueError(
            "version mismatch: "
            f"project={versions[0]!r}, runtime={versions[1]!r}, rust={versions[2]!r}"
        )
    return versions[0]


def verify_wheel(
    wheel: Path,
    *,
    expected_tag: str,
    root: Path,
    reference_wheel: Path | None = None,
) -> None:
    """Validate one candidate native wheel without executing its contents.

    Args:
        wheel: Candidate wheel path.
        expected_tag: Exact Python-ABI-platform tag expected in metadata and filename.
        root: Repository root used for lockstep version checks.
        reference_wheel: Optional pure-Python wheel whose package files must match.

    Raises:
        ValueError: If wheel contents, metadata, permissions, or records are invalid.
    """

    version = verify_versions(root)
    if not wheel.name.endswith(f"-{expected_tag}.whl"):
        raise ValueError(f"wheel filename does not end with expected tag {expected_tag!r}")

    with zipfile.ZipFile(wheel) as archive:
        names = archive.namelist()
        scripts = [name for name in names if name.endswith(_SCRIPT_SUFFIX)]
        if len(scripts) != 1:
            raise ValueError(f"expected one wisp-tui shared script, found {scripts!r}")
        script = scripts[0]
        if archive.getinfo(script).external_attr >> 16 & 0o111 == 0:
            raise ValueError("packaged wisp-tui is not executable")
        if "wisp/__init__.py" not in names or "wisp/py.typed" not in names:
            raise ValueError("candidate wheel is missing the Python package or py.typed")
        if reference_wheel is not None:
            with zipfile.ZipFile(reference_wheel) as reference:
                reference_names = reference.namelist()
                reference_wheel_name = _one(reference_names, ".dist-info/WHEEL")
                reference_metadata = email.parser.BytesParser().parsebytes(
                    reference.read(reference_wheel_name)
                )
                if (
                    reference_metadata["Root-Is-Purelib"] != "true"
                    or reference_metadata["Tag"] != "py3-none-any"
                ):
                    raise ValueError("reference wheel must remain the current pure-Python wheel")
            candidate_package = {
                info.filename
                for info in archive.infolist()
                if info.filename.startswith("wisp/") and not info.is_dir()
            }
            with zipfile.ZipFile(reference_wheel) as reference:
                reference_package = {
                    info.filename
                    for info in reference.infolist()
                    if info.filename.startswith("wisp/") and not info.is_dir()
                }
            if candidate_package != reference_package:
                missing = sorted(reference_package - candidate_package)
                extra = sorted(candidate_package - reference_package)
                raise ValueError(
                    f"candidate Python package differs from reference: missing={missing!r}, "
                    f"extra={extra!r}"
                )
            with zipfile.ZipFile(reference_wheel) as reference:
                changed = sorted(
                    name for name in candidate_package if archive.read(name) != reference.read(name)
                )
            if changed:
                raise ValueError(
                    f"candidate Python package content differs from reference: {changed!r}"
                )

        metadata_name = _one(names, ".dist-info/METADATA")
        wheel_name = _one(names, ".dist-info/WHEEL")
        record_name = _one(names, ".dist-info/RECORD")
        metadata = email.parser.BytesParser().parsebytes(archive.read(metadata_name))
        if metadata["Name"] != "wisp-ai" or metadata["Version"] != version:
            raise ValueError("candidate distribution name or version is not lockstep")

        wheel_metadata = email.parser.BytesParser().parsebytes(archive.read(wheel_name))
        if wheel_metadata["Root-Is-Purelib"] != "false":
            raise ValueError("candidate wheel must set Root-Is-Purelib: false")
        if wheel_metadata["Tag"] != expected_tag:
            raise ValueError("candidate wheel metadata tag is incorrect")

        record_rows = {
            row[0]: row[1:]
            for row in csv.reader(archive.read(record_name).decode("utf-8").splitlines())
        }
        if set(names) - record_rows.keys():
            raise ValueError("candidate wheel contains files missing from RECORD")
        for name in names:
            if name == record_name:
                continue
            digest, size = record_rows[name]
            content = archive.read(name)
            expected_digest = "sha256=" + base64.urlsafe_b64encode(
                hashlib.sha256(content).digest()
            ).rstrip(b"=").decode("ascii")
            if digest != expected_digest or size != str(len(content)):
                raise ValueError(f"candidate wheel has an invalid RECORD entry for {name}")
        unexpected_native = [
            name
            for name in names
            if name != script and name.lower().endswith((".so", ".dylib", ".dll", ".exe"))
        ]
        if unexpected_native:
            raise ValueError(
                f"candidate wheel contains unexpected native files: {unexpected_native!r}"
            )
        debug_files = [name for name in names if name.lower().endswith((".pdb", ".dsym", ".dwp"))]
        if debug_files:
            raise ValueError(f"candidate wheel contains debug artifacts: {debug_files!r}")


def _one(names: list[str], suffix: str) -> str:
    matches = [name for name in names if name.endswith(suffix)]
    if len(matches) != 1:
        raise ValueError(f"expected one {suffix}, found {matches!r}")
    return matches[0]


def main() -> None:
    """Run candidate wheel verification from the command line."""

    parser = argparse.ArgumentParser()
    parser.add_argument("wheel", type=Path, nargs="?")
    parser.add_argument("--expected-tag")
    parser.add_argument("--print-cargo-version", action="store_true")
    parser.add_argument("--reference-wheel", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args()
    if arguments.print_cargo_version:
        print(cargo_version(arguments.root))
        return
    if arguments.wheel is None or arguments.expected_tag is None:
        parser.error("wheel and --expected-tag are required unless --print-cargo-version is used")
    verify_wheel(
        arguments.wheel,
        expected_tag=arguments.expected_tag,
        root=arguments.root,
        reference_wheel=arguments.reference_wheel,
    )


if __name__ == "__main__":
    main()
