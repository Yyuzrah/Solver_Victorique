"""Hand-checkable synthetic statistical examples, without model/data access."""
import json
import io
import xml.etree.ElementTree as ET

import pytest

from victorique import analysis as a


def row(problem, arm, repetition="1", *, delivered=True, correct=True, tokens=10, **extra):
    return dict(problem_id=problem, arm=arm, repetition=repetition,
                delivered=delivered, correct=correct, tokens=tokens, **extra)


def paired():
    return [row("p1", "a", tokens=10), row("p1", "b", correct=False, tokens=20),
            row("p2", "a", correct=False, tokens=20), row("p2", "b", correct=False, tokens=40)]


def test_problem_means_precede_population_mean_and_keep_all_repetitions():
    rows = [row("p1", arm, str(rep), correct=arm == "a") for arm in ("a", "b") for rep in range(3)]
    rows += [row("p2", arm, correct=False) for arm in ("a", "b")]
    report = a.analyze(rows, comparisons=[{"arm_a": "a", "arm_b": "b"}], draws=100, seed=4)
    assert report["arms"]["a"]["run_counts"]["correct_delivery_rate"]["estimate"] == 0.75
    assert report["arms"]["a"]["problem_mean_bounds"]["correct_delivery"] == (0.5, 0.5)
    assert report["comparisons"][0]["estimate"] == 0.5
    assert report["comparisons"][0]["interval"]["two_sided"] == [0, 1]
    assert report["comparisons"][0]["sign_flip"]["paired_problems"] == 2


def test_paired_ratio_keeps_arms_together():
    result = a.paired_bootstrap(paired(), "a", "b", metric="tokens", operation="ratio", draws=100)
    assert result["estimate"] == 0.5
    assert result["interval"]["two_sided"] == [0.5, 0.5]
    assert result == a.paired_bootstrap(paired(), "a", "b", metric="tokens", operation="ratio", draws=100)


def test_identical_arms_and_all_zero_differences():
    rows = [row("p", arm, correct=False) for arm in ("a", "b")]
    result = a.paired_bootstrap(rows, "a", "b", draws=10)
    assert result["estimate"] == 0
    assert result["interval"]["two_sided"] == [0, 0]
    test = a.sign_flip([0, 0, 0])
    assert test["p_value"] == 1 and test["paired_problems"] == 3
    assert test["nonzero_problems"] == 0 and test["draws"] == 1


def test_unknown_grade_is_not_zero_and_non_delivery_is_known_zero():
    rows = [row("p", "a", correct=None), row("p", "b", delivered=False, correct=None)]
    result = a.analyze(rows, comparisons=[{"arm_a": "a", "arm_b": "b"}], draws=10)
    acounts, bcounts = (result["arms"][x]["run_counts"] for x in ("a", "b"))
    assert acounts["correct_delivery_rate"] == {"estimate": None, "bounds": (0, 1)}
    assert bcounts["correct_delivery_rate"] == {"estimate": 0, "bounds": (0, 0)}
    assert acounts["missing_grades"] == bcounts["missing_grades"] == 1
    comparison = result["comparisons"][0]
    assert comparison["estimate"] is None
    assert comparison["missing_result_bounds"] == (0, 1)
    assert comparison["interval"]["two_sided"] is None
    assert comparison["sign_flip"]["status"] == "not_supported_with_missing_pairs"
    assert acounts["available_correct_delivery_rate"] is None
    assert bcounts["available_correct_delivery_rate"] == 0
    assert bcounts["available_draft_correctness"] is None


def test_no_deliveries_and_abstention_draft_correctness():
    report = a.analyze([row("p", "a", delivered=False)], draws=5)
    result = report["arms"]["a"]
    assert result["run_counts"]["correct_delivery_rate"]["estimate"] == 0
    assert result["run_counts"]["delivery_rate"]["estimate"] == 0
    assert result["run_counts"]["delivery_precision"]["estimate"] is None
    assert result["problem_mean_bounds"]["draft_correctness"] == (1, 1)
    assert result["efficiency"]["correct_deliveries_per_million_tokens"] == 0
    assert result["efficiency"]["tokens_per_correct_delivery"] is None


def test_abstention_three_metrics_with_missing_and_execution_endpoint():
    rows = [row("p", "a", "1"), row("p", "a", "2", correct=False),
            row("p", "a", "3", delivered=False, endpoint="execution_failed"),
            row("p", "a", "4", correct=None)]
    result = a.analyze(rows, draws=5)["arms"]["a"]["run_counts"]
    assert result["correct_delivery_rate"]["bounds"] == (0.25, 0.5)
    assert result["delivery_rate"]["estimate"] == 0.75
    assert result["delivery_precision"]["bounds"] == [1 / 3, 2 / 3]
    assert result["available_delivery_precision"] == 0.5
    assert result["incorrect_deliveries"] == 1
    assert result["endpoint_counts"]["execution_failed"] == 1
    assert result["known_nondeliveries"] == 1
    assert "known_abstentions" not in result


def test_unknown_delivery_is_retained():
    result = a.analyze([row("p", "a", delivered=None)], draws=5)["arms"]["a"]["run_counts"]
    assert result["unknown_delivery"] == 1
    assert result["correct_delivery_rate"]["estimate"] is None
    assert result["delivery_precision"]["estimate"] is None


def test_missing_usage_subtotal_bounds_and_no_fake_pareto_point():
    rows = [row("p", "a", tokens=None, token_upper_bound=30), row("p", "b", tokens=10)]
    result = a.paired_bootstrap(rows, "a", "b", metric="tokens", operation="ratio", draws=10)
    assert result["estimate"] is None and result["missing_result_bounds"] == (0, 3)
    report = a.analyze(rows, draws=5)
    assert report["pareto_omitted_arms"] == ["a"]
    counts = report["arms"]["a"]["run_counts"]
    assert counts["missing_usage"] == 1 and counts["total_tokens"] is None
    assert counts["known_token_subtotal"] == 0
    rows[0].pop("token_upper_bound")
    result = a.paired_bootstrap(rows, "a", "b", metric="tokens", operation="ratio", draws=5)
    assert result["missing_result_bounds"] == (0, None)
    result = a.paired_bootstrap(rows, "b", "a", metric="tokens", operation="ratio", draws=5)
    assert result["missing_result_bounds"] == (0, None)


def test_zero_cost_ratio_denominator_is_undefined():
    rows = [row("p", "a"), row("p", "b", tokens=0)]
    result = a.paired_bootstrap(rows, "a", "b", metric="tokens", operation="ratio", draws=5)
    assert result["estimate"] is None and result["interval"]["two_sided"] is None
    assert result["interval"]["invalid_draws"] == 5


@pytest.mark.parametrize("rows,match", [
    ([row("p", "a"), row("p", "a")], "duplicate"),
    ([row("p", "a"), row("p", "b", "2")], "planned repetition"),
    ([row("p", "a"), row("q", "b")], "planned repetition"),
    ([row("p", "a", weight=1)], "weighted"),
    ([row("p", "a", tokens=-1)], "outside"),
    ([row("p", "a", tokens=float("nan"))], "finite"),
    ([row("p", "a", tokens=10, token_upper_bound=2)], "exceeds"),
    ([row("p", "a", correct=1)], "boolean"),
    ([], "planned row"),
])
def test_invalid_or_unsupported_rows_are_explicit(rows, match):
    with pytest.raises(ValueError, match=match):
        a.analyze(rows, draws=2)


def test_omitted_planned_values_must_be_explicit_null():
    value = row("p", "a")
    del value["correct"]
    with pytest.raises(ValueError, match="explicit correct"):
        a.analyze([value], draws=2)


def test_exact_sign_flip_hand_enumeration_and_tiny_scale():
    # Four sign vectors, two are as extreme as observed sum 3.
    assert a.sign_flip([1, 2, 0])["p_value"] == 0.5
    assert a.sign_flip([1, 2], alternative="greater")["p_value"] == 0.25
    assert a.sign_flip([-1, -2], alternative="less")["p_value"] == 0.25
    assert a.sign_flip([1e-15, 2e-15])["p_value"] == 0.5


def test_monte_carlo_plus_one_no_zero_pvalue():
    result = a.sign_flip([1] * 21, draws=31, seed=3)
    assert result["method"] == "monte_carlo"
    assert result["p_value"] == 1 / 32
    assert result == a.sign_flip([1] * 21, draws=31, seed=3)


def test_wilson_hand_calculated_endpoints_and_empty():
    assert a.wilson(0, 0)["interval"] is None
    z = 1.959963984540054
    bound = z * z / (10 + z * z)
    assert a.wilson(0, 10)["interval"] == pytest.approx([0, bound])
    assert a.wilson(10, 10)["interval"] == pytest.approx([1 - bound, 1])
    assert a.wilson(5, 10)["interval"] == pytest.approx([0.236593090512564, 0.763406909487436])
    with pytest.raises(ValueError):
        a.wilson(2, 1)


def test_holm_named_family_ties_monotonicity_and_bounds():
    assert a.holm({"c": 0.03, "a": 0.01, "b": 0.025, "d": 0.9}) == {
        "a": 0.04, "b": pytest.approx(0.075), "c": pytest.approx(0.075), "d": 0.9}
    assert a.holm({"a": 0.6, "b": 0.6}) == {"a": 1, "b": 1}


def test_token_efficiency_bootstraps_numerator_and_denominator_jointly():
    result = a.token_efficiency(paired(), "a", draws=100)
    assert result["correct_deliveries_per_million_tokens"] == pytest.approx(1_000_000 / 30)
    assert result["tokens_per_correct_delivery"] == 30
    # Some resamples have no correct delivery: report invalid ratio draws, no conditional CI.
    assert result["cost_per_correct_interval"]["invalid_draws"] > 0
    assert result["cost_per_correct_interval"]["two_sided"] is None


def test_diagnostics_require_real_labels_and_version_bindings():
    rows = [dict(initial_correct=x, final_correct=y, triggered=t, initial_version="v1", final_version="v2")
            for x, y, t in [(True, True, False), (True, False, True), (False, True, True), (False, False, False)]]
    result = a.diagnostics(rows + [{"triggered": True}])
    assert set(result["transition_matrix"].values()) == {1}
    assert result["trigger_precision"] == result["trigger_recall"] == 0.5
    assert result["trigger_rate"] == 3 / 5 and result["trigger_missing_labels"] == 1
    assert a.diagnostics([{"triggered": True}])["trigger_precision"] is None
    with pytest.raises(ValueError, match="initial_version"):
        a.diagnostics([{"initial_correct": False, "final_correct": True}])
    with pytest.raises(ValueError, match="conflicting"):
        a.diagnostics([dict(initial_correct=False, final_correct=True, initial_version="v1", final_version="v1")])


def test_pareto_only_observed_points_and_escaped_svg():
    points = [{"arm": "a<&", "cost": 10, "quality": 0.5},
              {"arm": "b", "cost": 20, "quality": 0.4},
              {"arm": "c", "cost": 30, "quality": 0.7},
              {"arm": "d", "cost": 10, "quality": 0.5}]
    assert [p["pareto"] for p in a.pareto_points(points)] == [True, False, True, True]
    document = ET.fromstring(a.pareto_svg(points))
    assert len(document.findall("{http://www.w3.org/2000/svg}circle")) == 4
    assert len(document.findall("{http://www.w3.org/2000/svg}path")) == 1  # axes only


def test_rogan_gladen_raw_estimate_and_flags():
    assert a.rogan_gladen(0.6, 0.8, 0.9)["raw_estimate"] == pytest.approx(5 / 7)
    assert a.rogan_gladen(0.5, 0.5, 0.5)["raw_estimate"] is None
    assert a.rogan_gladen(0.5, 0.5, 0.5)["issues"] == ["nonpositive_denominator"]
    assert "nonpositive_denominator" in a.rogan_gladen(0.5, 0.2, 0.2)["issues"]
    assert "unstable_denominator" in a.rogan_gladen(0.5, 0.51, 0.5)["issues"]
    result = a.rogan_gladen(1, 0.8, 0.9)
    assert result["raw_estimate"] > 1 and "out_of_range" in result["issues"]
    assert result["calibration_uncertainty_propagated"] is False


def calibration():
    # Each problem has both classes, so every resample has defined se/sp.
    return [dict(problem_id=p, sample_id=p + str(label), label=label, predicted=prediction)
            for p, positives in [("c1", True), ("c2", False)]
            for label, prediction in [(True, positives), (False, False)]]


def test_rg_resamples_calibration_not_just_target_with_fixed_errors():
    target = [dict(problem_id="t1", sample_id="t", predicted=True)]
    result = a.rogan_gladen_bootstrap(calibration(), target, draws=100, seed=2)
    assert result["raw_estimate"] == 2
    assert result["calibration_uncertainty_propagated"] is True
    assert result["draw_issues"]["nonpositive_denominator"] > 0
    assert result["raw_interval"]["invalid_draws"] > 0
    assert result["raw_interval"]["two_sided"] is None and not result["usable"]
    assert result == a.rogan_gladen_bootstrap(calibration(), target, draws=100, seed=2)


def test_rg_perfect_calibration_and_no_target_variation():
    c = [dict(problem_id="c", sample_id=str(y), label=y, predicted=y) for y in (False, True)]
    t = [dict(problem_id="t", sample_id=str(y), predicted=y) for y in (False, True)]
    result = a.rogan_gladen_bootstrap(c, t, draws=20)
    assert result["raw_estimate"] == 0.5 and result["raw_interval"]["two_sided"] == [0.5, 0.5]
    assert result["usable"]
    with pytest.raises(ValueError, match="overlap"):
        a.rogan_gladen_bootstrap(c, [dict(problem_id="c", sample_id="t", predicted=True)], draws=2)
    with pytest.raises(ValueError, match="duplicate"):
        a.rogan_gladen_bootstrap(c + c, t, draws=2)


DESIGN = {"labeled_sampling": "random_target", "unlabeled_sampling": "iid_target", "independent": True}


def ppi_data():
    return ([dict(problem_id="l1", label=1, prediction=0), dict(problem_id="l2", label=0, prediction=1)],
            [dict(problem_id="u1", prediction=1), dict(problem_id="u2", prediction=1)])


def test_ppi_negative_lambda_author_mean_example_formula():
    labeled, unlabeled = ppi_data()
    result = a.ppi_iid_mean(labeled, unlabeled, design=DESIGN)
    # cov_n=-1/4; pooled var=3/16; n=N=2; lambda=-2/3.
    assert result["lambda"] == pytest.approx(-2 / 3)
    assert result["estimate"] == pytest.approx(1 / 6)
    assert result["standard_error"] == pytest.approx(1 / 6)
    assert result["interval_kind"] == "asymptotic" and not result["finite_sample_guarantee"]
    assert not result["lambda_clipped"]


def test_ppi_constant_prediction_classical_fallback():
    labeled, unlabeled = ppi_data()
    for record in labeled + unlabeled:
        record["prediction"] = 0.2
    result = a.ppi_iid_mean(labeled, unlabeled, design=DESIGN)
    assert result["status"] == "classical_fallback_zero_prediction_variance"
    assert result["estimate"] == result["standard_error"] == 0.5
    assert result["lambda"] == 0


@pytest.mark.parametrize("sampling", ["targeted_audit", "external_domain", "stratified"])
def test_ppi_rejects_unsupported_sampling(sampling):
    labeled, unlabeled = ppi_data()
    with pytest.raises(ValueError, match="not supported"):
        a.ppi_iid_mean(labeled, unlabeled, design=dict(DESIGN, labeled_sampling=sampling))


def test_ppi_rejects_cluster_repeats_and_weights():
    labeled, unlabeled = ppi_data()
    with pytest.raises(ValueError, match="repeated problem"):
        a.ppi_iid_mean(labeled, labeled, design=DESIGN)
    labeled[0]["weight"] = 1
    with pytest.raises(ValueError, match="weighted"):
        a.ppi_iid_mean(labeled, unlabeled, design=DESIGN)


@pytest.mark.parametrize("field", ["inclusion_probability", "sampling_probability", "sampling"])
def test_sampling_design_metadata_cannot_be_silently_ignored(field):
    with pytest.raises(ValueError, match="not supported"):
        a.analyze([row("p", "a", **{field: 0.5})], draws=2)
    c = calibration()
    c[0][field] = 0.5
    with pytest.raises(ValueError, match="not supported"):
        a.rogan_gladen_bootstrap(c, [dict(problem_id="t", sample_id="t1", predicted=True)], draws=2)
    labeled, unlabeled = ppi_data()
    labeled[0][field] = 0.5
    with pytest.raises(ValueError, match="not supported"):
        a.ppi_iid_mean(labeled, unlabeled, design=DESIGN)


def test_json_cli_matches_api_and_svg(tmp_path):
    source, output, svg = (tmp_path / name for name in ("input.json", "output.json", "plot.svg"))
    arguments = {"rows": paired(), "draws": 10, "seed": 7,
                 "comparisons": [{"arm_a": "a", "arm_b": "b"}]}
    source.write_text(json.dumps({"operation": "analyze", "arguments": arguments}), encoding="utf-8")
    assert a.main([str(source), "--output", str(output), "--svg", str(svg)]) == 0
    assert json.loads(output.read_text(encoding="utf-8")) == json.loads(json.dumps(a.analyze(**arguments)))
    ET.fromstring(svg.read_text(encoding="utf-8"))


def test_json_cli_validation_and_wilson(tmp_path, capsys):
    source = tmp_path / "request.json"
    source.write_text(json.dumps({"operation": "wilson", "arguments": {"successes": 1, "total": 2}}), encoding="utf-8")
    assert a.main([str(source)]) == 0
    assert json.loads(capsys.readouterr().out)["estimate"] == 0.5
    assert a.main([str(source), "--svg", str(tmp_path / "bad.svg")]) == 2
    assert "requires analyze" in capsys.readouterr().err
    source.write_text('{"operation": "no_model"}', encoding="utf-8")
    assert a.main([str(source)]) == 2


@pytest.mark.parametrize("encoding", ["ascii", "gbk"])
def test_json_console_escapes_unicode_but_utf8_file_keeps_it(tmp_path, monkeypatch, encoding):
    source, output = tmp_path / "request.json", tmp_path / "report.json"
    name = "中文−"
    source.write_text(json.dumps({"operation": "holm", "arguments": {"p_values": {name: 0.2}}}), encoding="utf-8")
    buffer = io.BytesIO()
    console = io.TextIOWrapper(buffer, encoding=encoding)
    monkeypatch.setattr(a.sys, "stdout", console)
    assert a.main([str(source)]) == 0
    console.flush()
    assert json.loads(buffer.getvalue().decode(encoding)) == {name: 0.2}
    assert a.main([str(source), "--output", str(output)]) == 0
    assert name in output.read_text(encoding="utf-8")


@pytest.mark.parametrize("arguments", [{"draws": 0}, {"draws": True}, {"confidence": 1}, {"seed": -1}])
def test_invalid_bootstrap_settings(arguments):
    with pytest.raises(ValueError):
        a.paired_bootstrap(paired(), "a", "b", **arguments)
