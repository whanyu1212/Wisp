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
| `--codemode` | `off` (default), or `python` to offer the experimental `codemode` tool; rows then also count `scripts`, `failed_scripts`, `nested_calls` and why scripts failed (`script_failures`) |
| `--mcp-results` | `text` (default), or `structured` to have the devhub task tools declare an output schema and return their result as data beside the same text, so a codemode script receives a typed dict. Every other tool, and every answer's text, is the same in both; an answer that is an error, such as an unknown repository, becomes a failed call |

Runs happen in rounds: one repetition of every task and model per round. The budget is checked
before each round, so a stopped suite has the same number of runs for every pairing.

## Credentials

The runner follows Wisp's own resolution:

- It always passes `WISP_AUTH_FILE`. Codex signs in with OAuth, which lives only in `auth.json`.
- For the OpenAI API, Anthropic and Google it passes `OPENAI_API_KEY`, `ANTHROPIC_API_KEY` and `GOOGLE_API_KEY` from the
  runner's environment or, failing that, from the git-ignored `.env`. Wisp prefers those variables
  over a key stored in `auth.json`.

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
timed-out runs and cost. Per model it prints mean calls and tokens across tasks, and the mean
pass^1 with a 95% bootstrap interval over tasks. Compare calls and tokens only over the same
tasks. pass^k (from tau-bench) is the chance that k runs of a task all pass, so it shows
reliability that a plain pass rate hides.

Tokens are counted the same way for every provider. `Mean input` is every input token, `Cached` is
the part of it read from the provider's cache, and `Mean output` is the output tokens. Providers
report these differently: OpenAI's input count includes cached tokens, while Anthropic's leaves
out both the tokens read from its cache and those written to it, so a cached Anthropic run reports
a few hundred input tokens for a request of tens of thousands. The per-model `Mean tokens` is input
plus output.

`Known USD` is the cost of runs that had a price when they ran. `Est. USD` covers the runs that
had none, such as Codex subscription runs: their tokens at today's API list price for the same
model (`openai-codex` at `openai`'s prices), at the base price band. It is an estimate for
comparing setups, not what was charged, and it changes when the catalog's prices do.

For runs that offered codemode, a last table lists each model's scripts, how many failed, and why:
the failure kind, with the type checker's diagnostic code or the exception's class, such as
`type: unresolved-reference` or `runtime: KeyError`. It never quotes the error text, which can
hold local paths, so baselines keep it.

It also warns when one task and model's runs offered different tools (`tools_sha256`, from
`agent.started`), for example because an MCP server failed to connect in some runs. Compare
results only when it gives no such warning. The instructions hash is recorded but not compared: it
covers project context, which names each run's own temporary folder.

Baselines dated 2026-11-11 or earlier did not count cache writes. For Anthropic, whose cache writes are
not part of its input count, their input tokens are too low: by 13% to 28% of all tokens, going by
what those runs cost. The report warns with how many such
runs it read.

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
the tools are what changed. Tokens are input plus output, counted as in `report`, so a baseline
recorded earlier compares with a new run on the same count. `(cache writes uncounted on one side)`
says one side was recorded before cache writes were counted and the other has some: that side's
tokens are too low, so read the tokens change as overstated.

Each interval is 95% on its own, so across one comparison's many cells about one in twenty is
marked by chance; the output says how many to expect. Trust a change that is marked on several
tasks or models, or that holds when rerun with more repetitions, not a single `*`. The noise floor
line gives, for calls and tokens, the median interval half-width as a share of the earlier mean:
changes smaller than that are within noise. With three runs a side it is about ±25%, so this suite
detects large effects only; use more repetitions for smaller ones.

`baseline` saves a results folder as `baselines/<date>-<label>.json`: the suite's settings and,
for each run, its task, model, status, tool calls and errors, tokens, cost, setup hash and why
its scripts failed. It leaves out folders, error text and credential sources, takes only a finished results folder (one
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
| `2026-11-10-orchestration-direct.json` | The `orchestration` tasks, 5 runs each, with no codemode: the baseline codemode prototypes are compared against (HY-431) |
| `2026-11-10-codemode-off.json`, `2026-11-10-codemode-python.json` | The `orchestration` tasks and `mcp-triage`, 5 runs each on gpt-6.1-sol, gpt-5.6-luna and claude-sonnet-5-5, without and with the Python codemode tool (HY-495). Sonnet's `mcp-flaky-comments` runs errored on a `bash` schema Anthropic rejected (fixed in #957) |
| `2026-11-10-flaky-sonnet-codemode-off.json`, `2026-11-10-flaky-sonnet-codemode-python.json` | Sonnet's `mcp-flaky-comments` runs after that fix, 5 each, without and with codemode |
| `2026-11-10-on-demand-codemode-off.json`, `2026-11-10-on-demand-codemode-python.json` | The same 5 tasks and 3 models with MCP tools found on demand, without and with codemode, whose scripts call catalog tools by name (HY-498) |
| `2026-11-11-core-codemode-off.json`, `2026-11-11-core-codemode-python.json` | The 10 tasks without MCP tools, 5 runs each on the same 3 models, without and with codemode: whether offering it hurts ordinary coding work (HY-431) |
| `2026-11-11-stage2-on-demand-codemode-{off,python}.json` | The orchestration tasks and `mcp-triage` with MCP tools found on demand, without and with codemode, on the code that made codemode the default there (HY-509) |

On-demand discovery cut tokens per run by 76–77% on `mcp-triage` (102 tools) and 28–46% on
`mcp-pick-tool` (50 tools), and cost 20–34% more on `mcp-similar-servers` (6 tools), with every run
passing in both modes. On the one-question sweep it broke even at about 20 small tools, which placed
the `auto` threshold; see [the tools guide](../../site/src/content/pages/guide/tools-and-safety.md#what-on-demand-discovery-saves).

Without codemode, both models already batch: the 34 check-runs calls of `mcp-failing-lint` took
4 to 7 turns, not 36, and no task needed more than 8. So a script's room to save round trips is
small, and what it can save is tokens (100k–160k per run) and accuracy. gpt-6.1-sol passed every
run. gpt-5.6-luna passed 55% of them: it matched check results to the wrong pull requests
(0/5 on `mcp-failing-lint`), added up the comments wrongly (2/5 on `mcp-flaky-comments`), and once
named the wrong service. Those are the mistakes keeping results and their calls together in a
script should prevent.

## Add a task

Create `tasks/<id>/` with a `repo/` fixture, a `solution/` folder holding the files that solve
it, and a `task.toml`. If a solved run also leaves files in `HOME`, such as a log a verifier
counts, put them in `solution-home/`. `tests/evals/test_shipped_tasks.py` checks that every
verifier rejects the untouched fixture and accepts the fixture with `solution/` copied over it.
Ruff and mypy skip `tasks/`, so fixtures stay byte-exact.

```toml
id = "fix-failing-test"          # must match the folder name
title = "One line saying what the task asks for"
tags = ["general"]               # general, long-output, edit, mcp, mcp-sweep, multi-read,
                                 # orchestration, scale
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

The `orchestration` tasks measure work a script could do in one tool call (HY-431, codemode):
many calls, or calls that depend on earlier answers. Their repositories live in
`servers/devhub_repos.py`, which answers only for them, so devhub's definitions and its `acme/web`
answers stay what the earlier baselines measured. They are opt-in, so a whole-suite run does not
pay for them: select them with `--tags orchestration`.

| Task | What it takes |
|---|---|
| `mcp-failing-lint` | Two pages of open pull requests, then one check-runs call for each of 34 |
| `mcp-flaky-comments` | Adding up the comments on 127 issues across two pages of 100 |
| `mcp-deploy-chain` | Run, then its commit, then the pull request it merged, then that pull request's reviews |
| `mcp-service-alerts` | A local list of 20 services, then one alerts call for each |
| `mcp-large-result-filter` | Filter release decisions from ~77 KB default pages, with required evidence beyond the model-facing cap; direct calls can recover through smaller pages |

The large-result task covers HY-511. It uses `acme/release-audit` without changing the
existing catalog definitions or other repositories' answers. A default page places a required
match at byte 69,740, beyond the 50 KB model-facing cap but within codemode's 512 KiB internal
limit. Ten-record pages fit below 50 KB, so the same task remains solvable without codemode.
The verifier requires all three matching issues and rejects closed issues, the wrong region,
and a similar but nonmatching label. The prompt does not require a script.
The [2026-11-17 HY-511 report](reports/2026-11-17-hy511.md) records GPT/Claude results,
trace evidence, budgets, reproducible commands and limitations.

Commands run with `bash -c` in the run's folder. `{python}` becomes the runner's interpreter and
`{task}` the task's own folder, so a verifier script can live beside `task.toml`, out of the
model's reach. To check that the model left a file alone, compare it with the fixture
(`cmp -s {task}/repo/FILE FILE`), not with `HEAD`, which the model can move by committing. Keep
fixtures standard-library only so runs need no installs, and make `verify` reject plausible wrong
fixes, not only accept the right one.

## Session and approval scenarios (HY-547 / HY-546)

An opt-in task may replace `prompt` with a typed `scenario`. It uses the same fixtures,
verifier, credentials, budgets, reports and baselines as one-prompt tasks. The runner starts
`wisp --mode rpc` through the SDK's version-checked JSONL transport. Scenario runs never
use `--yes`; decisions below respond to actual approval requests from the command host.

```toml
opt_in = true
# Keep id, title, tags, verify, tools and resource limits as in an ordinary task.
[[scenario.steps]]
type = "prompt"
prompt = "Write exactly hello followed by a newline to ANSWER.txt using write."
[[scenario.steps.approvals]]
name = "write"
arguments = {path = "ANSWER.txt", content = "hello\n"}
approved = true
scope = "once"

[[scenario.steps]]
type = "checkpoint"
name = "saved"
[[scenario.steps]]
type = "prompt"
prompt = "Acknowledge this follow-up."
[[scenario.steps]]
type = "navigate"
checkpoint = "saved"
[[scenario.steps]]
type = "resume"
```

- `prompt`: run a user prompt to completion. Its optional approval decisions are ordered
  and must match the effective tool name and exact JSON arguments. A denied decision has
  `approved = false` and no scope. Allowed scopes are `once` and `tool_session`; a session
  grant can suppress later requests, so list only the requests expected to occur.
- `checkpoint`: record the current session and active leaf under a unique name.
- `navigate`: select an earlier checkpoint in the same session and verify the active leaf.
  This changes conversation history, not filesystem contents; it does not undo tool effects.
- `resume`: close the backend, start a fresh process, explicitly select the prior session
  and verify its active leaf. It does not rerun preceding prompts.
- `compact`: optionally supply `instructions`. Require an actual successful
  `compaction.completed` event, not merely command acknowledgement. Wisp requires at least
  two complete user turns on the selected branch before compaction.

Missing decisions, unexpected or mismatched requests, unsuccessful commands, wrong answers
and incomplete steps prevent a pass. Unexpected requests are denied and the scenario stops.
Transport errors remain infrastructure errors. The task timeout covers the complete scenario,
including startup, approvals and restarts; `max_tool_iterations` remains a per-prompt bound.
The outer runner retains its process-group cleanup after exit or deadline. This is process
cleanup, not an OS sandbox for untrusted commands.

`scenario-config.json` records the steps and backend command without credential values;
`scenario-result.json` records completed steps and failures. `events.jsonl` includes all
observed RPC and model events across restarts. Usage includes every prompt and compaction;
turns are summed across prompt runs. Result rows and sanitized baselines retain step counts,
requested effort and SHA-256 fingerprints for task definitions, fixture files and synthetic
MCP server sources. Raw event traces and configuration stay in git-ignored results folders.

Use `--effort` to pin the model's reasoning effort rather than relying on a provider default.
An omitted effort is recorded as null. Use a single supported effort per comparison; do not
pool runs with different effective settings. The runner still checks budgets between rounds,
so a complete multi-step round can overshoot a threshold. Estimate that round before paid runs.

### First scenario and acceptance

`session-branch-recall` gives a codeword, creates a conflicting conversation branch, returns
to the original branch, restarts, compacts, and requests one approved write of the remembered
word. Its verifier requires the original codeword exactly. The primary metric is whole-scenario
success: all transitions and approval expectations must pass and `ANSWER.txt` must be correct.
Costs, latency, script adoption/errors and repeated calls are secondary metrics. A correct file
cannot compensate for a failed earlier transition or an unexpected approval.

```bash
uv run python -m evals.tool_use run --tasks session-branch-recall \
  --models openai-codex::gpt-6.1-sol --effort medium --reps 3 --workers 1 \
  --budget-usd 5 --budget-tokens 1000000 --codemode off
```

The HY-547 foundation established deterministic coverage; it did not establish a paid
performance baseline for new capabilities. HY-511's saved GPT/Claude baselines remain evidence for single-prompt, read-only
orchestration; they do not establish state persistence or interactive approval correctness.
Run fresh controls and candidates for each future capability on the same scenario/fixture
fingerprints, model/provider/effort, permissions and output limits. Current evaluation model
scope is GPT and Claude; Gemini is excluded.

### Capability boundary

| Capability | Current foundation / evidence | Future work |
|---|---|---|
| Read-only composition, structured results, large bounded results | Existing codemode tests and HY-511 GPT/Claude baselines | Targeted regressions as capabilities change |
| Multiple prompts, backend resume and branch selection | RPC scenarios and transactional script-state reuse/isolation tests (HY-548) | Broader live state workloads |
| Explicit compaction | Verify subsequent history and retain branch-local script state outside summary text | Broader state workloads |
| Approve once, deny, session grants | Scripted decisions exercised against the shared RPC host | Codemode inner-action approval and cancellation scenarios |
| Mutating tools inside scripts | Still prohibited by the existing read-only executor | Separate codemode policy boundary before exposing mutations |
| Persistent store/load/delete | Bounded JSON transactions, durable result snapshots, branch/resume/compaction coverage (HY-548) | Broader workload and quota evaluation |
| Recoverable script artifacts, image/model helpers | Not implemented | HY-546 capability slices and dedicated acceptance metrics |

Windows, interactive TUI approval UX and paid-model reliability are not established by the
foundation tests. Keep deterministic CI coverage small; live comparisons and stress sweeps
remain opt-in.


### Persistent-state pilot (HY-548)

`mcp-session-reuse` fetches open pull requests, compacts and restarts the backend, then asks a
new question about that dataset and requires one exact approved answer file. It allows the
model to choose direct calls or scripts. Whole-scenario success is primary; retained state and
post-resume fetch counts distinguish actual reuse from a lucky remembered answer. The task
uses the existing synthetic `devhub` catalog and requires no new server behavior.

Run with `--tasks mcp-session-reuse --mcp-tool-exposure on_demand --mcp-results structured
--effort medium`, comparing codemode `off`, the pre-state build with `python`, and the state
candidate with `python`. Use identical task/catalog fingerprints and provider/model settings.
[The pilot report](reports/2026-11-17-hy548.md) records outcomes, usage, reproducibility and limits.
