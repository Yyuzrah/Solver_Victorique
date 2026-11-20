# Install Victorique with your coding agent

Give your agent the instructions below and the location of this repository,
https://github.com/Yyuzrah/Solver_Victorique, or a Victorique wheel from its
[release page](https://github.com/Yyuzrah/Solver_Victorique/releases). They
work from a terminal in Codex, Claude Code, Cursor, and
other coding environments. The solving backend is selected separately from the
agent doing the installation.

> Install Victorique from the source directory or wheel I supplied. Use Python
> 3.11 or newer and a dedicated virtual environment; preserve my existing Python
> environment and project configuration. Check the supplied location rather
> than guessing a repository URL or downloading an unrelated package.
>
> 1. Record the operating system, Python version, and source/wheel location.
>    Create `.victorique-venv` in a directory I can keep. If that environment
>    already exists, inspect it before reusing it. Record its absolute path.
> 2. Install using that environment's absolute Python path with
>    `-I -m pip install SOURCE_OR_WHEEL`; make the supplied source/wheel path absolute.
>    Move to a working directory outside every source checkout before invoking
>    Victorique. A local package named `victorique` can shadow an installed wheel.
>    Use the absolute venv `victorique` executable, or the absolute interpreter
>    with `-I -m victorique`, for every following command. On Windows use
>    `python.exe` normally: the product's background subprocesses already hide
>    their windows. Direct PowerShell invocation of `pythonw.exe` is not recommended.
> 3. Run the isolated entry point with `--version` and `--help`.
> 4. In a new temporary directory, create `problem.md` containing
>    `Prove that n + 0 = n for every integer n.`
>    Use the no-BOM UTF-8 PowerShell example below on Windows. Run the isolated
>    entry point with `solve problem.md --policy single --calls 3 --transport fake --out result`.
>    Read `result/summary.json`, `result/proof.md`, and `result/calls.jsonl`.
>    Expect outcome `complete`, policy `single`, three calls, and `synthetic: true`.
>    Missing model and token usage is expected for this offline demonstration.
> 5. Run the same isolated entry point with `resume result`. Confirm the call count remains
>    three. Then remove only the temporary demonstration directory, after
>    verifying its resolved path. Keep the installed environment.
> 6. Run public `codex --version` and `codex login --help` commands using the
>    selected native CLI; run `codex login status` if the installed CLI advertises
>    that subcommand. Report their output and exit status. Do not open credential
>    files, change login, or run a real solve. If the CLI or status subcommand is
>    missing, report that fact rather than inferring authentication state.
>    Codex Desktop alone does not ensure `codex` is on PATH; the user can install
>    the CLI or select an existing executable with `VICTORIQUE_CODEX_BINARY`.
> 7. Report the installed package location/version, exact interpreter path,
>    offline outcome and call count, CLI availability, the command that starts
>    the web interface (`victorique gui` with the absolute venv executable,
>    started from the folder where my runs should be kept), and the command I
>    should use for my first real problem. Clearly list anything that failed.

## Terminal examples

Windows PowerShell (including 5.1): replace these example paths with writable
absolute locations and the actual supplied wheel. Keep the working directory
outside all source checkouts.

```powershell
py -3.11 -m venv C:\Tools\victorique-venv
$victoriquePython = 'C:\Tools\victorique-venv\Scripts\python.exe'
& $victoriquePython -I -m pip install C:\Downloads\victorique-0.2.0a1-py3-none-any.whl
New-Item -ItemType Directory -Force C:\Work\victorique-demo | Out-Null
Set-Location C:\Work\victorique-demo
& $victoriquePython -I -m victorique --version
& $victoriquePython -I -m victorique --help
[System.IO.File]::WriteAllText((Join-Path (Get-Location).Path 'problem.md'), "Prove that n + 0 = n for every integer n.`n", [System.Text.UTF8Encoding]::new($false))
& $victoriquePython -I -m victorique solve problem.md --policy single --calls 3 --transport fake --out result
& $victoriquePython -I -m victorique resume result
```

The absolute console entry point is an equivalent alternative:

```powershell
& 'C:\Tools\victorique-venv\Scripts\victorique.exe' --version
```

On Linux or macOS, with Python 3.11 or newer, adapt these absolute paths:

```sh
python3 -m venv /home/you/.venvs/victorique
/home/you/.venvs/victorique/bin/python -I -m pip install /home/you/Downloads/victorique-0.2.0a1-py3-none-any.whl
mkdir -p /home/you/victorique-demo
cd /home/you/victorique-demo
/home/you/.venvs/victorique/bin/victorique --version
printf '%s\n' 'Prove that n + 0 = n for every integer n.' > problem.md
/home/you/.venvs/victorique/bin/victorique solve problem.md --policy single --calls 3 --transport fake --out result
/home/you/.venvs/victorique/bin/victorique resume result
```

An absolute source-checkout path can replace the wheel in the install command,
and so can the repository address
`git+https://github.com/Yyuzrah/Solver_Victorique.git@v0.2.0a1` (this needs Git).
Developers can use `pip install -e ".[test]"`; ordinary use needs no test extras.

## Codex CLI availability

Installing Codex Desktop alone does not ensure a standalone `codex` command on
PATH. Follow the [official CLI installation guide](https://learn.chatgpt.com/docs/codex/cli)
or supply the path of an already installed native executable. The
[official CLI reference](https://learn.chatgpt.com/docs/developer-commands?surface=cli)
documents `codex login status`; inspect the installed CLI's help before using it:

```console
codex --version
codex login --help
codex login status
```

Run the last command only if advertised. These checks report CLI availability
and public authentication status, not whether a future model request will succeed.
If `codex` is absent from PATH or resolves to a Windows shell wrapper, use the
confirmed absolute native executable path:

```powershell
$env:VICTORIQUE_CODEX_BINARY = 'C:\Tools\Codex\codex.exe'
& $env:VICTORIQUE_CODEX_BINARY --version
& $env:VICTORIQUE_CODEX_BINARY login --help
# If advertised by this executable:
& $env:VICTORIQUE_CODEX_BINARY login status
```

This variable selects the executable for Victorique; it does not install the CLI
or log in. Let the user complete any required login through the CLI's own UI.

## Claude Code CLI login

Victorique can also use Claude Code CLI, for solving (`--transport claude`) or
only for the statement and proof checks (`--check-transport claude`). For that
use, log Claude Code in with an Anthropic Console account: run `claude`, type
`/login` and choose the Anthropic Console account (API usage billing). Anthropic's
[legal and compliance page](https://code.claude.com/docs/en/legal-and-compliance)
(checked 2026-11-19) keeps the Claude.ai subscription login (Free, Pro, Max,
Ultra) for ordinary use of Anthropic's own apps and asks developers who build
products to use an API key. Victorique removes `ANTHROPIC_API_KEY` from the
environment of the Claude Code process, so Claude Code runs with the login
stored by `/login`. An installing agent checks only `claude --version` and
`claude --help` and leaves the login to the user.

Once the native Codex CLI is installed and logged in, a real solve is an explicit
new command with its own call budget and deadline. Without `--policy` it uses
`single`: 3 model calls (solution, statement check, proof check), plus one for
each call sent again after it reached the per-call time limit:

```powershell
& $victoriquePython -I -m victorique solve my-problem.md --calls 12 --hours 2 --out runs/my-proof
```

Use the same absolute venv entry point and outside-source working directory.
Each output directory identifies one run; use `resume` for an
existing run and a new directory for a new one.

## Installation validation status

A 0.1.0b1 wheel passed fresh installation on Windows
(Python 3.11.9) and WSL Ubuntu (Python 3.11.3). Windows completed fake `single`
and `full` runs in three and nine calls; Ubuntu completed `single` in three.
Resume added no calls. These were isolated package installations, not editable
source checkouts.

A new Codex chat followed these instructions using another fresh Windows
environment: version/help, fake `single`, and resume all passed, with three
calls retained. The native CLI reported `codex-cli 0.167.0-alpha.4.1`; its
version/help commands passed. No login or real solve was attempted.

After the default-policy and license updates, the Apache-2.0 wheel also passed
fresh Windows and Ubuntu installs, including the console entry point and an
omitted-policy solve, which then used `ladder`: complete, three synthetic calls,
unchanged after resume. Wheel metadata contains the license expression, LICENSE
and NOTICE.
On 2026-11-13, a fresh Claude Code session with no prior task context passed
these installation instructions. It installed the licensed v0.1.0b1 wheel in a
new venv, completed fake `single` with `complete`, three of three calls and
`synthetic: true`, then resumed with the count still three. Codex CLI was
absent from PATH and was reported to the user.
This validates installation through Claude Code, not real inference through the
Claude model backend. The source-shadowing, PowerShell and public-login-status
clarifications above follow that check. A later 0.1.0b1 wheel passed a fresh
Windows virtual environment check with Python 3.11.9 on 2026-11-13: one
fake `single` solve in three calls, and a resume that kept the count at three.
