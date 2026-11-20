# Independent sampling and tournament selection

`bestofn` is an optional solve policy. New runs default to `single`.
Pair each run with an explicitly named, completed `full` run on the exact same
original problem. Each reference names its public `calls.jsonl`,
`invocation.json`, `state/run-config.json` and `summary.json`.

```sh
victorique solve problem.md --policy bestofn --full-calls runs/full/calls.jsonl --calls 12 --out runs/bestofn
```

```python
from victorique import solve

summary = solve("problem.md", policy="bestofn", full_calls="runs/full/calls.jsonl",
                calls=12, out="runs/bestofn")
```

The reference loader checks the policy, run identity and exact original problem.
It consumes token counters and role labels. Full-run proofs and grading files
are never read. The token metric is cache-inclusive input plus output; cached
input and reasoning output are already included and are not added twice.
Every call, including failed calls, needs both counters for its run's total to
be measured. A missing counter or unknown call makes the total unknown.
Duplicate requests, non-full policies and mismatched run identities produce an
explicit error before any role dispatch.

## Missing target costs

The target fallback uses the paired full total first, then the median of
other measured full runs on the same original problem, then the median of
measured full runs in the same difficulty band. Even-sized pools use the mean
of the two middle totals, so a target may end in `.5`. Unknown totals never
contribute a zero or enter the median. All eligible runs in the selected pool
contribute; the same-problem pool takes priority regardless of difficulty label.

Supply additional runs explicitly with `--full-references references.json`, or
`solve(..., full_references="references.json")`. The JSON contains only the
current problem's difficulty and the named public calls files with their bands:

```json
{
  "difficulty": "Advanced",
  "runs": [
    {"calls": "full-repeat/calls.jsonl", "difficulty": "Advanced"},
    {"calls": "full-other-problem/calls.jsonl", "difficulty": "Advanced"}
  ]
}
```

Paths are absolute or relative to this JSON file. Each listed directory must
contain `invocation.json` and `state/run-config.json`; its original problem is
read only to identify the same-problem pool. Difficulty labels are explicit
caller input; `null` disables difficulty matching for that entry or target.
The paired run is already supplied by `--full-calls` and must not be listed
again. Duplicate run IDs are rejected. There is no directory discovery, and
proofs, reference solutions and grades are never opened. `summary.json` must
bind to the same run ID and full policy, with a terminal outcome of `complete`,
`abstain` or `partial`, before the calls can supply a total. A pending or absent
summary makes the total unavailable even when every existing call is complete;
measured costs of completed calls remain available for paired role estimates.
The summary's integer `calls_used` must equal the number of valid, unique call
rows. Missing or mismatched counts make the total unavailable; invalid counts
are rejected. A terminal run with a matching zero count has a measured total
of zero. The minimum two manuscripts still apply when that target is exceeded.

The frozen estimate records `target_fallback` (`paired_full`,
`same_problem_median`, `same_difficulty_median` or `unavailable`) and
`target_source_run_ids`. Without any measured target, the ordinary run directory
and summary retain the planned row as `outcome: unexecuted`, with
`reason: missing_target_tokens`, null target and actual costs, and zero calls.
No model CLI is inspected or started. Status, export and resume preserve this
result; newly available references require a new run.

## Missing role costs

Each role keeps its mean from the paired full run when that run measured the
role. A missing role uses the median of measured calls for that role in other
full runs from the same difficulty band, then the median across all explicitly
supplied full runs.
These are medians of individual call costs, not medians of per-run means.
Only terminal runs with matching summary call counts enter these pools.
Missing usage in another role does not discard the role's measured calls.
An explicit zero measurement remains zero. A null difficulty skips the
same-difficulty pool; no difficulty is inferred from problem identifiers.

If both external role pools are empty, a run with a known target and at least
five available calls can start. It generates at least two manuscripts, then
uses its own measured generation mean for each missing role. Only a complete
set of generation measurements supplies that mean. The prediction reserves
all tournament comparisons and both final gates as before. Incomplete current
usage stops further generation; the remaining call budget can still complete
selection and final checks, with unknown costs reported as null.

The frozen `role_sources` records each role's source kind and run IDs;
`role_sample_counts` records the number of contributing calls. The sample unit
is `measured_role_calls` for external estimates and `generated_manuscripts`
for the current-run mean. The ordinary summary's `effective_role_estimates`
reports each effective cost, source, run IDs and sample count. Current-run
estimates are derived from persisted generation calls, while external pools
stay frozen and are never reread on resume. An estimate frozen without
`role_sources` has no missing-role fallback, and incomplete generation usage
blocks its run instead of starting selection. Schedule bestofn after its
paired full ends.

## Frozen prediction rule

The rule `sequential_observed_mean_reserve_v1` decides after each manuscript
whether to generate another one. It uses the target fallback and the
missing-role costs described above.
The reference path, run identity, target, fallback and source run IDs, per-role
means, sample counts, token metric and rule name are frozen in
`state/run-config.json`.

Generate at least two independent manuscripts, each receiving only the original
problem and the same literal whole-problem obligation. Each manuscript uses a
separate candidate plan identity, and starts at revision 1 in a fresh role
context. Prompts do not receive earlier manuscripts, critiques, comparisons,
reference proofs or grading labels.

After `n` manuscripts, let `S` be their measured total token cost. Predict the
next generation cost `g` using this run's generation mean when `n >= 2`; the
single-manuscript fallback is the available solver estimate. Let `c`, `a` and
`p` be the critic, alignment reviewer and proof reviewer estimates after the
missing-role fallback. The critic cost is an explicit proxy for the different
pairwise comparison prompt.

Generate another manuscript only when
`S + g + n*c + a + p <= target` and the call cap can fit `2*(n+1)+1` calls.
Otherwise start selection. Once the first comparison is prepared, the generated
candidate count is frozen by the persisted requests. All remaining work is the
`n-1` comparisons and two final gates. The minimum call cap is five; smaller
caps are rejected. The first two manuscripts are retained even when the minimum
workflow exceeds the reference token target.

Missing generation usage stops expansion and preserves unknown costs. A
binding call cap can also stop generation without this estimate. Resume
reuses the original budget, absolute deadline, frozen reference estimates and
requests, without rereading the external reference file. Unknown transport
responsibilities are observed, never resent.

## Selection and delivery

The tournament compares the first two manuscripts, then compares their winner
with each remaining manuscript in generation order. Each comparison receives
only the original problem, the two exact manuscripts and their version bindings.
It uses the alignment and whole-proof review standards to express a relative
preference. Previous comparison reasons and gate results are withheld.

The selection response contains a winner reference and rationale, or an explicit
unknown result. A reference outside the supplied pair, an altered version or a
rewritten manuscript is rejected. The winner's original manuscript is passed
unchanged to a cold original-problem alignment review and a cold whole-proof
review. Both must pass on that same version for `outcome: complete`, by the pass
rule of every policy (see the [ladder policy](ladder.md)); with
`--minor-findings blocking`, a review that says pass with minor findings does
not pass, and the run abstains. Selection
alone grants no correctness status. An unknown selection, or a failed
comparison or final review, ends with abstention; transport unknown remains
pending until the original result or deadline. A failed generation call, such
as a timeout, stops generation: the manuscripts already returned go on to
selection and both final reviews, and `generation_stop_reason` is `call_failed`.
If no manuscript was returned, the run abstains. On abstention the exported
draft is the last provisional winner, or the first original manuscript if
selection never completed.

Each request reserves one call, including comparison and final reviews. A full
successful run with `n` manuscripts uses `n + (n-1) + 2 = 2n+1` calls, plus one
for a failed generation. There are no automatic retries, rewrites,
investigations or repairs in this policy.

## Reported costs

`summary.json` includes `bestofn.n`, `n_frozen`, `selected_ref`, the frozen
estimate, actual tokens, known tokens, missing-usage call count, actual/full
ratio and token overrun. All generation, comparison and final-review calls enter
these totals and `calls.jsonl`. Missing totals and ratios are null. The token
alignment tolerance is null: the rule predicts cost, while actual prompt and
response lengths vary. Overshooting samples remain in the results. The ordinary
hard call cap and absolute deadline continue to apply.

`--transport fake` exercises the lifecycle without model calls. Its default
responses contain no token measurements, so new runs stop generation after two
manuscripts and exercise selection and both gates with unknown usage. Five
calls suffice for that path.
