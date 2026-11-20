# Web interface

`victorique gui` serves a web page on this computer. From the page you can start
a run, follow its progress and read its result without typing commands.

```powershell
& $victoriquePython -I -m victorique gui
& $victoriquePython -I -m victorique gui --runs-dir C:\Work\proofs\runs --port 8766 --no-browser
```

`$victoriquePython` is the virtual environment's interpreter, as set in the
[terminal examples of INSTALL.md](../INSTALL.md#terminal-examples); the venv's
`victorique gui` command works the same way.

The command prints the page address (by default `http://127.0.0.1:8765/`) and
opens it in the default browser. Press Ctrl+C in the terminal to stop the
interface. Runs in progress keep going: each one ends at its own call budget or
time limit, or when you stop it with Stop run on its run page or with
`victorique stop DIR`, and starting the interface again shows them. The
messages the command prints when it starts and when it stops say this too.

Without `--port`, the interface listens on port 8765. When another program
holds that port, it tries 8766 to 8785 in turn and then a port the system
assigns, prints a line that names the taken ports, and prints and opens the
address it uses. A port given with `--port` is used as given; when it is taken,
the command stops with exit status 2 and asks for another `--port`.

| Option | Default | Meaning |
|---|---|---|
| `--host` | `127.0.0.1` | Address to listen on. Only this computer can connect. |
| `--port` | `8765`, else the next free port up to 8785, else any free port | Port to listen on; `0` picks a free port. |
| `--runs-dir` | `runs` | Folder that holds the runs, the same default as `victorique solve`. |
| `--no-browser` | off | Do not open a browser window. |
| `--allow-remote` | off | Allow a `--host` that other computers can reach. |

The command creates the runs folder when it is missing and checks that it can
write a new file there. When the folder cannot be created or written, it prints
one line that names the folder and the reason, for example
`victorique: cannot create or write the runs folder C:\Work\runs (Access is denied); choose another folder with --runs-dir`,
and stops with exit status 2.

An address other than a loopback address, such as `0.0.0.0` or a network
address, is refused unless `--allow-remote` is given, because anyone who can
reach the page can start runs that use your model account.

## New problem

| Field | `victorique solve` option | Notes |
|---|---|---|
| Title | none | Kept by the interface for the run list. Optional. |
| Problem statement | `PROBLEM` | Saved as UTF-8 exactly as typed; Markdown and LaTeX are plain text. At most 256,000 bytes. |
| Approach | `--policy single`, `ladder` or `full` | `single` is listed first and preselected: one solution, a statement check and a proof check, 3 model calls plus one for each call sent again after a timeout. |
| When a check is undecided | `--undecided-checks abstain` or `repair` | Ladder only. Abstain or Repair once; the form preselects the settings file's `undecided_checks`, else Abstain. See below. |
| When a check passes with minor findings | `--minor-findings nonblocking` or `blocking` | Every approach. Count it as passed (`nonblocking`) or Count it as not passed (`blocking`); the form preselects the settings file's `minor_findings`, else Count it as passed. See below. |
| Model service | `--transport codex`, `claude` or `fake` | `fake` calls no model and returns fixed example answers. For Claude Code CLI the help asks for a login with an Anthropic Console account (see below). |
| Model | `--model` | Empty uses the command-line tool's default model. |
| Reasoning effort | `--reasoning-effort` | Codex CLI only. |
| Checks: Check model service | `--check-transport codex`, `claude` or `fake` | Same as the solver (the default) passes nothing. The statement check and the proof check use this service; every other step uses the model service above. Practice run goes only with Practice run. |
| Checks: Check model | `--check-model` | Empty: the solver's model when the checks use the solver's service, else the tool's default model. |
| Checks: Check reasoning effort | `--check-reasoning-effort` | Only when the checks use the Codex CLI. Empty: as for the check model. |
| Maximum model calls | `--calls` | 2 to 100, default 12. |
| Time limit for the whole run | `--hours` | Default 2 hours. |
| Time limit per model call | `--call-timeout-seconds` | 0.05 to 86400 seconds. Empty passes nothing, so the command's default applies: 600 seconds, or `call_timeout_seconds` from the settings file named by `VICTORIQUE_CONFIG`. A call also ends at the run's time limit. |
| Retries after a call timeout | `--timeout-retries` | 0 to 3. The form shows the command's default, `timeout_retries` from the settings file named by `VICTORIQUE_CONFIG`, else 1, and passes the value shown; an emptied field passes that default. Each repetition is a new call within the call budget and the run's time limit. |

When Claude Code CLI is chosen for solving or for the checks, the field's help
says to log Claude Code in with an Anthropic Console account (run `claude`, type
`/login` and choose the Anthropic Console account). Anthropic's
[legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance)
(checked 2026-11-19) keeps the Claude.ai subscription login (Free, Pro, Max,
Ultra) for ordinary use of Anthropic's own apps, asks developers who build
products to use an API key, and does not allow third-party developers to route
requests through subscription credentials on behalf of users. Victorique
removes `ANTHROPIC_API_KEY` from the environment of the Claude Code process, so
Claude Code runs with the login stored by `/login`.

A check is undecided when it neither passes nor reports a serious or fatal
problem (an inconclusive check: no clear verdict, open questions, or a fail with
only minor remarks), or when it returns no usable report after one retry (an
incomplete check). With Abstain the run stops there without a proof. With
Repair once the first undecided check of the run is treated like a failed
check: the draft goes to one repair, and a later undecided check stops the run.
The form preselects the value the `solve` command would use on its own:
`undecided_checks` from the settings file named by `VICTORIQUE_CONFIG`, or
Abstain when the variable is not set, the file does not set the value or the
file cannot be read (the field's help then names the error). `/api/options`
reports this value as `undecided_checks.default`, with its `source`
(`settings file` or `built-in`), the file and any error. For a ladder run the
interface always passes the choice shown, so a choice changed on the form takes
precedence over the settings file; a form sent without a choice uses the
preselected value. `single` and `full` do not use the setting, and for them the
field is inactive and nothing is passed. The
[settings section of the usage guide](usage.md#settings) describes all the
settings.

A check can say pass and still list minor findings, such as unclear wording.
The field's help on the form says in two sentences what each choice does and
points to this guide for the replay below.
With Count it as passed (`nonblocking`, the default) the check passes and the
findings are only recorded. With Count it as not passed (`blocking`) the check
does not pass and its minor findings count like serious problems: `ladder` and
`full` send the draft to repair, and `single` delivers no proof. A check that
says fail with only minor remarks, or pass with open questions, stays undecided
under both choices. A replay judged one pair of checks, without repair, on 500
proofs with human grades (125 correct, 375 wrong): Count it as passed, the
0.1.0b2 rule, stopped 27 correct proofs and delivered 23 wrong ones. On the same
proofs 0.1.0b1 recorded 42 and 18; it counted a check that said pass and listed
findings as an invalid report. It also applied the old precheck, which stopped
9 of the proofs (1 correct, 8 wrong); each of the 9 also failed at least one of
the two checks, so the precheck does not change the 42 and 18. Count it as not
passed reads a check that says pass with findings the same way as 0.1.0b1,
without being identical to 0.1.0b1, and the replay has no separate count for
it; the [ladder policy](ladder.md#minor-findings) has the details.
The form preselects
`minor_findings` from the settings file named by `VICTORIQUE_CONFIG`, or Count it
as passed when the variable is not set, the file does not set the value or the
file cannot be read (the field's help then names the error). `/api/options`
reports this value as `minor_findings.default` (also `defaults.minor_findings`),
with its `source`, the file and any error. Every approach uses the choice, so
the interface always passes the choice shown, and a choice changed on the form
takes precedence over the settings file.

The runs start in the runs folder, so the interface passes a relative
`VICTORIQUE_CONFIG` to them as an absolute path, resolved against the folder
where `victorique gui` was started.

The interface reads `victorique solve --help` when it starts. The per-call field
is active when the help lists `--call-timeout-seconds` (or else exactly one
option whose name contains `timeout`), the undecided choice when it lists
`--undecided-checks`, the minor-findings choice when it lists
`--minor-findings`, the retries field when it lists `--timeout-retries`, and
the three check fields when it lists `--check-transport`, `--check-model` and
`--check-reasoning-effort`. With an older `solve` that has none of these
options, every call may take up to 600 seconds, a timed-out call is not sent
again, the checks use the solver's model service, an undecided check always
stops the run without a proof, and a check that passes with only minor findings
counts as passed. `/api/options` reports the retries default as
`timeout_retries.default`, with its `source` (`settings file` or `built-in`),
the file and any error, as for `undecided_checks`.

Start checks the values, saves the statement to
`RUNS/.victorique-gui/NAME/problem.md` and runs
`python -P -m victorique solve problem.md ... --out RUNS/NAME` in a separate
process, with the runs folder as its working directory. That command freezes
the budget and deadline, chooses each step and runs the checks; the interface
makes no model calls itself.

Every command the interface starts, including the help it reads, runs in the
runs folder with Python's `-P` option, which keeps the working directory off the
module search path. A folder or file named `victorique` in the folder where you
start the interface, or in the runs folder, therefore cannot take the place of
the installed package. When the interface itself runs with `-I`, as in the
commands above, its commands use `-I` as well.

## Runs

The list shows every folder in the runs folder that contains `invocation.json`,
including runs started with `victorique solve` in a terminal, together with
submissions that are still starting or could not start. It updates by itself.

| Status | Meaning |
|---|---|
| Starting | The command runs but has not created the run folder yet. |
| Running | The run's process is running. A run that this interface did not start, and that has no `summary.json`, counts as running until 15 minutes after its own time limit. |
| Stopping | A running run whose folder holds a stop request (`stop-request.json`), written by Stop run or `victorique stop`. |
| Proof delivered | `summary.json` outcome `complete`: both checks passed on the same final version. |
| No proof delivered | Outcome `abstain`. The run page shows the reason. |
| Stopped by you | The run ended at its stop request: outcome `partial`, end reason `stopped_by_user`. |
| Failed | The command stopped with an error, shown on the run page, or the run ended without a decision (outcome `partial`). |
| Stopped before finishing | The run was interrupted (outcome `pending`), its process ended without a summary, or it passed its time limit without a summary. `victorique resume RUNS/NAME` continues it; when it has no summary, so does the Resume button on its page. |

Every status tag has a Chinese caption: 启动中, 运行中, 停止中, 已交付, 未交付,
已停止, 失败 and 中途停止.

## Run page

- **Progress** while the run is going: the latest step from the command's
  progress messages, and one row per model call from `calls.jsonl`. The page
  refreshes every two seconds.
- **Stop run**, while the run is running. The button first asks for a
  confirmation: the current model call is abandoned, the run ends without a
  proof, and the best draft is kept. Stop the run then sends
  `POST /api/runs/NAME/stop`, which the server checks like the form (the page
  token, the Origin and the Host). The server records the request with the
  function behind `victorique stop`, which writes `RUNS/NAME/stop-request.json`.
  The page then shows Stopping until the run ends, and the end as Stopped by
  you. The run sends no new model call; the worker process of the call in
  flight stops the CLI, as at the per-call time limit, and that call shows as
  Stopped in the model calls, with an outlined bar. A run that has finished or
  has not created its folder yet takes no request (the server answers 409).
- **Resume**, for a run whose process ended without writing `summary.json`
  (status Stopped before finishing, or Failed after an error). The button sends
  `POST /api/runs/NAME/resume`, checked like the form, and the server starts
  `python -P -m victorique resume RUNS/NAME` in a separate process with the runs
  folder as its working directory, as for a run started from the form. Its
  output is added to `console.log`, `request.json` records the new process
  (`resume_command`, `pid`) and the earlier `exit.json` is removed, so the page
  follows the run like a running one. A run that has a stop request ends at once
  without a new model call. A run past its own time limit ends at once and
  writes its summary.
- **Run status**: two gauges, the model calls used of the call budget and the
  time used of the run's time limit, and the settings of the run: approach,
  model service, model, reasoning effort, the time limit per model call, the
  retries after a call timeout (per call, and how many the run used), for
  ladder and resample runs the choice for undecided checks, and the choice for
  checks that pass with minor findings, as recorded in `invocation.json` (a
  setting missing from it shows nothing). When the checks used
  another model service, model or effort than the solver, the check model
  service, the check model (as the CLI reported it, from `summary.json`
  `check_service`) and the check reasoning effort are listed as well.
- **Delivered proof** from `proof.md`, shown as plain text. When no proof is
  delivered, the best draft is shown separately and marked as not delivered. On
  a window at least 1200 pixels wide, the proof or draft and the checks are
  shown side by side.
- **Model calls**: one row per call with its step, status, duration and tokens,
  and a bar on a common time axis from the start of the run that shows when the
  call ran. A call that timed out has an outlined bar, and the call that sent it
  again is marked "sent again after a timeout". A call made by a separate check
  service names that service.
- **Checks and critiques**: each statement check, proof check and critique with
  its verdict (Passed, Did not pass, No clear verdict, or Undecided for a check
  that said pass with open questions or fail with only minor remarks), its
  findings and its open questions. Each finding shows its severity (Fatal,
  Serious for `material`, Minor), its kind, the claim and the quoted text. Fatal
  and serious findings block delivery; minor ones are only recorded. In a run
  with Count it as not passed, a check that said pass with minor findings shows
  as Did not pass, and its minor findings block delivery.
- **Reason for abstaining**: the run's end reason in words, the problems the
  checks found, open questions, checks that could not finish, checks that
  neither passed nor found a serious problem (`inconclusive_checks` in
  `summary.json`, one sentence each) and checks that did not run. An undecided
  check ends a run with the reason `<policy>_check_inconclusive` or
  `<policy>_check_incomplete`; runs made by 0.1.0b2 use
  `<policy>_nonblocking_or_unknown`, and the page shows each of them in words.
- **Files**: `proof.md`, `summary.json`, `calls.jsonl`, `invocation.json`,
  `problem.md`, `console.log`, and, once the run has finished, a zip of the whole
  run folder.

## Appearance and fonts

The page has one dark color scheme. Every run status, check verdict, call status
and finding severity is shown as a word with a marker of its own shape, so color
is never the only sign. The tabs, section labels and status tags carry short
Chinese captions. Animations stop when the system asks for reduced motion.

Three fonts ship with the package in `victorique/gui/static/`, so the page loads
nothing from other servers:

| Font | Used for | Files |
|---|---|---|
| Michroma 400 | the name, tabs, section labels, table heads, tags and buttons | latin, latin-ext |
| Barlow Semi Condensed 400 and 500 | body text, labels and help | latin and latin-ext for each weight |
| JetBrains Mono 400 and 500 | problem statements, proofs, quotes and logs; numbers, dates and file names | latin, latin-ext, greek and math symbols, one variable file each |

The ten WOFF2 files take 162,180 bytes. Each `@font-face` rule in `style.css`
has a `unicode-range`: for the latin, latin-ext and greek files the range of
their Google Fonts subset, for the math file 36 characters it has glyphs for
(see below). The browser loads a file only when the page shows a character in
its range. A character outside every range comes from the next font in the
stack. For problem and proof text the stack is
`"JetBrains Mono", "Math Fallback", Consolas, monospace`: after JetBrains Mono,
the `Math Fallback` rule for 39 negated math symbols (see below), then Consolas,
the browser's monospace font and the system's fallback fonts. Chinese captions
use the system's Chinese font, such as Microsoft YaHei on Windows.

Math characters in problem and proof text. The math file is the Google Fonts
`text=` subset of JetBrains Mono for math symbols. Its character map has 43
code points. Six of them (space, `=`, `¬`, `…`, `′`, `″`) are also in the latin
file and stay with it. The math rule's `unicode-range` has 36 code points: the
other 37 except U+0338 COMBINING LONG SOLIDUS OVERLAY (see the negated math
symbols below).

Of the 57 characters
`≡ ≤ ≥ ≠ ≈ ∣ ∤ ∑ ∏ ∫ ∈ ∉ ∀ ∃ → ← ↔ ⇒ ⇐ ⇔ √ ∞ ∘ ⊂ ⊆ ⊃ ⊇ ∪ ∩ ∅ ∂ ∇ ⌊ ⌋ ⌈ ⌉ ℕ ℤ ℚ ℝ ℂ ⟨ ⟩ ′ ″ … ∧ ∨ ¬ ⊕ ⊗ ≅ ∼ ≺ ≻ ⊥ ∥`,
the bundled JetBrains Mono files draw 35: the latin file draws `′ ″ … ¬` and
the math file the other 31. The math file also draws `∋ ∌ ≉ ⊄ ⊅` and the latin
file `± × ·`, which are not in this list. The remaining 22,
`∤ ⇒ ⇐ ⇔ ∪ ∩ ∅ ∇ ⌊ ⌋ ⌈ ⌉ ℤ ℚ ℝ ℂ ⊕ ⊗ ≅ ≺ ≻ ⊥`, come from other fonts: `∤` from
the `Math Fallback` rule, the other 21 from Consolas, then the browser's
monospace font, then the system's fallback fonts such as Cambria Math or Segoe
UI Symbol.

Negated math symbols. Unicode has 45 characters whose canonical decomposition
is a base character followed by U+0338. JetBrains Mono has glyphs of its own
for six of them, `≠ ∉ ∌ ≉ ⊄ ⊅`, and they stay in JetBrains Mono. The other 39,
such as `∤ ≢ ≮ ≰ ⊈`, come from the `Math Fallback` rule, which uses an installed
math font: Segoe UI Symbol on Windows, Cambria Math if that is missing; STIX Two
Math on macOS; Noto Sans Math or DejaVu Sans on Linux. JetBrains Mono and
Consolas have no glyph for these 39; where one of them has the base character,
the browser composes the form from that character and U+0338, and the slash
lands in the next character cell. The rule's range also includes U+0338, so
these 39 symbols are drawn with the same glyph when written as the base
character followed by U+0338, except `⫝̸` (U+2ADC), which Unicode does not
compose from its base character and U+0338; other characters followed by
U+0338, such as `x̸`, still come from Consolas, which draws the slash in the
next character cell. The rule names its fonts with `local()` only, so the page
fetches no file for it. When none of the five fonts is installed, these
characters come from the next fonts in the stack.

The fonts are licensed under the SIL Open Font License 1.1. The license texts
are next to them (`OFL-michroma.txt`, `OFL-barlowsemicondensed.txt`,
`OFL-jetbrainsmono.txt`, also served under `/static/`), and
[NOTICE](../NOTICE) lists their copyright lines.

## Files written by the interface

The interface writes in `RUNS/.victorique-gui/NAME/`: `problem.md`,
`request.json` (title, form values, command and process number, and after a
resume the resume command and its process number), `console.log` (the output of
the command and of each resume) and `exit.json` (the exit code of the latest
command). Inside a run folder it causes one file to be written:
`stop-request.json`, when you press Stop the run. Everything else in a run
folder is written by the `victorique` commands themselves.

## Reading run files on Windows

An ordinary `open()` on Windows denies other programs the right to delete or
rename a file while it is open. The interface reads every run file through
`victorique.shared_read`, which opens with read, write and delete sharing, reads
the whole file in one call and closes it at once.

The loop in `tests/test_shared_read.py` replaces a 4 KB JSON file 300 times in
one thread while another thread reads it continuously. Three runs of that loop
on Windows 10 (build 19045) gave:

| Writer | Reader | Failed reads | Failed replacements |
|---|---|---|---|
| rename with POSIX semantics (FileRenameInfoEx) | `shared_read` | 0 | 0 |
| rename with POSIX semantics | ordinary `open()` | 68–75 | 187–216 failed attempts |
| `os.replace`, retried after each failure | `shared_read` | 0 | 207–235 failed attempts, all later completed |

`os.replace` (MoveFileExW) fails on this Windows version while any program holds
the target open, whatever its sharing mode, so a program that replaces files
should retry for a moment.

## Security

- The server listens on `127.0.0.1` unless told otherwise.
- It answers only requests whose `Host` header names the server itself.
- Starting, stopping and resuming a run need a token that is embedded in the
  page; other web sites cannot read it, and requests from another origin are
  refused.
- The page loads no scripts, styles or fonts from other servers, and its content
  security policy allows only the local server (`font-src 'self'` for the
  bundled fonts). It also refuses inline scripts and style attributes; the page
  sets its bar, gauge and timeline values through the CSS object model.

The interface uses only the Python standard library. Its HTML, CSS, JavaScript
and font files are part of the installed package (`victorique/gui/static/`) and
need no build step or network access.

## 中文说明

### 启动

- `victorique gui` 在本机启动网页界面。默认地址为 `http://127.0.0.1:8765/`，只接受本机连接。
- 监听其他地址需要加 `--allow-remote`：能访问这个网页的人，都可以用你的模型账户启动运行。
- 未指定 `--port`、且 8765 端口已被其他程序占用时，界面依次尝试 8766 到 8785，再使用系统分配的端口，并打印和打开实际使用的地址。用 `--port` 指定的端口被占用时，命令报错，以退出码 2 结束。
- 运行文件夹不存在时，命令会创建它，并检查能否在其中写入新文件。无法创建或写入时，命令打印一行说明（文件夹路径、原因，以及可用 `--runs-dir` 另选文件夹），以退出码 2 结束。

### 新建题目

| 字段 | 命令选项 | 说明 |
| --- | --- | --- |
| 题面 | `PROBLEM` | Markdown 和 LaTeX 按原文作为纯文本提交。 |
| 标题 | 无 | 可选。 |
| 方法 | `--policy` | single、ladder 或 full。single 排在第一位并预选：一次求解，加题面检查和证明检查，共 3 次模型调用；每次超时重发另加 1 次。 |
| 模型服务 | `--transport` | |
| 模型 | `--model` | |
| 推理强度 | `--reasoning-effort` | 仅 Codex CLI。 |
| 检查模型服务 | `--check-transport` | 属于“审核”一组。可选与求解相同（默认）、Codex CLI、Claude Code CLI 或练习运行。只用于题面检查和证明检查，其余步骤使用上方的模型服务。 |
| 检查模型 | `--check-model` | 属于“审核”一组。 |
| 检查推理强度 | `--check-reasoning-effort` | 属于“审核”一组。仅在检查使用 Codex CLI 时可选。 |
| 调用次数上限 | `--calls` | |
| 整次运行的时间上限 | `--hours` | |
| 每次调用的时间上限 | `--call-timeout-seconds` | 0.05 到 86400 秒。留空时使用命令的默认值：600 秒，或设置文件中的 `call_timeout_seconds`。 |
| 调用超时后的重发次数 | `--timeout-retries` | 0 到 3。表单显示 `VICTORIQUE_CONFIG` 所指设置文件中的 `timeout_retries`，未设置或无法读取时显示 1。每次重发是一次新调用，计入调用次数上限和整次运行的时间上限。 |
| 检查未决时的处理 | `--undecided-checks` | 仅 ladder。弃权或修复一次。表单预选 `VICTORIQUE_CONFIG` 所指设置文件中的 `undecided_checks`，未设置或无法读取时预选弃权。 |
| 检查通过但列出轻微问题时的处理 | `--minor-findings` | 所有策略。计为通过（Count it as passed，`nonblocking`，默认）或计为未通过（Count it as not passed，`blocking`）。表单预选设置文件中的 `minor_findings`，未设置或无法读取时预选计为通过。 |

检查未决指两种情况：检查既未通过，也未报告严重或致命问题；或者重试一次后仍未返回有效报告。

选择“计为未通过”时，如果检查判为通过但列出了轻微问题，这些轻微问题按严重问题处理：ladder 和 full 把草稿送去修复，single 不交付证明。判为不通过且只有轻微问题、或判为通过但有未决疑问的检查，在两种选择下都属于检查未决。

一次回放使用 500 份有人工评分的外部证明（125 份正确，375 份错误），按一对检查判定是否交付，不含修复：

| 规则 | 拦下的正确证明 | 交付的错误证明 |
| --- | --- | --- |
| 计为通过（0.1.0b2 的规则） | 27 | 23 |
| 0.1.0b1 的记录 | 42 | 18 |

- 0.1.0b1 把判为通过但列出问题的报告算作无效报告。
- 0.1.0b1 还执行旧的预检查。预检查拦下其中 9 份证明（1 份正确，8 份错误）；这 9 份证明在两项检查中也都至少有一项未通过，所以预检查不改变 42 和 18 这两个数。
- 在“计为未通过”下，判为通过但列出问题的检查按 0.1.0b1 的方式读取；整体规则与 0.1.0b1 并不完全相同。回放中没有“计为未通过”单独的计数。

`VICTORIQUE_CONFIG` 为相对路径时，界面按启动时的工作目录解析，再以绝对路径传给界面启动的运行。

求解或检查选用 Claude Code CLI 时，说明文字要求用 Anthropic Console 账户登录 Claude Code：运行 `claude`，输入 `/login`，选择 Anthropic Console 账户。Anthropic 的法律与合规页面（https://code.claude.com/docs/en/legal-and-compliance，2026-11-19 核对）规定：

- Claude.ai 订阅登录（Free、Pro、Max、Ultra）用于 Anthropic 自家应用的普通使用；
- 开发产品的开发者应使用 API 密钥；
- 第三方开发者不得代用户使用订阅凭据转发请求。

Victorique 从 Claude Code 进程的环境变量中删除 `ANTHROPIC_API_KEY`，所以 Claude Code 使用 `/login` 保存的登录。

### 运行列表

列出每个运行的状态、策略、开始时间、调用次数和 token 数。状态：启动中、运行中、停止中、已交付、未交付、已停止、失败、中途停止。

- 停止中：运行仍在进行，运行文件夹中已有停止请求（`stop-request.json`）。
- 已停止：运行因停止请求结束，`summary.json` 的结果为 `partial`，结束原因为 `stopped_by_user`。

### 运行详情

运行页面显示：

- 进度；
- 调用次数和时间的用量条；
- 运行的设置，包括调用超时后的重发次数和已用次数，以及检查通过但有轻微问题时的处理（选择“计为未通过”的运行中，这类检查显示为未通过）。检查使用的模型服务、模型或推理强度与求解不同时，另列检查模型服务、检查模型和检查推理强度；
- 已交付的证明；未交付时显示最佳草稿，并标明未交付；
- 每次检查的报告和问题的严重程度；
- 每次模型调用在时间轴上的位置；
- 未交付的原因，包括 `summary.json` 中的 `inconclusive_checks`；
- `proof.md`、`summary.json`、`calls.jsonl` 等文件的下载。

结束原因 `<policy>_check_inconclusive`、`<policy>_check_incomplete` 和 0.1.0b2 的 `<policy>_nonblocking_or_unknown` 都显示为文字说明。

运行页面的中文标注：

| 内容 | 英文标签 | 中文标注 |
| --- | --- | --- |
| 证明 | Delivered proof；未交付时为 Best draft (not delivered) | 已交付证明；最佳草稿（未交付） |
| 检查结论 | Passed、Did not pass、Undecided、No clear verdict、Could not be read | 通过、未通过、未决、无结论、无法读取 |
| 批评 | N problem(s) reported、No problem reported | 发现问题、未发现问题 |
| 模型调用状态 | Done、Timed out、Stopped、Failed、Answer could not be used、In progress、No answer recorded | 完成、超时、已停止、失败、回答无效、进行中、无回答 |

### 停止运行

运行进行中时，运行页面显示“Stop run”按钮。点击后，页面先显示确认说明：放弃当前模型调用，运行结束，不交付证明，保留最佳草稿。确认后：

1. 页面发送 `POST /api/runs/NAME/stop`。服务器检查请求的令牌、Origin 和 Host，检查方式与表单相同。
2. 服务器用 `victorique stop` 所用的同一个函数，在运行文件夹中写入 `stop-request.json`。
3. 页面显示“停止中”，直到运行结束。运行结束后，状态显示为“Stopped by you”（已停止）。

运行不再发出新的模型调用。进行中的调用：执行它的 worker 进程终止 CLI 进程，处理方式与达到每次调用的时间上限时相同。这次调用在模型调用列表中显示为已停止，不会重发。

已结束的运行和尚未创建运行文件夹的运行，不接受停止请求（服务器返回 409）。

### 继续运行

进程已结束、但没有写出 `summary.json` 的运行，页面显示“Resume”按钮。点击后：

- 服务器在单独的进程中运行 `victorique resume RUNS/NAME`，启动方式与表单启动 solve 相同（`python -P`，工作目录为运行文件夹）；
- 输出追加到 `console.log`，`request.json` 记录新进程，服务器删除原有的 `exit.json`；
- 页面此后按进行中的运行显示它。

运行文件夹中已有停止请求时，继续运行会立即结束运行，不发出新的模型调用。

### 外观和字体

界面只有深色配色。运行状态、检查结论、调用状态和问题严重程度，都同时用文字和不同形状的标记表示。

字体随包安装在 `victorique/gui/static/`：Michroma、Barlow Semi Condensed 和 JetBrains Mono。许可证均为 SIL Open Font License 1.1，许可证文本与字体文件放在同一文件夹，版权信息见 NOTICE。

题面和证明使用 JetBrains Mono，所带文件包括一个数学符号子集。下面 57 个符号中，所带的 JetBrains Mono 文件显示 35 个：

≡ ≤ ≥ ≠ ≈ ∣ ∤ ∑ ∏ ∫ ∈ ∉ ∀ ∃ → ← ↔ ⇒ ⇐ ⇔ √ ∞ ∘ ⊂ ⊆ ⊃ ⊇ ∪ ∩ ∅ ∂ ∇ ⌊ ⌋ ⌈ ⌉ ℕ ℤ ℚ ℝ ℂ ⟨ ⟩ ′ ″ … ∧ ∨ ¬ ⊕ ⊗ ≅ ∼ ≺ ≻ ⊥ ∥

- latin 子集显示 ′ ″ … ¬，数学符号子集显示其余 31 个。
- 数学符号子集还显示 ∋ ∌ ≉ ⊄ ⊅，latin 子集还显示 ± × ·。这些符号不在上面 57 个之中。
- 其余 22 个符号（∤ ⇒ ⇐ ⇔ ∪ ∩ ∅ ∇ ⌊ ⌋ ⌈ ⌉ ℤ ℚ ℝ ℂ ⊕ ⊗ ≅ ≺ ≻ ⊥）由其他字体显示：∤ 由 `Math Fallback` 规则显示；其余 21 个由字体列表中的后续字体显示，依次为 Consolas、浏览器的等宽字体、系统的后备字体（如 Cambria Math、Segoe UI Symbol）。

否定数学符号：Unicode 有 45 个字符，规范分解为一个基本字符加 U+0338。

- ≠ ∉ ∌ ≉ ⊄ ⊅ 这 6 个在 JetBrains Mono 中有自己的字形，仍由 JetBrains Mono 显示。
- 其余 39 个（如 ∤ ≢ ≮ ≰ ⊈）由 `Math Fallback` 规则显示。这条规则使用本机已安装的数学字体：Windows 上为 Segoe UI Symbol，缺少时为 Cambria Math；macOS 上为 STIX Two Math；Linux 上为 Noto Sans Math 或 DejaVu Sans。
- JetBrains Mono 和 Consolas 没有这 39 个符号的字形。这两种字体含有基本字符时，浏览器用基本字符和 U+0338 组合显示，斜线落在下一个字符格中。
- 这条规则的范围还包含 U+0338，所以这 39 个符号写成基本字符加 U+0338 时，显示的字形与单个字符相同。⫝̸（U+2ADC）除外：Unicode 不把它的基本字符与 U+0338 合成为 ⫝̸。其他字符后接 U+0338 时（如 x̸），仍由 Consolas 显示，斜线落在下一个字符格中。
- 这条规则只用 `local()` 引用本机字体，网页不为它下载文件。五个字体都未安装时，这些符号由字体列表中的后续字体显示。

### 运行和文件

- 界面用 `victorique solve` 命令启动运行，本身不调用模型。
- 关闭界面不会停止进行中的运行。每个运行在自己的调用上限或时间上限处结束；也可以在运行页面点击“Stop run”，或执行 `victorique stop DIR` 停止运行。界面启动和退出时打印的提示也说明了这一点。
- 界面只在 `RUNS/.victorique-gui/NAME/` 中写入自己的文件。在运行文件夹内，界面只在确认停止后引起写入一个文件：`stop-request.json`。其余文件都由 `victorique` 命令写入。
- 界面启动的每个子进程（包括读取 `solve --help`）都使用 `python -P -m victorique`，工作目录为运行文件夹。`-P` 使当前目录不进入模块搜索路径，所以当前目录或运行文件夹中名为 `victorique` 的文件夹或文件，不会替代已安装的包。界面本身以 `-I` 运行时，子进程同样带 `-I`。

### Windows 上的文件读取

在 Windows 上，界面以允许共享读、写和删除的方式读取运行文件（CreateFileW，FILE_SHARE_READ | FILE_SHARE_WRITE | FILE_SHARE_DELETE），读完立即关闭。

实测（Windows 10，build 19045）：一个线程把 JSON 文件替换 300 次，另一个线程同时读取。结果：

- 读取没有出错；
- POSIX 语义的重命名没有失败；
- `os.replace` 在任何程序打开目标文件期间都会失败，写入方需要短暂重试。
