"""INERT pure contract acceptance; never execute a host or model."""
from copy import deepcopy
import json
from pathlib import Path
import unittest
from unittest import mock

from victorique.harness.hosts import parse_host_probe
from victorique.harness.codex_contract import (
    CODEX_VERSION, REQUIRED_EXEC_FLAGS, conservative_codex_argv,
    build_response_schema, parse_codex_capture,
)


ROOT = Path(__file__).resolve().parents[1]
BINARY = str(ROOT / "INERT-host.exe")
LAYOUT = {"workspace": str(ROOT / "INERT-workspace"), "schema_file": str(ROOT / "INERT-schema.json"),
          "final_file": str(ROOT / "INERT-final.json")}


def public_profile():
    def row(name, args, text):
        return {"name": name, "argv": [BINARY, *args], "stdout": text, "stderr": "", "exit_code": 0,
                "timed_out": False, "output_truncated": False, "error": None}
    probes = [row("version", ["--version"], "codex-cli " + CODEX_VERSION),
              row("help", ["--help"], "Codex CLI\nUsage: codex [OPTIONS]"),
              row("exec_help", ["exec", "--help"], "Run Codex non-interactively\nUsage: codex exec [OPTIONS]\n" + " ".join(REQUIRED_EXEC_FLAGS))]
    return parse_host_probe("codex", BINARY, probes)


def packet():
    binding = {"schema": "victorique.adaptive_binding.v2", "packet_id": "INERT-packet", "request_id": "INERT-request",
               "session_id": "INERT-core-session", "revision": 1, "flag": False, "optional": None,
               "dependency_refs": [{"id": "INERT-dependency", "revision": 2}]}
    return {"schema": "victorique.adaptive_actor_packet.v2", "binding": binding,
            "response_contract": {"schema": "victorique.adaptive_actor_response.v2",
                                  "fields": ["schema", "binding", "output"], "binding": deepcopy(binding)}}


def response(p=None, output=None):
    p = p or packet()
    return {"schema": p["response_contract"]["schema"], "binding": deepcopy(p["binding"]),
            "output": {"inert": "测试"} if output is None else output}


def response_bytes(value=None):
    return json.dumps(value or response(), ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def events(final=None, *, failed=False):
    final = response_bytes() if final is None else final
    start = [{"type": "thread.started", "thread_id": "INERT-host-thread"}, {"type": "turn.started"}]
    if failed:
        return start + [{"type": "turn.failed", "error": {"message": "INERT host failure"}, "usage": {"input_tokens": 3}}]
    return start + [{"type": "item.completed", "item": {"id": "INERT-message", "type": "agent_message", "text": final.decode("utf-8")}},
                    {"type": "turn.completed", "usage": {"input_tokens": 3, "output_tokens": 7}}]


def stdout(rows):
    return ("".join(json.dumps(row, ensure_ascii=False) + "\n" for row in rows)).encode("utf-8")


def process(**changes):
    return {"local_terminal": True, "exit_code": 0, "timed_out": False, "capture_truncated": False, **changes}


class CodexArgvTests(unittest.TestCase):
    def test_reasoning_effort_is_one_quoted_per_run_override_without_model_override(self):
        import tomllib
        from victorique.harness.codex_contract import REASONING_EFFORTS
        default = conservative_codex_argv(public_profile(), LAYOUT, None)
        for effort in REASONING_EFFORTS:
            profile = public_profile()
            profile["reasoning_effort"] = effort
            argv = conservative_codex_argv(profile, LAYOUT, None)
            overrides = [arg for arg in argv if arg.startswith("model_reasoning_effort=")]
            self.assertEqual(len(overrides), 1)
            self.assertEqual(tomllib.loads(overrides[0]), {"model_reasoning_effort": effort})
            index = argv.index(overrides[0])
            self.assertEqual(argv[index - 1], "--config")
            self.assertEqual(argv[:index - 1] + argv[index + 1:], default)
            self.assertNotIn("--model", argv)
        profile["reasoning_effort"] = None
        self.assertEqual(conservative_codex_argv(profile, LAYOUT, None), default)

    def test_invalid_reasoning_effort_is_rejected_without_coercion(self):
        for value in ("", "LOW", " medium", "unknown", "low\nmodel='other'", False, 1, [], {}):
            with self.subTest(value=value), self.assertRaisesRegex(ValueError, "reasoning_effort"):
                conservative_codex_argv({**public_profile(), "reasoning_effort": value}, LAYOUT, None)

    def test_exact_version_conservative_argv_is_pure_and_has_no_safety_bypass(self):
        with mock.patch("subprocess.run", side_effect=AssertionError("no process allowed")):
            argv = conservative_codex_argv(public_profile(), LAYOUT, "INERT-model")
        self.assertEqual(argv[:2], [BINARY, "exec"])
        self.assertEqual(argv[-3:], ["--model", "INERT-model", "-"])
        self.assertIn('approval_policy="never"', argv)
        self.assertIn("agents.enabled=false", argv)
        self.assertIn('web_search="disabled"', argv)
        self.assertEqual([argv[i + 1] for i, value in enumerate(argv[:-1]) if value == "--disable"],
                         ["multi_agent", "shell_tool", "apps", "goals", "memories", "plugins"])
        self.assertIn("project_doc_max_bytes=0", argv)
        self.assertNotIn("hooks", argv)
        self.assertFalse(set(argv) & {"--ignore-rules", "--ignore-user-config", "resume", "--yolo", "--search", "--add-dir"})

    def test_inherited_mcp_servers_are_disabled_without_empty_table_merge(self):
        import tomllib
        profile = public_profile()
        profile["disabled_mcp_servers"] = ["local_tool", "dotted.name"]
        argv = conservative_codex_argv(profile, LAYOUT, None)
        value = next(arg for arg in argv if arg.startswith("mcp_servers="))
        self.assertEqual(tomllib.loads(value)["mcp_servers"],
                         {"local_tool": {"enabled": False}, "dotted.name": {"enabled": False}})
        self.assertNotIn("--model", argv)

    def test_forged_verified_flag_cannot_replace_public_identity(self):
        p = public_profile()
        p["probes"][1]["stdout"] = "INERT unrelated tool"
        with self.assertRaises(ValueError):
            conservative_codex_argv(p, LAYOUT, "INERT-model")

    def test_version_must_match_both_profile_and_public_banner(self):
        for mutate in (lambda p: p.update(version="0.0.0"), lambda p: p["probes"][0].update(stdout="codex-cli 99.0.0"),
                       lambda p: p.update(version_banner="codex-cli 99.0.0")):
            p = public_profile()
            mutate(p)
            with self.assertRaises(ValueError):
                conservative_codex_argv(p, LAYOUT, "INERT-model")

    def test_missing_flag_even_if_advertised_on_top_help_is_rejected(self):
        p = public_profile()
        p["probes"][2]["stdout"] = p["probes"][2]["stdout"].replace("--json", "--json-other")
        p["probes"][1]["stdout"] += "\n--json"
        with self.assertRaises(ValueError):
            conservative_codex_argv(p, LAYOUT, "INERT-model")

    def test_required_three_probes_must_be_complete_and_exact(self):
        mutations = [lambda p: p["probes"].pop(), lambda p: p["probes"][0].update(exit_code=False),
                     lambda p: p["probes"][2].update(output_truncated=True),
                     lambda p: p["probes"][1].update(argv=[BINARY, "exec", "INERT prompt"])]
        for mutate in mutations:
            p = public_profile()
            mutate(p)
            with self.assertRaises(ValueError):
                conservative_codex_argv(p, LAYOUT, "INERT-model")

    def test_model_and_distinct_absolute_layout_required(self):
        for model in ("", "--resume", "bad\nmodel", " model", "two models"):
            with self.subTest(model=model), self.assertRaises(ValueError):
                conservative_codex_argv(public_profile(), LAYOUT, model)
        for layout in ({**LAYOUT, "workspace": "relative"}, {**LAYOUT, "final_file": LAYOUT["schema_file"]}):
            with self.assertRaises(ValueError):
                conservative_codex_argv(public_profile(), layout, "INERT-model")

    def test_shell_metacharacters_remain_argv_data(self):
        layout = {**LAYOUT, "workspace": str(ROOT / "INERT spaces $() 'quote'")}
        argv = conservative_codex_argv(public_profile(), layout, "INERT-model")
        self.assertEqual(argv[argv.index("--cd") + 1], layout["workspace"])


class ResponseSchemaTests(unittest.TestCase):
    def test_wraps_exact_envelope_without_domain_validator(self):
        schema = build_response_schema(packet(), {"type": "object", "properties": {"inert": {"type": "string"}}})
        self.assertEqual(schema["required"], ["schema", "binding", "output"])
        self.assertFalse(schema["additionalProperties"])
        binding = schema["properties"]["binding"]
        self.assertEqual(binding["type"], "object")
        self.assertFalse(binding["additionalProperties"])
        self.assertEqual(binding["required"], list(packet()["binding"]))
        self.assertEqual(set(binding["properties"]), set(packet()["binding"]))
        for key in ("schema", "packet_id", "request_id", "session_id"):
            self.assertEqual(binding["properties"][key], {"type": "string"})
        self.assertEqual(binding["properties"]["revision"], {"type": "integer"})
        self.assertEqual(binding["properties"]["flag"], {"type": "boolean"})
        self.assertEqual(binding["properties"]["optional"], {"type": ["string", "null"]})
        self.assertEqual(binding["properties"]["dependency_refs"], {
            "type": "array", "items": {
                "type": "object", "properties": {"id": {"type": "string"}, "revision": {"type": "integer"}},
                "required": ["id", "revision"], "additionalProperties": False,
            },
        })
        self.assertEqual(schema["properties"]["schema"]["enum"], [packet()["response_contract"]["schema"]])

    def test_local_refs_rebase_after_wrapping_without_mutating_input(self):
        original = {"$defs": {"item": {"type": "string"}}, "type": "object", "properties": {"inert": {"$ref": "#/$defs/item"}}}
        before = deepcopy(original)
        schema = build_response_schema(packet(), original)
        self.assertEqual(schema["properties"]["output"]["properties"]["inert"]["$ref"], "#/properties/output/$defs/item")
        self.assertEqual(original, before)

    def test_external_or_dynamic_schema_resolution_rejected(self):
        for schema in ({"$ref": "https://example.invalid/schema"}, {"$ref": "file:///INERT"},
                       {"$schema": "https://json-schema.org/draft/2020-12/schema"},
                       {"$id": "https://example.invalid/"}, {"$dynamicRef": "#/value"},
                       {"properties": {"x": {"$ref": "other.json#/x"}}}):
            with self.subTest(schema=schema), self.assertRaises(ValueError):
                build_response_schema(packet(), schema)

    def test_nonfinite_and_nonjson_schema_rejected(self):
        for schema in ({"const": float("nan")}, {"const": (1, 2)}, {1: "INERT"}, {"const": "\ud800"}):
            with self.assertRaises(ValueError):
                build_response_schema(packet(), schema)

    def test_packet_contract_cannot_change_binding_or_fields(self):
        p = packet()
        p["response_contract"]["binding"]["revision"] = True
        with self.assertRaises(ValueError):
            build_response_schema(p, {})
        p = packet()
        p["response_contract"]["fields"].append("extra")
        with self.assertRaises(ValueError):
            build_response_schema(p, {})


class CodexCaptureTests(unittest.TestCase):
    def parse(self, rows=None, final=None, proc=None, p=None):
        final = response_bytes() if final is None else final
        return parse_codex_capture(stdout(rows if rows is not None else events(final)), b"", final,
                                   proc if proc is not None else process(), p if p is not None else packet())

    def test_exact_complete_native_envelope_and_core_compatible_host_reference(self):
        result = self.parse()
        self.assertEqual(result["status"], "completed", result["diagnostics"])
        self.assertTrue(result["terminal_observed"])
        self.assertEqual(result["host_reference"], "/codex-thread/INERT-host-thread")
        self.assertEqual(result["parsed"]["session_id"], "INERT-host-thread")
        self.assertEqual(result["source_line"], 4)
        self.assertIsNone(result["source_event_id"])
        self.assertIsNone(result["actual_model"])
        self.assertEqual(result["usage"], {"input_tokens": 3, "output_tokens": 7})

    def test_success_after_only_explicit_preterminal_reconnecting_429(self):
        rows = events()
        messages = [f"Reconnecting... {n}/5 (exceeded retry limit, last status: 429 Too Many Requests)" for n in (1, 2)]
        rows[2:2] = [{"type": "error", "message": message} for message in messages]
        result = self.parse(rows)
        self.assertEqual(result["status"], "completed")
        self.assertTrue(result["terminal_observed"])
        self.assertEqual(result["diagnostics"], [])
        self.assertEqual(result["parsed"]["issues"], ["host_reported_failure"])
        self.assertEqual(result["recovered_reconnect_errors"], [
            {"source_line": line, "message": message} for line, message in zip((3, 4), messages)])

    CODE_MODE_NOTICE = ("Code Mode is unavailable because failed to spawn code-mode host "
                        "C:\\Users\\INERT\\AppData\\Local\\OpenAI\\Codex\\bin\\9e1da79431da526b\\codex-code-mode-host.exe: "
                        "host executable was not found. Code mode will fail closed; enable `features.code_mode_host` "
                        "and install `codex-code-mode-host`.")

    def test_pre_turn_code_mode_fail_closed_notice_is_tolerated(self):
        rows = events()
        rows.insert(1, {"type": "item.completed", "item": {"id": "item_0", "type": "error", "message": self.CODE_MODE_NOTICE}})
        result = self.parse(rows)
        self.assertEqual(result["status"], "completed", result["diagnostics"])
        self.assertEqual(result["diagnostics"], [])
        self.assertEqual(result["tolerated_host_notices"], [self.CODE_MODE_NOTICE])

    def test_code_mode_notice_variants_stay_unknown(self):
        notice = self.CODE_MODE_NOTICE
        cases = {"after_turn_start": (2, {"id": "item_0", "type": "error", "message": notice}),
                 "other_message": (1, {"id": "item_0", "type": "error", "message": notice.replace("fail closed", "run")}),
                 "other_host": (1, {"id": "item_0", "type": "error", "message": notice.replace("codex-code-mode-host.exe", "other.exe")}),
                 "extra_field": (1, {"id": "item_0", "type": "error", "message": notice, "detail": "x"}),
                 "other_type": (1, {"id": "item_0", "type": "command_execution", "message": notice})}
        for name, (index, item) in cases.items():
            with self.subTest(name=name):
                rows = events()
                rows.insert(index, {"type": "item.completed", "item": item})
                result = self.parse(rows)
                self.assertEqual(result["status"], "unknown")
                self.assertNotIn("tolerated_host_notices", result)

    def test_success_after_only_explicit_preterminal_websocket_403_reconnects(self):
        rows = events()
        messages = [f"Reconnecting... {n}/5 (unexpected status 403 Forbidden: Unknown error, url: "
                    f"wss://chatgpt.com/backend-api/codex/responses, cf-ray: a5a2603dac58a2c3-YUL)" for n in (2, 3)]
        rows[2:2] = [{"type": "error", "message": message} for message in messages]
        result = self.parse(rows)
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["diagnostics"], [])
        self.assertEqual(result["recovered_reconnect_errors"], [
            {"source_line": line, "message": message} for line, message in zip((3, 4), messages)])

    def test_websocket_403_variants_stay_unknown(self):
        base = ("Reconnecting... 2/5 (unexpected status 403 Forbidden: Unknown error, url: "
                "wss://chatgpt.com/backend-api/codex/responses, cf-ray: a5a2603dac58a2c3-YUL)")
        for message in (base.replace("wss://chatgpt.com", "wss://example.com"),
                        base.replace("403 Forbidden", "401 Unauthorized"),
                        base.replace("Unknown error", "Access denied"),
                        base.replace("2/5", "6/5"), base + " extra"):
            with self.subTest(message=message):
                rows = events()
                rows.insert(2, {"type": "error", "message": message})
                result = self.parse(rows)
                self.assertEqual(result["status"], "unknown")
                self.assertNotIn("recovered_reconnect_errors", result)

    def test_reconnect_exception_never_hides_other_errors_or_incomplete_evidence(self):
        error = {"type": "error", "message": "Reconnecting... 2/5 (exceeded retry limit, last status: 429 Too Many Requests)"}
        for change in ("unknown", "other_status", "bare429", "invalid_count", "extra_payload", "postterminal", "duplicate_terminal",
                       "failed_terminal", "truncated", "timeout", "nonzero", "missing_final", "mismatch", "binding", "schema"):
            with self.subTest(change=change):
                rows, final, proc = events(), response_bytes(), process()
                rows.insert(2, deepcopy(error))
                if change == "unknown":
                    rows.insert(2, {"type": "error", "message": "INERT unknown error"})
                elif change in ("other_status", "bare429", "invalid_count"):
                    rows[2]["message"] = {"other_status": error["message"].replace("429 Too Many Requests", "500 Internal Server Error"),
                                          "bare429": "429 Too Many Requests", "invalid_count": error["message"].replace("2/5", "6/5")}[change]
                elif change == "extra_payload":
                    rows[2]["error"] = "Unknown embedded error"
                elif change == "postterminal":
                    rows.append(rows.pop(2))
                elif change == "duplicate_terminal":
                    rows.append(deepcopy(rows[-1]))
                elif change == "failed_terminal":
                    rows[-1]["type"] = "turn.failed"
                elif change in ("truncated", "timeout", "nonzero"):
                    proc.update({"truncated": {"capture_truncated": True}, "timeout": {"timed_out": True}, "nonzero": {"exit_code": 1}}[change])
                elif change == "missing_final":
                    final = None
                elif change == "mismatch":
                    final += b"\n"
                elif change in ("binding", "schema"):
                    envelope = response()
                    if change == "binding":
                        envelope["binding"]["packet_id"] = "wrong"
                    else:
                        envelope["schema"] = "wrong"
                    final = response_bytes(envelope)
                    rows[-2]["item"]["text"] = final.decode()
                result = parse_codex_capture(stdout(rows), b"", final, proc, packet())
                self.assertEqual(result["status"], "failed" if change == "failed_terminal" else "unknown")
                self.assertNotIn("recovered_reconnect_errors", result)

    def test_domain_invalid_output_is_left_for_bridge_core(self):
        final = response_bytes(response(output="INERT domain-invalid scalar"))
        self.assertEqual(self.parse(final=final)["status"], "completed")

    def test_completed_invalid_envelope_is_terminal_failure(self):
        values = []
        value = response(); value["binding"]["revision"] = True; values.append(value)
        value = response(); value["binding"]["revision"] = 1.0; values.append(value)
        value = response(); del value["binding"]["optional"]; values.append(value)
        value = response(); value["binding"]["extra"] = "INERT"; values.append(value)
        value = response(); value["schema"] = "other"; values.append(value)
        value = response(); value["extra"] = "INERT"; values.append(value)
        for value in values:
            with self.subTest(value=value):
                result = self.parse(final=response_bytes(value))
                self.assertEqual(result["status"], "failed")
                self.assertTrue(result["terminal_observed"])
                self.assertIn("invalid_response", result["diagnostics"])

    def test_invalid_output_requires_complete_original_terminal_evidence(self):
        final = b"not a JSON envelope"
        self.assertEqual(self.parse(final=final)["status"], "failed")
        for proc in (process(local_terminal=False), process(exit_code=None), process(timed_out=True),
                     process(capture_truncated=True), process(exit_code=1)):
            with self.subTest(proc=proc):
                self.assertEqual(self.parse(final=final, proc=proc)["status"], "unknown")
        rows = events(final)
        rows.append(deepcopy(rows[-1]))
        self.assertEqual(self.parse(rows, final=final)["status"], "unknown")

    def test_final_missing_or_byte_different_is_not_repaired(self):
        raw = response_bytes()
        for final in (None, raw + b"\n", raw.replace(b'"revision":1', b'"revision":1 ')):
            with self.subTest(final=final):
                result = parse_codex_capture(stdout(events(raw)), b"", final, process(), packet())
                self.assertEqual(result["status"], "unknown")

    def test_explicit_official_failure_with_local_exit_needs_no_final(self):
        result = parse_codex_capture(stdout(events(failed=True)), b"INERT failure", None, process(exit_code=1), packet())
        self.assertEqual(result["status"], "failed")
        self.assertTrue(result["terminal_observed"])
        self.assertEqual(result["source_line"], 3)
        self.assertEqual(result["usage"], {"input_tokens": 3})

    def test_explicit_complete_failure_can_survive_local_shutdown_timeout(self):
        result = parse_codex_capture(stdout(events(failed=True)), b"", None,
                                     process(exit_code=-15, timed_out=True), packet())
        self.assertEqual(result["status"], "failed")

    def test_nonzero_pid_or_error_event_alone_cannot_claim_failed(self):
        rows = events()[:-1] + [{"type": "error", "message": "INERT"}]
        result = self.parse(rows, proc=process(exit_code=1, pid=12345))
        self.assertEqual(result["status"], "unknown")
        self.assertFalse(result["terminal_observed"])

    def test_local_exit_unknown_or_timeout_blocks_success(self):
        for proc in (process(local_terminal=False), process(exit_code=None), process(exit_code=1), process(timed_out=True)):
            with self.subTest(proc=proc):
                self.assertEqual(self.parse(proc=proc)["status"], "unknown")

    def test_truncation_blocks_success_and_official_failure(self):
        for failed in (False, True):
            result = self.parse(events(failed=failed), proc=process(capture_truncated=True))
            self.assertEqual(result["status"], "unknown")

    def test_utf8_errors_in_any_success_capture_remain_unknown(self):
        raw = stdout(events())
        for out, err, final in ((b"\xff", b"", response_bytes()), (raw, b"\xff", response_bytes()), (raw, b"", b"\xff")):
            with self.subTest(out=out[:10], err=err, final=final[:10]):
                self.assertEqual(parse_codex_capture(out, err, final, process(), packet())["status"], "unknown")

    def test_malformed_duplicate_keys_nonfinite_and_surrogate_streams_unknown(self):
        for line in (b'{"type":"turn.started","type":"turn.failed"}\n', b'{"type":"turn.started","x":1e999}\n',
                     b'{"type":"turn.started","x":"\\ud800"}\n', b'{bad}\n'):
            result = parse_codex_capture(stdout(events()) + line, b"", response_bytes(), process(), packet())
            self.assertEqual(result["status"], "unknown")

    def test_session_conflict_duplicate_terminal_and_postterminal_event_unknown(self):
        bad = []
        rows = events(); rows[-1]["thread_id"] = "INERT-other"; bad.append(rows)
        rows = events(); rows.append(deepcopy(rows[-1])); bad.append(rows)
        rows = events(); rows.append({"type": "turn.started"}); bad.append(rows)
        for rows in bad:
            self.assertEqual(self.parse(rows)["status"], "unknown")

    def test_tool_event_blocks_completed_and_failed(self):
        for failed in (False, True):
            rows = events(failed=failed)
            rows.insert(2, {"type": "item.completed", "item": {"type": "command_execution", "command": "INERT"}})
            self.assertEqual(self.parse(rows)["status"], "unknown")

    def test_missing_newline_blocks_failure_and_line_number_counts_real_lines(self):
        result = parse_codex_capture(stdout(events(failed=True)).rstrip(), b"", None, process(exit_code=1), packet())
        self.assertEqual(result["status"], "unknown")
        result = parse_codex_capture(b"\n" + stdout(events()), b"", response_bytes(), process(), packet())
        self.assertEqual(result["status"], "completed")
        self.assertEqual(result["source_line"], 5)

    def test_missing_usage_and_model_stay_null(self):
        rows = events(); del rows[-1]["usage"]
        rows[-1]["model"] = "INERT undocumented echoed model"
        result = self.parse(rows)
        self.assertIsNone(result["usage"])
        self.assertIsNone(result["actual_model"])
        self.assertNotIn("cost", result)

    def test_invalid_process_boolean_is_not_coerced(self):
        result = self.parse(proc=process(local_terminal=1))
        self.assertEqual(result["status"], "unknown")
        self.assertIn("invalid_local_process_evidence", result["diagnostics"])


if __name__ == "__main__":
    unittest.main()
