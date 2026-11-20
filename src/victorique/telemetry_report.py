"""Read-only summaries of calls.jsonl; no transport, credentials or new ledger."""
from __future__ import annotations

import argparse
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
from statistics import median

from .telemetry import TOKEN_KEYS


def utc_time(value):
    """Accept timezone-explicit ISO 8601 UTC timestamps only."""
    if not isinstance(value, str):
        raise ValueError("UTC timestamp required")
    try:
        result = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as error:
        raise ValueError("invalid UTC timestamp: " + value) from error
    if result.utcoffset() != timedelta(0):
        raise ValueError("timestamp must specify UTC (Z or +00:00)")
    return result.astimezone(timezone.utc)


def _time(value):
    try:
        return utc_time(value)
    except ValueError:
        return None


def _counter(value):
    return value if type(value) is int and value >= 0 else None


def _stats(values):
    values = sorted(v for v in values if v is not None)
    return {"n": len(values), "median": median(values) if values else None,
            "p90": values[math.ceil(.9 * len(values)) - 1] if values else None}


def load_calls(directories):
    """Deduplicate copied calls by (run_id, request_id), reject contradictions."""
    rows, seen, paths = [], {}, set()
    duplicates = 0
    for directory in directories:
        path = (Path(directory) / "calls.jsonl").resolve()
        if path in paths:
            continue
        paths.add(path)
        with path.open(encoding="utf-8") as stream:
            for number, line in enumerate(stream, 1):
                if not line.strip():
                    continue
                try:
                    row = json.loads(line)
                    if not isinstance(row, dict):
                        raise ValueError("call must be an object")
                    if row.get("tokens") is not None and not isinstance(row["tokens"], dict):
                        raise ValueError("tokens must be an object or null")
                    if any(row.get(k) is not None and not isinstance(row[k], str)
                           for k in ("role", "status", "transport")):
                        raise ValueError("call labels must be strings or null")
                except ValueError as error:
                    raise ValueError(f"{path}:{number}: invalid call JSON") from error
                identity = (row.get("run_id"), row.get("request_id"))
                if all(isinstance(v, str) and v for v in identity):
                    if identity in seen:
                        if seen[identity] != row:
                            raise ValueError(f"conflicting duplicate call: {identity!r}")
                        duplicates += 1
                        continue
                    seen[identity] = row
                rows.append(row)
    return rows, duplicates


def _duration(row):
    started, ended = (_time(row.get(k)) for k in ("started_at_utc", "ended_at_utc"))
    return (ended - started).total_seconds() if started and ended and ended >= started else None


def _peak(rows, start, end):
    events, missing = [], 0
    for row in rows:
        left, right = (_time(row.get(k)) for k in ("started_at_utc", "ended_at_utc"))
        if left is None or right is None or right < left:
            missing += 1
            continue
        if start:
            left, right = max(left, start), min(right, end)
        if left < right:
            events.extend(((left, 1), (right, -1)))
    active = peak = 0
    for _, delta in sorted(events):  # End before start at the same instant.
        active += delta
        peak = max(peak, active)
    return peak, missing


def _summary(rows, hours):
    counts = Counter(row.get("status") for row in rows)
    completed = counts["completed"]
    failures = sum(counts[s] for s in ("failed", "returned_invalid", "returned_invalid_model", "timed_out"))
    missing = {key: sum(_counter((r.get("tokens") or {}).get(key)) is None for r in rows)
               for key in TOKEN_KEYS}
    token_stats = {key: _stats([_counter((r.get("tokens") or {}).get(key)) for r in rows])
                   for key in TOKEN_KEYS}
    token_totals = {key: sum((r.get("tokens") or {})[key] for r in rows)
                    if rows and not missing[key] else None for key in TOKEN_KEYS}
    return {"calls": len(rows), "completed_calls": completed, "failed_calls": failures,
            "unknown_calls": len(rows) - completed - failures, "timed_out_calls": counts["timed_out"],
            "completed_calls_per_hour": completed / hours if hours is not None else None,
            "missing_model_calls": sum(not r.get("model") for r in rows),
            "missing_identity_calls": sum(not all(isinstance(r.get(k), str) and r[k]
                                                 for k in ("run_id", "request_id")) for r in rows),
            "missing_duration_calls": sum(_duration(r) is None for r in rows),
            "missing_tokens": missing, "tokens": token_stats, "token_totals": token_totals,
            "elapsed_seconds": _stats([_duration(r) for r in rows])}


def report(directories, *, start=None, end=None):
    """Count completions in [start,end); overlap intervals for peak concurrency."""
    if (start is None) != (end is None):
        raise ValueError("--start and --end must be supplied together")
    start, end = (utc_time(v) if v is not None else None for v in (start, end))
    if start is not None and end <= start:
        raise ValueError("--end must be after --start")
    rows, duplicates = load_calls(directories)
    selected = rows if start is None else [r for r in rows if (t := _time(r.get("ended_at_utc"))) is not None
                                           and start <= t < end]
    hours = (end - start).total_seconds() / 3600 if start else None
    roles, transports = defaultdict(list), defaultdict(list)
    for row in selected:
        roles[row.get("role") or "unknown"].append(row)
        transports[row.get("transport") or "unknown"].append(row)
    peak, missing_intervals = _peak(rows, start, end)
    result = {"window": {"start": start.isoformat(), "end": end.isoformat(), "hours": hours} if start else None,
              "input_calls": len(rows), "duplicate_rows": duplicates,
              "input_missing_end_calls": sum(_time(r.get("ended_at_utc")) is None for r in rows),
              "peak_concurrency": peak, "input_missing_interval_calls": missing_intervals,
              "overall": _summary(selected, hours),
              "by_role": {role: _summary(values, hours) for role, values in sorted(roles.items())},
              "by_transport": {name: _summary(values, hours) for name, values in sorted(transports.items())}}
    return result


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="+")
    parser.add_argument("--start", help="inclusive UTC ISO timestamp")
    parser.add_argument("--end", help="exclusive UTC ISO timestamp")
    args = parser.parse_args(argv)
    try:
        result = report(args.run_dirs, start=args.start, end=args.end)
    except (OSError, ValueError) as error:
        parser.error(str(error))
    print(json.dumps(result, ensure_ascii=True, allow_nan=False, indent=2))


if __name__ == "__main__":
    main()
