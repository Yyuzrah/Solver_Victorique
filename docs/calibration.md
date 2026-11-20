# Offline grader calibration

`victorique.calibration` compares existing human and machine scores. Supply an
explicit CSV file, JSON list, or Python iterable of dictionaries. The module
reads that input alone and performs no model calls. It accepts labels exported
from external datasets such as IMO-GradingBench, ProofBench, and OPC after they
have been mapped to the contract below.

```python
from victorique.calibration import calibrate, read_assessments

rows = read_assessments("assessments.csv")
report = calibrate(rows)  # both thresholds default to 7
robustness = calibrate(rows, human_threshold=5, machine_threshold=5)
```

The module CLI uses the same API:

```console
python -m victorique.calibration assessments.csv --out calibration.json
python -m victorique.calibration assessments.json --human-threshold 5 --machine-threshold 5
```

Without `--out`, JSON is printed to stdout. An output path must be a new file in
an existing directory. Input files and existing reports are preserved.

## Input contract

Each row represents one assessment of one proof sample. All fields below except
the three marked optional are required. CSV uses these names as unique headers;
JSON uses a list of objects. Unknown fields are rejected. Keep proof text and
other private material outside this table.

| Field | Meaning |
| --- | --- |
| `dataset_id` | Explicit dataset/version or analysis stratum identifier |
| `problem_id` | Problem cluster identifier within the dataset |
| `sample_id` | Proof identifier within the dataset; multiple proofs may share a problem |
| `assessment_id` | Globally unique identifier for this grader attempt in the input |
| `grader_config` | Explicit configuration/version identifying prompt, rubric and other grader settings |
| `grader_model` | Actual grader model/version reported by the assessment event; null / blank when unknown |
| `grader_family` | Grader family declared by the calling configuration |
| `grader_role` | `primary` or `secondary`, supplied by the caller |
| `human_score` | Human integer score 0–7, or JSON null / blank CSV cell when unavailable |
| `machine_score` | Machine integer score 0–7, or null / blank when unavailable |
| `status` | `completed`, `missing`, `timeout`, `invalid`, or `error` |
| `inclusion_probability` | Optional probability in (0, 1] that this proof sample was selected |
| `configured_model` | Optional separately observed configured model, supplied together with its source |
| `configured_model_source` | Optional source of that configuration observation, e.g. `config/read` |

Identifiers are nonempty strings without surrounding whitespace, except that an
unknown actual `grader_model` is null / blank. All scores and
thresholds are integer values from 0 through 7. JSON null and blank CSV cells are
the only missing-score representations. A non-completed status requires a null
machine score. A completed row with no recorded machine score remains missing.
Invalid raw model output is represented by `status=invalid, machine_score=null`.
Human labels remain null until an explicit human score is provided.

Assessment IDs must be unique even across groups. Repeated assessments of a
sample retain its problem ID, human score and selection probability consistently.
For adjudication or label versions, use explicit dataset/version identifiers.
The caller supplies the primary/secondary designation and family provenance;
the module groups those declarations without inferring them from model names.
A family declaration describes the calling configuration and does not establish
which backend model actually ran. A configured model observed separately through
`config/read` stays in `configured_model` with `configured_model_source`; it never
fills an unknown `grader_model`. Those optional observations are retained on the
assessment rows, while actual model availability remains explicit in each group.

Minimal synthetic JSON example:

```json
[
  {"dataset_id":"fixture-v1","problem_id":"p1","sample_id":"proof1",
   "assessment_id":"a1","grader_config":"rubric-v1","grader_model":"fixture-v1",
   "grader_family":"fixture","grader_role":"primary","human_score":7,
   "machine_score":7,"status":"completed","inclusion_probability":0.5},
  {"dataset_id":"fixture-v1","problem_id":"p1","sample_id":"proof2",
   "assessment_id":"a2","grader_config":"rubric-v1","grader_model":"fixture-v1",
   "grader_family":"fixture","grader_role":"primary","human_score":0,
   "machine_score":null,"status":"timeout","inclusion_probability":0.5}
]
```

## Output and denominators

Each group has its own dataset, grader configuration, model, family, role, human
threshold and machine threshold. Separate API invocations give separate threshold
analyses; the threshold pair is included in every group. Positive means score
greater than or equal to its threshold; at the default threshold only 7 is
completely correct.

`unweighted` contains assessment counts `tp`, `tn`, `fp`, and `fn`. For records
with both a human label and a completed machine score:

| Metric | Formula |
| --- | --- |
| sensitivity | TP / (TP + FN) |
| specificity | TN / (TN + FP) |
| false_accept_rate | FP / (TN + FP) |
| false_reject_rate | FN / (TP + FN) |

A zero denominator yields null. These rates are conditional on paired observed
scores. Missing results contribute to `total`, `unpaired`, missing counts and
status counts. `coverage` is paired / total; `human_coverage` and
`machine_coverage` report each source's availability. `positive_coverage` and
`negative_coverage` report machine availability among all human-positive and
human-negative assessments. `missing_machine_positive` and
`missing_machine_negative` expose each class's denominator gap. Unavailable
human labels contribute to `missing_human`; unavailable values never become a
score of zero or a confusion-matrix error. Missingness can affect observed rates,
so coverage travels with every calibration estimate.

When every row in a group has a selection probability, `weighted` repeats the
statistics with each row weighted by `1 / inclusion_probability`. Counts become
inverse-probability totals and rates become ratios of those totals. Probabilities
and their inverses must be finite. When any row lacks a probability, `weighted`
is null and `weighted_reason` explains the gap. `unweighted` always stays the
same. The caller is responsible for supplying probabilities from the actual
sampling design; a targeted sample without known probabilities uses the
unweighted descriptive report.

The report declares `count_unit=assessment` and
`cluster_unit=dataset_id/problem_id`. Each group preserves a `clusters` mapping
from problem ID to sample ID to assessment IDs, plus distinct problem and sample
counts. The normalized `assessments` retain scores, status, and probabilities.
Thus repeated assessments and multiple proofs of the same problem remain linked.
The four-cell counts describe assessments; they are not an independent sample
size. Groups with uneven repeated-assessment counts describe that assessment mix.
Inverse selection weights address sample selection, while repeated assessments
still require a prespecified analysis at the problem/sample level.

Rogan–Gladen correction, PPI, uncertainty intervals and problem-cluster bootstrap
are not part of this module; see [offline analysis](analysis.md) and
[calibration intervals](calibration-intervals.md). Application to another dataset
requires an explicit decision about population and grader compatibility;
calibration estimates here are tied to the reported dataset and configuration.
