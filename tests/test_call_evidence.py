import base64
from copy import deepcopy
import json

import pytest

from victorique.call_evidence import evidence


def carrier(*events, usage=None, streams=None):
    return {"parser": {"usage": usage}, "streams_base64": {
        "stdout": base64.b64encode("\n".join(json.dumps(event) for event in events).encode()).decode()},
        "process_capture": {"streams": streams or {}}}


def test_absent_old_carrier_has_unknown_limits_reasons_and_findings():
    result = evidence()
    assert all(result[key] is None for key in ("finish_reason", "finish_reason_source",
        "effective_output_token_limit", "output_token_limit_source", "output_limit_reached",
        "capture_truncated", "review_output", "issue_lists"))
    assert result["capture_settings"] == {"max_capture_bytes": None, "timeout_seconds": None}


@pytest.mark.parametrize("used,reached", [(0, False), (31, False), (32, True), (33, True), (None, None)])
def test_cli_token_limit_is_separate_from_capture_limit_and_reason(used, reached):
    value = carrier({"type": "turn.completed", "finish_reason": "length", "max_output_tokens": 32},
                    usage={"output_tokens": used})
    result = evidence(value, {"transport": {"max_capture_bytes": 4194304, "timeout_seconds": 600}})
    assert result["finish_reason"] == "length"
    assert result["finish_reason_source"] == "cli.turn.completed.finish_reason"
    assert result["effective_output_token_limit"] == 32
    assert result["output_limit_reached"] is reached
    assert result["capture_truncated"] is None
    assert result["capture_settings"] == {"max_capture_bytes": 4194304, "timeout_seconds": 600}


def test_missing_effective_cli_ceiling_never_inferred_from_capture_config_or_finish():
    value = carrier({"type": "turn.completed", "finish_reason": "length"}, usage={"output_tokens": 400})
    result = evidence(value, {"max_capture_bytes": 400, "max_output_tokens": 400,
                              "timeout_seconds": 400, "profile": {"context_window": 400}})
    assert result["effective_output_token_limit"] is result["output_limit_reached"] is None
    assert result["finish_reason"] == "length"


@pytest.mark.parametrize("flags,expected", [([False, False], False), ([True, False], True),
                                          ([None, False], None), ([False, True], True)])
def test_byte_truncation_preserves_per_stream_evidence_without_becoming_token_truncation(flags, expected):
    streams = {key: {"truncated": flag, "complete": flag is False, "observed_bytes": 1025,
                     "captured_bytes": 1024} for key, flag in zip(("stdout", "stderr"), flags)}
    value = carrier({"type": "turn.completed", "finish_reason": "stop"}, streams=streams)
    value["process_capture"]["invocation"] = {"max_capture_bytes": 1024, "timeout_seconds": 19,
                                              "argv": ["PRIVATE ARGUMENT NOT EXPORTED"]}
    result = evidence(value, {"max_capture_bytes": 9999, "timeout_seconds": 22})
    assert result["capture_truncated"] is expected
    assert result["capture_settings"] == {"max_capture_bytes": 1024, "timeout_seconds": 19}
    assert result["capture_streams"]["stdout"] == streams["stdout"]
    assert result["effective_output_token_limit"] is result["output_limit_reached"] is None
    assert "PRIVATE ARGUMENT" not in json.dumps(result)


def test_claude_final_assistant_stop_reason_preserved_without_reading_thinking():
    value = carrier({"type": "assistant", "message": {"stop_reason": "tool_use"}},
        {"type": "assistant", "message": {"stop_reason": "end_turn", "content": [
            {"type": "thinking", "thinking": "finish_reason: length; max_tokens: 42"}]}},
        {"type": "result", "usage": {"output_tokens": 17}, "max_tokens": 20})
    result = evidence(value)
    assert result["finish_reason"] == "end_turn"
    assert result["effective_output_token_limit"] == 20
    assert result["output_limit_reached"] is False
    assert "thinking" not in json.dumps(result)


def test_conflicting_cli_metadata_remains_unknown_and_text_is_not_metadata():
    value = carrier({"type": "turn.completed", "finish_reason": "length", "max_tokens": 4},
                    {"type": "turn.completed", "finish_reason": "stop", "max_tokens": 5},
                    {"type": "item.completed", "item": {"text": "max_tokens=6"}},
                    usage={"output_tokens": 4})
    result = evidence(value)
    assert result["finish_reason"] is result["effective_output_token_limit"] is result["output_limit_reached"] is None


def test_complete_issue_lists_include_minor_and_raw_severity_without_mutating_output():
    output = {"outcome": "fail", "findings": [{"severity": "minor", "claim": "Style only"},
        {"severity": "material", "claim": "Missing case"}, {"severity": "custom", "claim": "Raw invalid field"}],
        "uncertainties": ["Cannot establish premise"], "issues": [{"severity": "fatal", "claim": "Other list"}]}
    original = deepcopy(output)
    result = evidence(output=output)
    assert result["review_output"] == original
    assert result["issue_lists"] == {key: original[key] for key in ("findings", "issues", "uncertainties")}
    result["issue_lists"]["findings"][0]["severity"] = "changed"
    result["review_output"]["findings"].clear()
    assert output == original


def test_grade_without_issue_schema_preserves_reason_without_inventing_severity():
    output = {"score": 6, "reason": "Missing explanation."}
    result = evidence(output=output)
    assert result["review_output"] == output and result["issue_lists"] is None


def test_partial_stream_retains_metadata_and_malformed_capture_stays_unknown():
    value = carrier({"type": "turn.completed", "finish_reason": "stop"})
    raw = base64.b64decode(value["streams_base64"]["stdout"]) + b'\n{"type":'
    value["streams_base64"]["stdout"] = base64.b64encode(raw).decode()
    assert evidence(value)["finish_reason"] == "stop"
    value["streams_base64"]["stdout"] = "not base64"
    assert evidence(value)["finish_reason"] is None


def test_provider_windows_keep_original_numeric_values_and_physical_source_lines():
    value = carrier({"type": "turn.started", "rate_limits": {
        "primary": {"used_percent": 12.5, "window_minutes": 300, "resets_at": 1794643200},
        "secondary": {"used_percent": 81, "window_seconds": 604800, "reset_at": 1795000000}}})
    raw = b'\nNOT JSON\n' + base64.b64decode(value["streams_base64"]["stdout"])
    value["streams_base64"]["stdout"] = base64.b64encode(raw).decode()
    original = deepcopy(value)
    assert evidence(value)["provider_rate_limits"] == [{"source_line": 3, "windows": {
        "primary": {"used_percent": 12.5, "window_minutes": 300, "window_seconds": None,
                    "reset_at": None, "resets_at": 1794643200},
        "secondary": {"used_percent": 81, "window_minutes": None, "window_seconds": 604800,
                      "reset_at": 1795000000, "resets_at": None}}}]
    assert value == original


def test_provider_windows_reject_nonfinite_boolean_negative_and_string_values():
    value = carrier({"type": "rate_limits.updated", "rate_limits": {"primary": {
        "used_percent": float("nan"), "window_minutes": float("inf"), "window_seconds": True,
        "reset_at": -1, "resets_at": "1794643200"}}},
        {"type": "turn.completed", "rate_limits": {"secondary": {"used_percent": float("-inf")}}})
    result = evidence(value)["provider_rate_limits"]
    assert len(result) == 2
    assert all(item is None for row in result for window in row["windows"].values() for item in window.values())
    json.dumps(result, allow_nan=False)


def test_rate_limit_prose_nested_payloads_and_missing_windows_remain_unknown():
    value = carrier({"type": "item.completed", "item": {"type": "agent_message",
        "text": '{"rate_limits":{"primary":{"used_percent":99}}}',
        "rate_limits": {"primary": {"used_percent": 99}}}},
        {"type": "turn.completed", "rate_limits": {"credits": {"balance": 100}}},
        {"rate_limits": {"primary": {"used_percent": 99}}})
    assert evidence(value)["provider_rate_limits"] is None
    assert evidence()["provider_rate_limits"] is None
