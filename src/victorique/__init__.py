"""Victorique native mathematical workflow."""
__version__ = "0.2.0a1"


def solve(problem, *, policy="single", model=None, calls=12, hours=2, out=None, transport="codex",
          repair_rounds=2, investigate=False, escalate=False, full_calls=None, full_references=None, deadline_utc=None,
          reasoning_effort=None, call_timeout_seconds=None, undecided_checks=None, minor_findings=None,
          timeout_retries=None, check_transport=None, check_model=None, check_reasoning_effort=None):
    """Run a bounded workflow and return the run summary."""
    from .api import solve as run
    return run(problem, policy=policy, model=model, calls=calls, hours=hours, out=out, transport=transport,
               repair_rounds=repair_rounds, investigate=investigate, escalate=escalate,
               full_calls=full_calls, full_references=full_references, deadline_utc=deadline_utc,
               reasoning_effort=reasoning_effort, call_timeout_seconds=call_timeout_seconds,
               undecided_checks=undecided_checks, minor_findings=minor_findings, timeout_retries=timeout_retries,
               check_transport=check_transport, check_model=check_model, check_reasoning_effort=check_reasoning_effort)


def stop(directory):
    """Ask a run to stop; return what was recorded (see ``victorique stop``)."""
    from .api import stop as request
    return request(directory)
