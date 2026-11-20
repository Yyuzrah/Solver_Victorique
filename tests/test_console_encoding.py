"""Reports remain usable when Windows stdout cannot encode mathematical text."""
import importlib
import io
import json
import os
import subprocess
import sys

import pytest


PAYLOAD = {"outcome": "pass", "text": "证明：x − y ∈ ℝ", "path": "结果/α"}


@pytest.mark.parametrize("encoding", ["ascii", "gbk"])
@pytest.mark.parametrize("module,operation,argv", [
    ("cli", "status", ["status", "run"]),
    ("bench", "run_args", ["problems"]),
    ("verify", "verify", ["proof.md", "--problem", "problem.md"]),
    ("calibrate", "calibrate", ["labels.json"]),
    ("shared_prefix", "resume", ["resume", "run"]),
    ("telemetry_report", "report", ["run"]),
    ("calibration", "calibrate", ["assessments.json"]),
    ("harness.grade", "resume_grade", ["run"]),
    ("export", "export", ["--root", "run", "--output", "exported"]),
    ("prepare", "prepare", ["--problem", "problem", "--profile", "profile", "--authorization", "auth",
                            "--session-id", "session", "--run-id", "run", "--provider-id", "provider",
                            "--model", "model", "--deadline", "deadline", "--output", "output", "--calls", "3"]),
])
def test_json_console_roundtrip(monkeypatch, encoding, module, operation, argv):
    target = importlib.import_module("victorique." + module)
    owner = importlib.import_module("victorique.api") if module == "cli" else target
    monkeypatch.setattr(owner, operation, lambda *args, **kwargs: PAYLOAD)
    monkeypatch.setattr(sys, "argv", ["victorique", *argv])
    buffer = io.BytesIO()
    with io.TextIOWrapper(buffer, encoding=encoding, errors="strict") as stdout:
        monkeypatch.setattr(sys, "stdout", stdout)
        target.main()
        stdout.flush()
        assert json.loads(buffer.getvalue().decode(encoding)) == PAYLOAD


def test_solve_output_line_escapes_only_what_stdout_cannot_encode(monkeypatch, tmp_path):
    from victorique import api
    monkeypatch.delenv("VICTORIQUE_CONFIG", raising=False)
    problem = tmp_path / "problem.md"
    problem.write_text("Prove that n + 0 = n for every integer n.\n", encoding="utf-8")
    out = tmp_path / "结果ℝ" / "run"
    buffer = io.BytesIO()
    with io.TextIOWrapper(buffer, encoding="gbk", errors="strict") as stdout:
        monkeypatch.setattr(sys, "stdout", stdout)
        assert api.solve(problem, transport="fake", out=out)["outcome"] == "complete"
        stdout.flush()
        first = buffer.getvalue().decode("gbk").splitlines()[0]
    # GBK has 结果 but not ℝ; the path keeps its single backslashes.
    assert first == "Output: " + str(out).replace("ℝ", "\\u211d")


def test_calibration_file_retains_utf8(monkeypatch, tmp_path):
    from victorique import calibration
    monkeypatch.setattr(calibration, "calibrate", lambda **kwargs: PAYLOAD)
    destination = tmp_path / "calibration.json"
    calibration.main(["assessments.json", "--out", str(destination)])
    text = destination.read_text(encoding="utf-8")
    assert "证明：x − y ∈ ℝ" in text
    assert json.loads(text) == PAYLOAD


@pytest.mark.parametrize("entry", ["victorique", "victorique.bench"])
def test_fake_benchmark_finishes_with_legacy_stdout(tmp_path, entry):
    problems = tmp_path / "problems"
    problems.mkdir()
    (problems / "题−1.md").write_text("Show that 1 + 1 = 2.", encoding="utf-8")
    output = tmp_path / "结果−out"
    env = {**os.environ, "PYTHONIOENCODING": "gbk:strict"}
    command = [sys.executable, "-B", "-m", entry]
    if entry == "victorique":
        command.append("bench")
    command += [str(problems), "--policy", "single", "--transport", "fake", "--out", str(output)]
    options = {"creationflags": subprocess.CREATE_NO_WINDOW} if os.name == "nt" else {}
    completed = subprocess.run(command, capture_output=True, env=env, timeout=60, **options)
    assert completed.returncode == 0, completed.stderr.decode("gbk", errors="replace")
    # Progress lines precede the final JSON report.
    console = completed.stdout.decode("gbk")
    report = json.loads(console[console.index("{"):])
    persisted = (output / "bench.json").read_text(encoding="utf-8")
    assert report == json.loads(persisted)
    assert "题−1" in persisted
    assert report["counts"]["ungraded_delivery"] == 1
