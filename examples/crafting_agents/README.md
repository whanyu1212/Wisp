# Crafting Coding Agents checkpoints

Runnable sources for the first four chapters of the [mdBook](../../site/crafting-agents/index.md).
Use Python 3.12+ from the checkout root; no package installation, API key, network,
or third-party dependency is needed:

```bash
python3 -m examples.crafting_agents.checkpoint_01
python3 -m examples.crafting_agents.checkpoint_02
python3 -m examples.crafting_agents.checkpoint_02 --deny-edits
python3 -m examples.crafting_agents.checkpoint_03
python3 -m examples.crafting_agents.checkpoint_03 --untrusted
python3 -m examples.crafting_agents.checkpoint_03 --long-guidance
python3 -m examples.crafting_agents.checkpoint_03 --deny-edits
python3 -m examples.crafting_agents.checkpoint_04
python3 -m examples.crafting_agents.checkpoint_04 --scenario retry
python3 -m examples.crafting_agents.checkpoint_04 --scenario disconnect
python3 -m examples.crafting_agents.checkpoint_04 --scenario malformed
python3 -m examples.crafting_agents.checkpoint_04 --scenario output-limit
```

- `core.py`: message/tool types, the shared sequential loop, and a scripted provider.
- `checkpoint_01.py`: a failed read, corrected read, and diagnosis of an in-memory fixture.
- `checkpoint_02.py`: create a temporary project, reproduce failing tests, read,
  edit, and retest; or deny the edit and retain the original bug.
- `context.py`: ordered instruction blocks, separate character budgets, and
  trust-gated automatic project guidance loading.
- `checkpoint_03.py`: inspect the first request, discover fixture files, read the
  README, and continue the repair. Untrusted, oversized-guidance, and denied-edit
  scenarios show that project text and host permissions are separate inputs.
- `responses.py`: provider-native request translation, streamed previews, terminal
  validation, and bounded opening retries.
- `stream_replay.py`: authored native-shaped event fixtures and observation checks.
- `openai_transport.py`: optional OpenAI SDK transport, with SDK retries disabled.
- `checkpoint_04.py`: offline provider scenarios or an explicit live run.

The default providers replay authored decisions and check observations. They are not
live models or autonomous solvers. `model_finished` means the last response had no tool
calls; it does not mean the task succeeded. The file-based checkpoints print the
final source before their temporary directories are removed.

The fixture executor is for these known demonstrations, not arbitrary repositories
or untrusted code. It uses synchronous file operations and a fixed subprocess test
command, with post-capture output limits. Stronger filesystem isolation, bounded
process capture and interactive cancellation are later subjects. Chapter 4 closes
its provider stream on failure or task cancellation, but does not supervise tool
process trees.

## Optional live provider

From the development checkout, `uv sync --locked` installs the SDK dependency.
Supply `OPENAI_API_KEY` through your environment, then run:

```bash
uv run python -m examples.crafting_agents.checkpoint_04 --live --model gpt-4.1-mini
```

This is a billed API request over the generated fixture; no live request is part
of CI. It defaults to discovery and reading. Adding `--allow-execution` enables
model-selected edits and tests. The test tool runs edited Python with your process's
privileges: the temporary directory is not an OS sandbox.

The adapter intentionally supports text and function calls without reasoning-item
replay. GPT-4.1 mini is the documented non-reasoning example; this is not a generic
adapter for every model exposed by Responses. See the [provider chapter](../../site/crafting-agents/04-providers.md)
for terminal validation, retry limits, and the live-verification boundary.

The book includes code through mdBook `ANCHOR` regions. Edit these files rather
than creating duplicate snippets. From a development environment:

```bash
uv run pytest tests/repository/test_crafting_agents.py tests/repository/test_crafting_context.py tests/repository/test_crafting_providers.py
uv run ruff format --check .
uv run ruff check .
uv run mypy
mdbook build
```

The focused tests run in normal Python CI; the examples are included in configured
strict type checking. The docs workflow also runs all twelve offline commands before
building the book.
