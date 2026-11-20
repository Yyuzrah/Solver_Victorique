"""Read-only evidence from caller-owned CLI captures and structured reviews.

No filesystem access, inferred token ceilings, or changes to execution policy.
Byte capture truncation is independent of the model's output token limit.
"""
import base64
from copy import deepcopy
import json
import math


def _dict(value):
    return value if isinstance(value, dict) else {}


def _counter(value):
    return value if type(value) is int and value >= 0 else None


def _events(carrier, *, with_lines=False):
    encoded = _dict(carrier.get("streams_base64")).get("stdout")
    if not isinstance(encoded, str):
        return []
    try:
        text = base64.b64decode(encoded, validate=True).decode("utf-8")
    except (ValueError, UnicodeError):
        return []
    rows = []
    for number, line in enumerate(text.splitlines(), 1):
        try:
            row = json.loads(line)
        except (ValueError, RecursionError):
            continue  # A cut-off final line does not erase earlier metadata.
        if isinstance(row, dict):
            rows.append((number, row) if with_lines else row)
    return rows


def _rate_limits(events):
    fields = ("used_percent", "window_minutes", "window_seconds", "reset_at", "resets_at")
    projected = []
    for number, event in events:
        if not isinstance(event.get("type"), str):
            continue
        windows = {}
        for name, value in _dict(event.get("rate_limits")).items():
            if not isinstance(value, dict) or not any(key in value for key in fields):
                continue
            windows[name] = {key: value.get(key) if (
                type(value.get(key)) is int and value[key] >= 0
                or type(value.get(key)) is float and math.isfinite(value[key]) and value[key] >= 0)
                else None for key in fields}
        if windows:
            projected.append({"source_line": number, "windows": windows})
    return projected or None


def _unique(sources, keys, valid):
    found = [(value[key], source + "." + key) for source, value in sources for key in keys
             if key in value and valid(value[key])]
    values = {value for value, _ in found}
    if len(values) != 1:
        return None, None
    return found[0][0], "+".join(dict.fromkeys(source for _, source in found))


def evidence(carrier=None, config=None, output=None):
    """Project explicit metadata; absent/contradictory evidence stays unknown.

    ``config`` accepts either a loop config or transport config. ``output`` is
    the original decoded response's output object, including invalid reviews.
    CLI metadata is read only at terminal/message metadata boundaries; model
    prose, thinking blocks and tool contents are never inspected for metadata.
    """
    carrier, config = _dict(carrier), _dict(config)
    parser = _dict(carrier.get("parser"))
    capture = _dict(carrier.get("process_capture"))
    invocation = _dict(capture.get("invocation"))
    transport = _dict(config.get("transport")) if "transport" in config else config
    events = _events(carrier, with_lines=True)
    rows = [row for _, row in events]
    terminals = [(f"cli.{row['type']}", row) for row in rows
                 if row.get("type") in ("turn.completed", "turn.failed", "result")]
    sources = [("parser", parser)] + terminals
    # Claude's last assistant message can report stop_reason even when result
    # omits it. Earlier tool-use messages do not describe the final completion.
    assistants = [_dict(row.get("message")) for row in rows if row.get("type") == "assistant"]
    if assistants and not any(any(isinstance(value.get(key), str) and value[key]
                                 for key in ("finish_reason", "stop_reason"))
                              for _, value in sources):
        sources.append(("cli.assistant.message", assistants[-1]))
    finish, finish_source = _unique(sources, ("finish_reason", "stop_reason"),
                                    lambda value: isinstance(value, str) and bool(value))
    # Only CLI-reported effective values qualify. Frozen capture settings,
    # model context windows and generic configured requests are not evidence
    # of an effective single-response output-token limit.
    limit, limit_source = _unique(sources, ("max_output_tokens", "max_tokens", "effective_output_token_limit"),
                                  lambda value: type(value) is int and value > 0)
    used = _counter(_dict(parser.get("usage")).get("output_tokens"))
    if used is None:
        counts = {_counter(_dict(row.get("usage")).get("output_tokens")) for _, row in terminals}
        counts.discard(None)
        used = next(iter(counts)) if len(counts) == 1 else None
    streams = _dict(capture.get("streams"))
    public_streams = {}
    for name in ("stdout", "stderr", "final"):
        stream = _dict(streams.get(name))
        public_streams[name] = {
            "captured_bytes": _counter(stream.get("captured_bytes")),
            "observed_bytes": _counter(stream.get("observed_bytes")),
            "truncated": stream.get("truncated") if type(stream.get("truncated")) is bool else None,
            "complete": stream.get("complete") if type(stream.get("complete")) is bool else None}
    flags = [public_streams[name]["truncated"] for name in ("stdout", "stderr")]
    if "final" in streams:
        flags.append(public_streams["final"]["truncated"])
    truncated = True if True in flags else False if all(value is False for value in flags) else None
    settings = {key: invocation.get(key, transport.get(key)) for key in ("max_capture_bytes", "timeout_seconds")}
    settings["max_capture_bytes"] = _counter(settings["max_capture_bytes"])
    timeout = settings["timeout_seconds"]
    settings["timeout_seconds"] = timeout if type(timeout) in (int, float) and 0 < timeout < float("inf") else None
    review = _dict(output)
    issue_lists = {key: deepcopy(review[key]) for key in ("findings", "issues", "uncertainties")
                   if isinstance(review.get(key), list)}
    return {"finish_reason": finish, "finish_reason_source": finish_source,
            "provider_rate_limits": _rate_limits(events),
            "effective_output_token_limit": limit, "output_token_limit_source": limit_source,
            "output_limit_reached": used >= limit if used is not None and limit is not None else None,
            "capture_truncated": truncated, "capture_settings": settings, "capture_streams": public_streams,
            "review_output": deepcopy(review) if issue_lists or "score" in review else None,
            "issue_lists": issue_lists or None}
