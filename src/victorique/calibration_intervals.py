"""Approximate, problem-clustered calibration intervals."""
from __future__ import annotations

from collections import Counter
import math
import random

from .calibration import GROUP_FIELDS, _statistics, calibrate


MEASURES = {
    "sensitivity": ("tp", ("tp", "fn")),
    "specificity": ("tn", ("tn", "fp")),
    "false_accept_rate": ("fp", ("tn", "fp")),
    "false_reject_rate": ("fn", ("tp", "fn")),
    "coverage": ("paired", ("total",)),
    "positive_coverage": ("paired_positive", ("human_positive",)),
    "negative_coverage": ("paired_negative", ("human_negative",)),
}
COUNTS = {key for numerator, denominator in MEASURES.values() for key in (numerator, *denominator)}


def _percentile(values, probability):
    ordered = sorted(values)
    index = (len(ordered) - 1) * probability
    lower = math.floor(index)
    upper = min(lower + 1, len(ordered) - 1)
    return ordered[lower] + (ordered[upper] - ordered[lower]) * (index - lower)


def _rates(cells, weights):
    totals = dict.fromkeys(COUNTS, 0.0)
    for cell, weight in zip(cells, weights):
        for name in COUNTS:
            totals[name] += cell["counts"][name] * weight
    result = {}
    for name, (numerator, denominator) in MEASURES.items():
        divisor = sum(totals[key] for key in denominator)
        result[name] = totals[numerator] / divisor if divisor else None
    return result


def _interval(values, replicates):
    finite = [value for value in values if value is not None and math.isfinite(value)]
    missing = replicates - len(finite)
    return {"lower": _percentile(finite, .025) if not missing else None,
            "upper": _percentile(finite, .975) if not missing else None,
            "valid_replicates": len(finite), "undefined_replicates": missing,
            "reason": "undefined replicate rate; no redraw or conditional interval" if missing else None}


def calibration_intervals(assessments, strata, *, replicates=10000, seed=20261113,
                          human_threshold=7, machine_threshold=7):
    """Add percentile intervals without filling missing observations.

    ``strata`` is a list of dataset_id/sample_id/stratum records, kept outside
    verifier inputs. Cluster multipliers are shared across all configurations,
    samples and assessments of a problem. Missing design information disables
    weighted intervals, while retaining descriptive estimates and sensitivity.
    """
    if type(replicates) is not int or not 2 <= replicates <= 10000:
        raise ValueError("replicates must be an integer in [2, 10000]")
    if type(seed) is not int or not 0 <= seed < 2**32:
        raise ValueError("seed must be a 32-bit nonnegative integer")
    if not isinstance(strata, list):
        raise ValueError("strata must be an explicit list")
    mapping = {}
    for item in strata:
        if (not isinstance(item, dict) or set(item) != {"dataset_id", "sample_id", "stratum"}
                or any(type(value) is not str or not value.strip() for value in item.values())):
            raise ValueError("invalid stratum mapping")
        key = (item["dataset_id"], item["sample_id"])
        if key in mapping:
            raise ValueError("duplicate stratum mapping")
        mapping[key] = item["stratum"]
    report = calibrate(assessments, human_threshold=human_threshold, machine_threshold=machine_threshold)
    members = {}
    dataset_clusters = {}
    for row in report["assessments"]:
        key = tuple(row[field] for field in GROUP_FIELDS)
        cluster = (row["dataset_id"], row["problem_id"])
        dataset_clusters.setdefault(row["dataset_id"], set()).add(cluster)
        counts = _statistics([row], human_threshold, machine_threshold)
        counts.update(paired_positive=counts["tp"] + counts["fn"], paired_negative=counts["tn"] + counts["fp"])
        members.setdefault(key, []).append({"cluster": cluster,
            "stratum": mapping.get((row["dataset_id"], row["sample_id"])),
            "pi": row["inclusion_probability"], "counts": {key: counts[key] for key in COUNTS}})
    dataset_clusters = {key: sorted(value) for key, value in sorted(dataset_clusters.items())}
    prepared = []
    for group in report["groups"]:
        cells = members[tuple(group[field] for field in GROUP_FIELDS)]
        reasons = []
        if any(cell["pi"] is None for cell in cells):
            reasons.append("missing inclusion probability")
        if any(cell["stratum"] is None for cell in cells):
            reasons.append("missing stratum mapping")
        strata_clusters = {}
        for cell in cells:
            strata_clusters.setdefault(cell["stratum"], set()).add(cell["cluster"])
        if any(len(clusters) < 2 for clusters in strata_clusters.values()):
            reasons.append("a sampling stratum has fewer than two problem clusters")
        totals = {} if reasons else {
            stratum: sum(1 / cell["pi"] for cell in cells if cell["stratum"] == stratum)
            for stratum in strata_clusters}
        prepared.append((group, cells, reasons, totals,
                         {name: [] for name in MEASURES}, {name: [] for name in MEASURES}))
    multiplier_rng = random.Random(seed)
    cluster_rng = random.Random(seed)
    for _ in range(replicates):
        multipliers = {cluster: multiplier_rng.expovariate(1)
                       for clusters in dataset_clusters.values() for cluster in clusters}
        frequencies = {}
        for clusters in dataset_clusters.values():
            frequencies.update(Counter(cluster_rng.choices(clusters, k=len(clusters))))
        for _, cells, reasons, totals, weighted_draws, unweighted_draws in prepared:
            unweighted = _rates(cells, [frequencies.get(cell["cluster"], 0) for cell in cells])
            for name, value in unweighted.items():
                unweighted_draws[name].append(value)
            if reasons:
                continue
            raw = [multipliers[cell["cluster"]] / cell["pi"] for cell in cells]
            drawn = {stratum: sum(weight for cell, weight in zip(cells, raw) if cell["stratum"] == stratum)
                     for stratum in totals}
            if any(not math.isfinite(value) or value <= 0 for value in drawn.values()):
                weighted = dict.fromkeys(MEASURES)
            else:
                weights = [weight * totals[cell["stratum"]] / drawn[cell["stratum"]]
                           for cell, weight in zip(cells, raw)]
                weighted = _rates(cells, weights)
            for name, value in weighted.items():
                weighted_draws[name].append(value)
    for group, cells, reasons, _, weighted_draws, unweighted_draws in prepared:
        unweighted_supported = len({cell["cluster"] for cell in cells}) >= 2
        group["intervals"] = {
            "weighted": {"supported": not reasons, "reasons": reasons,
                         "rates": None if reasons else {key: _interval(value, replicates) for key, value in weighted_draws.items()}},
            "unweighted_sensitivity": {"supported": unweighted_supported,
                "reason": None if unweighted_supported else "fewer than two problem clusters",
                "rates": {key: _interval(value, replicates) for key, value in unweighted_draws.items()}
                if unweighted_supported else None},
        }
    report["interval_protocol"] = {"method": "problem Exp(1) multiplier; stratum-calibrated inverse probability weights",
        "sensitivity": "unweighted problem-cluster resampling within dataset",
        "replicates": replicates, "seed": seed, "confidence": .95,
        "approximate": True, "finite_population_exact": False,
        "undefined_replicates": "counted; no redraw; bounds null if any rate is undefined",
        "transfer_to_target_population": "requires an additional calibration-transfer assumption"}
    return report


def main(argv=None):
    import argparse
    import json
    from pathlib import Path
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assessments", help="explicit assessment JSON or CSV")
    parser.add_argument("strata", help="JSON list mapping dataset/sample IDs to sampling strata")
    parser.add_argument("--replicates", type=int, default=10000)
    parser.add_argument("--seed", type=int, default=20261113)
    parser.add_argument("--out", help="new report file; existing files are never overwritten")
    args = parser.parse_args(argv)
    try:
        mapping = json.loads(Path(args.strata).read_text(encoding="utf-8"))
        report = calibration_intervals(args.assessments, mapping, replicates=args.replicates, seed=args.seed)
        rendered = json.dumps(report, ensure_ascii=not bool(args.out), indent=2, allow_nan=False) + "\n"
        if args.out:
            with Path(args.out).open("x", encoding="utf-8") as stream:
                stream.write(rendered)
        else:
            print(rendered, end="")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
