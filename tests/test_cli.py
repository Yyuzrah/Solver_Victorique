from contextlib import redirect_stdout
import io
from pathlib import Path
import tomllib
from unittest import mock

import pytest

import victorique
from victorique import api, cli


@pytest.mark.parametrize("policy_args,expected", [([], "single"), (["--policy", "full"], "full")])
def test_solve_cli_policy_default_and_explicit_full(policy_args, expected):
    argv = ["victorique", "solve", "problem.md", *policy_args]
    with mock.patch("sys.argv", argv), mock.patch.object(api, "solve", return_value={}) as solve, \
         redirect_stdout(io.StringIO()):
        cli.main()
    options = solve.call_args.kwargs
    assert options["policy"] == expected
    assert options["repair_rounds"] == 2
    assert options["investigate"] is options["escalate"] is False
    assert options["reasoning_effort"] is None


@pytest.mark.parametrize("effort", ["low", "medium", "high", "xhigh", "max", "ultra"])
def test_solve_cli_passes_reasoning_effort(effort):
    with mock.patch("sys.argv", ["victorique", "solve", "problem.md", "--reasoning-effort", effort]), \
         mock.patch.object(api, "solve", return_value={}) as solve, redirect_stdout(io.StringIO()):
        cli.main()
    assert solve.call_args.kwargs["reasoning_effort"] == effort
    assert solve.call_args.kwargs["model"] is None


@pytest.mark.parametrize("command,effort", [("solve", "invalid"), ("resume", "low")])
def test_cli_rejects_invalid_or_resume_effort_override(command, effort):
    with mock.patch("sys.argv", ["victorique", command, "path", "--reasoning-effort", effort]), \
         mock.patch.object(api, "solve", side_effect=AssertionError("no run")), \
         mock.patch.object(api, "resume", side_effect=AssertionError("no resume")), \
         pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 2


@pytest.mark.parametrize("command", [None, "status", "resume", "export"])
def test_help_uses_public_command_name_and_describes_run_operations(capsys, command):
    arguments = [] if command is None else [command]
    with mock.patch("sys.argv", ["some/path/__main__.py", *arguments, "--help"]), \
         pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert output.startswith("usage: victorique ")
    assert "__main__.py" not in output
    descriptions = {"status": "read a run's current status",
                    "resume": "continue a run with its frozen settings",
                    "export": "export a run's proof and summary"}
    for name in descriptions if command is None else [command]:
        assert descriptions[name] in output


def test_top_level_help_writes_every_command_description_in_lower_case(capsys):
    with mock.patch("sys.argv", ["victorique", "--help"]), pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 0
    output = capsys.readouterr().out
    assert "review an existing proof" in output
    for name in ("solve", "status", "resume", "export", "stop", "bench", "verify", "gui"):
        line = next(row for row in output.splitlines() if row.split()[:1] == [name])
        assert line.split()[1][0].islower(), line


def test_solve_help_describes_each_option_and_states_the_defaults(capsys):
    with mock.patch("sys.argv", ["victorique", "solve", "--help"]), pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 0
    text = " ".join(capsys.readouterr().out.split())
    for phrase in ("problem UTF-8 file with the problem statement",
                   "--policy {single,ladder,resample,bestofn,full} single (default):",
                   "--investigate ladder only:",
                   "--escalate ladder only:",
                   "--model MODEL model name passed to the CLI (default: the CLI's configured default model)",
                   "--calls CALLS maximum number of model calls in the run, 2-100 (default 12)",
                   "--hours HOURS time limit for the whole run in hours (default 2)",
                   "--out OUT new folder for the run's files (default: runs/run-<random id> in the current folder)",
                   "--transport {codex,claude,fake} model service for every role (default codex, a real model "
                   "service)"):
        assert phrase in text


def test_solve_prints_the_run_folder_as_a_plain_path(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv("VICTORIQUE_CONFIG", raising=False)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove that n + 0 = n for every integer n.\n", encoding="utf-8")
    out = tmp_path / "run"
    with mock.patch("sys.argv", ["victorique", "solve", str(problem), "--transport", "fake", "--out", str(out)]):
        cli.main()
    assert capsys.readouterr().out.splitlines()[0] == "Output: " + str(out)


def test_solve_cli_passes_absolute_deadline():
    deadline = "2028-01-01T00:00:00Z"
    with mock.patch("sys.argv", ["victorique", "solve", "problem.md", "--deadline-utc", deadline]), \
         mock.patch.object(api, "solve", return_value={}) as solve, redirect_stdout(io.StringIO()):
        cli.main()
    assert solve.call_args.kwargs["deadline_utc"] == deadline


def test_version_is_the_package_metadata_version(capsys):
    root = Path(__file__).resolve().parents[1]
    version = tomllib.loads((root / "pyproject.toml").read_text(encoding="utf-8"))["project"]["version"]
    assert victorique.__version__ == version
    with mock.patch("sys.argv", ["victorique", "--version"]), pytest.raises(SystemExit) as error:
        cli.main()
    assert error.value.code == 0
    assert capsys.readouterr().out.strip() == version
