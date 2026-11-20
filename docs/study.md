# Study results

The [README](../README.md#compared-with-one-direct-model-call) summarizes the
comparison with one direct model call.

A study in November 2026 ran every policy once per problem-round
on 72 proof problems over four rounds, 288 problem-rounds per policy: 36
Nemotron tasks, 30 IMO-ProofBench Advanced problems and the six USAMO 2026
problems. All calls used the Codex CLI with gpt-6-astra at the CLI's default
reasoning effort. `single`, `ladder` and `resample` share each first draft and
its two checks. A problem-round counts as solved when the policy delivered a
proof that the reference grader scored 7/7. The grader belongs to the solver's
own model family, so these rates are provisional; grading by another model
family and by human experts has not been done.

| Policy | Solved | 95% CI | Calls per problem-round | Tokens per problem-round | Median time per problem-round |
| --- | ---: | --- | ---: | ---: | ---: |
| one direct call, no checks | 276/288 (95.8%) | 91.7–99.3 | 1.00 | 16,684 | 106 s |
| single | 271/288 (94.1%) | 88.9–98.3 | 2.91 | 44,149 | 145 s |
| ladder | 271/288 (94.1%) | 88.9–98.3 | 3.06 | 46,644 | 146 s |
| resample | 273/288 (94.8%) | 89.6–99.0 | 3.04 | 46,664 | 146 s |
| full | 277/288 (96.2%) | 92.4–99.0 | 8.06 | 138,263 | 562 s |
| bestofn | 256/288 (88.9%) | 82.6–94.4 | 6.71 | 106,305 | 501 s |

Intervals resample problems (10,000 draws). The direct call is the shared first
draft delivered without checks, a counterfactual from the recorded drafts and
their grades. Tokens are means of input, including cached input, plus output
over the policy's own calls; reference grading is not included. Time runs from
the first call's start to the last call's end. Every execution failure was a
600-second call timeout. With this model the first draft usually passed both
checks (271 of 288 shared first drafts), so repair and regeneration were rarely
triggered. `ladder` minus `full` was −2.1 percentage points (95% CI −5.2 to 0.7)
with about a third of the tokens. `bestofn` minus `single` was −5.2 points (−8.3
to −2.4): each of its 16 failed runs had a timed-out generation, and 9 of them
discarded a completed solution, a defect fixed in 0.1.0b2.

The same checks were audited on 500 proofs with expert grades, 300 from
IMO-GradingBench and 200 from ProofBench. On the 300 GradingBench proofs,
standalone `verify` with one review rejected 14 of the 89 correct proofs
(15.7%) and accepted 18 of the 210 incorrect ones it decided (8.6%). The two
checks inside the solving loop rejected 23 of the 89 (25.8%) and accepted 14 of
the 211 (6.6%), counting undecided verdicts as rejections. Their balanced
accuracy, the mean of the share of correct proofs accepted (66/89) and the share
of incorrect proofs rejected (197/211), is 0.84. Over all 125 correct
proofs, both procedures rejected the same 23 of the 106 that both decided;
counting undecided verdicts as rejections, the in-loop checks rejected 42 and
standalone `verify` 27. In the audit runs, 94 of 98 invalid check reports said
pass while listing a finding or a doubt, and the report contract discarded
them. Since 0.1.0b2 such a report is kept: by default it passes when all its
findings are minor (the `minor_findings` setting can count those findings as
blocking), and a listed doubt still prevents a pass.

The tables were computed from the run records without model calls; the
records and the scripts that computed the tables are not published. The study
ran before the 0.1.0b2 fixes. A recorded example run is in
[examples/proofbench-029-single](../examples/proofbench-029-single/proof.md).
