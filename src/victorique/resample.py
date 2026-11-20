"""Bounded independent regeneration after the same triggers as ladder."""
from datetime import datetime, timezone

from . import adaptive_contracts as c
from .ladder import LadderLoop, _LadderExchange, _LadderBridge
from .single import _result


class _ResampleExchange(_LadderExchange):
    complete_reason = "resample_complete"


class _ResampleBridge(_LadderBridge):
    core_type = _ResampleExchange


class ResampleLoop(LadderLoop):
    core_type = _ResampleExchange
    bridge_type = _ResampleBridge
    policy_name = "resample"
    l0_prefix = "resample.L0"

    def _prepare(self, base, kind, request_id, candidate, *, issues=None, extra=None):
        # A fresh plan namespace prevents even identical model-chosen artifact IDs
        # from sharing gates across independently generated manuscripts. It carries
        # no old proof or feedback; the whole-problem obligation remains identical.
        plan_id = (candidate["plan_id"] if candidate is not None else
                   "single-plan" if request_id == self.l0_prefix + ".solve" else
                   "resample-plan-" + request_id.rsplit(".", 2)[-2])
        return super()._prepare(base, kind, request_id, candidate,
                                issues=issues, extra=extra, plan_id=plan_id)

    def step(self):
        base = self.core.snapshot()["exchange"]
        if base["endpoint"] is not None:
            return {"phase": "native_endpoint", "status": self.status()}
        deadline = c.utc(base["manifest"]["limits"]["stop_condition"]["deadline_utc"])
        if datetime.now(timezone.utc) >= deadline:
            return self._finish(base, "resample_deadline")
        pending = self._pending(base)
        if pending:
            return pending
        previous_failed = []
        undecided_used = False  # A fresh solve is this policy's step after a failed check.
        for round_no in range(self.options.get("repair_rounds", 2) + 1):
            prefix = self.l0_prefix if round_no == 0 else "resample.L1." + str(round_no)
            rid = prefix + ".solve"
            candidate = _result(base, rid) or self._empty_draft(base, rid)
            if candidate is None:
                if rid in base["requests"]:
                    return self._finish(base, "resample_solve_failed")
                if len(base["requests"]) >= base["manifest"]["limits"]["global_call_limit"]:
                    return self._finish(base, "resample_budget")
                return self._prepare(base, "solve_obligation", rid, None)
            candidate = {key: value for key, value in candidate.items() if key != "assessments"}
            checked = self._check_candidate(base, candidate, prefix, round_no, previous_failed,
                undecided_repair=self._undecided_mode() == "repair" and not undecided_used)
            if checked.get("phase"):
                return checked
            undecided_used = undecided_used or checked.get("undecided", False)
            previous_failed = checked["failed"]
        return self._finish(base, "resample_unresolved")
