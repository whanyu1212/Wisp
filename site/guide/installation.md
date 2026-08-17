---
title: Installation
---

# Installation

Wisp is published on PyPI as [`wisp-ai`](https://pypi.org/project/wisp-ai/), installs a `wisp`
command, and requires Python 3.12 or newer. The `0.1.0rc1` release candidate supports Linux and
macOS; Windows remains best-effort until it has dedicated CI coverage.

Request the prerelease explicitly:

```bash
uv tool install "wisp-ai==0.1.0rc1"
```

If `wisp` is not on your `PATH`, run `uv tool update-shell` once and restart your shell.

To run Wisp without installing it:

```bash
uvx --from "wisp-ai==0.1.0rc1" wisp
```

Check the installed version with `wisp --version`.

## Updates

Installed builds check PyPI at most once every six hours after TUI startup. When a newer applicable
release is available, Wisp prints a non-blocking update command; it never installs updates
automatically.

```bash
wisp update --check   # bypass the cache and check immediately
wisp update           # check and confirm installation
```

Set `WISP_UPDATE_CHECK=0` to disable automatic checks; explicit checks still run.

Automatic installation is available only when Wisp is running from a persistent `uv tool`
installation. `uvx`, local-source, and other package-manager installs are never replaced.

## Next steps

- [Quickstart](./quickstart) — connect a provider and run your first prompt.
- [Providers & auth](./providers) — credentials, custom endpoints, and the model catalog.
