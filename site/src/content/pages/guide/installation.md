---
title: Installation
description: Wisp is published on PyPI as wisp-ai, installs a wisp command, and requires Python 3.12 or newer. Wisp supports Linux and macOS; Windows remains best-effort until it has…
route: guide/installation.html
section: using
order: 10
layout: docs
listed: true
searchable: true
---

# Installation

Wisp is published on PyPI as [`wisp-ai`](https://pypi.org/project/wisp-ai/), installs a `wisp`
command, and requires Python 3.12 or newer. Wisp supports Linux and macOS; Windows remains
best-effort until it has dedicated CI coverage.

Install the latest release:

```bash
uv tool install --python 3.12 wisp-ai
```

If `wisp` is not on your `PATH`, run `uv tool update-shell` once and restart your shell. To install
an exact version, pin it, for example `"wisp-ai==0.2.4"`.

To run Wisp without installing it:

```bash
uvx --python 3.12 --from wisp-ai wisp
```

Check the installed version with `wisp --version`.

Native wheels for macOS arm64 and Linux glibc 2.28+ x86_64 bundle the Rust TUI with the Python
backend in one install. Every other platform, including Intel macOS, gets a pure wheel: print, JSON,
RPC, and SDK use work, but interactive `wisp` and `wisp tui` report how to obtain or build a
matching Rust binary. Source checkouts build the binary separately; see
[Development setup](../contributing/development.md#rust-tui-scaffold). Upgrading from 0.1? Read the
[upgrade guide](upgrading.md) first. For the 0.3 release, see
[Upgrading from Wisp 0.2 to 0.3](upgrading-to-0.3.md), including session rollback guidance.

## The TUI does not start after installing

Wisp publishes native wheels (with the Rust TUI) for macOS arm64 and Linux glibc 2.28+ x86_64, plus
a pure wheel without it. When uv's default Python is older than 3.12, `uv tool install` and `uvx`
can select the pure wheel even on a supported platform: uv resolves for its default interpreter
first, where the native wheels don't apply, and keeps that choice after switching to a Python that
meets Wisp's minimum. `wisp --version` and print, JSON, RPC, and SDK use still work, but `wisp-tui`
is missing and interactive `wisp` reports that no Rust binary is available.

Name the Python version when installing:

```bash
uv tool install --force --python 3.12 wisp-ai
wisp-tui --version
```

`wisp update` pins the running Python version for the same reason.

## Updates

```bash
wisp update --check   # bypass the cache and check immediately
wisp update           # check and confirm installation
wisp update --yes     # check and install without confirmation
```

Automatic installation is available only when Wisp is running from a persistent `uv tool`
installation. `uvx`, local-source, and other package-manager installs are never replaced.

Run `wisp update --check` or `wisp update` outside the TUI; `/update` displays those instructions.

## Next steps

- [Quickstart](quickstart.md) — connect a provider and run your first prompt.
- [Python SDK](sdk.md) — embed Wisp and consume typed events.
- [Providers & auth](providers.md) — credentials, custom endpoints, and the model catalog.
