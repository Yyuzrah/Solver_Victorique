# Changelog

## 0.2.0a1 (2026-11-19)

First public release, an Alpha. It needs Python 3.11 or newer and was tested
on Windows 10 with Python 3.11.

Approaches:

- `single`, the default: one complete solution, then a statement check and a
  proof check on the same version, each in a fresh context. The proof is
  delivered only if both checks pass. 3 model calls, plus one for each call
  sent again after a timeout.
- `ladder`: the same first solution and checks, a precheck for unfinished
  text, and at most two repairs after a failed check (`--repair-rounds`, 0 to
  100). `--investigate` and `--escalate` (continue with `full`) are off by
  default.
- `resample`: like `ladder`, but a fresh solution instead of a repair.
- `full`: plans, solves in parts, criticises and repairs, then runs the two
  checks; for long problems.
- `bestofn`: at least two independent solutions, a pairwise tournament and the
  two checks on the winner, with a token budget taken from a paired `full` run
  (`--full-calls`).
- A run that delivers no proof (an abstention) names the check that did not
  pass and what it found, and keeps the best draft in `proof.md`, marked as
  not delivered.

Checks and settings:

- One pass rule in every approach: a check passes when it says pass, lists no
  doubt and has only minor findings (none with `minor_findings` set to
  `blocking`).
- The two checks can use another model service, model or reasoning effort than
  the solver: `--check-transport`, `--check-model`, `--check-reasoning-effort`.
- Four settings, as command-line options, in a TOML settings file
  (`--config FILE` or `VICTORIQUE_CONFIG`) or as Python keyword arguments:
  `call_timeout_seconds` (default 600), `timeout_retries` (default 1, 0 to 3),
  `undecided_checks` (`abstain` or `repair`) and `minor_findings`
  (`nonblocking` or `blocking`).
- Default limits per run: 12 model calls and 2 hours.
- `victorique verify` reviews an existing proof in `single`, `stepwise` or
  `multi` mode.
- `victorique bench` solves a collection of problems, with optional reference
  grading in separate model calls.

Web interface:

- `victorique gui` opens a page in the browser for starting runs, following
  them and reading the proof, the check reports and the run files.
- It listens on 127.0.0.1 (port 8765 or the next free port) unless started
  with `--allow-remote`, and the page loads nothing from other servers.

Stop and resume:

- `victorique stop DIR`, `victorique.stop(DIR)` and the Stop run button: the
  run sends no new call, abandons the call in progress and keeps its best
  draft.
- `victorique resume DIR` and the Resume button continue a run whose process
  ended early, within its original limits. A finished run sends no call when
  resumed.

Model services:

- Codex CLI and Claude Code CLI, installed and logged in by the user.
  Victorique opens no login or key file, and it removes `ANTHROPIC_API_KEY`
  from the environment of the Claude Code process.
- `VICTORIQUE_CODEX_BINARY` and `VICTORIQUE_CLAUDE_BINARY` name the native
  executables.
- `--transport fake` makes a practice run that calls no model.

Records:

- One folder per run with the problem, the settings (`invocation.json`), the
  proof or best draft (`proof.md`), a summary (`summary.json`), one line per
  model call with its times, status and tokens (`calls.jsonl`), and the full
  exchange with the model service.
- `victorique status` and `victorique export`; a read-only report of
  throughput and costs (`python -m victorique.telemetry_report`).
- Offline modules: paired runs of `single`, `ladder` and `resample` that share
  the first solution and its checks (`victorique.shared_prefix`), statistics
  for comparing approaches (`victorique.analysis`), and calibration against
  human grades (`victorique.calibration`, `victorique.calibration_intervals`).

Fonts and licenses:

- Code under Apache-2.0, with a NOTICE file.
- The web interface bundles ten WOFF2 subsets of Michroma, Barlow Semi
  Condensed and JetBrains Mono, each under the SIL Open Font License 1.1. The
  three license texts are in `victorique/gui/static/`; the package license is
  `Apache-2.0 AND OFL-1.1`.

Documentation:

- README, a getting-started page in Chinese and INSTALL.md, the step-by-step
  installation, also as an instruction for a coding agent.
- Guides to usage, the web interface, each approach, `verify`, `bench`,
  telemetry, the Codex CLI and Claude Code CLI services, the offline modules,
  plan dependency assumptions, session transaction performance and the study
  results of November 2026.
