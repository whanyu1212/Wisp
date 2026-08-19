---
title: Installation
---

# Installation

Wisp is published on PyPI as [`wisp-ai`](https://pypi.org/project/wisp-ai/), installs a `wisp`
command, and requires Python 3.12 or newer. The `0.1.0rc3` release candidate supports Linux and
macOS; Windows remains best-effort until it has dedicated CI coverage.

Request the prerelease explicitly:

```bash
uv tool install "wisp-ai==0.1.0rc3"
```

If `wisp` is not on your `PATH`, run `uv tool update-shell` once and restart your shell.

To run Wisp without installing it:

```bash
uvx --from "wisp-ai==0.1.0rc3" wisp
```

Check the installed version with `wisp --version`.

## Updates

Installed builds check PyPI at most once every six hours after TUI startup. When a newer applicable
release is available, the check never blocks startup:

- The Textual TUI waits until the session is safely idle and the composer is empty, then offers
  **Update & restart** (the default), **Later**, or **Skip version**.
- **Update & restart** is available for persistent `uv tool` installations. After installation,
  Wisp relaunches the exact original command with its original working directory and environment.
- Line/fullscreen renderers and installations managed by another package manager receive a passive
  notice instead of an install prompt.
- **Skip version** suppresses only that exact release; a newer compatible release is offered again.

```bash
wisp update --check   # bypass the cache and check immediately
wisp update           # check and confirm installation
wisp update --yes     # check and install without confirmation
```

Set `WISP_UPDATE_CHECK=0` to disable background checks; explicit checks still run and ignore a
skipped-version preference.

Automatic installation is available only when Wisp is running from a persistent `uv tool`
installation. `uvx`, local-source, and other package-manager installs are never replaced.

## Next steps

- [Quickstart](./quickstart) — connect a provider and run your first prompt.
- [Providers & auth](./providers) — credentials, custom endpoints, and the model catalog.
