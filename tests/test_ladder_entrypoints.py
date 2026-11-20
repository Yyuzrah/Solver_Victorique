"""Public CLI and package API exercise the integrated ladder policy."""
import json
import subprocess
import sys

from victorique import solve


def test_cli_ladder_three_call_completion_and_frozen_options(tmp_path):
    problem = tmp_path / "problem.md"
    problem.write_text("Prove that n + 0 = n for every integer n.")
    output = tmp_path / "run"
    command = [sys.executable, "-m", "victorique", "solve", str(problem), "--policy", "ladder",
               "--repair-rounds", "1", "--calls", "3", "--transport", "fake", "--out", str(output)]
    result = subprocess.run(command, capture_output=True, text=True, timeout=60,
                            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert result.returncode == 0, result.stderr
    summary = json.loads((output / "summary.json").read_text())
    assert summary["policy"] == "ladder"
    assert summary["outcome"] == "complete"
    assert summary["calls_used"] == 3
    parameters = json.loads((output / "invocation.json").read_text())["parameters"]
    assert parameters["repair_rounds"] == 1
    assert parameters["investigate"] is False
    assert parameters["escalate"] is False
    resumed = subprocess.run([sys.executable, "-m", "victorique", "resume", str(output)],
                             capture_output=True, text=True, timeout=30,
                             creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    assert resumed.returncode == 0, resumed.stderr
    assert json.loads((output / "summary.json").read_text())["calls_used"] == 3


def test_package_solve_exposes_ladder_options(tmp_path):
    problem = tmp_path / "problem.md"
    problem.write_text("Prove that n + 0 = n for every integer n.")
    result = solve(problem, policy="ladder", repair_rounds=0, investigate=False,
                   escalate=False, calls=3, transport="fake", out=tmp_path / "run")
    assert result["outcome"] == "complete"
    assert result["calls_used"] == 3
