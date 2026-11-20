"""Run settings shared by the command line, the Python API and an optional TOML file.

For one command the order is: an explicit option, then the settings file
(``--config FILE``, else the file named by ``VICTORIQUE_CONFIG``), then the
built-in default. A run records the resolved values when it starts; resume uses
those recorded values and reads no settings file.
"""
import math
import os
from pathlib import Path
import tomllib

from .adaptive_contracts import MINOR_FINDINGS  # ("nonblocking", "blocking"); the first is the default.

# The one built-in per-call limit. Every model call made by solve, verify, bench
# grading and the other runners of this package uses it unless a run sets another value.
CALL_TIMEOUT_SECONDS = 600
CALL_TIMEOUT_RANGE = (0.05, 86400)
UNDECIDED_CHECKS = ("abstain", "repair")
# How often a solve sends a model call again after it stopped at the per-call
# limit. Each repetition is a new call within the run's call budget and deadline.
TIMEOUT_RETRIES = 1
TIMEOUT_RETRIES_RANGE = (0, 3)
CONFIG_ENVIRONMENT = "VICTORIQUE_CONFIG"


def check_seconds(value, name="call_timeout_seconds"):
    """Validate an explicit per-call limit in seconds."""
    low, high = CALL_TIMEOUT_RANGE
    if type(value) not in (int, float) or not math.isfinite(value) or not low <= value <= high:
        raise ValueError(f"{name} must be a number of seconds from {low:g} to {high:g}")
    return value


def call_timeout_seconds(value=None, name="call_timeout_seconds"):
    """The per-call limit in seconds; None selects the built-in default."""
    return check_seconds(CALL_TIMEOUT_SECONDS if value is None else value, name)


def run_call_timeout(value, seconds_available):
    """The limit for each call of one run: the setting, at most the run's own duration."""
    return min(call_timeout_seconds(value), max(CALL_TIMEOUT_RANGE[0], seconds_available))


def undecided_checks(value=None):
    """Validate the handling of inconclusive and incomplete checks; None selects abstain."""
    if value is None:
        return UNDECIDED_CHECKS[0]
    if type(value) is not str or value not in UNDECIDED_CHECKS:
        raise ValueError("undecided_checks must be abstain or repair")
    return value


def minor_findings(value=None):
    """Validate the reading of a check that passes with minor findings; None selects nonblocking."""
    if value is None:
        return MINOR_FINDINGS[0]
    if type(value) is not str or value not in MINOR_FINDINGS:
        raise ValueError("minor_findings must be nonblocking or blocking")
    return value


def timeout_retries(value=None):
    """Validate the retries of a call that reached the per-call limit; None selects 1."""
    if value is None:
        return TIMEOUT_RETRIES
    low, high = TIMEOUT_RETRIES_RANGE
    if type(value) is not int or not low <= value <= high:
        raise ValueError(f"timeout_retries must be a whole number from {low} to {high}")
    return value


VALIDATORS = {"call_timeout_seconds": call_timeout_seconds, "undecided_checks": undecided_checks,
              "minor_findings": minor_findings, "timeout_retries": timeout_retries}


def load_config(path=None):
    """Read settings from a TOML file: PATH, else $VICTORIQUE_CONFIG, else none.

    Returns the validated keys present in the file; an unknown key is an error.
    """
    if path is None:
        path = os.environ.get(CONFIG_ENVIRONMENT) or None
    if path is None:
        return {}
    path = Path(path).expanduser()
    with path.open("rb") as stream:
        try:
            data = tomllib.load(stream)
        except tomllib.TOMLDecodeError as error:
            raise ValueError(f"{path}: invalid TOML: {error}") from error
    unknown = sorted(set(data) - set(VALIDATORS))
    if unknown:
        raise ValueError(f"{path}: unknown setting {', '.join(unknown)}; supported settings: "
                         + ", ".join(VALIDATORS))
    try:
        return {key: VALIDATORS[key](value) for key, value in data.items()}
    except ValueError as error:
        raise ValueError(f"{path}: {error}") from error


def command_settings(path, **given):
    """Settings for one command: each given option, else the file's value, else None.

    None leaves the built-in default to the API. Only the named settings are
    returned, so a file may hold settings that this command does not use.
    """
    stored = load_config(path)
    return {key: stored.get(key) if value is None else value for key, value in given.items()}


def seconds(text):
    """argparse type for a number of seconds; a whole number stays an integer."""
    value = float(text)
    return int(value) if value.is_integer() else value


def add_arguments(parser, *, undecided=True, minor=True, retries=True):
    """Command-line options for these settings; omitted options fall back to the file.

    ``undecided``, ``minor`` and ``retries`` add the options ``--undecided-checks``,
    ``--minor-findings`` and ``--timeout-retries``, which ``verify`` does not take.
    """
    parser.add_argument("--config", metavar="FILE",
                        help="TOML settings file (default: the file named by " + CONFIG_ENVIRONMENT + ", if set)")
    parser.add_argument("--call-timeout-seconds", type=seconds, metavar="SECONDS",
                        help=f"limit for each model call, {CALL_TIMEOUT_RANGE[0]:g}-{CALL_TIMEOUT_RANGE[1]:g} "
                             f"(default {CALL_TIMEOUT_SECONDS})")
    if retries:
        low, high = TIMEOUT_RETRIES_RANGE
        parser.add_argument("--timeout-retries", type=int, metavar="N",
                            help=f"how often a model call that reached the per-call limit is sent again, "
                                 f"{low}-{high} (default {TIMEOUT_RETRIES}); each repetition is a new call "
                                 "within --calls and the run's time limit")
    if undecided:
        parser.add_argument("--undecided-checks", choices=UNDECIDED_CHECKS,
                            help="ladder and resample after an inconclusive or incomplete check: "
                                 "abstain (default) or repair once")
    if minor:
        parser.add_argument("--minor-findings", choices=MINOR_FINDINGS,
                            help="a check that says pass and lists only minor findings: nonblocking (default) "
                                 "counts it as passed; blocking counts it as failed and treats the findings "
                                 "as blocking")
