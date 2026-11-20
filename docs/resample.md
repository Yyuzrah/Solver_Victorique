# Independent resampling policy

Select the policy explicitly; the default policy is `single`:

```sh
victorique solve examples/toy.md --policy resample --repair-rounds 2 --calls 12 --hours 2 --transport fake --out runs/resample-toy
```

```python
from victorique import solve

result = solve("examples/toy.md", policy="resample", repair_rounds=2,
               calls=12, hours=2, transport="fake", out="runs/resample-python")
```

The initial whole-problem solve, deterministic precheck, review templates and two
final gates are shared with ladder, and so is the pass rule: a check passes when
its report says pass, lists no doubt and has only minor findings. A passing
initial candidate ends after three calls. An empty or unfinished manuscript, a
missing required answer, or a blocking review finding triggers another complete
solve of the original problem. Minor findings on a passing report do not, unless
`--minor-findings blocking` is set: then such a check does not pass and triggers
another solve, which receives no findings like every new solve (see the
[ladder policy](ladder.md#minor-findings)). An
inconclusive or incomplete check (defined in the [ladder policy](ladder.md#undecided-checks))
stops the run by default, with endpoint reason `resample_check_inconclusive` or
`resample_check_incomplete`; with `--undecided-checks repair` the first one
triggers one fresh solve.

Each new solve receives the original problem and the same solve template, with
an independent plan namespace. It receives no previous candidate, criticisms,
investigation or repair instructions. Its first candidate revision is 1; the full
candidate reference includes its plan namespace, so gates cannot transfer between
samples even if the model reuses an artifact ID. After a trigger, the previously
failed gate is checked first, then the other gate on the same new version.

`repair_rounds` / `--repair-rounds` is the shared ladder/resample round limit:
0–100 additional generations, default 2. Every solve, gate and execution retry
counts against the original `calls` limit. Each additional round has the same
upper bound as ladder: one generation and two gates, each with at most one retry.
An execution failure only retries that check once; by default it does not
trigger a new solve. A request with unknown execution status remains the original outstanding
request and is never silently resent. `investigate` and `escalate` are rejected
for this policy.

`resume`, `status` and `export` use the persisted policy, call allowance and
absolute deadline. Two passing gates on one candidate are required to deliver.
At the round, call or time limit the policy abstains otherwise, exporting the
candidate with the most passing gates (earliest candidate on a tie) and unresolved
issues. Completed passing candidates remain deliverable at the deadline.
