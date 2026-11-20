"""Durable process tests using only the fixed bundled INERT executable."""
from concurrent.futures import ThreadPoolExecutor
import json
import os
from pathlib import Path
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

from victorique.harness.codex_process import DurableCodexProcess, fake_codex_argv
from victorique.harness.safety import HarnessError


BINDING = {"job_id": "inert-job-1", "core_claim_id": "inert-claim-1",
           "packet_id": "inert-packet-1", "mode": "synthetic"}
PACKET = b'{"inert":true,"text":"exact bytes"}\r\n'


class CodexProcessTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="victorique-codex-process-")
        self.addCleanup(self.cleanup_owned_fixture)
        self.base = Path(self.temp.name)
        self.root = self.base / "runner"
        self.runner = DurableCodexProcess.create(self.root)

    def cleanup_owned_fixture(self):
        # Receipt commit precedes the helper's final few exit instructions. Only
        # retry cleanup of this test's own known temporary root, never other paths.
        for attempt in range(50):
            try:
                self.temp.cleanup()
                return
            except PermissionError:
                if attempt == 49:
                    raise
                time.sleep(0.02)

    def invocation(self, binding=None, scenario="success", *, response_file=None, delay=0, **kwargs):
        layout = self.runner.layout(binding or BINDING)
        return {"argv": fake_codex_argv(layout["final_file"], scenario=scenario, response_file=response_file, delay=delay),
                "cwd": layout["workspace"], "final_file": layout["final_file"],
                "timeout_seconds": 5, "max_capture_bytes": 1024 * 1024, **kwargs}

    def await_finished(self, binding=None, timeout=10):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            result = self.runner.observe_existing(binding or BINDING)
            if result and result["worker_finished"]:
                return result
            time.sleep(0.025)
        self.fail("fixture helper did not finish within the bounded test wait")

    def test_success_preserves_exact_stdin_stdout_stderr_final_and_schema(self):
        response = b'{ "inert": true, "unicode": "\xe4\xb8\xad" }\r\n'
        response_path = self.base / "response.json"
        response_path.write_bytes(response)
        schema = b'{"type":"object"}\r\n'
        result = self.runner.start_once(BINDING, self.invocation(response_file=response_path, schema_bytes=schema), PACKET, wait_seconds=5)
        self.assertTrue(result["worker_finished"], result)
        self.assertTrue(result["local_terminal_observed"], result)
        self.assertEqual(result["exit_code"], 0)
        self.assertFalse(result["host_terminal_observed"])
        self.assertFalse(result["environment_recorded"])
        layout = self.runner.layout(BINDING)
        self.assertEqual(Path(layout["packet_file"]).read_bytes(), PACKET)
        self.assertEqual((Path(layout["workspace"]) / "stdin-observed.bin").read_bytes(), PACKET)
        self.assertEqual(Path(layout["schema_file"]).read_bytes(), schema)
        self.assertEqual(Path(result["streams"]["final"]["path"]).read_bytes(), response)
        self.assertEqual(Path(result["streams"]["stderr"]["path"]).read_bytes(), b"")
        stdout = Path(result["streams"]["stdout"]["path"]).read_bytes()
        parsed = [json.loads(line) for line in stdout.splitlines()]
        self.assertEqual(parsed[-2]["item"]["text"].encode("utf-8"), response)
        for capture in result["streams"].values():
            self.assertTrue(capture["complete"])
            self.assertFalse(capture["truncated"])
            self.assertEqual(capture["observed_bytes"], capture["captured_bytes"])

    def test_reopen_and_duplicate_start_never_launch_another_helper(self):
        invocation = self.invocation()
        initial = self.runner.start_once(BINDING, invocation, PACKET, wait_seconds=5)
        reopened = DurableCodexProcess(self.root)
        with patch("victorique.harness.codex_process.subprocess.Popen", side_effect=AssertionError("must not launch")):
            observed = reopened.start_once(BINDING, invocation, PACKET, wait_seconds=0)
            self.assertEqual(observed, initial)
            self.assertEqual(reopened.observe_existing(BINDING), initial)

    def test_parallel_instances_have_one_helper_launch(self):
        invocation = self.invocation(delay=0.1)
        real_popen = subprocess.Popen
        starts = []
        def count(*args, **kwargs):
            starts.append(args[0])
            if sys.platform == "win32":
                self.assertTrue(kwargs["creationflags"] & subprocess.CREATE_NO_WINDOW)
                self.assertFalse(kwargs["creationflags"] & subprocess.DETACHED_PROCESS)
            return real_popen(*args, **kwargs)
        with patch("victorique.harness.codex_process.subprocess.Popen", side_effect=count):
            with ThreadPoolExecutor(max_workers=2) as pool:
                a = pool.submit(self.runner.start_once, BINDING, invocation, PACKET)
                b = pool.submit(DurableCodexProcess(self.root).start_once, BINDING, invocation, PACKET)
                a.result(timeout=5)
                b.result(timeout=5)
        self.assertEqual(len(starts), 1)
        self.assertTrue(self.await_finished()["local_terminal_observed"])

    def test_binding_packet_and_invocation_cannot_be_replaced(self):
        invocation = self.invocation()
        self.runner.start_once(BINDING, invocation, PACKET, wait_seconds=5)
        for binding, body, command in (({**BINDING, "core_claim_id": "wrong"}, PACKET, invocation),
                                       ({**BINDING, "packet_id": "wrong"}, PACKET, invocation),
                                       (BINDING, PACKET + b" ", invocation),
                                       (BINDING, PACKET, {**invocation, "timeout_seconds": 4})):
            with self.assertRaises(HarnessError):
                self.runner.start_once(binding, command, body)
        with self.assertRaises(HarnessError):
            self.runner.observe_existing({**BINDING, "packet_id": "wrong"})

    def test_same_core_claim_cannot_start_different_job(self):
        self.runner.start_once(BINDING, self.invocation(), PACKET, wait_seconds=5)
        other = {**BINDING, "job_id": "other-job", "packet_id": "other-packet"}
        with self.assertRaisesRegex(HarnessError, "core claim"):
            self.runner.start_once(other, self.invocation(other), PACKET)

    def test_read_only_observation_of_missing_job_creates_nothing(self):
        before = set(self.root.rglob("*"))
        self.assertIsNone(self.runner.observe_existing(BINDING))
        self.assertEqual(set(self.root.rglob("*")), before)

    def test_fence_crash_is_unknown_and_never_restarted(self):
        invocation = self.invocation()
        def crash(stage, binding):
            if stage == "after_fence":
                raise SystemExit(17)
        self.runner._checkpoint = crash
        with self.assertRaises(SystemExit):
            self.runner.start_once(BINDING, invocation, PACKET)
        observed = DurableCodexProcess(self.root).observe_existing(BINDING)
        self.assertFalse(observed["worker_finished"])
        self.assertFalse(observed["local_terminal_observed"])
        self.assertIsNone(observed["worker_pid"])
        with patch("victorique.harness.codex_process.subprocess.Popen", side_effect=AssertionError("must not retry")):
            self.assertEqual(DurableCodexProcess(self.root).start_once(BINDING, invocation, PACKET), observed)

    def test_helper_launch_error_is_not_retried_or_falsely_terminal(self):
        invocation = self.invocation()
        with patch("victorique.harness.codex_process.subprocess.Popen", side_effect=OSError("INERT launch failure")) as launch:
            result = self.runner.start_once(BINDING, invocation, PACKET)
            self.assertEqual(launch.call_count, 1)
            self.assertEqual(result["launch_error"], "OSError")
            self.assertFalse(result["local_terminal_observed"])
            self.assertEqual(self.runner.start_once(BINDING, invocation, PACKET), result)
            self.assertEqual(launch.call_count, 1)

    def test_hardlinked_final_is_never_truncated(self):
        from victorique.harness.codex_process import _final_capture
        victim = self.base / "owned-INERT-victim.bin"
        victim.write_bytes(b"V" * 4096)
        alias = self.base / "owned-INERT-final.bin"
        os.link(victim, alias)
        with self.assertRaises(HarnessError):
            _final_capture(alias, 1024)
        self.assertEqual(victim.read_bytes(), b"V" * 4096)

    def test_helper_finishes_after_coordinator_abrupt_exit(self):
        invocation = self.invocation(delay=0.3)
        request_path = self.base / "request.json"
        request_path.write_text(json.dumps({"binding": BINDING, "invocation": invocation}), encoding="utf-8")
        script = ("import json, os, sys; "
                  "from victorique.harness.codex_process import DurableCodexProcess; "
                  "req=json.load(open(sys.argv[2], encoding='utf-8')); "
                  "DurableCodexProcess(sys.argv[1]).start_once(req['binding'], req['invocation'], b'INERT exact stdin'); "
                  "os._exit(29)")
        coordinator = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(self.root), str(request_path)],
                                     capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), timeout=10)
        self.assertEqual(coordinator.returncode, 29, coordinator.stderr)
        result = self.await_finished()
        self.assertEqual(result["exit_code"], 0, result)
        self.assertEqual((Path(self.runner.layout(BINDING)["workspace"]) / "stdin-observed.bin").read_bytes(), b"INERT exact stdin")
        self.assertFalse(result["restarted"])

    def test_duplicate_worker_entry_has_no_second_child(self):
        initial = self.runner.start_once(BINDING, self.invocation(), PACKET, wait_seconds=5)
        with self.runner._db() as db:
            token = db.execute("SELECT launch_token FROM jobs").fetchone()[0]
        from victorique.harness.codex_process import _run_worker
        with patch("victorique.harness.codex_process.subprocess.Popen", side_effect=AssertionError("duplicate helper must not launch")):
            self.assertEqual(_run_worker(self.root, BINDING["job_id"], token), 0)
        self.assertEqual(self.runner.observe_existing(BINDING), initial)

    def test_nonzero_process_is_local_exit_not_host_semantic_success(self):
        result = self.runner.start_once(BINDING, self.invocation(scenario="nonzero"), PACKET, wait_seconds=5)
        self.assertEqual(result["exit_code"], 7)
        self.assertTrue(result["local_terminal_observed"])
        self.assertFalse(result["host_terminal_observed"])
        self.assertFalse(result["streams"]["final"]["complete"])

    def test_timeout_records_kill_request_separately_from_actual_exit(self):
        result = self.runner.start_once(BINDING, self.invocation(scenario="timeout", timeout_seconds=0.1), PACKET, wait_seconds=5)
        self.assertTrue(result["timed_out"])
        self.assertTrue(result["kill_requested"])
        self.assertEqual(result["kill_reason"], "timeout")
        self.assertTrue(result["local_terminal_observed"])
        self.assertIsInstance(result["exit_code"], int)
        self.assertFalse(result["host_terminal_observed"])
        self.assertEqual(self.runner.observe_existing(BINDING), result)

    def test_small_thread_started_event_is_retained_before_process_exit(self):
        invocation = self.invocation(scenario="delay-after-thread-start", delay=0.8)
        self.runner.start_once(BINDING, invocation, PACKET)
        stdout = Path(self.runner.layout(BINDING)["stdout_file"])
        deadline = time.monotonic() + 4
        prefix = b""
        pending = None
        try:
            while time.monotonic() < deadline:
                if stdout.exists():
                    prefix = stdout.read_bytes()
                pending = self.runner.observe_existing(BINDING)
                if prefix or pending["worker_finished"]:
                    break
                time.sleep(0.01)
            self.assertIn(b'"type":"thread.started"', prefix)
            self.assertFalse(pending["worker_finished"])
            self.assertFalse(pending["local_terminal_observed"])
        finally:
            finished = self.await_finished()
        self.assertEqual(finished["exit_code"], 0)
        self.assertTrue(stdout.read_bytes().startswith(prefix))

    def test_binary_stdout_stderr_and_final_are_not_decoded_or_repaired(self):
        result = self.runner.start_once(BINDING, self.invocation(scenario="binary"), PACKET, wait_seconds=5)
        expected = {"stdout": b"\xff\x00\r\n", "stderr": b"\xfe\x00\n", "final": b"\x00\xff\x01"}
        for key, raw in expected.items():
            self.assertEqual(Path(result["streams"][key]["path"]).read_bytes(), raw)

    def test_capture_limits_keep_exact_prefix_and_true_observed_sizes(self):
        result = self.runner.start_once(BINDING, self.invocation(scenario="oversize", max_capture_bytes=1024), PACKET, wait_seconds=5)
        for key, char in (("stdout", b"O"), ("stderr", b"E"), ("final", b"F")):
            capture = result["streams"][key]
            self.assertEqual(capture["captured_bytes"], 1024)
            self.assertEqual(capture["observed_bytes"], 4096)
            self.assertTrue(capture["truncated"])
            self.assertFalse(capture["complete"])
            self.assertEqual(Path(capture["path"]).read_bytes(), char * 1024)

    def test_missing_final_is_explicit_not_reconstructed_from_stdout(self):
        result = self.runner.start_once(BINDING, self.invocation(scenario="no-final"), PACKET, wait_seconds=5)
        self.assertEqual(result["exit_code"], 0)
        self.assertEqual(result["streams"]["final"]["capture_error"], "final_file_missing")
        self.assertIsNone(result["streams"]["final"]["captured_bytes"])

    def test_malformed_stream_and_mismatched_final_preserved_for_adapter(self):
        for i, scenario in enumerate(("malformed", "mismatch")):
            binding = {**BINDING, "job_id": f"inert-{i}", "core_claim_id": f"claim-{i}", "packet_id": f"packet-{i}"}
            result = self.runner.start_once(binding, self.invocation(binding, scenario=scenario), PACKET, wait_seconds=5)
            self.assertEqual(result["exit_code"], 0)
            self.assertFalse(result["host_terminal_observed"])
            self.assertTrue(Path(result["streams"]["stdout"]["path"]).read_bytes())
            self.assertEqual(Path(result["streams"]["final"]["path"]).read_bytes(), b'{"inert":true}')

    def test_terminal_file_is_observable_if_database_ack_was_lost(self):
        result = self.runner.start_once(BINDING, self.invocation(), PACKET, wait_seconds=5)
        with self.runner._db(True) as db:
            db.execute("UPDATE jobs SET capture=NULL,state='worker_entered'")
        self.assertEqual(self.runner.observe_existing(BINDING), result)

    def test_process_root_and_artifact_hardlinks_are_rejected(self):
        alias_root = self.base / "alias"
        alias_root.mkdir()
        os.link(self.root / "processes.sqlite3", alias_root / "processes.sqlite3")
        with self.assertRaises(HarnessError):
            DurableCodexProcess(alias_root)
        (alias_root / "processes.sqlite3").unlink()
        self.runner.start_once(BINDING, self.invocation(), PACKET, wait_seconds=5)
        final = Path(self.runner.layout(BINDING)["final_file"])
        os.link(final, self.base / "unrelated-alias")
        with self.assertRaises(HarnessError):
            self.runner.observe_existing(BINDING)

    def test_changed_capture_size_is_not_silently_accepted(self):
        self.runner.start_once(BINDING, self.invocation(), PACKET, wait_seconds=5)
        Path(self.runner.layout(BINDING)["stdout_file"]).write_bytes(b"changed")
        with self.assertRaisesRegex(HarnessError, "size changed"):
            self.runner.observe_existing(BINDING)

    def test_invalid_policy_does_not_create_a_fence(self):
        invocation = self.invocation()
        for changed in ({**invocation, "env": {"INERT": "x"}}, {**invocation, "cwd": str(self.base)},
                        {**invocation, "final_file": str(self.base / "foreign")},
                        {**invocation, "timeout_seconds": float("nan")}, {**invocation, "max_capture_bytes": True},
                        {**invocation, "schema_bytes": "not bytes"}, {**invocation, "argv": ["relative"]}):
            with self.assertRaises(HarnessError):
                self.runner.start_once(BINDING, changed, PACKET)
        self.assertIsNone(self.runner.observe_existing(BINDING))

    def test_existing_root_and_unowned_database_not_initialized_or_overwritten(self):
        with self.assertRaises(HarnessError):
            DurableCodexProcess.create(self.root)
        wrong = self.base / "wrong"
        wrong.mkdir()
        (wrong / "processes.sqlite3").write_bytes(b"INERT non-database")
        with self.assertRaises(HarnessError):
            DurableCodexProcess(wrong)

    def test_no_environment_values_serialized(self):
        result = self.runner.start_once(BINDING, self.invocation(), PACKET, wait_seconds=5)
        spec = json.loads(Path(self.runner.layout(BINDING)["spec_file"]).read_bytes())
        self.assertEqual(spec["invocation"]["environment"], "inherited_by_process_without_inspection")
        self.assertNotIn("env", spec["invocation"])
        self.assertFalse(result["environment_recorded"])


if __name__ == "__main__":
    unittest.main()
