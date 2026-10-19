# How Wisp is built

Wisp ships as one Python distribution, `wisp-ai`. Each release publishes four files: a source
distribution, a pure-Python wheel, and two native wheels that add the Rust TUI and a small Rust
extension module. Two build backends produce them on purpose: `uv_build` for the pure files and
Hatchling, with a custom hook, for the native wheels. This page explains why, what each file
contains, and which workflow builds it.

## What a release contains

| File | Built by | Contains |
| -- | -- | -- |
| `wisp_ai-<version>.tar.gz` | `uv build --no-sources` (`uv_build`) | Python source, `pyproject.toml`, README and LICENSE; no Rust |
| `…-py3-none-any.whl` | `uv build --no-sources` (`uv_build`) | The `wisp` package only |
| `…-cp312-abi3-manylinux_2_28_x86_64.whl` | Hatchling + `packaging/hatch_build.py` | The same Python files, plus the `wisp-tui` executable and `wisp/_native.abi3.so` |
| `…-cp312-abi3-macosx_11_0_arm64.whl` | Hatchling + `packaging/hatch_build.py` | The same, for macOS 11+ on Apple silicon |

The native pieces come from two crates in `rust/`:

- **`wisp-tui`** is the terminal UI. The wheel installs it in the environment's scripts directory,
  and the launcher finds it through the distribution's own file list, never by searching `PATH`.
- **`wisp-python`** builds `wisp._native`, which provides the native literal grep and the
  pending-text buffer for managed process output. It uses the stable ABI (`abi3`), so one wheel
  per platform covers Python 3.12 and later.

Installers choose a native wheel on a matching platform and the pure wheel everywhere else. A pure
install keeps print, JSON, RPC and SDK use. Interactive `wisp` stops with a message naming the
supported platforms. Grep and process-output retention fall back to their Python implementations,
which the conformance fixtures hold to the same results.

## Why two build backends

`uv_build` is the `[build-system]` in `pyproject.toml`, so `uv sync`, `uv build` and every install
from the source distribution use it. It only builds pure-Python packages, so a source checkout or
an sdist install never contains `wisp-tui` or `wisp._native` (see
[Development setup](development.md) for using a locally built binary).

A native wheel has to run locked Cargo builds, add an executable to the wheel's scripts, add the
extension module, mark the wheel as platform-specific and apply an exact platform tag. Hatchling
does this with a short build hook. The alternatives considered when this was designed:

- **Maturin** rejects a binary project that also defines Wisp's Python console script.
- **A companion native package** would add a second distribution to keep in lockstep through
  installs, upgrades and rollbacks.
- **A runtime downloader** would conflict with offline use and verified artifacts.

The original decision record is
[`benchmarks/evidence/rust_tui_distribution_evidence.md`](https://github.com/whanyu1212/Wisp/blob/main/benchmarks/evidence/rust_tui_distribution_evidence.md).
It predates the extension module and the current two-target matrix.

## The build hook

`packaging/hatch_build.py` runs only for Hatchling's standard wheel target. It:

1. requires `WISP_RUST_TUI_WHEEL_TAG` and fails without it, so a build never guesses its
   platform;
2. runs `cargo rustc --release --locked` for the `wisp-tui` binary and the `wisp-python` library,
   stripping symbols (it honors `CARGO_TARGET_DIR`);
3. marks the wheel non-pure, applies the tag, adds the binary as the `wisp-tui` script and places
   the extension at `wisp/_native.abi3.so`.

To reproduce a candidate wheel locally, use the exact tag for your platform from
`rust-tui-wheels.yml`:

```bash
WISP_RUST_TUI_WHEEL_TAG=cp312-abi3-macosx_11_0_arm64 \
  uvx --from hatchling==1.27.0 hatchling build -t wheel -d candidate-dist
```

The tag is CI-owned packaging metadata, not runtime configuration. Never relabel an artifact built
for another platform.

## Which workflow builds what

- **`ci.yml`, "Build and verify package"** runs `uv build --no-sources` on every pull request, then
  checks the metadata with `twine` and runs the SDK verifier and an example against the installed
  pure wheel.
- **`rust-tui-wheels.yml`** builds each native target next to a pure reference wheel. It then:
  - checks the candidate with `scripts/verify_rust_tui_wheel.py`: tag, non-pure metadata, one
    executable script, a complete `RECORD`, no debug artifacts, and Python files identical to the
    reference;
  - writes SHA-256 sums and CycloneDX SBOMs;
  - installs the wheel in a clean environment without Cargo, and runs the native conformance tests,
    the SDK verifier and a version-mismatch check against it;
  - checks the platform: `auditwheel` on Linux; on macOS, the minimum OS, linkage and signing;
  - runs the install, replace and uninstall lifecycle script.

  It runs on every push to `main`, and on pull requests that change Rust crates, `packaging/`,
  `pyproject.toml`, the wheel scripts, or the Python modules that load native code.
- **`release.yml`** runs for `v*` tags. It repeats the quality gates, calls `rust-tui-wheels.yml`,
  builds the sdist and pure wheel, and verifies all four distributions together. It then attests
  their provenance, publishes to PyPI with trusted publishing, and creates the GitHub release. The
  release also carries per-target checksums, SBOMs, install evidence and the live RPC schema
  bundle.

## Version lockstep

The Python package, `wisp-tui` and `wisp-python` always carry the same version, which lives in
`pyproject.toml`, `src/wisp/__init__.py`, the two crates' `Cargo.toml`, `Cargo.lock` and `uv.lock`.
Release preparation bumps them together. A final release such as `0.2.0` is spelled the same
everywhere; for a prerelease only the spelling differs, `0.2.0rc13` in the Python files and
`0.2.0-rc.13` in the Cargo files. `tests/repository/test_tui_version.py` and `scripts/verify_rust_tui_wheel.py` fail on
any drift, and at runtime the TUI refuses a backend whose version differs. The internal crates
`wisp-protocol`, `wisp-search` and `wisp-process-text` are never published and stay at `0.1.0`.
