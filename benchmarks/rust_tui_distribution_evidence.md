# Rust TUI candidate wheel evidence (#469)

**Date:** 2026-09-14
**POC:** Hanyu Wu
**TL;DR:** Wisp can reproducibly build non-published, platform-tagged `wisp-ai` wheel candidates containing the lockstep Rust TUI. Publication, launcher discovery, lifecycle testing, and support promotion remain separate work.

## Packaging decision

The candidate uses one platform-specific `wisp-ai` wheel containing the existing Python package and a `wisp-tui` executable in the wheel scripts scheme. This keeps the Python runtime and native frontend on one distribution version and avoids a second package's resolution, upgrade, uninstall, and rollback states.

The ordinary PEP 517 and release path remains `uv_build`. Candidate wheels are built explicitly with pinned Hatchling because `uv_build` supports only pure-Python packages and Maturin rejects a binary project that also defines Wisp's existing Python console script. A small build hook runs a locked release Cargo build, strips symbols, marks the wheel non-pure, applies an explicit CI-owned platform tag, and includes the executable as a shared script.

Rejected alternatives:

- a companion native distribution, because it introduces cross-package lockstep and rollback;
- a runtime downloader or installer, because it conflicts with offline use and verified-artifact policy;
- hand-editing or retagging an existing wheel, because that would create custom `RECORD`, permission, and wheel-tag machinery;
- changing the production build backend before candidate artifacts and lifecycle behavior are proven.

## Candidate matrix

| Target | Candidate tag | Runner/build policy |
|---|---|---|
| Linux x86_64 glibc | `py3-none-manylinux_2_28_x86_64` | Pinned PyPA manylinux 2.28 x86_64 container |
| macOS x86_64 | `py3-none-macosx_11_0_x86_64` | GitHub-hosted Intel runner, deployment target 11.0 |
| macOS arm64 | `py3-none-macosx_11_0_arm64` | GitHub-hosted arm64 runner, deployment target 11.0 |

Linux arm64, musllinux/Alpine, and Windows are not claimed by this candidate matrix.

## Verification contract

Each target job:

1. builds the current pure-Python `uv_build` wheel as the package-file reference;
2. builds the Hatchling candidate from locked Cargo dependencies;
3. verifies exact project/runtime/Cargo version lockstep;
4. verifies non-pure metadata, exact platform tag, one executable shared script, executable mode, complete `RECORD`, no debug artifacts, and Python package file parity;
5. runs `twine check` and writes a SHA-256 manifest;
6. emits a CycloneDX 1.5 Rust binary SBOM;
7. installs the candidate into an isolated environment;
8. verifies the installed SDK, `wisp` command, native binary architecture and binary version;
9. proves version mismatch fails before backend spawn with Cargo removed from the consumer `PATH`.

Artifacts are uploaded for inspection but are not published by this workflow.

## Local proof

On macOS arm64, the candidate built as `wisp_ai-0.2.0rc1-py3-none-macosx_11_0_arm64.whl`. The wheel set `Root-Is-Purelib: false`, retained executable mode for `wisp-tui`, matched every Python package file in the current `uv_build` wheel, passed `twine check`, installed into an isolated environment, passed the installed SDK verifier, ran the installed `wisp` command, reported `wisp-tui 0.2.0-rc.1`, and rejected expected backend version `9.9.9` before spawning `/usr/bin/false`.

Two builds with the same source and `SOURCE_DATE_EPOCH` produced identical SHA-256 hashes. The generated CycloneDX 1.5 SBOM contained 185 components.

A local Docker run of the pinned manylinux 2.28 x86_64 image also built the candidate, passed package parity and full `RECORD` validation, reported exact `manylinux_2_28_x86_64` compatibility through `auditwheel show`, passed `twine check`, installed with Cargo absent from the consumer `PATH`, and ran the stripped x86-64 ELF binary. These local results do not substitute for terminal CI on every matrix target.

## Remaining work

This evidence does not make Rust a supported or default renderer. #469 still owns production release integration, environment-owned binary discovery, signed/attested publication, fake-provider installed-wheel smoke tests, upgrade/downgrade/rollback/uninstall, corruption and offline behavior, and size/startup/process-memory reporting.
