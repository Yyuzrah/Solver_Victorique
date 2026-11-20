from copy import deepcopy

import pytest

from victorique.calibration_intervals import calibration_intervals


def fixture():
    rows, mapping = [], []
    for problem in range(4):
        for stratum, human in (("positive", 7), ("negative", 0)):
            sample = f"{problem}-{stratum}"
            mapping.append({"dataset_id": "synthetic", "sample_id": sample, "stratum": stratum})
            rows.append({"dataset_id": "synthetic", "problem_id": str(problem), "sample_id": sample,
                "assessment_id": sample, "grader_config": "fixture", "grader_model": None,
                "grader_family": "fixture", "grader_role": "primary", "human_score": human,
                "machine_score": human if problem != 0 else 7 - human, "status": "completed",
                "inclusion_probability": .25 if stratum == "positive" else .5})
    return rows, mapping


def test_intervals_are_reproducible_and_do_not_change_point_estimates():
    rows, mapping = fixture()
    original = deepcopy(rows)
    first = calibration_intervals(rows, mapping, replicates=80)
    second = calibration_intervals(rows, mapping, replicates=80)
    assert first == second and rows == original
    group = first["groups"][0]
    assert group["weighted"]["sensitivity"] == group["unweighted"]["sensitivity"] == .75
    assert group["intervals"]["weighted"]["supported"]
    for method in ("weighted", "unweighted_sensitivity"):
        rates = group["intervals"][method]["rates"]
        assert rates["sensitivity"]["lower"] <= .75 <= rates["sensitivity"]["upper"]
        assert rates["coverage"]["lower"] == rates["coverage"]["upper"] == 1
        assert rates["sensitivity"]["valid_replicates"] == 80
    assert first["interval_protocol"]["finite_population_exact"] is False


def test_duplicating_assessments_does_not_create_independent_clusters():
    rows, mapping = fixture()
    first = calibration_intervals(rows, mapping, replicates=50)
    duplicated = rows + [{**row, "assessment_id": row["assessment_id"] + "-repeat"} for row in rows]
    second = calibration_intervals(duplicated, mapping, replicates=50)
    for method in ("weighted", "unweighted_sensitivity"):
        for metric, interval in first["groups"][0]["intervals"][method]["rates"].items():
            other = second["groups"][0]["intervals"][method]["rates"][metric]
            assert other["lower"] == pytest.approx(interval["lower"])
            assert other["upper"] == pytest.approx(interval["upper"])
    assert second["groups"][0]["problem_count"] == 4


def test_matching_configurations_share_problem_draws():
    rows, mapping = fixture()
    other = [{**row, "assessment_id": row["assessment_id"] + "-other", "grader_config": "other"} for row in rows]
    report = calibration_intervals(rows + other, mapping, replicates=50)
    assert report["groups"][0]["intervals"] == report["groups"][1]["intervals"]


@pytest.mark.parametrize("missing", ["probability", "mapping", "single_cluster"])
def test_unsupported_weighted_design_is_reported_not_imputed(missing):
    rows, mapping = fixture()
    if missing == "probability":
        rows[0]["inclusion_probability"] = None
    elif missing == "mapping":
        mapping.pop()
    else:
        mapping[0]["stratum"] = "single_cluster"
    report = calibration_intervals(rows, mapping, replicates=20)
    weighted = report["groups"][0]["intervals"]["weighted"]
    assert not weighted["supported"] and weighted["rates"] is None and weighted["reasons"]


def test_missing_assessments_remain_in_coverage_and_undefined_draws_not_redrawn():
    rows, mapping = fixture()
    for row in rows:
        if row["problem_id"] != "0":
            row.update(status="missing", machine_score=None)
    report = calibration_intervals(rows, mapping, replicates=80)
    group = report["groups"][0]
    assert group["unweighted"]["coverage"] == .25
    missing = group["intervals"]["unweighted_sensitivity"]["rates"]["sensitivity"]
    assert 0 < missing["undefined_replicates"] < 80
    assert missing["valid_replicates"] + missing["undefined_replicates"] == 80
    assert missing["lower"] is None and missing["upper"] is None
    assert group["intervals"]["weighted"]["rates"]["coverage"]["upper"] < 1


def test_single_cluster_has_no_inferential_interval_and_empty_is_valid():
    rows, mapping = fixture()
    report = calibration_intervals(rows[:2], mapping, replicates=20)
    assert not report["groups"][0]["intervals"]["unweighted_sensitivity"]["supported"]
    assert calibration_intervals([], [], replicates=20)["groups"] == []


@pytest.mark.parametrize("kwargs", [{"replicates": True}, {"replicates": 1}, {"replicates": 10001}, {"seed": -1}])
def test_invalid_options_are_rejected(kwargs):
    rows, mapping = fixture()
    with pytest.raises(ValueError):
        calibration_intervals(rows, mapping, **kwargs)


def test_duplicate_or_nontext_stratum_mapping_is_rejected():
    rows, mapping = fixture()
    with pytest.raises(ValueError, match="duplicate"):
        calibration_intervals(rows, mapping + [mapping[0]], replicates=20)
    mapping[0]["stratum"] = None
    with pytest.raises(ValueError, match="mapping"):
        calibration_intervals(rows, mapping, replicates=20)


def test_cli_uses_explicit_inputs_and_refuses_existing_output(tmp_path):
    import json
    from victorique.calibration_intervals import main
    rows, mapping = fixture()
    assessments, strata, output = (tmp_path / name for name in ("assessments.json", "strata.json", "report.json"))
    assessments.write_text(json.dumps(rows), encoding="utf-8")
    strata.write_text(json.dumps(mapping), encoding="utf-8")
    args = [str(assessments), str(strata), "--replicates", "20", "--out", str(output)]
    assert main(args) == 0
    original = output.read_bytes()
    assert json.loads(original)["interval_protocol"]["replicates"] == 20
    with pytest.raises(SystemExit):
        main(args)
    assert output.read_bytes() == original
