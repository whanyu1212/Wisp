"""Verify a candidate Wisp wheel containing the native Rust TUI."""

from __future__ import annotations

import argparse
import base64
import csv
import email.parser
import hashlib
import json
import re
import tomllib
import zipfile
from pathlib import Path

_SCRIPT_SUFFIX = ".data/scripts/wisp-tui"
_EXTENSION = "wisp/_native.abi3.so"
_CARGO_PRERELEASE = re.compile(
    r"^(?P<release>\d+\.\d+\.\d+)-(?P<kind>alpha|beta|rc)\.(?P<number>\d+)$"
)
_CARGO_TO_PYTHON_PRERELEASE = {"alpha": "a", "beta": "b", "rc": "rc"}
_NATIVE_RELEASE_TARGETS = {
    "manylinux-x86-64": "cp312-abi3-manylinux_2_28_x86_64",
    "macos-arm64": "cp312-abi3-macosx_11_0_arm64",
}


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
    return f"{match['release']}{_CARGO_TO_PYTHON_PRERELEASE[match['kind']]}{match['number']}"


def project_versions(root: Path) -> tuple[str, str, str, str]:
    """Read the project, runtime, and native component versions from source.

    Args:
        root: Repository root.

    Returns:
        Project, runtime, normalized Rust TUI, and normalized Python extension versions.

    Raises:
        ValueError: If the runtime version declaration cannot be parsed.
    """

    project = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))
    cargo = tomllib.loads((root / "rust/wisp-tui/Cargo.toml").read_text(encoding="utf-8"))
    python_cargo = tomllib.loads((root / "rust/wisp-python/Cargo.toml").read_text(encoding="utf-8"))
    runtime_source = (root / "src/wisp/__init__.py").read_text(encoding="utf-8")
    runtime_match = re.search(r'^__version__ = "([^"]+)"$', runtime_source, re.MULTILINE)
    if runtime_match is None:
        raise ValueError("src/wisp/__init__.py has no static __version__ declaration")
    return (
        project["project"]["version"],
        runtime_match.group(1),
        python_version(cargo["package"]["version"]),
        python_version(python_cargo["package"]["version"]),
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
    """Require project, runtime, and native component versions to match.

    Args:
        root: Repository root.

    Returns:
        The shared Python package version.

    Raises:
        ValueError: If any version differs.
    """

    versions = project_versions(root)
    if len(set(versions)) != 1:
        raise ValueError(
            "version mismatch: "
            f"project={versions[0]!r}, runtime={versions[1]!r}, "
            f"rust_tui={versions[2]!r}, rust_python={versions[3]!r}"
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
        if names.count(_EXTENSION) != 1:
            raise ValueError(f"candidate wheel must contain exactly one {_EXTENSION}")
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
            expected_candidate_package = reference_package | {_EXTENSION}
            if candidate_package != expected_candidate_package:
                missing = sorted(expected_candidate_package - candidate_package)
                extra = sorted(candidate_package - expected_candidate_package)
                raise ValueError(
                    f"candidate Python package differs from reference: missing={missing!r}, "
                    f"extra={extra!r}"
                )
            with zipfile.ZipFile(reference_wheel) as reference:
                changed = sorted(
                    name for name in reference_package if archive.read(name) != reference.read(name)
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
            if name not in {script, _EXTENSION}
            and name.lower().endswith((".so", ".dylib", ".dll", ".exe"))
        ]
        if unexpected_native:
            raise ValueError(
                f"candidate wheel contains unexpected native files: {unexpected_native!r}"
            )
        debug_files = [name for name in names if name.lower().endswith((".pdb", ".dsym", ".dwp"))]
        if debug_files:
            raise ValueError(f"candidate wheel contains debug artifacts: {debug_files!r}")


def verify_release_set(distributions: Path, release_assets: Path, *, root: Path) -> None:
    """Verify the complete publishable distribution and native evidence set.

    Args:
        distributions: Directory containing the source distribution and wheels.
        release_assets: Directory containing per-target checksums and SBOMs.
        root: Repository root used for version checks.

    Raises:
        ValueError: If the release set is incomplete, ambiguous, or inconsistent.
    """

    version = verify_versions(root)
    source_distributions = sorted(distributions.glob(f"wisp_ai-{version}.tar.gz"))
    pure_wheels = sorted(distributions.glob(f"wisp_ai-{version}-py3-none-any.whl"))
    if len(source_distributions) != 1 or len(pure_wheels) != 1:
        raise ValueError("release set requires exactly one sdist and one pure fallback wheel")
    pure_wheel = pure_wheels[0]
    expected_wheels = {pure_wheel.name}
    cargo = cargo_version(root)

    for target, tag in _NATIVE_RELEASE_TARGETS.items():
        candidates = sorted(distributions.glob(f"wisp_ai-{version}-{tag}.whl"))
        if len(candidates) != 1:
            raise ValueError(f"release set requires exactly one {target} native wheel")
        wheel = candidates[0]
        expected_wheels.add(wheel.name)
        verify_wheel(wheel, expected_tag=tag, root=root, reference_wheel=pure_wheel)
        _verify_checksum(release_assets / f"wisp-tui-{target}.sha256", wheel)
        _verify_sbom(release_assets / f"wisp-tui-{target}.cdx.json", cargo)
        _verify_sbom(
            release_assets / f"wisp-python-{target}.cdx.json",
            cargo,
            expected_name="wisp-python",
        )
        _verify_install_evidence(
            release_assets / f"wisp-tui-{target}-install.json",
            wheel,
        )

    actual_wheels = {wheel.name for wheel in distributions.glob("*.whl")}
    if actual_wheels != expected_wheels:
        raise ValueError(
            f"release wheel set differs from supported matrix: {sorted(actual_wheels)!r}"
        )


def _verify_checksum(manifest: Path, wheel: Path) -> None:
    line = manifest.read_text(encoding="utf-8").strip()
    parts = line.split()
    if len(parts) != 2 or parts[1].lstrip("*") != wheel.name:
        raise ValueError(f"invalid checksum manifest for {wheel.name}")
    if parts[0] != hashlib.sha256(wheel.read_bytes()).hexdigest():
        raise ValueError(f"checksum mismatch for {wheel.name}")


def _verify_sbom(path: Path, expected_version: str, *, expected_name: str = "wisp-tui") -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    component = document.get("metadata", {}).get("component", {})
    if (
        document.get("bomFormat") != "CycloneDX"
        or document.get("specVersion") != "1.5"
        or component.get("name") != expected_name
        or component.get("version") != expected_version
    ):
        raise ValueError(f"invalid native component SBOM: {path}")


def _verify_install_evidence(path: Path, wheel: Path) -> None:
    document = json.loads(path.read_text(encoding="utf-8"))
    required_positive = (
        "binary_bytes",
        "binary_startup_seconds",
        "extension_bytes",
        "max_rss_bytes",
        "ready_frame_seconds",
        "total_seconds",
    )
    if document.get("wheel_bytes") != wheel.stat().st_size or any(
        not isinstance(document.get(field), (int, float)) or document[field] <= 0
        for field in required_positive
    ):
        raise ValueError(f"invalid installed-wheel evidence: {path}")


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
    parser.add_argument("--release-dir", type=Path)
    parser.add_argument("--release-assets", type=Path)
    parser.add_argument("--root", type=Path, default=Path(__file__).resolve().parents[1])
    arguments = parser.parse_args()
    if arguments.print_cargo_version:
        print(cargo_version(arguments.root))
        return
    if arguments.release_dir is not None or arguments.release_assets is not None:
        if arguments.release_dir is None or arguments.release_assets is None:
            parser.error("--release-dir and --release-assets must be used together")
        verify_release_set(arguments.release_dir, arguments.release_assets, root=arguments.root)
        return
    if arguments.wheel is None or arguments.expected_tag is None:
        parser.error("wheel and --expected-tag are required unless another mode is selected")
    verify_wheel(
        arguments.wheel,
        expected_tag=arguments.expected_tag,
        root=arguments.root,
        reference_wheel=arguments.reference_wheel,
    )


if __name__ == "__main__":
    main()
