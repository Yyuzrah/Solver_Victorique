"""Codex transport adapter and native bridge with INERT executables only."""
from concurrent.futures import ThreadPoolExecutor
from contextlib import closing
from copy import deepcopy
from datetime import datetime, timedelta, timezone
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch
from uuid import uuid4

from victorique.harness.codex_transport import CodexFixtureTransport, CodexCliTransport, AUTHORIZATION
from victorique.harness.safety import HarnessError
from victorique.harness_bridge import AdaptiveHarnessBridge, DispatchRequest
from victorique.adaptive_session import AdaptiveSession, CONTRACTS_VERSION, CONTROLLER_VERSION, SESSION_VERSION, TEMPLATES_VERSION
from tests.test_victorique_adaptive_exchange import manifest
from tests.test_victorique_harness_bridge import response


def wait_observation(adapter, request, seconds=8):
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        capture = adapter.runner.observe_existing(adapter._binding(request))
        if capture and capture["worker_finished"]:
            return adapter.observe_existing(request)
        time.sleep(0.02)
    raise AssertionError("INERT helper did not finish within the test bound")


class CodexTransportTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(prefix="victorique-codex-adapter-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name).resolve() / "victorique-development"
        self.root.mkdir()
        self.m = manifest()
        self.m["implementation_versions"].update(contracts=CONTRACTS_VERSION, controller=CONTROLLER_VERSION,
                                                session=SESSION_VERSION, templates=TEMPLATES_VERSION)
        self.core = AdaptiveSession.create(self.root / "core", self.m)
        state = self.core.status()
        control = {"session_id": self.m["session_id"], "operation_id": "op_" + uuid4().hex,
                   "expected_session_sequence": state["session_sequence"], "expected_state_revision": state["state_revision"]}
        self.rid = "request_" + uuid4().hex
        action = self.core.propose()["legal_action_listing"]["actions"][0]
        self.core.prepare(control | {"request_id": self.rid, "logical_action_id": "logical_" + uuid4().hex,
                                    "action_id": "action_" + uuid4().hex, "selected_proposal": action})
        self.packet = self.core.snapshot()["exchange"]["requests"][self.rid]["packet"]

    def adapter(self, **kwargs):
        return CodexFixtureTransport.create(self.root / ("adapter_" + uuid4().hex),
                                             response_bytes=kwargs.pop("response_bytes", response(self.packet)), **kwargs)

    def request(self, packet=None):
        packet = packet or self.packet
        return DispatchRequest("job_" + uuid4().hex, "claim_" + uuid4().hex, packet,
                               (json.dumps(packet, ensure_ascii=False) + "\n").encode("utf-8"), "synthetic")

    def test_loop_pending_and_finished_unknown_use_original_full_job(self):
        from victorique.loop import OriginalProblemLoop
        adapter = self.adapter(scenario="no-final", delay_seconds=0.3)
        bridge = AdaptiveHarnessBridge.create(self.root / "bridge", self.core.root, adapter.describe())
        subject = OriginalProblemLoop()
        subject.config = {"manifest": self.m, "transport": {"kind": "codex_cli"}}
        subject.core, subject.bridge, subject.adapter = self.core, bridge, adapter
        self.assertTrue(bridge.dispatch(self.rid, adapter)["dispatched"])
        self.assertNotIn("packet", bridge.job_status(self.rid))
        # Hold observation pending deterministically, then observe the real INERT
        # child finishing without an answer. Neither path may dispatch again.
        with patch.object(adapter.runner, "start_once", side_effect=AssertionError("no redispatch")):
            with patch.object(adapter.runner, "observe_existing", return_value=None):
                self.assertEqual(subject.step()["phase"], "pending_original_observation")
            wait_observation(adapter, bridge._request(bridge._job(self.rid)))
            self.assertEqual(subject.step()["phase"], "blocked_original_result")
            self.assertEqual(subject.step()["phase"], "blocked_original_result")
        with closing(sqlite3.connect(adapter.runner.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_loop_completion_between_observations_ingests_existing_response(self):
        from victorique.loop import OriginalProblemLoop
        adapter = self.adapter(delay_seconds=0.1)
        bridge = AdaptiveHarnessBridge.create(self.root / "bridge", self.core.root, adapter.describe())
        subject = OriginalProblemLoop()
        subject.config = {"manifest": self.m, "transport": {"kind": "codex_cli"}}
        subject.core, subject.bridge, subject.adapter = self.core, bridge, adapter
        bridge.dispatch(self.rid, adapter)
        wait_observation(adapter, bridge._request(bridge._job(self.rid)))
        recover = bridge.recover
        count = 0
        def pending_then_complete(*args):
            nonlocal count
            count += 1
            return {"phase": "unknown"} if count == 1 else recover(*args)
        with patch.object(bridge, "recover", side_effect=pending_then_complete), \
             patch.object(adapter.runner, "start_once", side_effect=AssertionError("no redispatch")):
            self.assertEqual(subject.step()["phase"], "original_response_observed")
        self.assertEqual(count, 2)
        self.assertEqual(self.core.snapshot()["exchange"]["requests"][self.rid]["phase"], "returned_valid")

    def test_real_bridge_claim_to_fixed_executable_then_only_observe_projection(self):
        adapter = self.adapter(delay_seconds=0.1)
        bridge = AdaptiveHarnessBridge.create(self.root / "bridge", self.core.root, adapter.describe())
        first = bridge.dispatch(self.rid, adapter)
        self.assertTrue(first["dispatched"])
        request = bridge._request(bridge._job(self.rid))
        observation = wait_observation(adapter, request)
        self.assertEqual(observation.metadata["status"], "completed")
        result = bridge.recover(self.rid, adapter)
        self.assertEqual(result["core_classification"], "returned_valid")
        self.assertFalse(bridge.dispatch(self.rid, adapter)["dispatched"])
        with closing(sqlite3.connect(bridge.path)) as db:
            raw = db.execute("SELECT response FROM observations WHERE response IS NOT NULL").fetchone()[0]
        self.assertEqual(raw, response(self.packet))
        capture = adapter.runner.observe_existing(adapter._binding(request))
        self.assertEqual(Path(capture["invocation"]["cwd"]).joinpath("stdin-observed.bin").read_bytes(), request.packet_bytes)
        self.assertIsNone(observation.metadata["model_observation"])
        self.assertIsNone(observation.metadata["charge_observation"])

    def test_duplicate_start_and_reopened_observation_are_exactly_stable(self):
        adapter, request = self.adapter(), self.request()
        adapter.start_once(request)
        first = wait_observation(adapter, request)
        with patch.object(adapter.runner, "start_once", side_effect=AssertionError("no redispatch")):
            repeated = adapter.start_once(request)
        reopened = CodexFixtureTransport(adapter.root).observe_existing(request)
        self.assertEqual(first, repeated)
        self.assertEqual(first, reopened)

    def test_explicit_redecode_appends_persistent_observation_without_launch_or_old_row_changes(self):
        from victorique.harness import codex_contract
        from victorique.harness.codex_transport import REDECODE_PHASE
        adapter = self.adapter(scenario="reconnect-success", delay_seconds=0.1)
        bridge = AdaptiveHarnessBridge.create(self.root / "bridge", self.core.root, adapter.describe())
        decoder = codex_contract.parse_codex_capture
        def legacy(*args):
            value = decoder(*args)
            if value.pop("recovered_reconnect_errors", None):
                value.update(schema="victorique.harness.codex_contract.v3", status="unknown", terminal_observed=False,
                             diagnostics=["host_reported_failure"])
            return value
        with patch.object(codex_contract, "parse_codex_capture", side_effect=legacy):
            bridge.dispatch(self.rid, adapter)
            request = bridge._request(bridge._job(self.rid))
            old = wait_observation(adapter, request)
        self.assertEqual(old.metadata["status"], "unknown")
        with adapter._db() as db:
            before = [tuple(row) for row in db.execute("SELECT * FROM observations ORDER BY rowid")]
            jobs = [tuple(row) for row in db.execute("SELECT * FROM jobs")]
        with patch.object(adapter, "start_once", side_effect=AssertionError("no launch")), \
             patch.object(adapter.runner, "start_once", side_effect=AssertionError("no process")):
            self.assertEqual(adapter.observe_existing(request), old)
            bridge.recover(self.rid, adapter)
            with patch.object(adapter.runner, "observe_existing", side_effect=AssertionError("archived bytes only")):
                corrected = adapter.redecode_existing(request)
                self.assertEqual(corrected.metadata["status"], "completed")
                self.assertNotEqual(corrected.metadata["observation_id"], old.metadata["observation_id"])
                self.assertEqual(corrected.raw_response, old.raw_response)
                self.assertEqual(adapter.redecode_existing(request), corrected)
                self.assertEqual(adapter.observe_existing(request), corrected)
            received = bridge.receive(self.rid, corrected)
            self.assertEqual(received["core_classification"], "returned_valid")
        self.assertEqual(CodexFixtureTransport(adapter.root).observe_existing(request), corrected)
        carrier, old_carrier = json.loads(corrected.raw_transport), json.loads(old.raw_transport)
        self.assertEqual(carrier["streams_base64"], old_carrier["streams_base64"])
        self.assertEqual(carrier["process_capture"], old_carrier["process_capture"])
        self.assertEqual(carrier["binding"], old_carrier["binding"])
        self.assertEqual(carrier["explicit_redecode"], {"decoder_version": codex_contract.CONTRACT_VERSION,
            "reconnect_whitelist": "429 retry limit; websocket 403; code-mode fail-closed notice",
            "original_observation_id": old.metadata["observation_id"], "original_phase": "worker_finished"})
        with adapter._db() as db:
            after = [tuple(row) for row in db.execute("SELECT * FROM observations ORDER BY rowid")]
            self.assertEqual(after[:-1], before)
            self.assertEqual(after[-1][1], REDECODE_PHASE)
            self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM jobs")], jobs)
        with closing(sqlite3.connect(adapter.runner.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)
        self.assertEqual(self.core.status()["budget"]["reserved_calls"], 1)

    def test_explicit_redecode_rejections_never_append_or_start(self):
        for scenario, status in (("reconnect-failed", "failed"), ("no-final", "unknown"), ("mismatch", "unknown")):
            with self.subTest(scenario=scenario):
                adapter, request = self.adapter(scenario=scenario), self.request()
                adapter.start_once(request)
                old = wait_observation(adapter, request)
                self.assertEqual(old.metadata["status"], status)
                with adapter._db() as db:
                    before = [tuple(row) for row in db.execute("SELECT * FROM observations ORDER BY rowid")]
                with patch.object(adapter.runner, "start_once", side_effect=AssertionError("no launch")), \
                     patch.object(adapter.runner, "observe_existing", side_effect=AssertionError("no new capture")):
                    self.assertEqual(adapter.redecode_existing(request), old)
                    self.assertIsNone(adapter.redecode_existing(self.request()))
                with adapter._db() as db:
                    self.assertEqual([tuple(row) for row in db.execute("SELECT * FROM observations ORDER BY rowid")], before)

    def test_concurrent_adapter_instances_call_runner_only_once(self):
        adapter, request = self.adapter(delay_seconds=0.15), self.request()
        with ThreadPoolExecutor(max_workers=3) as pool:
            list(pool.map(lambda _: CodexFixtureTransport(adapter.root).start_once(request), range(3)))
        observation = wait_observation(adapter, request)
        self.assertEqual(observation.metadata["status"], "completed")
        with closing(sqlite3.connect(adapter.runner.db_path)) as db:
            self.assertEqual(db.execute("SELECT count(*) FROM jobs").fetchone()[0], 1)

    def test_adapter_fence_failure_before_runner_remains_unknown_without_retry(self):
        adapter, request = self.adapter(), self.request()
        with patch.object(adapter.runner, "start_once", side_effect=OSError("INERT crash gap")):
            with self.assertRaises(OSError):
                adapter.start_once(request)
        reopened = CodexFixtureTransport(adapter.root)
        with patch.object(reopened.runner, "start_once", side_effect=AssertionError("no restart")):
            observation = reopened.start_once(request)
        self.assertEqual(observation.metadata["status"], "unknown")
        self.assertIsNone(reopened.runner.observe_existing(reopened._binding(request)))

    def test_claim_cannot_be_reused_under_another_job(self):
        adapter, request = self.adapter(), self.request()
        adapter.start_once(request)
        other = DispatchRequest("another_job", request.core_claim_id, request.packet, request.packet_bytes, request.mode)
        with self.assertRaises((HarnessError, sqlite3.IntegrityError)):
            adapter.start_once(other)
        wait_observation(adapter, request)

    def test_same_packet_object_with_different_original_bytes_is_rejected_on_reopen(self):
        adapter, request = self.adapter(), self.request()
        adapter.start_once(request)
        wait_observation(adapter, request)
        changed = DispatchRequest(request.job_id, request.core_claim_id, request.packet, request.packet_bytes + b"\n", request.mode)
        with self.assertRaises(HarnessError):
            CodexFixtureTransport(adapter.root).observe_existing(changed)

    def test_packet_object_and_original_bytes_must_match_before_process(self):
        adapter, request = self.adapter(), self.request()
        altered = deepcopy(request.packet)
        altered["context"]["problem"]["content"] = "INERT changed"
        bad = DispatchRequest(request.job_id, request.core_claim_id, altered, request.packet_bytes, request.mode)
        with self.assertRaises(HarnessError):
            adapter.start_once(bad)
        self.assertIsNone(adapter.runner.observe_existing(adapter._binding(request)))

    def test_malformed_mismatched_missing_and_invalid_utf8_remain_unknown(self):
        for scenario in ("malformed", "mismatch", "no-final", "binary"):
            with self.subTest(scenario=scenario):
                adapter, request = self.adapter(scenario=scenario), self.request()
                adapter.start_once(request)
                observation = wait_observation(adapter, request)
                self.assertEqual(observation.metadata["status"], "unknown")
                self.assertFalse(observation.metadata["terminal_observed"])
                carrier = json.loads(observation.raw_transport)
                self.assertIn("stdout", carrier["streams_base64"])

    def test_official_failure_with_actual_thread_is_failed_but_timeout_is_unknown(self):
        for scenario, expected in (("nonzero", "failed"), ("timeout", "unknown")):
            adapter, request = self.adapter(scenario=scenario, timeout_seconds=0.12 if scenario == "timeout" else 2), self.request()
            adapter.start_once(request)
            observation = wait_observation(adapter, request)
            self.assertEqual(observation.metadata["status"], expected)
            if expected == "failed":
                self.assertTrue(observation.metadata["host_reference"])
                self.assertTrue(observation.metadata["terminal_observed"])

    def test_bad_native_binding_never_completes(self):
        bad = json.loads(response(self.packet))
        bad["binding"]["packet_id"] = "wrong_packet"
        adapter, request = self.adapter(response_bytes=json.dumps(bad).encode()), self.request()
        adapter.start_once(request)
        observed = wait_observation(adapter, request)
        self.assertEqual(observed.metadata["status"], "failed")
        self.assertTrue(observed.metadata["terminal_observed"])
        self.assertIn("invalid_response", json.loads(observed.raw_transport)["parser"]["diagnostics"])

    def test_invalid_domain_output_is_decided_by_core_not_transport(self):
        bad = json.loads(response(self.packet))
        bad["output"] = {"inert": "not a valid domain plan"}
        adapter = self.adapter(response_bytes=json.dumps(bad).encode())
        bridge = AdaptiveHarnessBridge.create(self.root / "bridge", self.core.root, adapter.describe())
        bridge.dispatch(self.rid, adapter)
        request = bridge._request(bridge._job(self.rid))
        observation = wait_observation(adapter, request)
        self.assertEqual(observation.metadata["status"], "completed")
        result = bridge.recover(self.rid, adapter)
        self.assertEqual(result["core_classification"], "returned_invalid")
        self.assertFalse(result["mathematical_truth_verified"])

    def test_bridge_close_before_late_completion_does_not_reopen_core(self):
        adapter = self.adapter(delay_seconds=0.2)
        bridge = AdaptiveHarnessBridge.create(self.root / "bridge", self.core.root, adapter.describe())
        bridge.dispatch(self.rid, adapter)
        request = bridge._request(bridge._job(self.rid))
        bridge.close("INERT finite local close")
        endpoint = deepcopy(self.core.snapshot()["endpoint"])
        wait_observation(adapter, request)
        bridge.recover(self.rid, adapter)
        self.assertEqual(self.core.snapshot()["endpoint"], endpoint)

    def test_live_constructor_without_explicit_authorization_cannot_start(self):
        with self.assertRaises(HarnessError):
            CodexCliTransport.create(self.root / "unapproved", profile={}, authorization=None,
                                      output_schemas={"propose_plan": {"type": "object"}})
        self.assertFalse((self.root / "unapproved").exists())


if __name__ == "__main__":
    unittest.main()
