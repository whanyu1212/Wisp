# Installation

Wisp is published on PyPI as [`wisp-ai`](https://pypi.org/project/wisp-ai/), installs a `wisp`
command, and requires Python 3.12 or newer. Wisp 0.1.0 supports Linux and macOS; Windows remains
best-effort until it has dedicated CI coverage.

Install the stable release:

```bash
uv tool install "wisp-ai==0.1.0"
```

If `wisp` is not on your `PATH`, run `uv tool update-shell` once and restart your shell.

To run Wisp without installing it:

```bash
uvx --from "wisp-ai==0.1.0" wisp
```

Check the installed version with `wisp --version`.

The RC3 candidate has native wheels for macOS arm64 and Linux glibc 2.28+ x86_64. A compatible wheel
bundles the Rust TUI and Python backend in one install. RC3 removes the
Python TUI: pure-wheel installs retain print, JSON, RPC, and SDK use, but interactive `wisp` /
`wisp tui` reports how to obtain or build a matching Rust binary. Source checkouts build the binary
separately; see
[Development setup](../contributing/development#rust-tui-scaffold). Check the
[0.2 upgrade guide](./upgrading) and
[release page](https://github.com/whanyu1212/Wisp/releases) for the behavior and availability of a
specific prerelease. The commands above continue to install the published stable release.

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

- [Quickstart](./quickstart) — connect a provider and run your first prompt.
- [Python SDK](./sdk) — embed Wisp and consume typed events.
- [Providers & auth](./providers) — credentials, custom endpoints, and the model catalog.
