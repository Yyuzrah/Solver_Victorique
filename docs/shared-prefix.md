# Shared L0 paired runs

This opt-in runner compares `single`, `ladder`, and `resample` on each problem.
L0 is one whole-problem solve followed by the same candidate-version-bound
alignment and whole-proof checks. A deterministic precheck can stop L0 after
the solve when the manuscript is empty or unfinished. All three arms use the
common ladder/resample precheck. **The paired `single` arm therefore differs
from standalone `--policy single`, which does not use that precheck.** The
summary records this distinction.

```python
from victorique.shared_prefix import solve, resume

report = solve(["problem-a.md", "problem-b.md"], out="paired-run",
               calls=12, repair_rounds=12, hours=2, seed=42, transport="fake")
report = resume("paired-run")
```

```console
python -m victorique.shared_prefix solve problem-a.md problem-b.md --out paired-run --calls 12 --repair-rounds 12 --hours 2 --seed 42 --transport fake
python -m victorique.shared_prefix resume paired-run
```

`codex` and `claude` are also supported through the ordinary installed
transports. This feature has been tested offline with `FakeTransport` and local
fixture processes through both CLI adapters. Real-host paired execution has not
been tested. It does not change the default solver or the top-level commands.

## Outcomes and cost

A passing L0 delivers the same proof and result to all three arms, with three
physical calls in total. A mathematically blocking L0 failure permits the two
adaptive arms to continue in separate directories. The `single` arm stops at
L0. An unresolved request, an unknown check conclusion, or a failed check
execution does not permit a fork. Resume observes the original request, without
starting a replacement. L0 does not retry a failed execution.

`--calls N` is each adaptive arm's complete budget, including the common L0;
the paired `single` arm uses at most three calls. After a failed prefix using
`k` calls, each adaptive arm has at most `N-k` new calls. The total physical
ceiling for that problem is `k + 2*(N-k)`. The up-front ceiling is `2*N-1` per
problem because deterministic precheck failure can give `k=1`; a full
three-call L0 gives `2*N-3`.

`repair_rounds` accepts 0 through 100 in both the ordinary solve API/CLI and
the shared-prefix API/CLI; its default remains 2. For a twelve-call paired
run, explicitly set `repair_rounds=12` (CLI: `--repair-rounds 12`) with
`calls=12`. Each new version checks the previously failing gate first and skips
the second gate when that first check still blocks. Consequently a fixed three
rounds can stop after only nine calls. Setting the round ceiling to the call
ceiling lets the original call budget control continued blocking failures.
The first manuscript that passes both gates stops immediately. Unknown results,
execution failures, other existing stop conditions and the original absolute
deadline still apply. Resume preserves the saved round limit and call budget;
existing runs keep their original values.

For example, if L0 uses three calls and each adaptive arm then uses three more:

| Cost | single | ladder | resample | Physical total |
|---|---:|---:|---:|---:|
| Logical calls, including L0 | 3 | 6 | 6 | 9 |
| Additional physical calls | 0 | 3 | 3 | 6 plus the shared 3 |

The root `calls.jsonl` contains each physical call once. Each `prefix/calls.jsonl`
contains the shared calls, and each arm's `calls.jsonl` contains only its new
calls. Arm summaries separately name `logical_calls`,
`additional_physical_calls`, and `logical_tokens`; the root summary names
`physical_calls` and `physical_tokens`. Unknown token counts stay unknown.
Arm call totals must not be added to estimate actual model cost.

## Isolation and recovery

The prefix freezes the original native `N`-call budget, model/role bindings,
and absolute deadline before dispatch. Its own transport allows at most three
starts. Each branch receives the original completed native L0 events, packets,
claims, observations, and charges byte for byte, with the same identities.
Only the local database root differs. These retained rows represent shared
calls, not new executions or new receipts.

Each branch creates its own writer, bridge, and transport. The transport gets
a distinct authorization limited to `N-k` starts with the original expiry;
no original transport state or reusable start authorization is copied. New
request IDs use their policy's namespace. Resample solvers see the original
problem without the previous proof or criticism, and cold checks remain bound
to the exact candidate version. Unless explicitly configured, the runner uses
the ordinary default ladder settings: up to two repair rounds, no investigation
or full escalation.

The saved `pair.json` freezes the input copies, problem order, random seed,
per-problem strategy order, budget, and original start time. Problems run in
input order; each problem's shared L0 and shuffled strategy arms finish before
the next problem. All problems and arms share the batch's original absolute
deadline. Resume does not grant another time allowance or rerun a completed
prefix. Use this module's `resume` for paired directories.

For a Codex-only capability setting, `solve(..., reasoning_effort="low")` or
`--reasoning-effort low` overrides the solver's effort. Supported values are
`low`, `medium`, `high`, `xhigh`, `max`, and `ultra`. The value is frozen in the
pair, invocation, and transport profile; continuations inherit it without a
fresh override. Omitting it keeps the CLI default and stores no
`reasoning_effort` in the plan.
Reference graders use their own default effort.

## Explicit E2 branches

A one-problem pair can bind a task identity before dispatch:

```python
from victorique.shared_prefix import solve, branch_e2, resume_e2, e2_reservation

solve("problem.md", out="shared-task", calls=12, repair_rounds=12,
      task_id="round-1-problem-shared", repetition=1, transport="fake")
reservation = e2_reservation("shared-task", task_id="round-1-problem-shared", repetition=1)
result = branch_e2("shared-task", out="e2-task", task_id="round-1-problem-shared", repetition=1)
result = resume_e2("e2-task")
```

`task_id` and `repetition` are optional for `solve` but must be provided
together for E2 sources. They are frozen in the pair and the
prefix invocation. E2 refuses a different task/repetition, an unbound old pair,
or a source whose call/round ceilings differ from the main ladder's 12/12.
`index=0` selects the source problem; identity-bound pairs contain exactly one.
The caller chooses eligible tasks and repetitions; ordinary paired runs never
automatically create E2. E2 output must be outside the original paired run.

The `victorique.e2.v1` interface runs four branches serially:

| Variant | Repair rounds | Investigate | Escalate | Parent configuration |
|---|---:|---|---|---|
| V1 | 12 | on | off | main ladder |
| V2 | 2 | off | on | V4 |
| V3 | 1 | off | off | V4 |
| V4 | 2 | off | off | main ladder |

Each branch retains a total limit of 12 calls, including the original `k` L0
calls, and the original absolute deadline. A passed L0 shares its original
candidate and uses no additional calls. Only a mathematically failed prefix
can start branches. Unknown results, incomplete executions and uncertain gates
do not trigger E2. E2 never resumes or exports the source prefix. If its status
changes after E2 was frozen, recovery refuses the changed source rather than
silently dispatching a newly eligible branch.

`e2_reservation` is read-only. For a mathematically failed, unexpired prefix,
`new_physical_call_limit = 4 * (12 - k)`; otherwise it is zero. `slots` is 2 for
Codex/fake and 1 for Claude when execution is eligible, zero otherwise. Branches
are serial, but two independent checks within one branch can overlap. A
caller that shares one call limit across several tasks must reserve the
new-call ceiling against that limit before invoking `branch_e2` or recovering
unfinished branches.

The E2 root's `calls.jsonl` contains new calls only, once each. Its
`physical_calls` / `new_physical_calls` and `physical_tokens` exclude the prefix;
each arm's `logical_calls` and `logical_tokens` include it. Each arm has a unique
`branch_id`, separate native writer, bridge and transport authorization limited
to `12-k` new starts. Native request IDs remain local to their branch; public
`physical_call_id` combines the branch ID and request ID, including escalation
calls. Resume retains the saved source, variant options and authorization.

Use each arm's `candidate_binding` when selecting or reusing a grade. Only a
`kind: shared_L0` binding points to the original prefix and can share its grade.
A `kind: E2_branch` binding includes both `run_directory` and `branch_id` with
the candidate reference. Different branches can have identical native candidate
references; those references alone never authorize cross-branch grade reuse.

## Reference-informed V5 diagnostic

The v2 variant set replaces V3 with V5. It uses a `victorique.e2.v2` run,
selected by passing `oracle_grade` to `branch_e2` and `e2_reservation`. A v1
run keeps its four variants and resumes without conversion. A v2 run needs
this argument:

```python
reservation = e2_reservation("runs/r001-task-shared", task_id="r001-task-shared",
    repetition=1, oracle_grade="runs/r001-task-shared-grade-single")
report = branch_e2("runs/r001-task-shared", out="runs/e2-task", task_id="r001-task-shared",
    repetition=1, oracle_grade="runs/r001-task-shared-grade-single")
```

The explicit grade directory must be the source task's sibling
`{task_id}-grade-single`. Its public request and completed result must bind the
original L0 candidate/version; the request must contain the exact original
problem and manuscript. The source invocation also binds the task/repetition.
Missing, failed, unknown, or invalid grades are rejected before any branch is
started. No original grading request is retried. Recovery refuses changed
reference inputs or a changed terminal score/reason.

V5 uses main ladder's twelve-round, twelve-generation-call limits, including
shared L0. A reference score below 7 triggers repair even when both original
checks passed. The grader's reason becomes the initial defect; only its score
and reason are released to V5's first repair. The full reference solution and
guidelines are retained solely for reference grading. V1, V2 and V4 receive
none of the oracle feedback. Later V5 checks use the ordinary
two cold, version-bound gates with the problem and current manuscript. A passing
old L0 cannot be selected as a successfully repaired version after a negative
reference score. A score of 7 keeps the original manuscript with zero new V5
calls, retaining the source's original delivery/abstention status.

After a terminal changed V5 manuscript, one separate reference grade is run
using the original reference and the grader's default effort. It shares the
source absolute deadline and is never resent after an unknown attempt. If the
best draft remains the original L0, its existing grade is reused. Exhausted
deadlines leave the final grade explicitly unexecuted. The summary retains
`final_grade`, `grading_status`, and `grading_calls`; generation `logical_calls`
still includes L0 and is bounded by 12. E2 physical totals include the new final
grade once, with a distinct public physical-call ID.

Reserve at most `4*(12-k)+1` new calls for a v2 task (at most 45 when `k=1`).
The read-only reservation helper accounts for
the actual native L0 status, reference score and original deadline: a native
passing L0 with score below 7 needs only V5's `12-k+1`; a fully passing L0 with
score 7 needs zero. V5 is a diagnostic upper-bound comparator that changes
both the trigger and repair information relative to main ladder, so it is not
a comparison under equal information.
