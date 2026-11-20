# Standalone proof review

Review an existing UTF-8 proof against its original problem:

```sh
victorique verify PROOF.md --problem PROBLEM.md --transport fake --out runs/review
```

The module command `python -m victorique.verify` is also available. The Python entry point is
`from victorique.verify import verify`:

```python
result = verify("proof.md", problem="problem.md", mode="single", passes=1,
                transport="fake", out="runs/review")
```

`single` performs one complete review per pass. `stepwise` asks the reviewer to
enumerate the reasoning steps in its analysis, check each step for errors, and
report any defects in the existing gate format. `multi` performs three reviews
per pass in fresh contexts, with no previous verdicts or feedback supplied.
Every review checks the same original problem and candidate version. `--passes k`
requires k consecutive successful rounds; multi therefore needs 3k successful
reviews. A blocking defect stops the run immediately. Defaults are `single` and
one pass.

`--calls` sets a hard invocation limit (1–100, default 12); unused calls are not
spent. `--hours` sets an absolute deadline (default 2 hours).
`--call-timeout-seconds` limits each review call (0.05–86400, default 600; it can
also come from a settings file, see the [settings](usage.md#settings)).
`--model` selects a
model; omitting it uses the installed CLI's default. The default transport is
`codex`; `fake` exercises the workflow offline and always supplies synthetic
passing reviews. Each review uses the existing persisted request lifecycle.

The command writes `proof.md`, `summary.json`, `calls.jsonl`, frozen inputs and request state
under `--out`. The summary keeps every raw review, its normalized verdict,
candidate/problem references, calls, elapsed time and available token usage.
`blocking` contains fatal/material findings; `non_blocking` contains minor
wording or presentation findings. A raw `pass` or `fail` with only minor
findings and no uncertainty is normalized to a passing review while its raw
outcome and explanation remain visible. A raw `unknown` always stays incomplete.
`verify` keeps this rule: the `minor_findings` setting of `solve` and `bench`
does not apply to it, and `verify` has no `--minor-findings` option (a settings
file may still contain the key).

Exit codes are 0 for `pass`, 1 for `fail`, and 2 for `incomplete` or invalid
arguments. Invalid model output, call failure, unknown results, exhausted calls
and deadline expiry produce `incomplete`. They do not trigger another review.
`resume_verify("runs/review")` observes existing live requests and continues only
with the frozen inputs and remaining budget. It never resubmits an unknown
request. A completed run keeps its original result.

## Human-label calibration

Create a JSON list whose paths are relative to the dataset file:

```json
[
  {"id": "example-1", "label": "correct", "problem": "p1.md", "proof": "proof1.md"},
  {"id": "example-2", "label": "incorrect", "problem": "p2.md", "proof": "proof2.md"}
]
```

```sh
python -m victorique.calibrate labels.json --transport fake --out runs/calibration
```

The script defaults to all three modes and the fake transport. Use `--modes`
to select modes. `--calls` applies separately to each sample/mode; the summary
records the resulting whole-batch call ceiling. `--hours` bounds the whole
batch with one absolute deadline, and `--call-timeout-seconds` each review call
(default 600). Human labels are kept outside review packets.

`calibration.json` contains every sample, plus per-mode false accepts (incorrect
proofs receiving `pass`), false rejects (correct proofs receiving `fail`), and
their rates over all examples in the respective label class. Incomplete samples
remain in these denominators and are counted separately by label. Coverage is
the completed fraction. Conditional error rates over completed examples are
also reported, with `null` for empty denominators. Cost includes invocation
count, wall time and reported token totals; unavailable usage stays `null`, with
the number of samples missing usage shown explicitly. Fake results test these
calculations and carry `synthetic: true`.
