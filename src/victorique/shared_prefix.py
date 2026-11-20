"""Opt-in paired single/ladder/resample runs with one bounded common L0."""
import argparse
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
import math
from pathlib import Path
import random
import sqlite3
from uuid import uuid4

from . import api, adaptive_contracts as c, adaptive_exchange as exchange
from .fake import FakeTransport
from .harness.safety import checked_path
from .ladder import LadderLoop, GATES, _blocking, _passing, _best
from .loop import _live_transport
from .precheck import precheck
from .resample import ResampleLoop
from .single import _output
from .telemetry import call_rows, totals, write_calls


POLICIES = ("single", "ladder", "resample")
L0 = "ladder.L0"
BASELINE_NOTE = "Paired single uses the common ladder/resample L0 precheck; standalone single does not."
E2_VARIANTS = {
    "V1": {"repair_rounds": 12, "investigate": True, "escalate": False, "parent": "main_ladder"},
    "V2": {"repair_rounds": 2, "investigate": False, "escalate": True, "parent": "V4"},
    "V3": {"repair_rounds": 1, "investigate": False, "escalate": False, "parent": "V4"},
    "V4": {"repair_rounds": 2, "investigate": False, "escalate": False, "parent": "main_ladder"},
}
E2_ORACLE_VARIANTS = {key: value for key, value in E2_VARIANTS.items() if key != "V3"} | {
    "V5": {"repair_rounds": 12, "investigate": False, "escalate": False, "parent": "main_ladder"}}
E2_BLOCK_VARIANTS = {"parent": {"repair_rounds": 12, "investigate": False,
                                 "escalate": False, "parent": "main_ladder"}, **E2_ORACLE_VARIANTS}
E2_MECHANISM_VARIANTS = {**E2_BLOCK_VARIANTS,
    "V6": {"repair_rounds": 12, "investigate": False, "escalate": False, "parent": "main_ladder"},
    "V7": {"repair_rounds": 12, "investigate": False, "escalate": False, "parent": "main_ladder"}}


class _PrefixLoop(LadderLoop):
    @property
    def options(self):
        return {**super().options, "repair_rounds": 0, "investigate": False, "escalate": False}

    def _finish(self, base, reason):
        # Leave the completed native prefix open for its two bounded continuations.
        # No close event or fabricated endpoint is imported into either branch.
        return {"phase": "native_endpoint", "reason": reason}

    def _review(self, base, candidate, prefix, suffix, kind, evidence):
        row = base["requests"].get(prefix + "." + suffix)
        if row is not None and row["phase"] != "returned_valid":
            return {"failed_call": True}
        return super()._review(base, candidate, prefix, suffix, kind, evidence)


class _SharedResampleLoop(ResampleLoop):
    l0_prefix = L0


LOOPS = {"ladder": LadderLoop, "resample": _SharedResampleLoop}


def _classification(loop):
    base = loop.core.snapshot()["exchange"]
    requests = base["requests"]
    if not requests or any(row["phase"] not in exchange.TERMINAL for row in requests.values()):
        if (not any(row["claim_id"] is not None for row in requests.values())
                and datetime.now(timezone.utc) >= c.utc(base["manifest"]["limits"]["stop_condition"]["deadline_utc"])):
            return "expired", None
        return "pending", None
    candidate = _output(base, L0 + ".solve") or loop._empty_draft(base, L0 + ".solve")
    if candidate is None:
        return "incomplete", None
    if precheck(candidate, base["manifest"]["problem"]["content"]):
        return "failed", candidate
    gates = [_output(base, L0 + "." + suffix) for suffix, _ in GATES]
    if any(gate is None for gate in gates):
        return "incomplete", candidate
    if any(gate["outcome"] == "unknown" or gate["uncertainties"] for gate in gates):
        return "inconclusive", candidate
    if any(gate["target_ref"] != c.candidate_ref(candidate) for gate in gates):
        raise ValueError("shared L0 gates must refer to the original candidate version")
    if all(_passing(gate) for gate in gates):
        return "passed", candidate
    return ("failed" if any(_blocking(gate) for gate in gates) else "inconclusive"), candidate


def _adapter(plan, factory, index, policy):
    return (factory(index, policy) if factory else FakeTransport()) if plan["transport"] == "fake" else None


def _prefix(root, plan, item, factory):
    directory = root / item["directory"] / "prefix"
    adapter = _adapter(plan, factory, item["index"], "prefix")
    if not directory.exists():
        _, loop = api._create_run(root / item["problem_file"], policy="ladder", model=plan["model"],
            calls=plan["calls"], hours=plan["hours"], out=directory, transport=plan["transport"],
            # The shared prefix is exactly three process starts; a timed-out call is not repeated.
            repair_rounds=plan["repair_rounds"], timeout_retries=0, _loop_type=_PrefixLoop, _process_starts=3,
            _started_at=datetime.fromisoformat(plan["started_at"]), _adapter=adapter,
            deadline_utc=plan.get("absolute_deadline_utc"),
            _resume_module="victorique.shared_prefix",
            **({"reasoning_effort": plan["reasoning_effort"]} if plan.get("reasoning_effort") is not None else {}))
        if "identity" in plan:
            invocation = api._read(directory / "invocation.json")
            api._write(directory / "invocation.json", {**invocation, "shared_identity": plan["identity"]})
    else:
        loop = _PrefixLoop.open(directory / "state", adapter=adapter)
    limits = loop.config["manifest"]["limits"]
    parameters = api._read(directory / "invocation.json")["parameters"]
    if (limits["global_call_limit"] != plan["calls"]
            or limits["stop_condition"]["deadline_utc"] != plan["deadline_utc"]
            or parameters.get("reasoning_effort") != plan.get("reasoning_effort")
            or any(parameters[key] != plan[key] for key in ("hours", "model", "transport", "repair_rounds"))):
        raise ValueError("paired parameters differ from the frozen original prefix")
    return loop


def _branch_config(prefix, policy, remaining):
    config = deepcopy(prefix.config)
    if config["manifest"]["mode"] != "synthetic":
        auth = config["transport"]["authorization"]
        auth.update(authorization_id=auth["authorization_id"] + "." + policy,
                    max_process_starts=remaining,
                    source_ref="Paired solve invocation; remaining calls after the shared L0")
        grant = config["execution_grant"]
        grant.update(grant_id=grant["grant_id"] + "." + policy, source_ref=auth["source_ref"])
    return config


def _fork_l0(prefix, directory, policy, adapter, *, options=None, branch_id=None, oracle_feedback=None):
    """Copy only this terminal L0's native records, never a bridge or transport."""
    base = prefix.core.snapshot()["exchange"]
    classification, _ = _classification(prefix)
    allowed = {L0 + ".solve", *(L0 + "." + suffix for suffix, _ in GATES)}
    reference_trigger = (oracle_feedback is not None and oracle_feedback.get("reference_trigger",
                          oracle_feedback.get("score", 7) < 7))
    accepted = ("passed", "failed") if reference_trigger else ("failed",)
    if (classification not in accepted or not set(base["requests"]) <= allowed
            or base["endpoint"] is not None
            or any(row["phase"] not in exchange.TERMINAL for row in base["requests"].values())):
        raise ValueError("only a completed, mathematically failed shared L0 can fork")
    remaining = base["manifest"]["limits"]["global_call_limit"] - len(base["requests"])
    if remaining <= 0:
        raise ValueError("no remaining per-arm calls")
    loop_type = LOOPS[policy]
    if oracle_feedback is not None:
        from .oracle_branch import OracleLadderLoop
        loop_type = OracleLadderLoop
    state = directory / "state"
    config = _branch_config(prefix, branch_id or policy, remaining)
    config["output_schemas_path"] = str(state / "role-output-schemas.json")
    invocation = api._read(prefix.root.parent / "invocation.json")
    invocation["parameters"].update(policy=policy, out=str(directory))
    if options is not None:
        invocation["parameters"].update(options)
    if branch_id is not None:
        invocation["e2_branch_id"] = branch_id
    if oracle_feedback is not None:
        invocation["oracle_feedback"] = oracle_feedback
    directory.mkdir(parents=True, exist_ok=True)
    state.mkdir(exist_ok=True)
    for path, value in ((directory / "invocation.json", invocation), (state / "run-config.json", config)):
        if path.exists():
            if api._read(path) != value:
                raise ValueError("frozen branch configuration differs")
        else:
            api._write(path, value)
    schema = state / "role-output-schemas.json"
    if not schema.exists():
        schema.write_bytes((prefix.root / "role-output-schemas.json").read_bytes())
    native = state / "native"
    core = loop_type.core_type.open(native) if native.exists() else loop_type.core_type.create(native, base["manifest"])
    local = core.snapshot()["exchange"]
    if not local["requests"]:
        # A fresh local root is the only changed metadata. The original events,
        # packets, claims, observations and charges are copied byte for byte.
        with closing(sqlite3.connect(core.path)) as target, closing(sqlite3.connect(prefix.core.path)) as source:
            source.execute("BEGIN")
            with target:
                for table in ("events", "requests", "packets", "observations", "charges"):
                    rows = source.execute("SELECT * FROM " + table).fetchall()
                    if rows:
                        target.executemany("INSERT INTO " + table + " VALUES (" + ",".join("?" for _ in rows[0]) + ")", rows)
        local = core.snapshot()["exchange"]
    if any(local["requests"].get(rid) != row for rid, row in base["requests"].items()):
        raise ValueError("branch shared L0 identity differs")
    if config["manifest"]["mode"] != "synthetic":
        spec = config["transport"]
        _, adapter_type = _live_transport(spec["kind"])
        transport_root = state / "transport"
        if transport_root.exists():
            adapter = adapter_type(transport_root)
        else:
            if set(local["requests"]) != set(base["requests"]):
                raise ValueError("missing branch transport after branch work; no recreation")
            adapter = adapter_type.create(transport_root, profile=spec["profile"], authorization=spec["authorization"],
                output_schemas=prefix.schemas, timeout_seconds=spec["timeout_seconds"], max_capture_bytes=spec["max_capture_bytes"])
    if not (state / "bridge").exists():
        if set(local["requests"]) != set(base["requests"]):
            raise ValueError("missing branch bridge after branch work; no recreation")
        loop_type.bridge_type.create(state / "bridge", native, adapter.describe(), execution_grant=config["execution_grant"])
    return loop_type.open(state, adapter=adapter if config["manifest"]["mode"] == "synthetic" else None)


def _arm(directory, policy, outcome, candidate, prefix_rows, extra_rows, plan, deadline):
    directory.mkdir(parents=True, exist_ok=True)
    (directory / "proof.md").write_text(candidate["content"] if candidate else "# No proof delivered\n", encoding="utf-8")
    result = {"policy": policy, "outcome": outcome,
        "logical_calls": len(prefix_rows) + len(extra_rows), "additional_physical_calls": len(extra_rows),
        "calls_limit": 3 if policy == "single" else plan["calls"], "deadline_utc": deadline,
        "logical_tokens": totals(prefix_rows + extra_rows)[0],
        "candidate_ref": c.candidate_ref(candidate) if candidate else None,
        "shared_prefix": "../prefix", "baseline_note": BASELINE_NOTE}
    write_calls(directory, extra_rows)
    api._write(directory / "summary.json", result)
    return result


def resume(directory, *, adapter_factory=None):
    """Continue the saved order with the original per-arm caps and absolute deadline."""
    root = checked_path(Path(directory).absolute())
    plan = api._read(root / "pair.json")
    if adapter_factory is not None and plan["transport"] != "fake":
        raise ValueError("adapter_factory is only available for fake transport")
    results, physical_rows = [], []
    for item in plan["problems"]:
        if (datetime.now(timezone.utc) >= c.utc(plan["deadline_utc"])
                and not (root / item["directory"] / "prefix").exists()):
            arms = {policy: _arm(root / item["directory"] / policy, policy, "abstain", None, [], [], plan,
                                  plan["deadline_utc"]) for policy in item["order"]}
            results.append({"problem": item["problem"], "order": item["order"], "prefix_status": "expired",
                            "shared_calls": 0, "physical_calls": 0, "physical_call_limit": 3, "arms": arms})
            continue
        prefix = _prefix(root, plan, item, adapter_factory)
        classification, candidate = _classification(prefix)
        if classification == "pending" or (classification == "incomplete" and
                any(row["phase"] == "returned_valid" for row in prefix.core.snapshot()["exchange"]["requests"].values())):
            prefix.run()
            classification, candidate = _classification(prefix)
        base = prefix.core.snapshot()["exchange"]
        prefix_rows = call_rows(prefix, "shared_L0")
        prefix_ids = set(base["requests"])
        deadline = base["manifest"]["limits"]["stop_condition"]["deadline_utc"]
        write_calls(prefix.root.parent, prefix_rows)
        physical_rows.extend(prefix_rows)
        arms = {}
        for policy in item["order"]:
            arm_dir = root / item["directory"] / policy
            outcome = "complete" if classification == "passed" else "pending" if classification == "pending" else "abstain"
            best, extra = candidate, []
            if (policy != "single" and classification == "failed" and len(prefix_ids) < plan["calls"]
                    and (datetime.now(timezone.utc) < c.utc(deadline) or (arm_dir / "state").exists())):
                loop = _fork_l0(prefix, arm_dir, policy, _adapter(plan, adapter_factory, item["index"], policy))
                loop.run()
                snapshot = loop.reporting_snapshot()
                endpoint = snapshot["endpoint"]
                outcome = "pending" if endpoint is None else endpoint["reducer_endpoint"]["decision"]
                best = _best(snapshot["exchange"]) if endpoint is None else endpoint["root_candidate"]
                extra = [row for row in api._calls(loop, policy) if row["request_id"] not in prefix_ids]
                physical_rows.extend(extra)
            arms[policy] = _arm(arm_dir, policy, outcome, best, prefix_rows, extra, plan, deadline)
        results.append({"problem": item["problem"], "order": item["order"], "prefix_status": classification,
            "shared_calls": len(prefix_rows), "physical_calls": len(prefix_rows) + sum(a["additional_physical_calls"] for a in arms.values()),
            "physical_call_limit": (len(prefix_ids) + 2 * (plan["calls"] - len(prefix_ids))
                                    if classification == "failed" and not plan.get("prefix_only") else 3),
            "arms": arms})
    result = {"schema": "victorique.shared_prefix.v1", "synthetic": plan["transport"] == "fake",
        "seed": plan["seed"], "baseline_note": BASELINE_NOTE, "problems": results,
        "physical_calls": len(physical_rows), "physical_tokens": totals(physical_rows)[0],
        "physical_call_limit": plan["physical_call_limit"]}
    write_calls(root, physical_rows)
    api._write(root / "summary.json", result)
    return result


def solve(problems, *, out, calls=12, hours=2, seed=0, transport="codex", model=None,
          repair_rounds=2, adapter_factory=None, deadline_utc=None, task_id=None, repetition=None,
          reasoning_effort=None, prefix_only=False):
    """Run each problem's common L0, then its seeded random arm order.

    ``calls`` includes L0 for each adaptive arm; single uses at most three.
    All arms retain the batch's original deadline. Physical starts are bounded
    by k + 2*(calls-k) after a failed k-call prefix, with k in [1, 3].
    """
    c.integer(calls, "calls per adaptive arm including L0", 3, 100)
    if type(prefix_only) is not bool:
        raise ValueError("prefix_only must be boolean")
    c.integer(repair_rounds, "repair_rounds", 0, 100)
    if type(hours) not in (int, float) or not math.isfinite(hours) or hours <= 0:
        raise ValueError("hours must be a positive finite number")
    if type(seed) is not int:
        raise ValueError("seed must be an integer")
    if transport not in ("codex", "claude", "fake"):
        raise ValueError("transport must be codex, claude or fake")
    from .harness.codex_contract import validate_reasoning_effort
    validate_reasoning_effort(reasoning_effort)
    if reasoning_effort is not None and transport != "codex":
        raise ValueError("reasoning_effort requires Codex transport")
    if adapter_factory is not None and transport != "fake":
        raise ValueError("adapter_factory is only available for fake transport")
    if model is not None and (type(model) is not str or not model or model.strip() != model
            or model.startswith("-") or any(ch.isspace() for ch in model)):
        raise ValueError("model must be an identifier")
    if isinstance(problems, (str, Path)):
        problems = [problems]
    inputs = [(str(p), checked_path(Path(p).absolute()).read_text(encoding="utf-8")) for p in problems]
    if not inputs:
        raise ValueError("at least one problem is required")
    identity = None
    if task_id is not None or repetition is not None:
        identity = _task_identity(task_id, repetition)
        if len(inputs) != 1:
            raise ValueError("a task/repetition binding requires exactly one problem")
    for _, content in inputs:
        c.validate_problem({"artifact_id": "input", "revision": 1, "content": content})
    root = checked_path(Path(out).absolute())
    if root.exists():
        raise ValueError("output directory already exists; use resume")
    rng = random.Random(seed)
    started = datetime.now(timezone.utc)
    deadline = api._bounded_deadline(started, hours, deadline_utc)
    plan = {"started_at": started.isoformat(), "deadline_utc": deadline, "calls": calls, "hours": hours,
        "seed": seed, "transport": transport, "model": model, "repair_rounds": repair_rounds,
        "physical_call_limit": len(inputs) * (2 * calls - 1), "problems": []}
    if deadline_utc is not None:
        plan["absolute_deadline_utc"] = deadline_utc
    if identity is not None:
        plan["identity"] = identity
    if reasoning_effort is not None:
        plan["reasoning_effort"] = reasoning_effort
    if prefix_only:
        plan.update(prefix_only=True, physical_call_limit=3 * len(inputs))
    root.mkdir(parents=True)
    for index, (problem, content) in enumerate(inputs):
        order = ["single"] if prefix_only else list(POLICIES)
        rng.shuffle(order)
        problem_file = f"problem-{index:04d}.md"
        (root / problem_file).write_text(content, encoding="utf-8")
        plan["problems"].append({"index": index, "problem": problem, "problem_file": problem_file,
                                 "directory": f"{index:04d}", "order": order})
    api._write(root / "pair.json", plan)
    return resume(root, adapter_factory=adapter_factory)


def _task_identity(task_id, repetition):
    if type(task_id) is not str or not task_id.strip() or task_id != task_id.strip():
        raise ValueError("task_id must be nonempty text")
    c.integer(repetition, "repetition", 1, 100)
    return {"task_id": task_id, "repetition": repetition}


def _e2_source(directory, index, task_id, repetition):
    """Open an explicitly bound existing prefix without running or exporting it."""
    root = checked_path(Path(directory).absolute())
    plan = api._read(root / "pair.json")
    identity = _task_identity(task_id, repetition)
    if plan.get("identity") != identity:
        raise ValueError("E2 requires the same frozen task/repetition identity")
    c.integer(index, "problem index", 0, len(plan["problems"]) - 1)
    item = plan["problems"][index]
    directory = checked_path(root / item["directory"] / "prefix")
    invocation = api._read(directory / "invocation.json")
    if invocation.get("shared_identity") != identity:
        raise ValueError("prefix task/repetition identity differs")
    prefix = _PrefixLoop.open(directory / "state", adapter=FakeTransport() if plan["transport"] == "fake" else None)
    limits = prefix.config["manifest"]["limits"]
    if (plan["calls"] != 12 or plan["repair_rounds"] != 12
            or limits["global_call_limit"] != 12 or invocation["parameters"]["repair_rounds"] != 12
            or limits["stop_condition"]["deadline_utc"] != plan["deadline_utc"]):
        raise ValueError("E2 requires the frozen main ladder configuration and deadline")
    classification, candidate = _classification(prefix)
    base = prefix.core.snapshot()["exchange"]
    allowed = {L0 + ".solve", *(L0 + "." + suffix for suffix, _ in GATES)}
    if not set(base["requests"]) <= allowed or base["endpoint"] is not None:
        raise ValueError("E2 source must contain only the shared L0")
    return plan, item, prefix, classification, candidate, call_rows(prefix, "shared_L0")


def e2_reservation(directory, *, task_id, repetition, index=0, oracle_grade=None, include_parent=False,
                   include_mechanism=False):
    """Read-only maximum NEW E2 calls; each of four serial arms has 12-k left."""
    plan, _, prefix, classification, candidate, rows = _e2_source(directory, index, task_id, repetition)
    if type(include_parent) is not bool or (include_parent and oracle_grade is None):
        raise ValueError("include_parent requires an explicit oracle grade")
    if type(include_mechanism) is not bool or (include_mechanism and not include_parent):
        raise ValueError("include_mechanism requires the independent parent block")
    active = classification == "failed" and datetime.now(timezone.utc) < c.utc(plan["deadline_utc"])
    maximum = 4 * (12 - len(rows)) if active else 0
    if oracle_grade is not None:
        from .oracle_branch import load_l0_grade
        feedback = load_l0_grade(directory, prefix, candidate, task_id, repetition, oracle_grade)
        oracle_active = (feedback["score"] < 7 and classification in ("passed", "failed")
                         and datetime.now(timezone.utc) < c.utc(plan["deadline_utc"]))
        maximum = (3 * (12 - len(rows)) if active else 0) + (13 - len(rows) if oracle_active else 0)
        active |= oracle_active
    if include_parent and classification == "failed" and datetime.now(timezone.utc) < c.utc(plan["deadline_utc"]):
        maximum += 12 - len(rows)
    if include_mechanism and datetime.now(timezone.utc) < c.utc(plan["deadline_utc"]):
        maximum += (13 - len(rows)) * (int(classification == "failed") + int(oracle_active))
    return {"prefix_status": classification, "shared_calls": len(rows),
            "new_physical_call_limit": maximum,
            "slots": 2 if active and plan["transport"] != "claude" else 1 if active else 0,
            "deadline_utc": plan["deadline_utc"], "prefix_run_id": prefix.config["manifest"]["session_id"]}


def branch_e2(directory, *, out, task_id, repetition, index=0, adapter_factory=None, oracle_grade=None,
              include_parent=False, include_mechanism=False):
    """Explicitly branch V1-V4 from this task/repetition's frozen shared L0."""
    plan, _, prefix, classification, candidate, rows = _e2_source(directory, index, task_id, repetition)
    if type(include_parent) is not bool or (include_parent and oracle_grade is None):
        raise ValueError("include_parent requires an explicit oracle grade")
    if type(include_mechanism) is not bool or (include_mechanism and not include_parent):
        raise ValueError("include_mechanism requires the independent parent block")
    if include_parent and not plan.get("prefix_only"):
        raise ValueError("independent E2 blocks require their own prefix-only source")
    if adapter_factory is not None and plan["transport"] != "fake":
        raise ValueError("adapter_factory is only available for fake transport")
    root = checked_path(Path(out).absolute())
    source = checked_path(Path(directory).absolute())
    if root == source or source in root.parents or root in source.parents:
        raise ValueError("E2 output must be separate from the original paired run")
    feedback = None
    if oracle_grade is not None:
        from .oracle_branch import load_l0_grade
        feedback = load_l0_grade(source, prefix, candidate, task_id, repetition, oracle_grade)
    root.mkdir(parents=True, exist_ok=False)
    api._write(root / "e2.json", {"schema": "victorique.e2.block.v2" if include_mechanism else "victorique.e2.block.v1" if include_parent else "victorique.e2.v2" if feedback else "victorique.e2.v1", "run_id": "e2-" + uuid4().hex,
        "source": str(source), "index": index, **_task_identity(task_id, repetition),
        "prefix_run_id": prefix.config["manifest"]["session_id"], "prefix_status": classification,
        "shared_calls": len(rows), "deadline_utc": plan["deadline_utc"],
        "variants": E2_MECHANISM_VARIANTS if include_mechanism else E2_BLOCK_VARIANTS if include_parent else E2_ORACLE_VARIANTS if feedback else E2_VARIANTS,
        **({"oracle_feedback": feedback} if feedback else {})})
    return resume_e2(root, adapter_factory=adapter_factory)


def resume_e2(directory, *, adapter_factory=None):
    """Resume only E2 branches, retaining source identity, options and deadline."""
    root = checked_path(Path(directory).absolute())
    saved = api._read(root / "e2.json")
    plan, item, prefix, classification, candidate, prefix_rows = _e2_source(
        saved["source"], saved["index"], saved["task_id"], saved["repetition"])
    if adapter_factory is not None and plan["transport"] != "fake":
        raise ValueError("adapter_factory is only available for fake transport")
    feedback = saved.get("oracle_feedback")
    include_mechanism = saved["schema"] == "victorique.e2.block.v2"
    include_parent = saved["schema"] in ("victorique.e2.block.v1", "victorique.e2.block.v2")
    if include_parent and (feedback is None or not plan.get("prefix_only")):
        raise ValueError("independent E2 block lost its prefix or oracle grade")
    variants = E2_MECHANISM_VARIANTS if include_mechanism else E2_BLOCK_VARIANTS if include_parent else E2_ORACLE_VARIANTS if feedback is not None else E2_VARIANTS
    if feedback is not None:
        from .oracle_branch import load_l0_grade
        if load_l0_grade(saved["source"], prefix, candidate, saved["task_id"], saved["repetition"], feedback["directory"]) != feedback:
            raise ValueError("frozen oracle grade changed")
    if (saved["variants"] != variants or saved["deadline_utc"] != plan["deadline_utc"]
            or saved["prefix_run_id"] != prefix.config["manifest"]["session_id"]
            or saved["prefix_status"] != classification or saved["shared_calls"] != len(prefix_rows)):
        raise ValueError("frozen E2 source or configuration changed")
    prefix_ids = set(prefix.core.snapshot()["exchange"]["requests"])
    arms, physical_rows = {}, []
    for variant, options in variants.items():
        arm_dir = root / variant
        branch_id = saved["run_id"] + "." + variant
        if variant in ("V5", "V6", "V7"):
            from .oracle_branch import run_arm
            arms[variant], extra = run_arm(prefix, arm_dir, plan, prefix_rows, candidate, classification, feedback,
                                           branch_id, _adapter(plan, adapter_factory, item["index"], variant), variant=variant)
            physical_rows.extend(extra)
            if include_parent and (arms[variant]["outcome"] == "pending" or any(row["status"] == "unknown" for row in extra)):
                break
            continue
        outcome = "complete" if classification == "passed" else "pending" if classification == "pending" else "abstain"
        best, extra = candidate, []
        if classification == "failed" and (datetime.now(timezone.utc) < c.utc(plan["deadline_utc"]) or (arm_dir / "state").exists()):
            loop = _fork_l0(prefix, arm_dir, "ladder", _adapter(plan, adapter_factory, item["index"], variant),
                options={key: value for key, value in options.items() if key != "parent"}, branch_id=branch_id)
            loop.run()
            snapshot = loop.reporting_snapshot()
            endpoint = snapshot["endpoint"]
            outcome = "pending" if endpoint is None else endpoint["reducer_endpoint"]["decision"]
            best = _best(snapshot["exchange"]) if endpoint is None else endpoint["root_candidate"]
            extra = [{**row, "branch_id": branch_id, "physical_call_id": branch_id + "/" + row["request_id"]}
                     for row in api._calls(loop, "ladder") if row["request_id"] not in prefix_ids]
        shared_candidate = (best is not None and candidate is not None
                            and c.candidate_ref(best) == c.candidate_ref(candidate)
                            and best["content"] == candidate["content"])
        arm = _arm(arm_dir, "ladder", outcome, best, prefix_rows, extra, plan, plan["deadline_utc"])
        arm.update(variant=variant, branch_id=branch_id, parent=options["parent"],
                   options={key: value for key, value in options.items() if key != "parent"},
                   shared_prefix=str(prefix.root.parent),
                   candidate_binding=None if best is None else {
                       "kind": "shared_L0" if shared_candidate else "E2_branch",
                       "run_directory": str(prefix.root.parent if shared_candidate else arm_dir),
                       "branch_id": None if shared_candidate else branch_id,
                       "candidate_ref": c.candidate_ref(best)})
        api._write(arm_dir / "summary.json", arm)
        arms[variant] = arm
        physical_rows.extend(extra)
        if include_parent and (outcome == "pending" or any(row["status"] == "unknown" for row in extra)):
            break  # An unresolved original process retains the block's two slots.
    maximum = (3 if feedback else 4) * (12 - len(prefix_rows)) if classification == "failed" else 0
    if feedback and feedback["score"] < 7 and classification in ("passed", "failed"):
        maximum += 13 - len(prefix_rows)
    if include_parent and classification == "failed":
        maximum += 12 - len(prefix_rows)
    if include_mechanism:
        maximum += (13 - len(prefix_rows)) * (int(classification == "failed")
            + int(feedback["score"] < 7 and classification in ("passed", "failed")))
    result = {"schema": saved["schema"], "run_id": saved["run_id"],
              "task_id": saved["task_id"], "repetition": saved["repetition"],
              "prefix_status": classification, "shared_calls": len(prefix_rows),
              "physical_calls": len(physical_rows), "new_physical_calls": len(physical_rows),
              "physical_call_limit": maximum,
              "physical_tokens": totals(physical_rows)[0], "deadline_utc": plan["deadline_utc"], "arms": arms}
    write_calls(root, physical_rows)
    api._write(root / "summary.json", result)
    return result


def main():
    from .harness.codex_contract import REASONING_EFFORTS
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    run = commands.add_parser("solve", help="explicitly enable paired shared L0")
    run.add_argument("problems", nargs="+")
    run.add_argument("--out", required=True)
    run.add_argument("--calls", type=int, default=12, help="per adaptive arm, INCLUDING shared L0; total <= problems*(2*calls-1)")
    run.add_argument("--hours", type=float, default=2, help="one original absolute deadline for all arms/problems")
    run.add_argument("--deadline-utc", help="absolute UTC deadline; can only shorten --hours")
    run.add_argument("--seed", type=int, default=0)
    run.add_argument("--transport", choices=("codex", "claude", "fake"), default="codex")
    run.add_argument("--model")
    run.add_argument("--reasoning-effort", choices=REASONING_EFFORTS)
    run.add_argument("--prefix-only", action="store_true", help="run only shared L0 for an explicit independent block")
    run.add_argument("--repair-rounds", type=int, choices=range(101), metavar="0..100", default=2)
    continued = commands.add_parser("resume", help="retain saved budget, deadline and order")
    continued.add_argument("directory")
    args = vars(parser.parse_args())
    command = args.pop("command")
    result = resume(**args) if command == "resume" else solve(**args)
    print(json.dumps(result, ensure_ascii=True, allow_nan=False))


if __name__ == "__main__":
    main()
