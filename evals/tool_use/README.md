# Tool-use evals

These evals measure how real models use Wisp's built-in tools on small synthetic tasks. They
compare Wisp *setups* with the model held fixed, for example every MCP tool shown versus on-demand
discovery, or an edit-recovery behavior on versus off. They are not a model leaderboard. Design and
decisions: [HY-443](https://linear.app/queryverse/issue/HY-443).

The suite is run by hand or on a schedule, never in CI: it calls paid models. CI tests the runner
itself (`tests/evals/`) with the `fake` provider, and runs the scripted layer,
`tests/evals/test_scripted_sequences.py`: fixed tool-call sequences on Wisp's real tools that
pin the recovery paths these tasks exercise (reading cut output with `read_output`, retrying an
ambiguous edit, finding and calling an MCP tool).

> [!WARNING]
> Runs execute real shell commands on this machine. Each run gets a fresh temporary folder, a
> private `HOME`, an allowlisted environment, and its own process group that is killed when the
> run ends, but that confines files and leftovers, not commands. Tasks are synthetic and benign,
> and each exposes only the tools it needs.

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
| `--mcp-tool-exposure` | `direct` (default) sends every MCP tool's definition; `on_demand` gives the model `mcp_search`, `mcp_describe` and `mcp_call` instead; `auto`, Wisp's own default, picks one by catalog size |

Runs happen in rounds: one repetition of every task and model per round. The budget is checked
before each round, so a stopped suite has the same number of runs for every pairing.

## Credentials

The runner follows Wisp's own resolution:

- It always passes `WISP_AUTH_FILE`. Codex signs in with OAuth, which lives only in `auth.json`.
- For the OpenAI API it passes `OPENAI_API_KEY` from the runner's environment or, failing that,
  from the git-ignored `.env`. Wisp prefers that variable over a key stored in `auth.json`.

Wisp itself does not read `.env` files. The runner reads only an allowlist of provider key names
from it, because a `.env` may also set `WISP_*` variables that would silently change the setup
under test. For the same reason, and to keep other services' tokens out of the model's commands,
a run inherits only a short allowlist of the runner's variables (`PATH`, locale, temp folders,
proxies and certificate paths); setup and verify commands get the same environment without any
credentials.
Each result row records whether the credential came from the `environment` or was `stored`, never
the value. The runner stops before any run if a requested provider has no credential.

## What a run does

1. Copies the task's `repo/` fixture into a temporary folder, commits it with `git`, and runs the
   task's `setup`, if any.
2. Runs the checkout's own `wisp` (next to the `uv run` interpreter, not one on `PATH`):
   `wisp -p PROMPT --mode json --provider P --model M --yes --timeout T --max-tool-iterations N`
   with only the task's tools allowed, a private `HOME` and `WISP_TRUST=1`.
3. Runs the task's `verify` command in the folder and checks the run's events.
4. Appends one row to `runs.jsonl`. The run's `events.jsonl`, `stderr.txt`, `verify.txt` (and
   `setup.txt`) and session are kept under `results/<task>/<provider>__<model>/rep<n>/`.

Each command runs in its own process group, which is killed when the command ends or overruns, so
a stuck build or a background process the model started does not outlive the run.

A row's `status` is:

| Status | Meaning |
|---|---|
| `passed` | `verify` exited 0, every check held, and the run finished before its deadline |
| `failed` | `verify` or a check failed; or the run hit its deadline (`timed_out`), its tool-iteration limit, or its context window |
| `errored` | The run says nothing about the task: Wisp crashed or exited badly, could not sign in, ended on a provider error, setup failed, or an MCP server did not start |

Reports leave errored runs out of pass rates and count them on their own, so a rate limit does not
read as a task failure. Rows also record tool calls by name, tool errors by `failure_code`,
repeated `bash` commands, whether `read_output` was used, tokens (including cache reads), the known
cost, provider retries, the setup hashes from `agent.started`, the Wisp version, and the commit.
A proxied `mcp_call` counts under the MCP tool it ran, so both exposure modes count calls the same
way; rows also record the exposure, the number of proxied calls, and how many of them were
rejected for their arguments.

## Report

Current models pass most tasks, so `report` leads with how much work a run took: per task and
model, mean tool calls, mean tool errors and mean tokens, then passes, pass^1, pass^k, errored and
timed-out runs and known cost. Per model it prints mean calls and tokens across tasks, and the mean
pass^1 with a 95% bootstrap interval over tasks. Compare calls and tokens only over the same
tasks. pass^k (from tau-bench) is the chance that k runs of a task all pass, so it shows
reliability that a plain pass rate hides.

It also warns when one task and model's runs offered different tools (`tools_sha256`, from
`agent.started`), for example because an MCP server failed to connect in some runs. Compare
results only when there are no warnings. The instructions hash is recorded but not compared: it
covers project context, which names each run's own temporary folder.

With about a dozen tasks and three repetitions, only large differences stand out from noise. Read
small differences as no difference.

## Compare and baselines

```bash
uv run python -m evals.tool_use compare evals/tool_use/baselines/<date>-current-tools.json \
  evals/tool_use/results/<timestamp>
uv run python -m evals.tool_use baseline evals/tool_use/results/<timestamp> --label current-tools
```

`compare BEFORE AFTER` pairs runs by task and model (each side a results folder, its
`runs.jsonl`, or a baseline file) and prints, per pair, passes before and after and the change in
mean tool calls, tool errors and tokens, with a 95% Welch t-interval for each change. A change
whose interval excludes zero is marked `*`. A t-interval rather than a bootstrap, because a
bootstrap over two or three runs is far too narrow; the t distribution widens the interval for
the few runs it rests on. A side needs at least two scored runs for an interval; with fewer the
cell shows `—`. Errored runs are left out of the metrics and counted in the passes column.
`(tools differ)` after a task says the two sides offered different tools, which is expected when
the tools are what changed.

Each interval is 95% on its own, so across one comparison's many cells about one in twenty is
marked by chance; the output says how many to expect. Trust a change that is marked on several
tasks or models, or that holds when rerun with more repetitions, not a single `*`. The noise floor
line gives, for calls and tokens, the median interval half-width as a share of the earlier mean:
changes smaller than that are within noise. With three runs a side it is about ±25%, so this suite
detects large effects only; use more repetitions for smaller ones.

`baseline` saves a results folder as `baselines/<date>-<label>.json`: the suite's settings and,
for each run, its task, model, status, tool calls and errors, tokens, cost and setup hash. It
leaves out folders, error text and credential sources, takes only a finished results folder (one
with `suite.json`), and never overwrites an existing baseline. Baselines are
committed so later changes to Wisp's tools are compared against the same numbers. Record one on
the commit the change starts from, with the models the change will be measured on.

## Recorded results

| Baseline | What it measures |
|---|---|
| `2026-11-08-current-tools.json` | All 12 tasks before the `bash` head-and-tail preview (#921) |
| `2026-11-08-bash-preview.json` | All 12 tasks after it; compare tool changes against this |
| `2026-11-08-mcp-direct.json`, `2026-11-08-mcp-on-demand.json` | The MCP tasks, 5 runs each, with every MCP tool's definition sent and with on-demand discovery |
| `2026-11-09-mcp-sweep-direct.json`, `2026-11-09-mcp-sweep-on-demand.json` | The `mcp-sweep` tasks, 5 runs each in each mode: the inventory question with 6, 12, 20, 30 and 50 tools |

On-demand discovery cut tokens per run by 76–77% on `mcp-triage` (102 tools) and 28–46% on
`mcp-pick-tool` (50 tools), and cost 20–34% more on `mcp-similar-servers` (6 tools), with every run
passing in both modes. On the one-question sweep it broke even at about 20 small tools, which placed
the `auto` threshold; see [the tools guide](../../site/src/content/pages/guide/tools-and-safety.md#what-on-demand-discovery-saves).

## Add a task

Create `tasks/<id>/` with a `repo/` fixture, a `solution/` folder holding the files that solve
it, and a `task.toml`. If a solved run also leaves files in `HOME`, such as a log a verifier
counts, put them in `solution-home/`. `tests/evals/test_shipped_tasks.py` checks that every
verifier rejects the untouched fixture and accepts the fixture with `solution/` copied over it.
Ruff and mypy skip `tasks/`, so fixtures stay byte-exact.

```toml
id = "fix-failing-test"          # must match the folder name
title = "One line saying what the task asks for"
tags = ["general"]               # general, long-output, edit, mcp, mcp-sweep, multi-read, scale
prompt = "What the user types"
setup = "optional command run after the fixture is committed"
verify = "{python} -m unittest -q"   # exit 0 means passed
timeout_seconds = 300
max_tool_iterations = 30
tools = ["read", "edit", "write", "bash", "grep", "find", "ls"]
mcp_servers = ["inventory"]      # optional; synthetic servers from servers/catalogs.py
opt_in = false                   # optional; true runs it only when named by id or tag

[checks]                         # optional; a passing run must also meet these
no_rerun = "make build"          # a bash command containing this may run only once
uses_tool = "read_output"
max_tool_errors = 2
```

A task's `mcp_servers` start for each run from the run's private `HOME`, and all their tools are
exposed with the built-in `tools`. `servers/catalogs.py` defines each server's tools and answers;
decoy tools return plausible wrong values rather than errors, so a model cannot find the right
tool by trying them all. A run whose server failed to start is `errored`, since that says nothing
about the model: Wisp reports it as an `MCP server … is unavailable` error, and in direct mode the
run is also missing the server's tools. On demand, the run must have been offered the three
discovery tools.

`inventory` (50 small tools), `billing`, `billing-legacy` and `billing-sandbox` keep definitions
short. `devhub` (`servers/devhub.py`) is about 100 tools of a code-hosting API with descriptions
of a few sentences and typed, partly nested parameters, close to real servers, so it measures
what large catalogs cost; `mcp-triage` uses it over several steps.

Commands run with `bash -c` in the run's folder. `{python}` becomes the runner's interpreter and
`{task}` the task's own folder, so a verifier script can live beside `task.toml`, out of the
model's reach. To check that the model left a file alone, compare it with the fixture
(`cmp -s {task}/repo/FILE FILE`), not with `HEAD`, which the model can move by committing. Keep
fixtures standard-library only so runs need no installs, and make `verify` reject plausible wrong
fixes, not only accept the right one.
