"""Evaluate review decisions against human labels, retaining incomplete samples."""
import argparse
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from time import monotonic
from uuid import uuid4

from . import adaptive_contracts as c, settings
from .harness.safety import checked_path
from .verify import verify, _write


def _rate(numerator, denominator):
    return None if denominator == 0 else numerator / denominator


def aggregate(samples):
    """Rates use all labelled examples; conditional rates expose completion bias."""
    groups = {}
    for mode in dict.fromkeys(row["mode"] for row in samples):
        rows = [row for row in samples if row["mode"] == mode]
        correct = [row for row in rows if row["label"] == "correct"]
        incorrect = [row for row in rows if row["label"] == "incorrect"]
        completed = [row for row in rows if row["outcome"] != "incomplete"]
        false_accepts = sum(row["outcome"] == "pass" for row in incorrect)
        false_rejects = sum(row["outcome"] == "fail" for row in correct)
        complete_correct = sum(row["outcome"] != "incomplete" for row in correct)
        complete_incorrect = sum(row["outcome"] != "incomplete" for row in incorrect)
        keys = ("input_tokens", "output_tokens", "cached_input_tokens", "reasoning_output_tokens")
        tokens = {key: sum(row["tokens"][key] for row in rows)
                  if all(type(row.get("tokens")) is dict and type(row["tokens"].get(key)) is int
                         for row in rows) else None for key in keys}
        groups[mode] = {"samples": len(rows), "correct": len(correct), "incorrect": len(incorrect),
            "completed": len(completed), "incomplete": len(rows) - len(completed),
            "incomplete_correct": len(correct) - complete_correct,
            "incomplete_incorrect": len(incorrect) - complete_incorrect,
            "coverage": _rate(len(completed), len(rows)),
            "false_accepts": false_accepts, "false_rejects": false_rejects,
            "false_accept_rate": _rate(false_accepts, len(incorrect)),
            "false_reject_rate": _rate(false_rejects, len(correct)),
            "completed_false_accept_rate": _rate(false_accepts, complete_incorrect),
            "completed_false_reject_rate": _rate(false_rejects, complete_correct),
            "calls_used": sum(row["calls_used"] for row in rows),
            "wall_seconds": sum(row["wall_seconds"] for row in rows), "tokens": tokens,
            "token_usage_unknown_samples": sum(not isinstance(row.get("tokens"), dict)
                or any(row["tokens"].get(key) is None for key in ("input_tokens", "output_tokens"))
                for row in rows)}
    return groups


def calibrate(dataset, *, modes=("single", "stepwise", "multi"), passes=1, calls=12,
              hours=2, model=None, transport="fake", out=None, call_timeout_seconds=None):
    """Read a JSON list with id, human label, proof path and problem path.

    The deadline bounds the entire batch. Each sample/mode also has a call cap,
    and each review call the per-call limit (None: 600 seconds).
    Human labels are kept out of all model request packets.
    """
    c.need(type(modes) in (list, tuple) and 1 <= len(modes) <= 3
           and all(mode in ("single", "stepwise", "multi") for mode in modes)
           and len(set(modes)) == len(modes), "unique review modes required")
    c.integer(passes, "passes", 1, 100)
    c.integer(calls, "calls", 1, 100)
    call_timeout_seconds = settings.call_timeout_seconds(call_timeout_seconds)
    c.need(type(hours) in (int, float) and math.isfinite(hours) and hours > 0,
           "hours must be a positive finite number")
    c.need(transport in ("fake", "codex"), "transport must be fake or codex")
    path = checked_path(Path(dataset).absolute())
    rows = json.loads(path.read_text(encoding="utf-8"))
    c.need(type(rows) is list and 1 <= len(rows) <= 1000, "dataset must contain 1..1000 samples")
    inputs, ids = [], set()
    for row in rows:
        c.fields(row, {"id", "label", "proof", "problem"}, "calibration sample")
        c.identifier(row["id"])
        c.need(row["id"] not in ids, "duplicate sample id")
        ids.add(row["id"])
        c.need(row["label"] in ("correct", "incorrect"), "human label must be correct or incorrect")
        paths = {key: checked_path((path.parent / row[key]).absolute()) for key in ("proof", "problem")}
        for key, source in paths.items():
            c.text(source.read_bytes().decode("utf-8"), key + " content")
        inputs.append((row, paths))
    directory = checked_path(Path(out).absolute() if out is not None else Path.cwd() / "runs" / ("calibrate-" + uuid4().hex))
    c.need(not directory.exists(), "output directory already exists")
    started = monotonic()
    try:
        deadline = datetime.now(timezone.utc) + timedelta(hours=hours)
    except OverflowError as error:
        raise ValueError("hours exceeds the supported date range") from error
    directory.mkdir(parents=True)
    samples = []
    for index, (row, paths) in enumerate(inputs):
        for mode in modes:
            remaining = (deadline - datetime.now(timezone.utc)).total_seconds()
            if remaining <= 0:
                result = {"outcome": "incomplete", "reason": "batch_deadline", "calls_used": 0,
                          "wall_seconds": 0, "tokens": None}
            else:
                result = verify(paths["proof"], problem=paths["problem"], mode=mode,
                                passes=passes, calls=calls, hours=remaining / 3600,
                                model=model, transport=transport,
                                out=directory / (str(index + 1) + "-" + mode),
                                call_timeout_seconds=call_timeout_seconds,
                                _deadline=deadline.isoformat())
            samples.append({"id": row["id"], "label": row["label"], "mode": mode,
                            **{key: result[key] for key in ("outcome", "reason", "calls_used", "wall_seconds", "tokens")}})
            report = {"synthetic": transport == "fake", "deadline_utc": deadline.isoformat(),
                      "calls_limit": len(inputs) * len(modes) * calls,
                      "wall_seconds": monotonic() - started, "modes": aggregate(samples), "samples": samples}
            _write(directory / "calibration.json", report)
    return report


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("dataset", help="JSON list of human-labelled proof/problem file paths")
    parser.add_argument("--modes", nargs="+", choices=("single", "stepwise", "multi"),
                        default=["single", "stepwise", "multi"])
    parser.add_argument("--passes", type=int, default=1)
    parser.add_argument("--calls", type=int, default=12, help="Maximum calls per sample and mode")
    parser.add_argument("--hours", type=float, default=2, help="Whole-batch deadline")
    parser.add_argument("--model")
    parser.add_argument("--transport", choices=("fake", "codex"), default="fake")
    parser.add_argument("--out")
    parser.add_argument("--call-timeout-seconds", type=settings.seconds, metavar="SECONDS",
                        help=f"limit for each review call (default {settings.CALL_TIMEOUT_SECONDS})")
    try:
        result = calibrate(**vars(parser.parse_args(argv)))
    except (ValueError, OSError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
