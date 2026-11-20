import csv
import json
from pathlib import Path
import tempfile

import pytest

from victorique.calibration import calibrate, main, read_assessments


def row(number, human=7, machine=7, **changes):
    return {"dataset_id": "synthetic", "problem_id": "problem-1", "sample_id": f"sample-{number}",
            "assessment_id": f"assessment-{number}", "grader_config": "rubric-v1",
            "grader_model": "fixture-v1", "grader_family": "fixture", "grader_role": "primary",
            "human_score": human, "machine_score": machine, "status": "completed", **changes}


def stats(rows, **kwargs):
    return calibrate(rows, **kwargs)["groups"][0]["unweighted"]


@pytest.mark.parametrize("scores, expected", [
    ([(7, 7), (0, 0)], (1, 1, 0, 0, 1, 1)),
    ([(7, 0), (0, 7)], (0, 0, 1, 1, 0, 0)),
    ([(7, 7), (7, 0), (0, 7), (0, 0)], (1, 1, 1, 1, .5, .5)),
])
def test_confusion_and_conditional_rates(scores, expected):
    result = stats([row(i, *pair) for i, pair in enumerate(scores)])
    assert tuple(result[k] for k in ("tp", "tn", "fp", "fn", "sensitivity", "specificity")) == expected
    assert result["false_accept_rate"] == 1 - expected[-1]
    assert result["false_reject_rate"] == 1 - expected[-2]
    assert result["coverage"] == 1


@pytest.mark.parametrize("human, absent", [(0, ("sensitivity", "false_reject_rate", "positive_coverage")),
                                         (7, ("specificity", "false_accept_rate", "negative_coverage"))])
def test_missing_class_has_null_rates(human, absent):
    result = stats([row(1, human, 7)])
    assert all(result[key] is None for key in absent)


def test_missing_labels_and_machine_failures_preserve_denominators():
    rows = [row(0), row(1, 7, None, status="timeout"), row(2, 0, None, status="invalid"),
            row(3, 0, None, status="missing"), row(4, None, 7), row(5, None, None, status="error"),
            row(6, 7, None), row(7, 0, 0)]
    report = calibrate(rows)
    result = report["groups"][0]["unweighted"]
    assert result["total"] == 8
    assert result["paired"] == 2
    assert result["missing_human"] == 2
    assert result["missing_machine"] == 5
    assert result["missing_both"] == 1
    assert result["unpaired"] == 6
    assert result["missing_machine_positive"] == result["missing_machine_negative"] == 2
    assert result["tp"] == result["tn"] == 1
    assert result["fp"] == result["fn"] == 0
    assert result["coverage"] == .25
    assert result["human_coverage"] == .75
    assert result["machine_coverage"] == 3 / 8
    assert result["positive_coverage"] == result["negative_coverage"] == 1 / 3
    assert result["sensitivity"] == result["specificity"] == 1
    assert result["status_counts"] == {"completed": 4, "missing": 1, "timeout": 1, "invalid": 1, "error": 1}
    assert report["assessments"][4]["human_score"] is None


def test_only_missing_has_no_calibration_rates():
    result = stats([row(1, None, None, status="missing")])
    assert result["coverage"] == 0
    assert all(result[key] is None for key in ("sensitivity", "specificity", "false_accept_rate", "false_reject_rate"))


def test_duplicate_assessment_rejected_even_across_groups():
    with pytest.raises(ValueError, match="duplicate assessment_id"):
        calibrate([row(1), row(1, dataset_id="other")])


def test_repeated_assessments_and_proofs_retain_problem_cluster():
    report = calibrate([row(1), row(2), row(3, sample_id="sample-1"), row(4, problem_id="problem-2")])
    group = report["groups"][0]
    assert group["problem_count"] == 2
    assert group["sample_count"] == 3
    assert group["unweighted"]["total"] == 4
    assert group["clusters"] == {"problem-1": {"sample-1": ["assessment-1", "assessment-3"],
                                               "sample-2": ["assessment-2"]},
                                 "problem-2": {"sample-4": ["assessment-4"]}}
    assert report["count_unit"] == "assessment"
    assert report["cluster_unit"] == "dataset_id/problem_id"
    assert len(report["assessments"]) == 4


@pytest.mark.parametrize("change", [{"problem_id": "other"}, {"human_score": 0}, {"inclusion_probability": .5}])
def test_sample_identity_and_labels_must_agree(change):
    with pytest.raises(ValueError, match="sample must keep"):
        calibrate([row(1), row(2, sample_id="sample-1", **change)])


def test_inverse_probability_weights_are_separate_and_include_missing():
    rows = [row(1, 7, 7, inclusion_probability=.5), row(2, 7, 0, inclusion_probability=.25),
            row(3, 0, 0, inclusion_probability=1), row(4, 0, 7, inclusion_probability=.5),
            row(5, 7, None, status="timeout", inclusion_probability=.25)]
    group = calibrate(rows)["groups"][0]
    plain, weighted = group["unweighted"], group["weighted"]
    assert plain["tp"] == plain["tn"] == plain["fp"] == plain["fn"] == 1
    assert plain["coverage"] == .8
    assert weighted["tp"] == 2
    assert weighted["tn"] == 1
    assert weighted["fp"] == 2
    assert weighted["fn"] == 4
    assert weighted["total"] == 13
    assert weighted["coverage"] == 9 / 13
    assert weighted["sensitivity"] == weighted["specificity"] == 1 / 3
    assert weighted["missing_machine_positive"] == 4
    assert group["weighted_reason"] is None
    rows[0].pop("inclusion_probability")
    incomplete = calibrate(rows)["groups"][0]
    assert incomplete["unweighted"] == plain
    assert incomplete["weighted"] is None
    assert "missing" in incomplete["weighted_reason"]


@pytest.mark.parametrize("field, value", [("dataset_id", "other"), ("grader_config", "rubric-v2"),
    ("grader_model", "fixture-v2"), ("grader_family", "other"), ("grader_role", "secondary")])
def test_each_configuration_dimension_is_grouped_separately(field, value):
    report = calibrate([row(1), row(2, 0, 0, **{field: value})])
    assert len(report["groups"]) == 2
    assert all(group["unweighted"]["total"] == 1 for group in report["groups"])


def test_unknown_actual_model_stays_separate_from_configuration_observation():
    rows = [row(1, grader_model=None, configured_model="configured-v1", configured_model_source="config/read"),
            row(2)]
    report = calibrate(rows)
    assert len(report["groups"]) == 2
    assert report["groups"][0]["grader_model"] is None
    assert report["assessments"][0]["grader_model"] is None
    assert report["assessments"][0]["configured_model"] == "configured-v1"
    assert report["assessments"][0]["configured_model_source"] == "config/read"
    assert report["groups"][0]["grader_family"] == "fixture"
    assert calibrate([row(1, grader_model="")])["groups"][0]["grader_model"] is None
    with pytest.raises(ValueError, match="supplied together"):
        calibrate([row(1, configured_model="configured-v1")])


def test_thresholds_are_explicit_and_default_to_seven():
    rows = [row(1, 6, 6)]
    default = calibrate(rows)["groups"][0]
    assert default["human_threshold"] == default["machine_threshold"] == 7
    assert default["unweighted"]["tn"] == 1
    changed = calibrate(rows, human_threshold=5, machine_threshold=7)["groups"][0]
    assert changed["human_threshold"] == 5
    assert changed["machine_threshold"] == 7
    assert changed["unweighted"]["fn"] == 1
    assert stats(rows, human_threshold=5, machine_threshold=5)["tp"] == 1
    with pytest.raises(ValueError, match="human_threshold"):
        calibrate(rows, human_threshold=True)


@pytest.mark.parametrize("change", [
    {"human_score": True}, {"human_score": 1.5}, {"human_score": 8}, {"machine_score": -1},
    {"machine_score": "nan"}, {"status": "unknown"}, {"status": "timeout"},
    {"grader_role": "arbitrary"}, {"sample_id": ""}, {"dataset_id": " padded "},
    {"inclusion_probability": 0}, {"inclusion_probability": 1.1}, {"inclusion_probability": True},
    {"inclusion_probability": float("nan")}, {"inclusion_probability": float("inf")},
    {"inclusion_probability": "bogus"}, {"inclusion_probability": 5e-324}, {"unknown_field": 1},
])
def test_invalid_contract_rejected(change):
    with pytest.raises(ValueError):
        calibrate([row(1, **change)])


def test_required_human_score_key_cannot_be_silently_inferred():
    record = row(1)
    del record["human_score"]
    with pytest.raises(ValueError, match="required fields"):
        calibrate([record])


def test_json_csv_and_cli_have_identical_results(capsys):
    rows = [row(1, inclusion_probability=.5), row(2, None, None, status="missing", inclusion_probability=.25)]
    with tempfile.TemporaryDirectory() as directory:
        root = Path(directory)
        json_path, csv_path = root / "labels.json", root / "labels.csv"
        json_path.write_text(json.dumps(rows), encoding="utf-8")
        with csv_path.open("w", encoding="utf-8-sig", newline="") as handle:
            writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
            writer.writeheader()
            writer.writerows(rows)
        expected = calibrate(rows, human_threshold=5)
        assert read_assessments(json_path) == read_assessments(csv_path)
        assert main([str(csv_path), "--human-threshold", "5"]) == 0
        assert json.loads(capsys.readouterr().out) == expected
        output = root / "report.json"
        assert main([str(json_path), "--human-threshold", "5", "--out", str(output)]) == 0
        assert json.loads(output.read_text(encoding="utf-8")) == expected
        with pytest.raises(SystemExit) as error:
            main([str(json_path), "--out", str(output)])
        assert error.value.code == 2
        assert json.loads(output.read_text(encoding="utf-8")) == expected


@pytest.mark.parametrize("suffix, content", [(".json", "{}"), (".json", "["),
    (".csv", "human_score,human_score\n7,7\n"), (".txt", "[]")])
def test_invalid_files(suffix, content):
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / ("input" + suffix)
        path.write_text(content, encoding="utf-8")
        with pytest.raises(ValueError):
            read_assessments(path)


def test_empty_input_is_explicit():
    assert calibrate([])["groups"] == []
    assert calibrate([])["assessments"] == []
