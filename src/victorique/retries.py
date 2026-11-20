"""Calls sent again after they stopped at the per-call time limit.

A call whose own process the transport stopped at the per-call limit ends as
an unknown request. When the run's ``timeout_retries`` setting allows it, the
loop settles that request as failed with the reason ``local_timeout`` and sends
the same call again as a new request, ``REQUEST.timeout-retry-N``. Each
repetition is a new call: it counts toward the call budget and gets at most the
run's remaining time. These helpers name and find such requests in a run's
exchange state; they never dispatch.
"""
from .adaptive_exchange import LOCAL_TIMEOUT

MARK = ".timeout-retry-"


def logical(request_id):
    """The request a chain of repetitions started from."""
    head, mark, number = request_id.rpartition(MARK)
    return head if mark and number.isdigit() else request_id


def attempt(request_id):
    """0 for a first call, N for its N-th repetition."""
    head, mark, number = request_id.rpartition(MARK)
    return int(number) if mark and number.isdigit() else 0


def next_id(request_id):
    """The request ID of the next repetition."""
    return logical(request_id) + MARK + str(attempt(request_id) + 1)


def retry_of(request_id):
    """The request this one repeats, or None for a first call."""
    number = attempt(request_id)
    if number == 0:
        return None
    return logical(request_id) if number == 1 else logical(request_id) + MARK + str(number - 1)


def settled(base, request_id):
    """True when the request ended because it stopped at its per-call limit and was settled for a repetition."""
    row = base["requests"].get(request_id)
    if row is None or row["phase"] != "failed":
        return False
    observation = row["first_result_observation_id"]
    return any(item["observation_id"] == observation and item["record"]["reason"] == LOCAL_TIMEOUT
               for item in base["observations"])


def latest(base, request_id):
    """The newest request of the call that ``request_id`` names."""
    current = request_id
    while next_id(current) in base["requests"]:
        current = next_id(current)
    return current


def superseded(base, request_id):
    """True for a request that a repetition replaced."""
    return next_id(request_id) in base["requests"]


def used(request_ids):
    """The number of repetitions among these requests."""
    return sum(attempt(request_id) > 0 for request_id in request_ids)
