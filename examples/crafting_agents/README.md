# Crafting Coding Agents checkpoints

Runnable sources for the first three chapters of the [mdBook](../../site/crafting-agents/index.md).
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

The provider replays authored decisions and checks observations. It is not a live
model or an autonomous solver. `model_finished` means the last response had no tool
calls; it does not mean the task succeeded. The file-based checkpoints print the
final source before their temporary directories are removed.

The fixture executor is for these known demonstrations, not arbitrary repositories
or untrusted code. It uses synchronous file operations and a fixed subprocess test
command, with post-capture output limits. Stronger filesystem isolation, bounded
process capture, cancellation, and real-provider integration are later subjects.

The book includes code through mdBook `ANCHOR` regions. Edit these files rather
than creating duplicate snippets. From a development environment:

```bash
uv run pytest tests/test_crafting_agents.py tests/test_crafting_context.py
uv run ruff format --check .
uv run ruff check .
uv run mypy
mdbook build
```

The focused tests run in normal Python CI; the examples are included in configured
strict type checking. The docs workflow also runs all seven checkpoint commands before
building the book.
