# Call telemetry

`calls.jsonl` is the existing per-run projection of claimed requests and captured
CLI results. Export and resume rebuild this projection, so each request appears
once. Reports read these files only; they do not invoke a model, inspect account
files, read prompts or responses, or create a new ledger.

Each row keeps `run_id`, `request_id`, policy, rung, role, status and recorded
start/end timestamps. `transport` identifies the CLI family of the service that
made the call. `completed` means a valid returned response; `failed`,
`returned_invalid`, `returned_invalid_model` and `timed_out` are failures.
Unresolved requests remain `unknown`. Failed calls retain any reported token
counters, including explicit zeroes.

`timed_out` marks a call whose process the transport stopped at the per-call
time limit. When the run's `timeout_retries` setting sends it again, the new
call has the request ID `REQUEST.timeout-retry-N` and `retry_of` names the
request it repeats (`null` for every other call). Each repetition is a row of
its own and counts as a call.

`stopped` marks a call that the run's stop request ended (`victorique stop`, see
[stopping and resuming a run](usage.md#stopping-and-resuming-a-run)): its worker
process stopped the CLI, or did not start it when the
request was already there. Such a call has no result and is not sent again;
reports count it with the unknown calls. `bestofn` accepts a full run's
`calls.jsonl` with such a row as a token reference; the call has no measured
cost, so that run gives no token total, and the target comes from the runs in
`--full-references`, as for a run with an unknown call.

`service` is `solver` for the solver's model service and `checks` for the
separate service of the statement and proof checks (`--check-transport`,
`--check-model`, `--check-reasoning-effort`); a run whose checks use the
solver's service has only `solver` rows. `requested_model` and
`requested_reasoning_effort` are the values the run requested for that service
(`null`: the CLI's default); `model` is the model the CLI reported.

`summary.json` adds `timeout_retries_per_call` (the setting; 0 when
`invocation.json` does not record it), `timeout_retries_used` (rows with `retry_of`) and
`check_service`: `null` when the checks use the solver's service, else their
`transport`, `requested_model`, `requested_reasoning_effort`, `cli_version`,
`reported_model` and number of `calls`. The top-level `reported_model` then
describes the solver's calls only.

## Token fields

Counters are nonnegative integers; missing or invalid counters are JSON `null`.
An absent usage object remains `tokens: null`. Unreported cache or reasoning
usage is never inferred to be zero. The source is recorded as `token_source`.

| Field in `tokens` | Codex | Claude |
| --- | --- | --- |
| `input_tokens` | Reported input, including cached input | Reported fresh input, excluding cache creation and cache reads |
| `cached_input_tokens` | Reported cached subset of input | Reported `cache_read_input_tokens` |
| `cache_creation_input_tokens` | Null unless explicitly reported | Reported cache creation input |
| `input_tokens_including_cache` | Same as input | Fresh + cache creation + cache read, only when all three are known |
| `output_tokens` | Reported total output | Reported total output |
| `reasoning_output_tokens` | Explicit reasoning output | Explicit reasoning output, or `output_tokens_details.thinking_tokens` if supplied |

Use **`input_tokens_including_cache + output_tokens`** for total processed tokens
when both values are known. Cached input and reasoning output are breakdowns;
adding every column would double count. `input_token_semantics` makes the vendor
input convention explicit. Existing `input_tokens` values keep their original
meaning. Aggregate terminal usage is used once; per-message usage and Claude
`modelUsage` are not added to it. Timing does not estimate reasoning tokens.

## Model and observation metadata

`model` is the exact observed CLI model string, with `model_source` identifying
Codex event/header or Claude system-init evidence. Conflicting Codex model
strings yield null. `model_observed_at_utc` is the capture end timestamp (start
if end is unavailable); it dates the observation, not the model release.
`cli_version` comes from the already recorded public CLI profile of the service
that made the call. `configured_model`, `configured_effort` and
`configured_model_source` preserve separate Codex CLI configuration evidence;
in a run with a separate check service they are null on calls of another CLI. Configuration aliases never fill an absent
observed model. Older records may lack the new provenance fields.

## Read-only report

```console
python -m victorique.telemetry_report RUN_DIR_A RUN_DIR_B --start 2026-11-13T01:00:00Z --end 2026-11-13T02:00:00Z
```

The command prints JSON to stdout. Both window boundaries are required together,
must explicitly specify UTC, and must increase. Completion statistics select
calls by recorded end time in the half-open interval **[start, end)**. Completed
calls per hour is the number of selected `completed` calls divided by the full
window duration, including idle time. Without a supplied window, the report
returns null throughput; a first-to-last-call span is not a subscription rate.

`overall`, `by_role` and `by_transport` report completed, failed, timed-out and
unknown counts; missing identities, models, durations and token fields; token
totals; and token and elapsed-time median/p90. Statistics include failed and
unknown calls with available values, and each statistic includes its sample
count. Median uses the usual midpoint for even samples; p90 uses nearest rank
(`ceil(0.9*n)`). A token total is null when any selected call lacks that counter.

Elapsed time is recorded worker-start to process-finish wall time, including CLI
startup, network waits and shutdown. It is not pure model reasoning time. For
fake transport, timestamps instead reflect request claim and result observation.
`peak_concurrency` uses all complete start/end intervals overlapping the window,
including calls that finish after its end. Intervals are clipped to the window;
an end at the exact time of another start does not overlap. Zero-length intervals
contribute no concurrency. Missing or reversed intervals are excluded and counted
in `input_missing_interval_calls`, so the peak covers observed complete intervals.
`input_missing_end_calls` counts input calls that cannot be placed in the window.

Copies across supplied run directories deduplicate by `(run_id, request_id)`.
Identical request IDs from different runs remain distinct. Conflicting copies
fail with an error so a stale projection cannot silently replace a newer one.
Rows without full identity are retained and counted; repeated directory paths
are read once. Missing files and malformed rows produce errors.

Reports describe the supplied run population. A transport label does not identify
a subscription or prove that four calls were continuously in flight. Existing
call files contain no reliable rate-limit indicator; infer neither absence nor
presence of rate limiting from latency. Historical rows without transport or new
token fields remain unknown in those columns; the report does not backfill them.
