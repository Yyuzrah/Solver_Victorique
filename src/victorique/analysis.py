"""Offline statistical primitives; no transport or experiment execution."""
from __future__ import annotations

import argparse
from collections import defaultdict
from html import escape
import itertools
import json
import math
from pathlib import Path
import random
from statistics import NormalDist, fmean, variance
import sys


def _number(value, name, low=None, high=None):
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
        raise ValueError(f"{name} must be finite")
    if (low is not None and value < low) or (high is not None and value > high):
        raise ValueError(f"{name} outside supported range")
    return value


def _integer(value, name, low=0):
    _number(value, name, low)
    if not isinstance(value, int):
        raise ValueError(f"{name} must be an integer")
    return value


def _settings(draws, seed, confidence):
    _integer(draws, "draws", 1)
    _integer(seed, "seed", 0)
    _number(confidence, "confidence", 0, 1)
    if not 0 < confidence < 1:
        raise ValueError("confidence must be strictly between zero and one")


def _bool(value, name, nullable=False):
    if value is None and nullable:
        return value
    if type(value) is not bool:
        raise ValueError(f"{name} must be a boolean" + (" or null" if nullable else ""))
    return value


def _identifier(value, name):
    if not isinstance(value, str) or not value.strip():
        raise ValueError(f"{name} must be a nonempty string")
    return value


def _unweighted(rows):
    unsupported = {"weight", "weights", "inclusion_probability", "sampling_probability",
                   "sampling_weight", "sampling"}
    if any(unsupported.intersection(row) for row in rows):
        raise ValueError("weighted or sampling-probability designs are not supported")


def _quantile(values, probability):
    ordered = sorted(values)
    position = (len(ordered) - 1) * probability
    left = int(position)
    right = min(left + 1, len(ordered) - 1)
    return ordered[left] + (ordered[right] - ordered[left]) * (position - left)


def _interval(values, confidence):
    valid = [x for x in values if x is not None and math.isfinite(x)]
    result = {"valid_draws": len(valid), "invalid_draws": len(values) - len(valid),
              "confidence": confidence, "two_sided": None,
              "one_sided_lower": None, "one_sided_upper": None}
    # Conditioning on defined ratios would silently change the distribution.
    if not valid or len(valid) != len(values):
        return result
    alpha = 1 - confidence
    result.update(two_sided=[_quantile(valid, alpha / 2), _quantile(valid, 1 - alpha / 2)],
                  one_sided_lower=_quantile(valid, alpha),
                  one_sided_upper=_quantile(valid, confidence))
    return result


def wilson(successes, total, confidence=0.95):
    """Wilson score interval for a simple independent binomial proportion."""
    _integer(successes, "successes")
    _integer(total, "total")
    _settings(1, 0, confidence)
    if successes > total:
        raise ValueError("successes exceeds total")
    if not total:
        return {"estimate": None, "interval": None, "total": 0}
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    p = successes / total
    divisor = 1 + z * z / total
    center = (p + z * z / (2 * total)) / divisor
    half = z * math.sqrt(p * (1 - p) / total + z * z / (4 * total * total)) / divisor
    return {"estimate": p, "interval": [max(0.0, center - half), min(1.0, center + half)],
            "total": total, "confidence": confidence}


def holm(p_values):
    """Adjusted p-values for one explicitly named comparison family."""
    if not isinstance(p_values, dict):
        raise ValueError("p_values must map comparison names to p-values")
    for name, p in p_values.items():
        _identifier(name, "comparison name")
        _number(p, "p-value", 0, 1)
    ordered = sorted(p_values, key=p_values.get)
    maximum = 0.0
    result = {}
    for index, name in enumerate(ordered):
        maximum = max(maximum, (len(ordered) - index) * p_values[name])
        result[name] = min(1.0, maximum)
    return result


def sign_flip(differences, *, seed=20261115, draws=99999, alternative="two-sided"):
    """Problem-level sign flips; requires symmetric/exchangeable paired errors."""
    _settings(draws, seed, 0.95)
    if alternative not in {"two-sided", "greater", "less"}:
        raise ValueError("unsupported alternative")
    differences = list(differences)
    if not differences:
        raise ValueError("at least one paired problem required")
    for value in differences:
        _number(value, "difference")
    nonzero = [value for value in differences if value != 0]
    observed = math.fsum(differences)
    def extreme(value):
        tolerance = 1e-12 * math.fsum(abs(x) for x in differences)
        if alternative == "two-sided":
            return abs(value) >= abs(observed) - tolerance
        return value >= observed - tolerance if alternative == "greater" else value <= observed + tolerance
    exact = len(nonzero) <= 20
    rng = random.Random(seed)
    signs = itertools.product((-1, 1), repeat=len(nonzero)) if exact else (
        [rng.choice((-1, 1)) for _ in nonzero] for _ in range(draws))
    count = sum(extreme(math.fsum(s * d for s, d in zip(vector, nonzero))) for vector in signs)
    evaluations = 2 ** len(nonzero) if exact else draws
    return {"estimate": observed / len(differences), "paired_problems": len(differences),
            "nonzero_problems": len(nonzero), "method": "exact" if exact else "monte_carlo",
            "alternative": alternative, "draws": evaluations, "seed": seed,
            "p_value": count / evaluations if exact else (count + 1) / (draws + 1)}


def _clusters(rows):
    rows = list(rows)
    if not rows:
        raise ValueError("at least one planned row required")
    _unweighted(rows)
    groups = defaultdict(lambda: defaultdict(dict))
    seen = set()
    for original in rows:
        row = dict(original)
        problem = _identifier(row.get("problem_id"), "problem_id")
        arm = _identifier(row.get("arm"), "arm")
        rep = _identifier(row.get("repetition"), "repetition")
        key = (problem, arm, rep)
        if key in seen:
            raise ValueError("duplicate problem/arm/repetition")
        seen.add(key)
        for field in ("delivered", "correct"):
            if field not in row:
                raise ValueError(f"explicit {field} (possibly null) required")
            _bool(row[field], field, True)
        if "tokens" not in row:
            raise ValueError("explicit tokens (possibly null) required")
        for field in ("tokens", "token_upper_bound"):
            if row.get(field) is not None:
                _number(row[field], field, 0)
        if "endpoint" in row:
            _identifier(row["endpoint"], "endpoint")
        if row.get("tokens") is not None and row.get("token_upper_bound") is not None:
            if row["tokens"] > row["token_upper_bound"]:
                raise ValueError("tokens exceeds token_upper_bound")
        groups[problem][arm][rep] = row
    arms = sorted({row["arm"] for row in rows})
    for problem in groups.values():
        if sorted(problem) != arms or len({tuple(sorted(x)) for x in problem.values()}) != 1:
            raise ValueError("all arms need explicit rows for every planned repetition in each problem")
    return {p: {a: list(groups[p][a].values()) for a in arms} for p in sorted(groups)}


def _row_bounds(row, metric):
    if metric == "tokens":
        return (row["tokens"], row["tokens"]) if row["tokens"] is not None else (0.0, row.get("token_upper_bound"))
    if metric == "delivery":
        value = row["delivered"]
    elif metric == "draft_correctness":
        value = row["correct"]
    elif metric == "correct_delivery":
        d, y = row["delivered"], row["correct"]
        value = False if d is False or y is False else (True if d is True and y is True else None)
    else:
        raise ValueError("unsupported metric")
    return (float(value), float(value)) if value is not None else (0.0, 1.0)


def _mean_bounds(rows, metric):
    bounds = [_row_bounds(row, metric) for row in rows]
    return (fmean(x[0] for x in bounds),
            None if any(x[1] is None for x in bounds) else fmean(x[1] for x in bounds))


def _point(bounds):
    return bounds[0] if bounds[0] == bounds[1] else None


def _ratio(a, b):
    return a / b if a is not None and b is not None and b > 0 else None


def _summarize(rows):
    n = len(rows)
    delivered = sum(row["delivered"] is True for row in rows)
    unknown_d = sum(row["delivered"] is None for row in rows)
    correct = sum(row["delivered"] is True and row["correct"] is True for row in rows)
    unknown_y_d = sum(row["delivered"] is True and row["correct"] is None for row in rows)
    known_tokens = [r["tokens"] for r in rows if r["tokens"] is not None]
    known_grades = [r["correct"] for r in rows if r["correct"] is not None]
    known_endpoint = [_point(_row_bounds(r, "correct_delivery")) for r in rows]
    known_endpoint = [value for value in known_endpoint if value is not None]
    endpoint_counts = defaultdict(int)
    for row in rows:
        endpoint_counts[row.get("endpoint", "unspecified")] += 1
    delivery_bounds = _mean_bounds(rows, "delivery")
    quality_bounds = _mean_bounds(rows, "correct_delivery")
    # Precision with unknown delivery is deliberately unavailable: D and DY covary.
    precision_bounds = [correct / delivered, (correct + unknown_y_d) / delivered] if delivered and not unknown_d else None
    return {"planned_runs": n, "endpoint_counts": dict(endpoint_counts),
            "known_deliveries": delivered, "unknown_delivery": unknown_d,
            "known_nondeliveries": sum(r["delivered"] is False for r in rows),
            "missing_grades": sum(r["correct"] is None for r in rows),
            "incorrect_deliveries": sum(r["delivered"] is True and r["correct"] is False for r in rows),
            "correct_deliveries": correct,
            "correct_delivery_rate": {"estimate": _point(quality_bounds), "bounds": quality_bounds},
            "delivery_rate": {"estimate": _point(delivery_bounds), "bounds": delivery_bounds},
            "delivery_precision": {"estimate": _point(precision_bounds) if precision_bounds else None,
                                   "bounds": precision_bounds},
            "available_delivery_precision": _ratio(correct, delivered - unknown_y_d),
            "available_draft_correctness": _ratio(sum(known_grades), len(known_grades)),
            "available_correct_delivery_rate": _ratio(sum(known_endpoint), len(known_endpoint)),
            "known_correct_delivery_outcomes": len(known_endpoint),
            "known_token_subtotal": sum(known_tokens), "missing_usage": n - len(known_tokens),
            "total_tokens": sum(known_tokens) if len(known_tokens) == n else None}


def paired_bootstrap(rows, arm_a, arm_b, *, metric="correct_delivery", operation="difference",
                     draws=10000, seed=20261115, confidence=0.95):
    """Resample complete problem clusters after averaging repetitions within them."""
    _settings(draws, seed, confidence)
    groups = _clusters(rows)
    if operation not in {"difference", "ratio"}:
        raise ValueError("operation must be difference or ratio")
    if any(arm_a not in group or arm_b not in group for group in groups.values()):
        raise ValueError("unknown comparison arm")
    pairs = [(_mean_bounds(group[arm_a], metric), _mean_bounds(group[arm_b], metric)) for group in groups.values()]
    def statistic(sample):
        a = (fmean(x[0][0] for x in sample), None if any(x[0][1] is None for x in sample) else fmean(x[0][1] for x in sample))
        b = (fmean(x[1][0] for x in sample), None if any(x[1][1] is None for x in sample) else fmean(x[1][1] for x in sample))
        if operation == "difference":
            bounds = (a[0] - b[1] if b[1] is not None else None,
                      a[1] - b[0] if a[1] is not None else None)
            point = _point(a) - _point(b) if _point(a) is not None and _point(b) is not None else None
        else:
            bounds = (0.0 if b[1] is None else _ratio(a[0], b[1]), _ratio(a[1], b[0]))
            point = _ratio(_point(a), _point(b))
        return point, bounds
    rng = random.Random(seed)
    samples = [statistic([rng.choice(pairs) for _ in pairs]) for _ in range(draws)]
    point, bounds = statistic(pairs)
    return {"arm_a": arm_a, "arm_b": arm_b, "metric": metric, "operation": operation,
            "problems": len(pairs), "draws": draws, "seed": seed, "estimate": point,
            "missing_result_bounds": bounds,
            "interval": _interval([x[0] for x in samples] if point is not None else [None] * draws, confidence),
            "bound_intervals": {"lower": _interval([x[1][0] for x in samples], confidence),
                                "upper": _interval([x[1][1] for x in samples], confidence)},
            "complete_problem_pairs": sum(_point(a) is not None and _point(b) is not None for a, b in pairs)}


def token_efficiency(rows, arm, *, draws=10000, seed=20261115, confidence=0.95):
    """Observed policy efficiency; ratios of equally weighted problem means."""
    _settings(draws, seed, confidence)
    groups = _clusters(rows)
    if arm not in next(iter(groups.values())):
        raise ValueError("unknown arm")
    pairs = [(_point(_mean_bounds(g[arm], "correct_delivery")), _point(_mean_bounds(g[arm], "tokens"))) for g in groups.values()]
    def estimate(sample):
        if any(y is None or t is None for y, t in sample):
            return None, None
        y, t = fmean(x[0] for x in sample), fmean(x[1] for x in sample)
        return _ratio(y * 1_000_000, t), _ratio(t, y)
    rng = random.Random(seed)
    samples = [estimate([rng.choice(pairs) for _ in pairs]) for _ in range(draws)]
    value = estimate(pairs)
    return {"arm": arm, "draws": draws, "seed": seed,
            "correct_deliveries_per_million_tokens": value[0], "tokens_per_correct_delivery": value[1],
            "efficiency_interval": _interval([x[0] for x in samples], confidence),
            "cost_per_correct_interval": _interval([x[1] for x in samples], confidence),
            "coverage": _summarize([r for g in groups.values() for r in g[arm]])}


def diagnostics(rows):
    """Optional explicit initial/final correctness labels; trigger targets errors."""
    matrix = {"correct_to_correct": 0, "correct_to_incorrect": 0,
              "incorrect_to_correct": 0, "incorrect_to_incorrect": 0}
    tp = fp = fn = tn = triggered = trigger_known = paired = 0
    for row in rows:
        before = _bool(row.get("initial_correct"), "initial_correct", True)
        after = _bool(row.get("final_correct"), "final_correct", True)
        trigger = _bool(row.get("triggered"), "triggered", True)
        if before is not None and after is not None:
            initial_version = _identifier(row.get("initial_version"), "initial_version")
            final_version = _identifier(row.get("final_version"), "final_version")
            if initial_version == final_version and before != after:
                raise ValueError("conflicting labels for the same candidate/version")
            matrix[("correct" if before else "incorrect") + "_to_" + ("correct" if after else "incorrect")] += 1
        if trigger is not None:
            trigger_known += 1
            triggered += trigger
        if trigger is not None and before is not None:
            paired += 1
            tp += trigger and not before
            fp += trigger and before
            fn += not trigger and not before
            tn += not trigger and before
    return {"transition_matrix": matrix, "transition_labeled": sum(matrix.values()),
            "transition_missing": len(rows) - sum(matrix.values()), "trigger_labeled": paired,
            "trigger_missing_labels": len(rows) - paired, "trigger_rate": _ratio(triggered, trigger_known),
            "trigger_observed": trigger_known, "true_positive": tp, "false_positive": fp,
            "false_negative": fn, "true_negative": tn,
            "trigger_precision": _ratio(tp, tp + fp), "trigger_recall": _ratio(tp, tp + fn)}


def pareto_points(points):
    """Mark nondominated observed cost/quality points; never interpolate outcomes."""
    for point in points:
        _identifier(point.get("arm"), "arm")
        _number(point.get("cost"), "cost", 0)
        _number(point.get("quality"), "quality", 0, 1)
    return [dict(p, pareto=not any(q["cost"] <= p["cost"] and q["quality"] >= p["quality"]
                and (q["cost"] < p["cost"] or q["quality"] > p["quality"]) for q in points)) for p in points]


def pareto_svg(points):
    points = pareto_points(points)
    maximum = max([p["cost"] for p in points] + [1])
    parts = ['<svg xmlns="http://www.w3.org/2000/svg" width="720" height="440" viewBox="0 0 720 440">',
             '<rect width="720" height="440" fill="white"/>',
             '<text x="40" y="25">Observed cost / quality (no interpolation)</text>',
             '<path d="M60 60 V370 H650" fill="none" stroke="black"/>',
             '<text x="60" y="410">Mean measured tokens per problem/run →</text>',
             '<text x="5" y="60">1.0</text><text x="5" y="370">0.0</text>',
             '<text x="65" y="390">0</text>',
             f'<text x="590" y="390">{maximum:g}</text>']
    for p in points:
        x, y = 60 + 570 * p["cost"] / maximum, 370 - 300 * p["quality"]
        color = "#126b31" if p["pareto"] else "#777777"
        parts.extend([f'<circle cx="{x:g}" cy="{y:g}" r="5" fill="{color}"/>',
                      f'<text x="{x + 7:g}" y="{y - 7:g}">{escape(p["arm"])}</text>'])
    return "\n".join(parts + ["</svg>"])


def rogan_gladen(q, sensitivity, specificity, *, minimum_denominator=0.05):
    """Raw external-error correction; flagged estimates are never clipped."""
    for value, name in ((q, "q"), (sensitivity, "sensitivity"), (specificity, "specificity")):
        _number(value, name, 0, 1)
    _number(minimum_denominator, "minimum_denominator", 0, 1)
    denominator = sensitivity + specificity - 1
    raw = (q + specificity - 1) / denominator if denominator != 0 else None
    issues = []
    if denominator <= 0:
        issues.append("nonpositive_denominator")
    elif denominator < minimum_denominator:
        issues.append("unstable_denominator")
    if raw is not None and not 0 <= raw <= 1:
        issues.append("out_of_range")
    return {"q": q, "sensitivity": sensitivity, "specificity": specificity,
            "denominator": denominator, "raw_estimate": raw, "issues": issues,
            "minimum_denominator": minimum_denominator, "calibration_uncertainty_propagated": False}


def rogan_gladen_bootstrap(calibration_rows, target_rows, *, draws=10000, seed=20261115,
                           confidence=0.95, minimum_denominator=0.05):
    """Independent problem bootstrap in calibration and target populations."""
    _settings(draws, seed, confidence)
    _number(minimum_denominator, "minimum_denominator", 0, 1)
    def group(rows, calibration):
        _unweighted(rows)
        groups = defaultdict(list)
        seen = set()
        for row in rows:
            problem = _identifier(row.get("problem_id"), "problem_id")
            sample = _identifier(row.get("sample_id"), "sample_id")
            if sample in seen:
                raise ValueError("duplicate sample_id")
            seen.add(sample)
            prediction = _bool(row.get("predicted"), "predicted")
            if calibration:
                label = _bool(row.get("label"), "label")
                groups[problem].append((float(label and prediction), float(label),
                                        float(not label and not prediction), float(not label)))
            else:
                groups[problem].append((float(prediction),))
        if not groups:
            raise ValueError("nonempty calibration and target samples required")
        return {p: tuple(fmean(x[i] for x in groups[p]) for i in range(len(groups[p][0]))) for p in sorted(groups)}
    c, t = group(calibration_rows, True), group(target_rows, False)
    if set(c) & set(t):
        raise ValueError("independent calibration/target clusters required; overlap not supported")
    def estimate(cs, ts):
        se = _ratio(sum(x[0] for x in cs), sum(x[1] for x in cs))
        sp = _ratio(sum(x[2] for x in cs), sum(x[3] for x in cs))
        if se is None or sp is None:
            return {"raw_estimate": None, "issues": ["missing_calibration_class"]}
        return rogan_gladen(fmean(x[0] for x in ts), se, sp, minimum_denominator=minimum_denominator)
    cv, tv = list(c.values()), list(t.values())
    result = estimate(cv, tv)
    rng = random.Random(seed)
    samples = [estimate([rng.choice(cv) for _ in cv], [rng.choice(tv) for _ in tv]) for _ in range(draws)]
    issues = defaultdict(int)
    for sample in samples:
        for issue in sample["issues"]:
            issues[issue] += 1
    result.update(calibration_uncertainty_propagated=True, draws=draws, seed=seed,
                  calibration_problems=len(c), target_problems=len(t), draw_issues=dict(issues),
                  raw_interval=_interval([s["raw_estimate"] for s in samples], confidence),
                  usable=not result["issues"] and not issues)
    return result


def ppi_iid_mean(labeled_rows, unlabeled_rows, *, design, confidence=0.95):
    """Optional PPI++ Example 6.1 IID mean special case, not a default analysis."""
    _settings(1, 0, confidence)
    required = {"labeled_sampling": "random_target", "unlabeled_sampling": "iid_target", "independent": True}
    if design != required:
        raise ValueError("PPI++ only supports random target labels and independent IID target predictions; targeted/external/weighted designs not supported")
    if len(labeled_rows) < 2 or len(unlabeled_rows) < 2:
        raise ValueError("at least two labeled and two unlabeled observations required")
    _unweighted([*labeled_rows, *unlabeled_rows])
    seen = set()
    for row in [*labeled_rows, *unlabeled_rows]:
        problem = _identifier(row.get("problem_id"), "problem_id")
        if problem in seen:
            raise ValueError("repeated problem clusters violate this IID special case")
        seen.add(problem)
        _number(row.get("prediction"), "prediction")
    y = [_number(row.get("label"), "label") for row in labeled_rows]
    f_l = [row["prediction"] for row in labeled_rows]
    f_u = [row["prediction"] for row in unlabeled_rows]
    n, big_n = len(y), len(f_u)
    pooled = f_l + f_u
    mean_f, mean_y = fmean(f_l), fmean(y)
    pooled_mean = fmean(pooled)
    pooled_var = fmean((f - pooled_mean) ** 2 for f in pooled)
    covariance = fmean((label - mean_y) * (f - mean_f) for label, f in zip(y, f_l))
    lam = covariance / ((1 + n / big_n) * pooled_var) if pooled_var else 0.0
    estimate = mean_y + lam * (fmean(f_u) - mean_f)
    residuals = [label - lam * f for label, f in zip(y, f_l)]
    standard_error = math.sqrt(variance(residuals) / n + lam * lam * variance(f_u) / big_n)
    z = NormalDist().inv_cdf((1 + confidence) / 2)
    return {"estimate": estimate, "lambda": lam, "lambda_clipped": False,
            "status": "classical_fallback_zero_prediction_variance" if not pooled_var else "ppi_iid_mean",
            "standard_error": standard_error, "interval": [estimate - z * standard_error, estimate + z * standard_error],
            "interval_kind": "asymptotic", "confidence": confidence,
            "finite_sample_guarantee": False, "design": dict(design), "labeled_n": n, "unlabeled_n": big_n}


def analyze(rows, *, comparisons=(), draws=10000, seed=20261115, confidence=0.95):
    """Analyze a single declared provider/population; no inferred strata or labels."""
    _settings(draws, seed, confidence)
    groups = _clusters(rows)
    arms = sorted(next(iter(groups.values())))
    report = {"status": "analysis_foundation", "problem_count": len(groups),
              "draws": draws, "seed": seed, "confidence": confidence, "arms": {}, "comparisons": []}
    points = []
    for arm in arms:
        arm_rows = [row for group in groups.values() for row in group[arm]]
        weighted = {metric: tuple(None if any(b[i] is None for b in bounds) else fmean(b[i] for b in bounds) for i in (0, 1))
                    for metric in ("correct_delivery", "delivery", "draft_correctness", "tokens")
                    for bounds in [[_mean_bounds(group[arm], metric) for group in groups.values()]]}
        report["arms"][arm] = {"run_counts": _summarize(arm_rows), "problem_mean_bounds": weighted,
                              "diagnostics": diagnostics(arm_rows),
                              "efficiency": token_efficiency(rows, arm, draws=draws, seed=seed, confidence=confidence)}
        quality, cost = _point(weighted["correct_delivery"]), _point(weighted["tokens"])
        if quality is not None and cost is not None:
            points.append({"arm": arm, "quality": quality, "cost": cost})
    for spec in comparisons:
        a, b = spec["arm_a"], spec["arm_b"]
        comparison = paired_bootstrap(rows, a, b, metric=spec.get("metric", "correct_delivery"),
                                      operation=spec.get("operation", "difference"), draws=draws, seed=seed, confidence=confidence)
        if comparison["operation"] == "difference":
            pairs = [(_point(_mean_bounds(group[a], comparison["metric"])), _point(_mean_bounds(group[b], comparison["metric"]))) for group in groups.values()]
            comparison["sign_flip"] = sign_flip([x - y for x, y in pairs], seed=seed) if all(x is not None and y is not None for x, y in pairs) else {"status": "not_supported_with_missing_pairs"}
        report["comparisons"].append(comparison)
    report["pareto_points"] = pareto_points(points)
    report["pareto_omitted_arms"] = [a for a in arms if a not in {p["arm"] for p in points}]
    return report


def main(argv=None):
    """JSON CLI with the same functions and options as the Python API."""
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("input", type=Path, help="JSON object containing operation and arguments")
    parser.add_argument("--output", type=Path, help="JSON output (default stdout)")
    parser.add_argument("--svg", type=Path, help="observed Pareto points SVG (analyze only)")
    args = parser.parse_args(argv)
    operations = {f.__name__: f for f in (analyze, paired_bootstrap, token_efficiency, sign_flip, wilson,
                  holm, diagnostics, pareto_points, rogan_gladen, rogan_gladen_bootstrap, ppi_iid_mean)}
    try:
        payload = json.loads(args.input.read_text(encoding="utf-8"))
        operation = payload["operation"]
        if operation not in operations:
            raise ValueError("unsupported operation")
        if args.svg and operation != "analyze":
            raise ValueError("--svg requires analyze")
        result = operations[operation](**payload["arguments"])
        output = json.dumps(result, ensure_ascii=args.output is None, allow_nan=False, indent=2) + "\n"
        if args.output:
            args.output.write_text(output, encoding="utf-8")
        else:
            print(output, end="")
        if args.svg:
            args.svg.write_text(pareto_svg(result["pareto_points"]), encoding="utf-8")
    except (ValueError, TypeError, KeyError, OSError) as error:
        print(f"analysis: {error}", file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
