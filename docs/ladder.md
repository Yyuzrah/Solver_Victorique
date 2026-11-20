# Ladder policy

`solve(problem, policy="ladder", calls=12, hours=2, repair_rounds=2,
investigate=False, escalate=False, undecided_checks=None, minor_findings=None)` starts with one
complete solution. The default policy is `single`; ladder is chosen
explicitly. The policy and switches are stored in `invocation.json`;
resume keeps the original budget, absolute deadline and settings. `full`
retains its serial controller.

## Steps

- **L0:** Solve once. Check for empty text, unfinished markers such as TODO,
  a bracketed `[omitted]`, "proof omitted" or 留给读者, and a missing explicit
  answer in a definite-answer task. Ordinary mathematical uses such as "the
  omitted variable" are not markers; a marker finding quotes the matched text
  with its exact location. Precheck failures go straight to repair. Otherwise reserve and dispatch the
  alignment and whole-proof checks together, in fresh contexts.
- **L1:** Repair up to `repair_rounds` times (0–100, default 2). The original
  call budget and deadline still bound every round. The repair
  response gives one `assessments` entry per issue, with `issue_id`, a decision
  (`confirmed`, `refuted`, or `unresolved`), and a reason. It returns a complete
  candidate and changes only confirmed issues. If none are confirmed, the
  controller retains the original candidate even if the response changes text.
  It rechecks the previously failed gate first, then the other gate on the same
  candidate. An unresolved judgment remains unresolved. If a precheck failure
  led to the repair and no issue is confirmed, the run stops with
  `ladder_precheck_unresolved`: the deterministic precheck would fail the
  unchanged candidate again.
- **L2:** With `investigate=True`, a refuted claim that appears again gets one
  fresh investigation. The controller explicitly releases that completed
  investigation to the subsequent repair and gate roles. Gate roles receive
  only the selected investigation, not prior gate reports or repair judgments.
- **L3:** With `escalate=True`, exhausting the repair rounds starts the serial
  full controller with the remaining calls and the original absolute deadline.
  Child requests, tokens, and elapsed time are included in the parent result.
- **L4:** Deliver a candidate only after both gates pass on that version.
  Otherwise abstain and export the candidate with the most passed checks.
  `summary.json` includes unresolved issues/questions, non-blocking findings,
  and inconclusive and incomplete checks. Ties retain the earlier draft.

A check passes when its report says pass, lists no doubt and has only minor
findings. `single`, `ladder`, `resample`, `bestofn` and `full` all use this rule
(`adaptive_contracts.gate_passes`). By default, minor findings on a passing
report are recorded as non-blocking and never trigger a repair; the
`minor_findings` setting below can make them blocking. A report that says fail
with only minor findings does not pass. Standalone `verify` reads such a report
as a passing review.
A failed CLI call or invalid gate response retries only that gate once, within
the budget. A call that reached the per-call time limit is first sent again as
its own new request (`REQUEST.timeout-retry-N`), at most `timeout_retries`
times (default 1; see the [settings](usage.md#settings)); the gate's one retry
applies only when that repetition fails in another way. Each repetition counts
toward the call budget and gets at most the run's remaining time. Any other
unfinished or unknown request is observed again; it never permits a
replacement dispatch. The same rule applies after interruption.

## Undecided checks

A check is *inconclusive* when its report neither passes nor lists a fatal or
material finding: outcome unknown, a listed doubt, or a fail with only minor
findings. It is *incomplete* when it returns no valid report after its one
retry, for example after an invalid response. Neither names a
located defect. The `undecided_checks` setting (`--undecided-checks`, or the
settings file described in the [usage guide](usage.md#settings)) selects what
happens:

- `abstain` (default): the run stops and abstains. The endpoint reason is
  `ladder_check_inconclusive` or `ladder_check_incomplete`, and `summary.json`
  lists the checks in `inconclusive_checks` (outcome, number of doubts and of
  minor findings) or `incomplete_checks` (request status).
- `repair`: the first undecided result in a run counts as a failed check. When
  the round has no blocking finding, the repair receives as its issues the
  doubts, the minor findings of a fail, or a statement that the check returned
  no valid report, and judges each one like any other issue. The repaired draft
  is checked again, failed check first. A later undecided result stops the run
  as in `abstain`.
  An incomplete check beside a blocking finding goes to repair with that
  finding. The repair counts against `repair_rounds`, the call budget and the
  deadline.

`resample` uses the same setting; its step after a failed check is one fresh
solution instead of a repair. `single` and `bestofn` have no repair step, and
`full` decides with its own controller.

## Minor findings

A check report can say pass, list no doubt and still list minor findings
(wording or presentation). The `minor_findings` setting (`--minor-findings`, the
settings file described in the [usage guide](usage.md#settings), or the keyword
argument of the Python
`solve` and `bench`) selects how such a report counts, in every policy:

- `nonblocking` (default): the check passes. Its minor findings are recorded
  (`non_blocking` in the `summary.json` of `ladder` and `resample`) and are not
  repaired. This is the 0.1.0b2 rule.
- `blocking`: the check does not pass, and its minor findings are treated like
  fatal or material findings. `ladder` sends them to a repair, which judges each
  one like any other issue; the repaired draft is checked again, failed check
  first. `resample` starts a fresh solution, as after any failed check (the new solve
  receives no findings); `full` adds them to its open issues, which it
  investigates and repairs as it does for a failed check; `single` and `bestofn`
  do not deliver. When such findings remain on the exported draft, the
  `summary.json` of `ladder` and `resample` lists them in `unresolved_issues`
  instead of `non_blocking`. 0.1.0b1 counted such a report as invalid, so it
  did not pass there either.

Only this kind of report changes. A report that says fail with only minor
findings, or says pass but keeps a doubt, is inconclusive under both values (see
[undecided checks](#undecided-checks)), and fatal or material findings block
under both values. A run records the value in `invocation.json`; a `blocking`
run also stores it in its frozen limits (`manifest.limits.minor_findings` in
`state/run-config.json`), where every policy reads it, so `resume` and an
escalated `full` continuation keep it. A run whose limits do not contain
the setting resumes with `nonblocking`. No role is told the setting; under
`blocking` the minor findings reach the repair (in `full`, the investigation and
the repair) as issues. Standalone `verify` keeps its own verdict rule; the
setting does not apply to it.

The default stays `nonblocking`. A replay judged one pair of in-loop checks,
without repair, on 500 externally written proofs with human grades (125
correct, 375 wrong), using the check reports recorded for each proof:

| Decision | Correct proofs stopped | Wrong proofs delivered |
| --- | ---: | ---: |
| The 0.1.0b2 rule (`nonblocking`), replayed | 27 | 23 |
| 0.1.0b1, as recorded | 42 | 18 |

0.1.0b1 counted a report that said pass and listed findings as invalid. It also
applied the old precheck, which stopped 9 of the 500 proofs (1 correct, 8
wrong); each of the 9 also failed at least one of the two checks, so the
precheck does not change the 42 and 18. `blocking` reads a report that says
pass with findings the same way as 0.1.0b1, without being identical to
0.1.0b1; the replay has no separate count for it. Against what 0.1.0b1
recorded, the 0.1.0b2 rule delivered 15 more correct proofs and 5 more wrong
ones; the human scores of those 5 wrong proofs are 6, 6, 4, 4 and 4 out of 7.

All prepared and in-flight calls consume the shared call allowance. Two initial
checks may overlap in `single` and `ladder`; dependent repair checks and the
`full` controller run serially. The two checks use the solver's model service
unless `--check-transport`, `--check-model` or `--check-reasoning-effort`
gives them another; repair and investigation stay with the solver's service. The public telemetry records L0, L1, L2, or L3
for each ladder call. L2 and L3 are disabled by default.

The precheck recognizes explicit text markers and answer statements. `ladder`,
`resample` and the paired runner run it; `single`, `bestofn` and `full` do not.
An empty
solver body remains an invalid raw response: only when its remaining envelope,
binding, and candidate fields validate does ladder create an explicit unfinished
repair input. It does not export that scaffold as a valid candidate. Full and
standalone verification retain their existing nonempty-body contract.

Offline tests: `python -m pytest -q tests/test_ladder.py tests/test_api.py
tests/test_pass_rule.py tests/test_undecided_checks.py tests/test_minor_findings.py`.
