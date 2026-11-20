import json
import subprocess
import sys
from unittest import mock

import pytest

from victorique import calibrate as calibration


def dataset(tmp_path):
    (tmp_path / "problem.md").write_text("Prove n + 0 = n.")
    (tmp_path / "proof.md").write_text("By the additive identity, n + 0 = n.")
    path = tmp_path / "labels.json"
    path.write_text(json.dumps([{"id": "correct-one", "label": "correct", "problem": "problem.md", "proof": "proof.md"},
                               {"id": "incorrect-one", "label": "incorrect", "problem": "problem.md", "proof": "proof.md"}]))
    return path


def sample(label, outcome, tokens=None):
    return {"label": label, "outcome": outcome, "mode": "single", "calls_used": 1,
            "wall_seconds": 2.5, "tokens": tokens}


def test_rates_keep_incomplete_denominators_and_costs():
    rows = [sample("correct", "pass"), sample("correct", "fail"), sample("correct", "incomplete"),
            sample("incorrect", "pass"), sample("incorrect", "incomplete")]
    report = calibration.aggregate(rows)["single"]
    assert report["false_accepts"] == report["false_rejects"] == 1
    assert report["false_accept_rate"] == .5
    assert report["false_reject_rate"] == 1 / 3
    assert report["completed_false_accept_rate"] == 1
    assert report["completed_false_reject_rate"] == .5
    assert report["coverage"] == 3 / 5
    assert report["incomplete_correct"] == report["incomplete_incorrect"] == 1
    assert report["calls_used"] == 5
    assert report["wall_seconds"] == 12.5
    assert report["tokens"]["input_tokens"] is None
    assert report["token_usage_unknown_samples"] == 5


def test_empty_denominators_and_known_tokens():
    tokens = {"input_tokens": 20, "output_tokens": 5, "cached_input_tokens": 0, "reasoning_output_tokens": None}
    report = calibration.aggregate([sample("incorrect", "incomplete", tokens)])["single"]
    assert report["false_reject_rate"] is None
    assert report["completed_false_accept_rate"] is None
    assert report["coverage"] == 0
    assert report["tokens"] == tokens


def test_fake_calibration_all_modes_and_label_isolation(tmp_path):
    path = dataset(tmp_path)
    report = calibration.calibrate(path, out=tmp_path / "run")
    assert report["synthetic"] is True
    assert len(report["samples"]) == 6
    for mode, calls in (("single", 2), ("stepwise", 2), ("multi", 6)):
        stats = report["modes"][mode]
        assert stats["calls_used"] == calls
        assert stats["false_accept_rate"] == 1
        assert stats["false_reject_rate"] == 0
        assert stats["coverage"] == 1
    assert json.loads((tmp_path / "run" / "calibration.json").read_text()) == report
    # Labels stay in the calibration layer, outside the reviewer's frozen packet.
    from victorique.verify import VerifyLoop
    from victorique.fake import FakeTransport
    loop = VerifyLoop.open(tmp_path / "run" / "1-single" / "state", adapter=FakeTransport())
    row = next(iter(loop.core.snapshot()["exchange"]["requests"].values()))
    assert "correct-one" not in json.dumps(row["packet"])
    assert '"label"' not in json.dumps(row["packet"])
    assert loop.config["manifest"]["limits"]["stop_condition"]["deadline_utc"] == report["deadline_utc"]


def test_incomplete_samples_are_retained(tmp_path):
    report = calibration.calibrate(dataset(tmp_path), modes=["multi"], calls=2, out=tmp_path / "run")
    assert len(report["samples"]) == 2
    stats = report["modes"]["multi"]
    assert stats["incomplete"] == 2
    assert stats["coverage"] == 0
    assert stats["calls_used"] == 4


def test_expired_batch_marks_every_sample_without_dispatch(tmp_path):
    with mock.patch.object(calibration, "verify", side_effect=AssertionError("no dispatch")):
        report = calibration.calibrate(dataset(tmp_path), hours=1e-15, out=tmp_path / "run")
    assert len(report["samples"]) == 6
    assert all(row["reason"] == "batch_deadline" for row in report["samples"])
    assert all(row["calls_used"] == 0 for row in report["samples"])


def test_invalid_label_rejected_before_dispatch(tmp_path):
    path = dataset(tmp_path)
    rows = json.loads(path.read_text())
    rows[0]["label"] = "model_said_yes"
    path.write_text(json.dumps(rows))
    with mock.patch.object(calibration, "verify", side_effect=AssertionError("no dispatch")):
        with pytest.raises(ValueError, match="human label"):
            calibration.calibrate(path, out=tmp_path / "run")
    assert not (tmp_path / "run").exists()


def test_calibration_module_cli(tmp_path):
    result = subprocess.run([sys.executable, "-m", "victorique.calibrate", str(dataset(tmp_path)),
                             "--modes", "single", "--calls", "1", "--out", str(tmp_path / "run")],
                            capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True, timeout=60)
    assert result.returncode == 0, result.stderr
    report = json.loads((tmp_path / "run" / "calibration.json").read_text())
    assert report["modes"]["single"]["calls_used"] == 2
