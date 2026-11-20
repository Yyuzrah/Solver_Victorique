# Approximate calibration intervals

This analysis operates on explicit 0–7 human/machine assessment rows accepted
by `victorique.calibration`, plus a separate JSON list mapping `dataset_id` and
`sample_id` to the original sampling `stratum`. These mappings and human labels
are analysis inputs; they are never sent to a verifier. This offline command
does not start models, select proofs or modify experimental records.

```text
python -I -m victorique.calibration_intervals assessments.json strata.json --out intervals.json
```

The default is 10,000 replicates with seed 20261113. If that takes too long,
pass `--replicates 2000`; the actual value appears in the
report. Output files must be new. Python callers can use
`calibration_intervals(assessments, strata, replicates=10000, seed=20261113)`
from `victorique.calibration_intervals`.

Each replicate draws one Exp(1) multiplier per `(dataset_id, problem_id)`, shared
across all its manuscripts, strata and repeated assessments/configurations.
Weights start at multiplier divided by inclusion probability. Within each
group and sampling stratum, weights are then scaled to the original inverse-
probability weighted assessment total. The statistical unit remains assessment;
repeating assessments does not create independent problem clusters.

Reports retain the existing weighted and unweighted point estimates, adding
95% percentile intervals for sensitivity, specificity, false acceptance, false
rejection and coverage. The sensitivity analysis resamples whole problem
clusters within dataset without sampling weights, sharing each draw across
configurations. These are approximate cluster-calibrated intervals, not exact
finite-population intervals. Transfer from benchmark calibration to generated
proofs requires a separate assumption.

Missing probabilities, missing stratum mappings, or a stratum with fewer than
two problem clusters disable weighted intervals. Fewer than two problem
clusters also disables the unweighted sensitivity interval. Missing judgments
remain missing and stay in coverage denominators. Undefined replicate rates
are counted, never redrawn; if any replicate has an undefined rate, its bounds
are null instead of reporting a conditional-on-defined interval. Descriptive
estimates and coverage remain available. No result in this module is a new
human label or an independently verified mathematical truth.
