# Reference grading

`victorique bench PROBLEMS_DIR --policy single --calls 3 --grader codex`
runs the solver and gives each delivered proof with a reference one separate
Codex grading call. `--grading-seconds S` bounds that call and its deadline
independently of the solver's `--hours` deadline. It defaults to the per-call
limit (`--call-timeout-seconds`, 600 seconds unless set; see the
[settings](usage.md#settings)); the accepted range is 0.05–86400 seconds. A batch
with N problems therefore permits at most N additional grading calls. Failures
and uncertain attempts consume the same allowance and are never resent.
`--call-timeout-seconds`, `--timeout-retries`, `--undecided-checks` and
`--minor-findings` are passed to every solve in the batch; `bench.json` records
all four. Without `--policy` each solve uses `single`.

For cross-family grading, select the other transport explicitly:

```console
victorique bench PROBLEMS_DIR --policy single --calls 3 --transport codex --grader claude
victorique bench PROBLEMS_DIR --policy resample --calls 12 --transport claude --grader codex --same-family-secondary
```

`--same-family-secondary` is off by default. Enabling it adds one independent
grade using the solver's transport, for a ceiling of two additional calls per
delivered proof with a reference (2N for N problems). Each grade has its own
one-call allowance, including failed or unknown attempts, and its own frozen
absolute deadline. Neither grade retries automatically. The primary and
secondary scores remain separate; no vote or combined truth label is computed.

By default, only delivered proofs are graded. `--grade-abstentions` (Python:
`grade_abstentions=True`) additionally grades the frozen endpoint candidate of
an abstained run. It requires a primary grader. The original `outcome` and
`classification` remain `abstain`, even if the draft scores 7/7 or matches a
reference answer. This separates best-draft quality from successful delivery.
Both graders bind to the same exported candidate/version. A missing endpoint
candidate is reported as `grading_status: missing_candidate`, with zero grading
calls; a placeholder file alone is insufficient. Pending executions and failed
solves are not converted into terminal drafts by this option.

The additional ceiling is still one call per eligible candidate, or two with
the secondary enabled; for N problems the upper bound remains N or 2N. Reports
record `grade_abstentions` and `grading_calls_limit_per_candidate`. The original
`grading_calls_limit_per_delivery` field remains as a compatibility alias for
the same allowance. Grader failures retain their usage and leave draft scores
unknown. When you enable this option, count these grading attempts in your
call budget; it does not regrade batches that already ran.

Codex `single`, `ladder`, and `resample` batches allow at most `--jobs 2`, because each
problem can run two checks concurrently. Grading runs after solve on the same
worker slot, without another pool. Count other running jobs against your own
limit on concurrent model processes.
Any batch using a Claude solver or grader requires `--jobs 1`; its calls run
serially. Codex-only `full` batches retain their four-job ceiling.

Each grader uses a fresh isolated context and its own CLI default model.
`--grader-model` sets only the primary model; `--secondary-grader-model` sets
only the optional secondary model. The solver's `--model` is never inherited
by a grader. Each grade receives only
the problem, delivered candidate, reference solution, problem-specific grading
guidelines, and fixed 0–7 rubric. Solver checks, feedback, and conversation
history are excluded, and neither grader sees the other's assessment. The
candidate text is frozen once for both grades and its candidate/version
reference is retained in each public request and result. Reference JSON
sidecars supply `reference_solution` and optional `grading_guidelines`; these
fields are not passed to the solver.

The CLI's observed model and token usage appear in `grading_model` and
`grading_tokens`. `grading_calls` is separate from solver `calls_used`.
If the CLI omits a model name, the CLI's configuration defaults captured before
the request are used and labeled `grading_model_source: cli_default_config`.
An actual CLI report takes precedence and is labeled `cli_reported`; unavailable
model names stay null. Explicit model selections are labeled `request_config`
when no event reports a model. Claude currently reports its model in the init
event; absent events and unavailable CLI defaults remain unknown. The same
source distinction applies to reasoning effort.
Answer matching and `proof_score` are both retained: an answer match can coexist
with a low proof score. Failed, timed-out, and unknown grading results retain
their cost and the solver's original outcome, with an empty proof score.

`bench.json` stores each full result under `rows[].grades.primary` and, when
enabled, `rows[].grades.secondary`. Each valid score has
`fully_correct = (score == 7)`; incomplete grades have null score and null
`fully_correct`. The existing flat `proof_score`, `fully_correct`, and
`grading_status` describe the primary assessment. `grading_calls` and
`grading_tokens` total all enabled grades, separately from solver costs.
Token totals use `input_tokens_including_cache` plus output when available.
Otherwise known Codex input already includes cache reads; known Claude input
adds cache reads and cache creation. Missing Claude counters remain unknown,
including an explicitly unknown normalized input total. Callable graders can
provide `input_tokens_including_cache` to make their total unambiguous.
The Markdown report includes both grades and their sources, models, and costs.

Each row also stores `usage` for solver costs and `grading_usage` for all enabled
grades: `total`, `known_subtotal`, `calls`, `missing_calls`, and `source`.
Solver accounting reads only the current problem run's public `calls.jsonl`;
it does not scan databases, nested runs, or historical results. Custom solve
functions without that file retain summary-based accounting (`source: summary`).
The original `tokens` and `grading_tokens` fields remain available.

A timed-out call with missing usage keeps the token total null but does not
discard earlier calls' known costs. Partial calls also contribute their known,
nonoverlapping counters to `known_subtotal`. Codex cache reads are already in
input, and reasoning tokens are already in output; neither is added again.
Claude's raw input, cache reads, and cache creation are added once, or its
normalized cache-inclusive input is used directly. An explicitly null normalized
input remains incomplete even if some raw counters are known.

Token aggregates retain `total`, `known_subtotal`, and `missing` (problem rows),
and add `missing_calls` (individual calls with incomplete usage). The report
labels these as **missing rows** and **missing calls**. For example, one known
planner call costing 33,979 tokens followed by an unknown solver call yields
total null, known subtotal 33,979, one missing row, and one missing call.
Primary and secondary grading costs obey the same rule independently.
Summary-only incomplete aggregates cannot identify the number of missing calls,
so that count stays null unless the summary represents a single call. A known
zero remains zero; a zero known subtotal with missing usage is only a lower bound.
If the summary's call count is unavailable or smaller than the public projection,
the row's usage adds `coverage: unconfirmed`: the projection may be only a prefix
or conflict with the summary. Its known costs remain, but its total and missing
call count stay null. When the summary counts more calls than the projection,
the omitted calls are counted as missing usage.
These fields describe new bench snapshots; existing records are not
rewritten.

Each solver output has `grading-primary/request.json` and
`grading-primary/result.json`, plus `grading-secondary/` when enabled.
The database, captures, schema and final response stay in each grade's `transport/`.
Only the execution working directory is external and temporary; completed
grades remain recoverable after that empty working directory is removed.
`VICTORIQUE_CODEX_BINARY` selects the native Codex executable, as for solve and
verify; `VICTORIQUE_CLAUDE_BINARY` selects Claude.
To observe the same attempt after an interruption:

```console
python -m victorique.harness.grade OUTPUT_DIR/PROBLEM_ID/grading-primary
```

Recovery reads existing local captures and updates only that grade's `result.json`.
It never starts a process, renews a deadline, or increases the one-call budget.
The original `bench.json` remains the batch-time snapshot. A missing final
response remains `unknown`; an explicit host failure is `failed`. Re-running
the batch or creating a new grading directory is a new run and needs its own
budget. Existing `grading/` directories and the `grade_codex` Python entry point
remain recoverable. `--grader codex` still enables only one grade by default.
`--grader fake` uses the durable offline fixture with fake solve transport.

The Python API accepts the same options:

```python
from victorique.bench import bench

result = bench("problems", policy="single", calls=3, transport="codex",
               grader="claude", grader_model=None, same_family_secondary=True,
               secondary_grader_model=None, out="runs/comparison")
```

An injected callable grader still supports one fresh-context call per proof;
its caller must enforce a bounded execution time. Its request and result are
also retained, and recovery reads that result without invoking the callable.

Console JSON escapes non-ASCII characters, including mathematical symbols,
so redirected Windows output works with legacy code pages. JSON decoding
restores the original text. Saved JSON and Markdown files use UTF-8.

## Problem files

Put each problem in a top-level UTF-8 `ID.md`. Optional `ID.json` contains
reference material, which is used only after `solve` returns:

```json
{
  "reference_answer": "42",
  "answer_aliases": ["forty-two"],
  "reference_solution": "A complete reference proof goes here.",
  "grading_guidelines": "Problem-specific allocation of partial credit."
}
```

Omit `reference_answer` for proof-only tasks. Keep answer aliases fixed before
running. Each problem receives its own output directory, call allowance and
deadline; `--jobs` controls concurrent problems (1–4).

```console
victorique bench PROBLEMS_DIR --policy single --calls 3 --jobs 2 --transport fake --grader fake --out OUTPUT_DIR
```

The module command `python -m victorique.bench` is also available.
Policies are passed to the public `solve` API. `BENCH.md`
and `bench.json` contain every submitted problem, including failures and timeouts.
Existing output directories are refused. The solve API enforces each run's
`--hours` deadline; queued problems start their deadline when their solve starts.

## Scoring rules

An execution result of `complete` means the strategy delivered a candidate.
Correctness is assessed separately:

- For answer problems, explicit `Final answer: ...` text or the last balanced
  `\boxed{...}` is compared with the reference and predeclared aliases. Matching
  normalizes whitespace and outer dollar delimiters. A match is
  `correct_delivery`; a mismatch is `incorrect_delivery`. These labels measure
  exact answer matching. Algebraic equivalence requires predeclared aliases or
  separate adjudication. Missing extraction stays `ungraded_delivery`.
- For proof-only problems, a completed independent grade of 7 is
  `correct_delivery`; 0–6 is `incorrect_delivery`. Without a completed grade,
  delivery is `ungraded_delivery`. The score and grader status stay visible for
  answer problems even when answer correctness differs from proof correctness.
- `abstain`, `failed`, `timeout`, and `unknown` each have their own count. Public
  API outcomes such as `pending` or `partial` remain `unknown`, with their raw
  outcome in the row. Grader failures leave the solve outcome unchanged.
- Calls and input-plus-output tokens are reported separately for solving and
  grading; input includes cached input, as described above. Missing usage stays
  `unknown`; totals show known subtotals and the number of missing rows. Wall
  time includes grading and the report also records total batch elapsed time.

Each grade scores the proof with this fixed 0–7 rubric and gives a reason:

| Score | Meaning |
| ---: | --- |
| 0 | No relevant progress, or an entirely invalid argument |
| 1 | A relevant observation without substantial valid progress |
| 2 | Some valid progress; the central argument is missing or incorrect |
| 3 | Substantial partial progress with major gaps |
| 4 | A promising main approach with significant gaps |
| 5 | Mostly valid proof with a substantive remaining gap |
| 6 | Essentially complete proof with a minor mathematical gap |
| 7 | Complete correct proof of the stated problem |
