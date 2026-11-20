# Offline analysis foundation

`victorique.analysis` is a standard-library-only API and JSON CLI. It reads
explicit analysis records, never opens run directories or invokes a model.
These are statistical building blocks for comparing approaches; the caller
fixes the main endpoint, populations, primary comparisons, sampling designs,
multiplicity families and decision rules. Software output does not authorize
or constitute a confirmatory experiment.

## Input and invocation

Run `python -m victorique.analysis request.json --output report.json`.
For `analyze`, add `--svg observed.svg` to plot measured points. Without
`--output`, JSON goes to stdout with ASCII Unicode escapes for legacy consoles;
output files preserve UTF-8 text. Both decode to the same JSON values.
Invalid or unsupported input exits with code 2.
There is no new subcommand in the main solver CLI. The JSON envelope dispatches
to the identically named Python function with keyword arguments:

```json
{
  "operation": "analyze",
  "arguments": {
    "rows": [
      {"problem_id": "synthetic-1", "arm": "a", "repetition": "1",
       "delivered": true, "correct": true, "tokens": 10},
      {"problem_id": "synthetic-1", "arm": "b", "repetition": "1",
       "delivered": false, "correct": null, "tokens": 20}
    ],
    "comparisons": [{"arm_a": "a", "arm_b": "b"}],
    "draws": 10000,
    "seed": 20261115,
    "confidence": 0.95
  }
}
```

The Python equivalent is `analysis.analyze(rows, comparisons=[...], draws=10000,
seed=20261115, confidence=0.95)`. Functions also available as JSON operations:

| Operation | Required arguments | Additional arguments |
| --- | --- | --- |
| `paired_bootstrap` | `rows`, `arm_a`, `arm_b` | `metric`, `operation`, `draws`, `seed`, `confidence` |
| `token_efficiency` | `rows`, `arm` | `draws`, `seed`, `confidence` |
| `sign_flip` | `differences` | `draws`, `seed`, `alternative` |
| `wilson` | `successes`, `total` | `confidence` |
| `holm` | `p_values` (named mapping) | none |
| `diagnostics` | `rows` | none |
| `pareto_points` | `points` | none |
| `rogan_gladen` | `q`, `sensitivity`, `specificity` | `minimum_denominator` |
| `rogan_gladen_bootstrap` | `calibration_rows`, `target_rows` | `draws`, `seed`, `confidence`, `minimum_denominator` |
| `ppi_iid_mean` | `labeled_rows`, `unlabeled_rows`, `design` | `confidence` |

Supply one provider and declared task population per analysis. Split providers,
full/development-excluded/strict-holdout sets before calling; no strata are
inferred from names. Keep the complete planned roster, including unknown,
failed and abstained runs. Each row requires nonempty string `problem_id`,
`arm`, `repetition`; explicit `delivered` and `correct` booleans or null; and
explicit nonnegative finite `tokens` or null. `correct` is the independently
graded binary endpoint (for example 7/7); use separately labeled input for a
≥5/7 robustness endpoint. Preserve the original
candidate/version and grading provenance in the upstream table. The analysis
module cannot authenticate those labels or reconstruct omitted planned runs.
For policy cost comparisons, `tokens` is the measured logical arm total of
input including cache plus output; shared-prefix usage is charged to each
arm, and missing components make the total unknown. Keep physical shared
cost and separate grading cost in their own upstream columns.

Duplicate `(problem_id, arm, repetition)` tuples are errors. Every problem
must contain the same arms, and each arm must have the same repetition IDs
within that problem. Different problems may have different repetition counts.
An unexecuted planned run needs an explicit row with null values; an absent
arm/repetition is an error. `weight`/`weights`, `inclusion_probability`,
`sampling_probability`, `sampling_weight`, and `sampling` are rejected: survey weighting,
stratified population estimates and more complex sampling designs are not
implemented. Unknown metadata fields are preserved by callers, not used to
invent labels. Optional `endpoint` strings produce separate endpoint counts
(for example `execution_failed`, `incomplete`, `abstained`). `known_nondeliveries`
counts D=false without classifying execution failures as deliberate abstentions;
the explicit endpoint counts preserve that distinction.

## Problem clusters, missingness and intervals

The pairing slot is problem/repetition. Each bootstrap draw samples problems
uniformly with replacement, keeping all repetitions and arms together.
Repetitions are averaged within each problem before computing the equally
weighted problem mean. Thus a problem with eight repetitions does not receive
twice the influence of one with four. Duplicated bootstrap clusters retain
their multiplicity. `problem_mean_bounds` reports that estimand;
`run_counts` explicitly reports the separate pooled-run descriptive counts.

Supported metrics are `correct_delivery` (D×Y), `delivery` (D),
`draft_correctness` (Y), and `tokens`. Paired operations are A−B and A/B,
with A/B defined only when the denominator is positive. Bootstrap intervals
use linearly interpolated empirical percentiles. Output contains a two-sided
interval and separate one-sided lower/upper bounds at the stated confidence.
The default is 10,000 draws, seed 20261115, confidence 0.95. Small or constant
samples can yield degenerate intervals; the software does not claim adequate
power or representativeness for purposively sampled mathematical problems.

A missing binary value has bounds [0,1]. If D is known false, D×Y is known
zero even when the abstained draft's grade is unknown; Y remains unknown.
The report gives point estimates only when identified, along with lower/upper
missing-result bounds and their bootstrap distributions. Those bound intervals
are sensitivity summaries, not a newly derived simultaneous confidence region.
`complete_problem_pairs` records completeness; an incomplete problem is never
silently dropped. Sign-flip inference on an incomplete comparison is explicitly
unsupported. Unknown delivery makes delivery precision unavailable because its
numerator and denominator covary.

Unknown usage is never interpreted as zero observed cost. Reports show its
missing count, known subtotal, and null total. A nonnegative cost lower bound
is zero; the upper bound is unbounded (JSON null) unless the caller supplies
an actual `token_upper_bound`. This optional bound must dominate observed
usage; it must not be a forecast presented as a hard ceiling. Null upper
bounds and zero ratio denominators remain explicit. If any bootstrap ratio
draw is undefined, its interval is null with valid/invalid draw counts;
the tool does not condition the interval on surviving draws.

The three abstention measures always appear together in `run_counts`:
sum(D×Y)/N, sum(D)/N, and sum(D×Y)/sum(D). The last is undefined when no
proofs are delivered. Missing grades give best/worst bounds. The separately
named `available_delivery_precision` uses known grades among known deliveries;
`available_draft_correctness` and `available_correct_delivery_rate` similarly
use observed labels or identified D×Y outcomes. Their coverage is reported,
and they are descriptive only, not replacements for planned-population results.

## Tests and decisions

`sign_flip` consumes one difference per problem. It retains zero differences
in the reported paired count, enumerates all sign assignments for at most 20
nonzero differences, and otherwise uses seeded Monte Carlo with
`(extreme+1)/(draws+1)` (default 99,999 draws). Alternatives are `two-sided`,
`greater`, and `less`. This test requires the relevant sign symmetry or
exchangeability under the null; pairing alone does not establish it.
`analyze` applies the two-sided test to complete difference comparisons.

`wilson` is for a simple independent binomial proportion, not repeated runs
sharing a problem. No observations gives null estimate/interval. `holm`
returns monotone Holm-adjusted p-values for a supplied mapping of comparison
names to p-values. The caller must freeze and record the family; unrelated
families or missing comparisons are not automatically combined or inferred.

For a noninferiority-and-cheaper comparison of ladder with full, consult a
one-sided 95% lower bound for ladder−full (>−0.10) and a one-sided 95% upper
bound for ladder/full token cost (<1). Both must pass. The module does not
issue a confirmatory conclusion or choose the multiplicity correction for two
primary claims.

## Explicit mechanism labels and measured efficiency

`diagnostics` consumes optional nullable `initial_correct`, `final_correct`,
and `triggered` booleans. Initial correctness is an independently supplied
label; a trigger is never treated as proof of an error. True positive means
triggered on an initially incorrect candidate. Precision/recall use only
explicitly labeled pairs and report labeled/missing counts. They are
descriptive for that labeled subset if annotation is incomplete or targeted.
The four transition cells require both correctness labels and nonempty
`initial_version`/`final_version` identifiers bound to those labels. Conflicting
correctness labels for the same version are rejected. Use a separate table
per repair/investigation step; do not collapse intermediate transitions.

`token_efficiency` bootstraps correctness and cost jointly within each problem,
then reports the ratio of their equally weighted problem means: correct
deliveries per million measured tokens and measured tokens per correct
delivery. At equal repetition counts these equal the corresponding pooled
totals. No correct delivery makes the latter undefined; missing usage or
unidentified quality makes both unknown. Counts and known cost subtotal remain
available. These describe the observed policies, not outcomes at untested
budgets. `pareto_points` accepts observed `{arm, cost, quality}` points;
`analyze` uses known mean token cost and correct-delivery quality and lists
omitted incomplete arms. `pareto_svg` draws those points, labels, and axes,
without fitted lines or interpolated equal-budget performance. Tied points
remain separate; strict dominance marks frontier membership.

## External calibration correction

`rogan_gladen` reports raw `(q + specificity - 1) / (sensitivity + specificity - 1)`.
It never clips to [0,1]. A zero denominator gives null; a negative denominator
still exposes the algebraic raw estimate with a nonpositive flag. A positive
denominator below `minimum_denominator` (default 0.05, an explicit diagnostic
setting rather than a scientific universal) is flagged unstable. Out-of-range
estimates are also flagged. Fixed sensitivity/specificity input explicitly
sets `calibration_uncertainty_propagated=false`. Interpretation requires the
error rates to transfer to the target population; the formula itself cannot
establish transfer. See [Rogan and Gladen (1978)](https://pubmed.ncbi.nlm.nih.gov/623091/).

`rogan_gladen_bootstrap` propagates calibration sampling error using the raw
rows, independently resampling calibration and target problem clusters.
Calibration rows need `problem_id`, unique `sample_id`, binary `label` and
`predicted`; target rows need `problem_id`, unique `sample_id`, and binary
`predicted`. Repeated observations within a problem remain together, and
problem means receive equal weight. Sensitivity/specificity are the ratios
of problem-mean joint and class indicators. This explicitly unweighted
estimator is not suitable for a weighted or disproportionate stratified
calibration design. The [external calibration](calibration.md) API can
compute IPW point estimates from `inclusion_probability`, but this independent
RG bootstrap does not directly support that design and rejects those rows.
[Calibration intervals](calibration-intervals.md) gives weighted intervals for
sensitivity and specificity; no weighted interval for the Rogan–Gladen
correction is implemented.

Missing labels/predictions are rejected, not dropped or filled. Calibration
and target problem IDs must be disjoint; dependent or overlapping calibration
designs are not supported by this independent two-sample bootstrap. The
caller must still establish independence and representative sampling.
Every draw recalculates sensitivity, specificity and target q. Missing-class,
unstable/nonpositive-denominator and out-of-range draws are counted.
`raw_interval` is an algebraic percentile summary, retaining finite flagged
values; undefined draws suppress it. `usable=false` if the point or any draw
has a diagnostic issue. Sampling uncertainty does not quantify domain shift:
transfer sensitivity analyses and overlap disclosure remain necessary.

## Optional PPI++ IID mean special case

This implements only the IID mean example of
[Angelopoulos, Duchi and Zrnic, PPI++, Example 6.1](https://arxiv.org/html/2311.01453v2).
It is optional and never part of the default strategy comparison. Labeled
rows require a unique `problem_id`, finite numerical `label` and `prediction`;
unlabeled rows require a unique `problem_id` and finite `prediction`.
At least two observations are required in each group. There must be exactly
one observation per independent problem and no overlap across groups.

The required design declaration is exactly:

```json
{"labeled_sampling": "random_target", "unlabeled_sampling": "iid_target", "independent": true}
```

It asserts random target-population labeling and independent IID unlabeled
observations from the same target population, with a fixed prediction rule
independent of these samples. The declaration is not evidence that these
assumptions hold. External benchmark labels, targeted disagreement audits,
repeated problems, weights, clustered/stratified designs, learned prediction
rules fitted on the analysis sample, and general PPI++ estimands are not
supported. Do not relabel an external or targeted sample as random target data.

For labeled sample size n and unlabeled size N, the formula is

```
lambda = cov_n(y, fL) / ((1 + n/N) * var_(n+N)(fL concatenated with fU))
theta  = mean(y) + lambda * (mean(fU) - mean(fL))
```

The empirical covariance and pooled variance here use divisors n and n+N,
respectively. Lambda is not clipped; an anticorrelated predictor may produce
a negative lambda. Zero pooled prediction variance explicitly returns the
classical labeled-sample mean with `classical_fallback_zero_prediction_variance`.
The plug-in standard error is
`sqrt(sample_var(y-lambda*fL)/n + lambda**2*sample_var(fU)/N)`.
The accompanying normal interval is labeled **asymptotic**, has no claimed
finite-sample guarantee, and may extend outside the outcome's support. It is
not a problem-cluster bootstrap interval.
