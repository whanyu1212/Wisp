# Tool-use evals

These evals measure how real models use Wisp's built-in tools on small synthetic tasks. They
compare Wisp *setups* with the model held fixed, for example every MCP tool shown versus on-demand
discovery, or an edit-recovery behavior on versus off. They are not a model leaderboard. Design and
decisions: [HY-443](https://linear.app/queryverse/issue/HY-443).

The suite is run by hand or on a schedule, never in CI: it calls paid models. CI only tests the
runner itself (`tests/evals/`), with the `fake` provider.

> [!WARNING]
> Runs execute real shell commands on this machine. Each run gets a fresh temporary folder and a
> private `HOME`, but that confines files, not commands. Tasks are synthetic and benign, and each
> exposes only the tools it needs.

## Run

```bash
uv run python -m evals.tool_use run --models openai-codex::gpt-6.1-sol,openai::gpt-6.1-sol \
  --reps 3 --budget-usd 10 --budget-tokens 2000000
uv run python -m evals.tool_use report evals/tool_use/results/<timestamp>
```

`run` options:

| Option | Meaning |
|---|---|
| `--models` | Comma-separated `provider::model` list |
| `--reps` | Repetitions of each task and model (default 3) |
| `--tasks`, `--tags` | Run only these task ids, or tasks with any of these tags |
| `--workers` | Runs in flight at once (default 2) |
| `--budget-usd` | Stop starting rounds once priced runs (OpenAI API) cost this much |
| `--budget-tokens` | Stop starting rounds once unpriced runs (Codex subscription) used this many tokens |
| `--env-file`, `--auth-file` | Credential sources (default: the repo's `.env` and `~/.wisp/auth.json`) |
| `--results` | Output folder (default: a new timestamped folder under `results/`) |

Runs happen in rounds: one repetition of every task and model per round. The budget is checked
before each round, so a stopped suite has the same number of runs for every pairing.

## Credentials

The runner follows Wisp's own resolution:

- It always passes `WISP_AUTH_FILE`. Codex signs in with OAuth, which lives only in `auth.json`.
- For the OpenAI API it passes `OPENAI_API_KEY` from the runner's environment or, failing that,
  from the git-ignored `.env`. Wisp prefers that variable over a key stored in `auth.json`.

Wisp itself does not read `.env` files. The runner reads only an allowlist of provider key names
from it, because a `.env` may also set `WISP_*` variables that would silently change the setup
under test; for the same reason every run starts without the runner's own `WISP_*` variables.
Each result row records whether the credential came from the `environment` or was `stored`, never
the value. The runner stops before any run if a requested provider has no credential.

## What a run does

1. Copies the task's `repo/` fixture into a temporary folder, commits it with `git`, and runs the
   task's `setup`, if any.
2. Runs the checkout's own `wisp` (next to the `uv run` interpreter, not one on `PATH`):
   `wisp -p PROMPT --mode json --provider P --model M --yes --timeout T --max-tool-iterations N`
   with only the task's tools allowed, a private `HOME` and `WISP_TRUST=1`.
3. Runs the task's `verify` command in the folder and checks the run's events.
4. Appends one row to `runs.jsonl`. The run's `events.jsonl`, `stderr.txt` and session are kept
   next to it under `results/<task>/<provider>__<model>/rep<n>/`.

A row's `status` is:

| Status | Meaning |
|---|---|
| `passed` | `verify` exited 0, every check held, and the run finished before its deadline |
| `failed` | The run finished or hit its deadline (`timed_out`), and `verify` or a check failed |
| `errored` | The run says nothing about the task: Wisp crashed, could not sign in, ended on a provider error, or setup failed |

Reports leave errored runs out of pass rates and count them on their own, so a rate limit does not
read as a task failure. Rows also record tool calls by name, tool errors by `failure_code`,
repeated `bash` commands, whether `read_output` was used, tokens (including cache reads), the known
cost, provider retries, the setup hashes from `agent.started`, the Wisp version, and the commit.

## Report

`report` prints, per task and model, passes, pass^1, pass^k, errored and timed-out runs, mean
tokens and known cost; and per model, the mean pass^1 across tasks with a 95% bootstrap interval
over tasks. pass^k (from tau-bench) is the chance that k runs of a task all pass, so it shows
reliability that a plain pass rate hides.

It also warns when one task and model's runs offered different tools (`tools_sha256`, from
`agent.started`), for example because an MCP server failed to connect in some runs. Compare
results only when there are no warnings. The instructions hash is recorded but not compared: it
covers project context, which names each run's own temporary folder.

With about a dozen tasks and three repetitions, only large differences stand out from noise. Read
small differences as no difference.

## Add a task

Create `tasks/<id>/` with a `repo/` fixture and a `task.toml`:

```toml
id = "fix-failing-test"          # must match the folder name
title = "One line saying what the task asks for"
tags = ["general"]               # general, long-output, edit, mcp, multi-read
prompt = "What the user types"
setup = "optional command run after the fixture is committed"
verify = "{python} -m unittest -q"   # exit 0 means passed
timeout_seconds = 300
max_tool_iterations = 30
tools = ["read", "edit", "write", "bash", "grep", "find", "ls"]

[checks]                         # optional; a passing run must also meet these
no_rerun = "make build"          # a bash command containing this may run only once
uses_tool = "read_output"
max_tool_errors = 2
```

Commands run with `bash -c` in the run's folder. `{python}` becomes the runner's interpreter and
`{task}` the task's own folder, so a verifier script can live beside `task.toml`, out of the
model's reach. Keep fixtures standard-library only so runs need no installs, and make `verify`
reject plausible wrong fixes, not only accept the right one.
