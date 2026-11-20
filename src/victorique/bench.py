"""Batch the public solve API with per-problem budgets and reference grading."""
import argparse
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from concurrent.futures import ThreadPoolExecutor
import json
import math
from pathlib import Path
import subprocess
import time
from uuid import uuid4

from . import settings, solve
from .grading import FakeGrader, GradingIncomplete, answer_match, grade_proof
from .harness.safety import checked_path


CATEGORIES = ("correct_delivery", "incorrect_delivery", "ungraded_delivery",
              "abstain", "failed", "timeout", "unknown")


def _read(path):
    return json.loads(checked_path(path).read_text(encoding="utf-8"))


def _write(path, value):
    checked_path(path).write_text(json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n", encoding="utf-8")


def _metadata(problem):
    path = problem.with_suffix(".json")
    data = _read(path) if path.exists() else {}
    if type(data) is not dict:
        raise ValueError(f"{path.name}: metadata must be an object")
    for key in ("reference_answer", "reference_solution", "grading_guidelines"):
        if data.get(key) is not None and (type(data[key]) is not str or not data[key].strip()):
            raise ValueError(f"{path.name}: {key} must be nonempty text")
    aliases = data.get("answer_aliases", [])
    if type(aliases) is not list or any(type(x) is not str or not x.strip() for x in aliases):
        raise ValueError(f"{path.name}: answer_aliases must be a list of nonempty strings")
    return data


def _grade(grader, *, problem, proof, metadata, directory, model, seconds, candidate_ref):
    """Freeze one private grading input and retain even a failed callable attempt."""
    from .harness.grade import grade_codex, grade_reference
    started = time.monotonic()
    result = {"status": "unknown", "score": None, "fully_correct": None,
              "calls_used": 1, "calls_limit": 1, "tokens": None, "model": None,
              "transport": grader if isinstance(grader, str) else "callable",
              "candidate_ref": candidate_ref}

    def invoke(request):
        if isinstance(grader, str):
            kwargs = dict(directory=directory, model=model, seconds=seconds, candidate_ref=candidate_ref)
            if grader == "codex":
                return grade_codex(request, **kwargs)
            return grade_reference(request, transport=grader, **kwargs)
        directory.mkdir()
        _write(directory / "request.json", {
            "transport": "callable", "context": request, "candidate_ref": candidate_ref,
            "calls_limit": 1, "deadline_utc": (datetime.now(timezone.utc) + timedelta(seconds=seconds)).isoformat()})
        _write(directory / "result.json", result)
        # A new JSON value prevents a callable primary from altering secondary inputs.
        return grader(deepcopy(request))

    try:
        value = grade_proof(invoke, problem=problem, candidate=proof,
                            reference_solution=metadata["reference_solution"],
                            guidelines=metadata.get("grading_guidelines", ""))
        result.update(value, status="completed")
    except GradingIncomplete as error:
        result.update(error.result)
    except Exception as error:
        result.update(status="timeout" if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) else "failed",
                      error=f"{type(error).__name__}: {error}")
    result["wall_seconds"] = time.monotonic() - started
    # Durable transports retain their complete public result (deadline, sources).
    if (directory / "result.json").exists():
        result = _read(directory / "result.json") | result
    if directory.exists():
        _write(directory / "result.json", result)
    return result


def _candidate_ref(directory):
    if not (directory / "state").exists():
        return None  # Custom solve functions may not expose a versioned candidate.
    from .api import _open, _snapshot
    from .adaptive_contracts import candidate_ref
    _, loop = _open(directory)
    endpoint = _snapshot(loop)["endpoint"]
    return candidate_ref(endpoint["root_candidate"]) if endpoint and endpoint["root_candidate"] else None


def _one(problem, metadata, directory, parameters, grader, solve_fn, grading_seconds=None,
         grader_model=None, secondary_grader=None, secondary_grader_model=None,
         grade_abstentions=False):
    started = time.monotonic()
    row = {"problem": problem.stem, "outcome": "unknown", "classification": "unknown",
           "calls_used": None, "tokens": None, "answer_match": "unknown",
           "proof_score": None, "grading_status": "not_requested", "grading_calls": 0,
           "grading_tokens": 0, "grading_wall_seconds": 0.0, "error": None,
           "fully_correct": None, "grades": {}}
    try:
        summary = solve_fn(problem, out=directory, **parameters)
        row.update({key: summary.get(key) for key in ("outcome", "calls_used", "tokens")})
    except Exception as error:
        row["outcome"] = "timeout" if isinstance(error, (TimeoutError, subprocess.TimeoutExpired)) else "failed"
        row["error"] = f"{type(error).__name__}: {error}"
        directory.mkdir(parents=True, exist_ok=True)
        # A failed/unknown request may already have consumed budget. Keep missing
        # telemetry unknown and retain whatever the public summary recorded.
        try:
            summary = _read(directory / "summary.json")
            row.update({key: summary.get(key) for key in ("calls_used", "tokens")})
        except (OSError, ValueError, TypeError, AttributeError):
            pass
    row["solve_wall_seconds"] = time.monotonic() - started
    row["usage"] = _solve_usage(directory, row, parameters["transport"])
    row["grading_usage"] = _combine_usage([], "grades")
    delivered = row["outcome"] == "complete"
    abstained = row["outcome"] == "abstain"
    if delivered or (abstained and grade_abstentions):
        row["classification"] = "ungraded_delivery" if delivered else "abstain"
        try:
            binding = _candidate_ref(directory) if grader is not None else None
            # An abstention can export a placeholder rather than a candidate.
            # Only a frozen endpoint candidate can enter optional draft grading.
            if abstained and binding is None:
                row["grading_status"] = "missing_candidate"
                row["wall_seconds"] = time.monotonic() - started
                return row
            proof = checked_path(directory / "proof.md").read_text(encoding="utf-8")
            row["answer_match"] = answer_match(proof, metadata.get("reference_answer"), metadata.get("answer_aliases", []))
            if grader is not None:
                if not metadata.get("reference_solution"):
                    row["grading_status"] = "missing_reference"
                else:
                    problem_text = problem.read_text(encoding="utf-8")
                    for role, selected, selected_model in (("primary", grader, grader_model),
                                                           ("secondary", secondary_grader, secondary_grader_model)):
                        if selected is not None:
                            row["grades"][role] = _grade(selected, problem=problem_text, proof=proof, metadata=metadata,
                                directory=directory / ("grading-" + role), model=selected_model,
                                seconds=grading_seconds, candidate_ref=binding)
                    primary = row["grades"]["primary"]
                    row.update(proof_score=primary.get("score"), fully_correct=primary.get("fully_correct"),
                               grading_status=primary["status"])
                    for key in ("reason", "model", "model_source", "effort", "effort_source", "error"):
                        row["grading_" + key] = primary.get(key)
                    grades = list(row["grades"].values())
                    row["grading_calls"] = sum(g["calls_used"] for g in grades)
                    tokens = [_token_total(g.get("tokens"), g.get("transport")) for g in grades]
                    row["grading_tokens"] = (grades[0].get("tokens") if len(grades) == 1 else
                                             sum(tokens) if all(t is not None for t in tokens) else None)
                    row["grading_wall_seconds"] = sum(g["wall_seconds"] for g in grades)
                    row["grading_usage"] = _combine_usage([
                        _usage(g.get("tokens"), g.get("transport"), "grade_result", g["calls_used"])
                        for g in grades], "grades")
            if delivered and metadata.get("reference_answer") is not None:
                # Short-answer accuracy is the primary metric when available.
                row["classification"] = {"match": "correct_delivery", "mismatch": "incorrect_delivery"}.get(row["answer_match"], "ungraded_delivery")
            elif delivered and row["proof_score"] is not None:
                row["classification"] = "correct_delivery" if row["proof_score"] == 7 else "incorrect_delivery"
        except (OSError, ValueError) as error:
            row["error"] = f"{type(error).__name__}: {error}"
    elif row["outcome"] in ("abstain", "failed", "timeout"):
        row["classification"] = row["outcome"]
    row["wall_seconds"] = time.monotonic() - started
    return row


def _total(rows, key):
    values = [row[key] for row in rows]
    known = [x for x in values if type(x) is int]
    return {"total": sum(known) if len(known) == len(values) else None,
            "known_subtotal": sum(known), "missing": len(values) - len(known)}


def _token_total(value, transport=None):
    """Count cache-inclusive input plus output, preserving missing counters."""
    if type(value) is int:
        return value if value >= 0 else None
    if type(value) is dict:
        if "input_tokens_including_cache" in value:
            # A present null means normalization could not establish the total.
            parts = [value["input_tokens_including_cache"], value.get("output_tokens")]
        elif transport in ("claude", "claude_cli"):
            parts = [value.get(key) for key in ("input_tokens", "cache_read_input_tokens",
                                                "cache_creation_input_tokens", "output_tokens")]
        elif transport in ("codex", "codex_cli"):
            # Codex input already includes cached input; never add it again.
            parts = [value.get("input_tokens"), value.get("output_tokens")]
        else:
            return None
        if all(type(x) is int and x >= 0 for x in parts):
            return sum(parts)
    return None


def _grading_token_total(row):
    grades = list(row["grades"].values())
    transport = grades[0].get("transport") if len(grades) == 1 else None
    return _token_total(row["grading_tokens"], transport)


def _usage(value, transport, source, calls):
    """Keep disjoint known counters even when a complete total is unavailable."""
    total = _token_total(value, transport)
    subtotal = total or 0
    if total is None and type(value) is dict:
        normalized = value.get("input_tokens_including_cache")
        if type(normalized) is int and normalized >= 0:
            parts = [normalized, value.get("output_tokens")]
        elif transport in ("claude", "claude_cli"):
            # Public calls use cached_input_tokens; raw grader usage uses
            # cache_read_input_tokens. These are aliases, never additive.
            read = value.get("cache_read_input_tokens", value.get("cached_input_tokens"))
            parts = [value.get("input_tokens"), read,
                     value.get("cache_creation_input_tokens"), value.get("output_tokens")]
        elif transport in ("codex", "codex_cli"):
            parts = [value.get("input_tokens"), value.get("output_tokens")]
        else:
            # Unknown cache semantics prevent a complete total, but raw input
            # and output still provide a lower bound without adding any cache.
            parts = [value.get("input_tokens"), value.get("output_tokens")]
        subtotal = sum(p for p in parts if type(p) is int and p >= 0)
    count = calls if type(calls) is int and calls >= 0 else None
    # An incomplete aggregate cannot tell us how many constituent calls lack
    # usage. A single grade is one call; a legacy summary may represent many.
    missing = 0 if total is not None else 1 if count == 1 else None
    return {"total": total, "known_subtotal": subtotal, "calls": count,
            "missing_calls": missing, "source": source}


def _combine_usage(usages, source):
    def complete_sum(key):
        values = [usage[key] for usage in usages]
        return sum(values) if all(value is not None for value in values) else None
    return {"total": complete_sum("total"),
            "known_subtotal": sum(usage["known_subtotal"] for usage in usages),
            "calls": complete_sum("calls"), "missing_calls": complete_sum("missing_calls"),
            "source": source}


def _solve_usage(directory, row, transport):
    """Read only this run's public projection, never its database or history."""
    try:
        lines = checked_path(directory / "calls.jsonl").read_text(encoding="utf-8").splitlines()
    except (OSError, ValueError):
        return _usage(row["tokens"], transport, "summary", row["calls_used"])
    usages = []
    for line in lines:
        if not line.strip():
            continue
        try:
            call = json.loads(line)
        except ValueError:
            call = None
        call = call if type(call) is dict else {}
        usages.append(_usage(call.get("tokens"), call.get("transport", transport), "call", 1))
    count = row["calls_used"]
    if type(count) is int and count > len(usages):
        # An interrupted export can omit calls that the summary already counted.
        usages.extend(_usage(None, transport, "call", 1) for _ in range(count - len(usages)))
    combined = _combine_usage(usages, "calls.jsonl")
    if type(count) is not int or count < len(usages):
        # A known prefix alone cannot establish that an interrupted export
        # covered every call. A conflicting summary count is also inconclusive.
        combined.update(total=None, missing_calls=None, coverage="unconfirmed")
    return combined


def _usage_total(rows, key):
    usages = [row[key] for row in rows]
    combined = _combine_usage(usages, key)
    return {key: combined[key] for key in ("total", "known_subtotal", "missing_calls")} | {
        "missing": sum(usage["total"] is None for usage in usages)}


def _cell(value):
    return "unknown" if value is None else str(value).replace("|", "\\|").replace("\n", " ")


def _report(result):
    lines = ["# Benchmark", "", f"Policy: {result['policy']}; transport: {result['transport']}; "
             f"synthetic: {str(result['synthetic']).lower()}; calls per problem: {result['calls_limit']}; jobs: {result['jobs']}.",
             "", "Answer matching uses explicit final answers; proof scores use a separate reference-based 0–7 grader.",
             "For answer problems, correct/incorrect delivery means answer match/mismatch. For proof-only problems, a score of 7 is correct delivery.",
             "", "| Classification | Count |", "| --- | ---: |"]
    lines.extend(f"| {key} | {result['counts'][key]} |" for key in CATEGORIES)
    lines.extend(["", "| Problem | Outcome | Classification | Answer match | Proof /7 | Grading | Solve calls | Grade calls | Solve tokens | Grade tokens | Wall s | Error |",
                  "| --- | --- | --- | --- | ---: | --- | ---: | ---: | ---: | ---: | ---: | --- |"])
    for row in result["rows"]:
        cells = [row[k] for k in ("problem", "outcome", "classification", "answer_match", "proof_score", "grading_status", "calls_used", "grading_calls")]
        cells.extend([row["usage"]["total"], row["grading_usage"]["total"], f"{row['wall_seconds']:.3f}", row.get("error") or row.get("grading_error") or ""])
        lines.append("| " + " | ".join(_cell(x) for x in cells) + " |")
    lines.extend(["", "Primary and secondary grades are retained separately. Answer problems use answer matching for classification; proof-only problems use the primary grade.",
                  f"Additional grading call ceiling per eligible candidate: {result['grading_calls_limit_per_candidate']}.",
                  f"Grade abstained endpoint candidates: {str(result['grade_abstentions']).lower()}. An abstained draft's score does not change its abstain classification.",
                  "", "| Problem | Grader | Transport | Status | Score /7 | Fully correct | Model | Calls | Tokens |",
                  "| --- | --- | --- | --- | ---: | --- | --- | ---: | ---: |"])
    for row in result["rows"]:
        for role, grade in row["grades"].items():
            cells = [row["problem"], role] + [grade.get(key) for key in
                     ("transport", "status", "score", "fully_correct", "model", "calls_used")]
            cells.append(_token_total(grade.get("tokens"), grade.get("transport")))
            lines.append("| " + " | ".join(_cell(x) for x in cells) + " |")
    lines.extend(["", f"Batch wall time: {result['wall_seconds']:.3f} s.", ""])
    for key, value in result["totals"].items():
        missing_calls = f"; missing calls: {_cell(value['missing_calls'])}" if "missing_calls" in value else ""
        lines.append(f"{key}: {_cell(value['total'])}; known subtotal: {value['known_subtotal']}; missing rows: {value['missing']}{missing_calls}.")
    lines.append("Missing token rows count problems with incomplete usage; missing calls count individual calls with incomplete usage. "
                 "Missing calls are unknown when a summary cannot resolve them. Known subtotals include partial counters and are lower bounds.")
    return "\n".join(lines) + "\n"


def bench(problems_dir, *, policy="single", calls=12, jobs=1, hours=2, model=None,
          out=None, transport="codex", grader=None, solve_fn=None, grading_seconds=None,
          grader_model=None, same_family_secondary=False, secondary_grader_model=None,
          grade_abstentions=False, call_timeout_seconds=None, undecided_checks=None, minor_findings=None,
          timeout_retries=None):
    """Run each top-level *.md once; optional matching *.json holds references.

    `grader` is a callable accepting the grade_proof request; it must perform
    at most one bounded, fresh-context call. It has a separate grading budget.
    `call_timeout_seconds` limits each solver call (None: 600 seconds) and, unless
    `grading_seconds` is given, each grading call. `undecided_checks` and
    `minor_findings` are passed to every solve. `timeout_retries` (None: 1) is
    passed to each solve: how often a call that reached that limit is sent again.
    """
    if policy not in ("single", "ladder", "resample", "full"):
        raise ValueError("policy must be single, ladder, resample or full")
    if type(calls) is not int or not 2 <= calls <= 100:
        raise ValueError("calls must be an integer from 2 to 100")
    if type(jobs) is not int or not 1 <= jobs <= 4:
        raise ValueError("jobs must be an integer from 1 to 4")
    if type(hours) not in (int, float) or not math.isfinite(hours) or hours <= 0:
        raise ValueError("hours must be a positive finite number")
    if transport not in ("codex", "claude", "fake"):
        raise ValueError("transport must be codex, claude or fake")
    if grader is not None and grader not in ("codex", "claude", "fake") and not callable(grader):
        raise ValueError("grader must be a callable, codex, claude, fake, or None")
    if type(same_family_secondary) is not bool:
        raise ValueError("same_family_secondary must be boolean")
    if type(grade_abstentions) is not bool:
        raise ValueError("grade_abstentions must be boolean")
    if grader is None and (same_family_secondary or grader_model is not None or grade_abstentions):
        raise ValueError("a primary grader is required")
    if secondary_grader_model is not None and not same_family_secondary:
        raise ValueError("secondary_grader_model requires same_family_secondary")
    for selected_model in (grader_model, secondary_grader_model):
        if selected_model is not None and (type(selected_model) is not str or not selected_model or selected_model.startswith("-") or any(ch.isspace() for ch in selected_model)):
            raise ValueError("grader model must be an identifier")
    call_timeout_seconds = settings.call_timeout_seconds(call_timeout_seconds)
    undecided_checks = settings.undecided_checks(undecided_checks)
    minor_findings = settings.minor_findings(minor_findings)
    timeout_retries = settings.timeout_retries(timeout_retries)
    grading_seconds = (call_timeout_seconds if grading_seconds is None
                       else settings.check_seconds(grading_seconds, "grading seconds"))
    if (transport == "claude" or grader == "claude") and jobs != 1:
        raise ValueError("Claude batches require jobs=1 for serial execution")
    if transport == "codex" and policy in ("single", "ladder", "resample") and jobs > 2:
        raise ValueError("Codex single/ladder/resample batches allow at most 2 jobs (up to 4 concurrent checks)")
    if (isinstance(grader, FakeGrader) or grader == "fake") and transport != "fake":
        raise ValueError("fake grading requires fake transport")
    if isinstance(grader, FakeGrader):
        grader = "fake"
    secondary_grader = transport if same_family_secondary else None
    root = checked_path(Path(problems_dir).absolute())
    problems = sorted(checked_path(p) for p in root.glob("*.md") if p.is_file())
    if not problems:
        raise ValueError("problems directory must contain at least one *.md file")
    metadata = [_metadata(p) for p in problems]
    directory = checked_path(Path(out).absolute() if out is not None else Path.cwd() / "runs" / ("bench-" + uuid4().hex))
    if directory == root:
        raise ValueError("output directory must differ from the problems directory")
    directory.mkdir(parents=True, exist_ok=False)
    parameters = dict(policy=policy, calls=calls, hours=hours, model=model, transport=transport,
                      call_timeout_seconds=call_timeout_seconds, undecided_checks=undecided_checks,
                      minor_findings=minor_findings, timeout_retries=timeout_retries)
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        futures = [pool.submit(_one, p, meta, directory / p.stem, parameters, grader, solve_fn or solve, grading_seconds,
                               grader_model, secondary_grader, secondary_grader_model, grade_abstentions)
                   for p, meta in zip(problems, metadata)]
        rows = [future.result() for future in futures]
    result = {"policy": policy, "transport": transport, "synthetic": transport == "fake",
              "calls_limit": calls, "jobs": jobs, "hours_per_problem": hours,
              "call_timeout_seconds": call_timeout_seconds, "undecided_checks": undecided_checks,
              "minor_findings": minor_findings, "timeout_retries": timeout_retries,
              "grading_calls_limit_per_delivery": int(grader is not None) + int(same_family_secondary),
              "grading_calls_limit_per_candidate": int(grader is not None) + int(same_family_secondary),
              "grade_abstentions": grade_abstentions,
              "same_family_secondary": same_family_secondary,
              "grading_seconds_per_delivery": grading_seconds,
              "grading_seconds_per_candidate": grading_seconds,
              "wall_seconds": time.monotonic() - started, "rows": rows,
              "counts": {key: sum(row["classification"] == key for row in rows) for key in CATEGORIES},
              "totals": {key: _total(rows, key) for key in ("calls_used", "grading_calls")},
              "output_directory": str(directory)}
    for key, usage in (("tokens", "usage"), ("grading_tokens", "grading_usage")):
        result["totals"][key] = _usage_total(rows, usage)
    _write(directory / "bench.json", result)
    checked_path(directory / "BENCH.md").write_text(_report(result), encoding="utf-8")
    return result


def _arguments(parser):
    parser.add_argument("problems_dir")
    parser.add_argument("--policy", choices=("single", "ladder", "resample", "full"), default="single",
                        help="policy of every solve (default single; see victorique solve --help)")
    parser.add_argument("--calls", type=int, default=12)
    parser.add_argument("--jobs", type=int, default=1)
    parser.add_argument("--hours", type=float, default=2)
    parser.add_argument("--model")
    parser.add_argument("--out")
    parser.add_argument("--transport", choices=("codex", "claude", "fake"), default="codex")
    parser.add_argument("--grader", choices=("none", "fake", "codex", "claude"), default="none",
                        help="Primary reference grader: one extra call per eligible reference proof")
    parser.add_argument("--grader-model", help="Primary grader model (default: its own CLI default)")
    parser.add_argument("--grade-abstentions", action="store_true",
                        help="Also grade frozen best drafts on abstention, without changing their delivery status")
    parser.add_argument("--same-family-secondary", action="store_true",
                        help="Enable a separate solver-family grade: one additional call, two grading calls total per eligible candidate, including failures")
    parser.add_argument("--secondary-grader-model", help="Secondary model (default: its own CLI default)")
    parser.add_argument("--grading-seconds", type=settings.seconds, default=None,
                        help="Separate limit and deadline for each grading call in seconds "
                             "(0.05-86400; default: the per-call limit)")
    settings.add_arguments(parser)


def add_parser(subparsers):
    parser = subparsers.add_parser("bench", help="run a directory of problems with separate reference grading")
    _arguments(parser)
    return parser


def run_args(args):
    options = settings.command_settings(getattr(args, "config", None),
                                        call_timeout_seconds=getattr(args, "call_timeout_seconds", None),
                                        undecided_checks=getattr(args, "undecided_checks", None),
                                        minor_findings=getattr(args, "minor_findings", None),
                                        timeout_retries=getattr(args, "timeout_retries", None))
    return bench(args.problems_dir, policy=args.policy, calls=args.calls, jobs=args.jobs,
                 hours=args.hours, model=args.model, out=args.out, transport=args.transport,
                 grader=None if args.grader == "none" else args.grader,
                 grading_seconds=args.grading_seconds, grader_model=args.grader_model,
                 same_family_secondary=args.same_family_secondary, secondary_grader_model=args.secondary_grader_model,
                 grade_abstentions=args.grade_abstentions, **options)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    _arguments(parser)
    try:
        result = run_args(parser.parse_args())
    except (ValueError, OSError) as error:
        parser.exit(2, f"victorique bench: {error}\n")
    print(json.dumps(result, ensure_ascii=True, indent=2))


if __name__ == "__main__":
    main()
