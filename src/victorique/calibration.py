"""Offline calibration of explicit human and machine 0--7 assessments."""
import argparse
import csv
import json
import math
from pathlib import Path


IDENTIFIERS = ("dataset_id", "problem_id", "sample_id", "assessment_id",
               "grader_config", "grader_model", "grader_family", "grader_role")
REQUIRED = set(IDENTIFIERS) | {"human_score", "machine_score", "status"}
OPTIONAL = {"inclusion_probability", "configured_model", "configured_model_source"}
GROUP_FIELDS = ("dataset_id", "grader_config", "grader_model", "grader_family", "grader_role")
STATUSES = ("completed", "missing", "timeout", "invalid", "error")


def _score(value, name, *, nullable=True):
    if nullable and value in (None, ""):
        return None
    if isinstance(value, str) and value in tuple(str(n) for n in range(8)):
        value = int(value)
    if type(value) is not int or not 0 <= value <= 7:
        raise ValueError(f"{name} must be an integer from 0 to 7")
    return value


def _validate(rows):
    result, assessment_ids, samples = [], set(), {}
    for index, raw in enumerate(rows, 1):
        if not isinstance(raw, dict) or not REQUIRED <= raw.keys():
            raise ValueError(f"row {index}: required fields are {', '.join(sorted(REQUIRED))}")
        if raw.keys() - REQUIRED - OPTIONAL:
            raise ValueError(f"row {index}: unknown fields")
        row = dict(raw)
        for field in IDENTIFIERS:
            if field == "grader_model" and row[field] in (None, ""):
                row[field] = None
                continue
            if not isinstance(row[field], str) or not row[field].strip():
                raise ValueError(f"row {index}: {field} must be a nonempty string")
            if row[field] != row[field].strip():
                raise ValueError(f"row {index}: {field} has surrounding whitespace")
        for field in ("configured_model", "configured_model_source"):
            value = row.get(field)
            if value in (None, ""):
                row[field] = None
            elif not isinstance(value, str) or not value.strip() or value != value.strip():
                raise ValueError(f"{field} must be a nonempty string or null")
            else:
                row[field] = value
        if (row["configured_model"] is None) != (row["configured_model_source"] is None):
            raise ValueError("configured_model and configured_model_source must be supplied together")
        if row["assessment_id"] in assessment_ids:
            raise ValueError(f"duplicate assessment_id: {row['assessment_id']}")
        assessment_ids.add(row["assessment_id"])
        if row["grader_role"] not in ("primary", "secondary"):
            raise ValueError("grader_role must be primary or secondary")
        if row["status"] not in STATUSES:
            raise ValueError(f"status must be one of {', '.join(STATUSES)}")
        for field in ("human_score", "machine_score"):
            row[field] = _score(row[field], field)
        if row["status"] != "completed" and row["machine_score"] is not None:
            raise ValueError("non-completed assessment must have a null machine_score")
        probability = row.get("inclusion_probability")
        if probability in (None, ""):
            probability = None
        else:
            if isinstance(probability, bool):
                raise ValueError("inclusion_probability must be in (0, 1]")
            try:
                probability = float(probability)
            except (TypeError, ValueError) as error:
                raise ValueError("inclusion_probability must be in (0, 1]") from error
            if not math.isfinite(probability) or not 0 < probability <= 1 or not math.isfinite(1 / probability):
                raise ValueError("inclusion_probability must be in (0, 1] with a finite inverse")
        row["inclusion_probability"] = probability
        sample_key = (row["dataset_id"], row["sample_id"])
        identity = (row["problem_id"], row["human_score"], probability)
        if sample_key in samples and samples[sample_key] != identity:
            raise ValueError("a sample must keep the same problem, human_score and inclusion_probability")
        samples[sample_key] = identity
        result.append(row)
    return result


def read_assessments(path):
    """Read only the explicit JSON list or CSV file; validate the row contract."""
    path = Path(path)
    if path.suffix.lower() == ".json":
        rows = json.loads(path.read_text(encoding="utf-8-sig"))
        if not isinstance(rows, list):
            raise ValueError("JSON input must be a list of assessment objects")
    elif path.suffix.lower() == ".csv":
        with path.open(encoding="utf-8-sig", newline="") as handle:
            reader = csv.DictReader(handle)
            fields = reader.fieldnames or []
            if len(fields) != len(set(fields)) or not REQUIRED <= set(fields):
                raise ValueError("CSV requires unique headers and all required fields")
            rows = list(reader)
    else:
        raise ValueError("input must have a .csv or .json extension")
    return _validate(rows)


def _rate(numerator, denominator):
    return numerator / denominator if denominator else None


def _statistics(rows, human_threshold, machine_threshold, *, weighted=False):
    counts = dict.fromkeys(("total", "human_available", "machine_available", "paired",
                            "human_positive", "human_negative", "missing_human", "missing_machine",
                            "missing_both", "unpaired", "missing_machine_positive",
                            "missing_machine_negative", "tp", "tn", "fp", "fn"), 0)
    status_counts = dict.fromkeys(STATUSES, 0)
    for row in rows:
        weight = 1 / row["inclusion_probability"] if weighted else 1
        human, machine = row["human_score"], row["machine_score"]
        positive = human is not None and human >= human_threshold
        available = row["status"] == "completed" and machine is not None
        flags = {"total": True, "human_available": human is not None, "machine_available": available,
                 "paired": human is not None and available, "missing_human": human is None,
                 "missing_machine": not available, "missing_both": human is None and not available,
                 "unpaired": human is None or not available,
                 "human_positive": positive, "human_negative": human is not None and not positive,
                 "missing_machine_positive": positive and not available,
                 "missing_machine_negative": human is not None and not positive and not available}
        if human is not None and available:
            accepted = machine >= machine_threshold
            flags[("tp" if accepted else "fn") if positive else ("fp" if accepted else "tn")] = True
        for key, include in flags.items():
            if include:
                counts[key] += weight
        status_counts[row["status"]] += weight
    if any(not math.isfinite(value) for value in counts.values()):
        raise ValueError("inverse-probability totals overflow; probabilities are too small")
    tp, tn, fp, fn = (counts[key] for key in ("tp", "tn", "fp", "fn"))
    return {**counts, "status_counts": status_counts,
            "sensitivity": _rate(tp, tp + fn), "specificity": _rate(tn, tn + fp),
            "false_accept_rate": _rate(fp, tn + fp), "false_reject_rate": _rate(fn, tp + fn),
            "coverage": _rate(counts["paired"], counts["total"]),
            "human_coverage": _rate(counts["human_available"], counts["total"]),
            "machine_coverage": _rate(counts["machine_available"], counts["total"]),
            "positive_coverage": _rate(tp + fn, counts["human_positive"]),
            "negative_coverage": _rate(tn + fp, counts["human_negative"])}


def calibrate(assessments, *, human_threshold=7, machine_threshold=7):
    """Return descriptive grouped calibration and retained problem clusters.

    ``assessments`` is an iterable of row dictionaries or an explicit CSV/JSON
    path. Rates condition on paired observed labels; missing rows remain in
    coverage denominators. Repeated assessments are not independent trials.
    """
    human_threshold = _score(human_threshold, "human_threshold", nullable=False)
    machine_threshold = _score(machine_threshold, "machine_threshold", nullable=False)
    rows = (read_assessments(assessments) if isinstance(assessments, (str, Path))
            else _validate(assessments))
    grouped = {}
    for row in rows:
        grouped.setdefault(tuple(row[field] for field in GROUP_FIELDS), []).append(row)
    groups = []
    for key, members in sorted(grouped.items(), key=lambda item: tuple(value or "" for value in item[0])):
        clusters = {}
        for row in members:
            clusters.setdefault(row["problem_id"], {}).setdefault(row["sample_id"], []).append(row["assessment_id"])
        complete_probabilities = all(row["inclusion_probability"] is not None for row in members)
        groups.append({**dict(zip(GROUP_FIELDS, key)), "human_threshold": human_threshold,
                       "machine_threshold": machine_threshold,
                       "problem_count": len(clusters),
                       "sample_count": len({row["sample_id"] for row in members}),
                       "clusters": clusters,
                       "unweighted": _statistics(members, human_threshold, machine_threshold),
                       "weighted": _statistics(members, human_threshold, machine_threshold, weighted=True)
                       if complete_probabilities else None,
                       "weighted_reason": None if complete_probabilities else "inclusion_probability missing for one or more assessments"})
    return {"schema_version": 1, "count_unit": "assessment", "cluster_unit": "dataset_id/problem_id",
            "rate_denominator": "paired observed human and completed machine scores",
            "groups": groups, "assessments": rows}


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("assessments", help="Explicit CSV or JSON assessment file")
    parser.add_argument("--human-threshold", type=int, default=7)
    parser.add_argument("--machine-threshold", type=int, default=7)
    parser.add_argument("--out", help="Write report JSON to this new file; otherwise print to stdout")
    args = vars(parser.parse_args(argv))
    out = args.pop("out")
    try:
        report = calibrate(**args)
        rendered = json.dumps(report, ensure_ascii=not bool(out), indent=2, allow_nan=False) + "\n"
        if out:
            with Path(out).open("x", encoding="utf-8") as handle:
                handle.write(rendered)
        else:
            print(rendered, end="")
    except (ValueError, OSError) as error:
        parser.error(str(error))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
