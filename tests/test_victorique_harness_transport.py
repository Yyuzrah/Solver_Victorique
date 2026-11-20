"""INERT transport invariants; these tests never run a real host or model."""

from concurrent.futures import ThreadPoolExecutor
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import unittest

from victorique.harness.journal import HarnessError, HarnessJournal
from victorique.harness.transport import MockProcessTransport


PACKET = {"data_classification": "synthetic_software_test", "inert": True,
          "requested_model": "INERT_REQUESTED_LABEL", "payload": "INERT"}


class HarnessTransportTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix="victorique-harness-test-")
        self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name) / "state"

    def journal(self, **kwargs):
        journal = HarnessJournal.create(self.root, **kwargs)
        journal.prepare("job-1", PACKET)
        return journal

    def test_success_records_real_fixture_pid_raw_output_and_null_model_cost(self):
        journal = self.journal()
        result = journal.start_once("job-1", MockProcessTransport())
        self.assertTrue(result["dispatched"])
        self.assertEqual(result["state"], "completed")
        receipt = result["receipt"]
        self.assertGreater(receipt["process"]["pid"], 0)
        self.assertEqual(json.loads(receipt["stdout"])["response"], "INERT_OK")
        self.assertEqual(receipt["requested_model"], PACKET["requested_model"])
        self.assertIsNone(receipt["actual_model"])
        self.assertIsNone(receipt["usage"])
        self.assertIsNone(receipt["cost"])
        self.assertFalse(receipt["external_host_verified"])
        self.assertFalse(receipt["isolation_verified"])
        exported = journal.export()
        self.assertEqual([x["kind"] for x in exported["events"]], ["prepared", "entered", "receipt"])
        self.assertEqual(exported["jobs"][0]["receipt"], receipt)
        self.assertEqual(journal.status()["calls_reserved"], 1)
        self.assertFalse(journal.status()["financially_settled"])

    def test_no_repeat_after_completion_or_reopen(self):
        journal = self.journal()
        journal.start_once("job-1", MockProcessTransport())
        def forbidden(*args):
            self.fail("already entered job was re-executed")
        result = HarnessJournal(self.root).start_once("job-1", forbidden)
        self.assertFalse(result["dispatched"])
        self.assertEqual(result["state"], "completed")

    def test_prepared_packet_frozen_and_not_mutated_by_callback(self):
        journal = self.journal()
        def mutate(packet, claim):
            packet["payload"] = "changed"
            return MockProcessTransport()(packet, claim)
        journal.start_once("job-1", mutate)
        self.assertEqual(journal.export()["jobs"][0]["packet"], PACKET)
        self.assertFalse(journal.prepare("job-1", dict(PACKET))["prepared"])
        with self.assertRaisesRegex(HarnessError, "different frozen"):
            journal.prepare("job-1", {**PACKET, "payload": "replacement"})

    def test_claim_committed_before_callback(self):
        journal = self.journal()
        def inspect(packet, claim):
            reopened = HarnessJournal(self.root)
            status = reopened.job_status("job-1")
            self.assertEqual(status["state"], "entered")
            self.assertEqual(status["dispatch_id"], claim["dispatch_id"])
            self.assertEqual(reopened.status()["calls_reserved"], 1)
            return MockProcessTransport()(packet, claim)
        journal.start_once("job-1", inspect)

    def test_two_concurrent_coordinators_execute_once(self):
        journal = self.journal()
        entered = threading.Event()
        release = threading.Event()
        calls = []
        def execute(packet, claim):
            calls.append(claim["dispatch_id"])
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test wait expired")
            return MockProcessTransport()(packet, claim)
        with ThreadPoolExecutor(max_workers=2) as pool:
            first = pool.submit(journal.start_once, "job-1", execute)
            self.assertTrue(entered.wait(5))
            second = pool.submit(HarnessJournal(self.root).start_once, "job-1", execute)
            self.assertFalse(second.result(timeout=5)["dispatched"])
            release.set()
            self.assertEqual(first.result(timeout=5)["state"], "completed")
        self.assertEqual(len(calls), 1)

    def test_unknown_holds_capacity_and_cumulative_budget(self):
        journal = self.journal(max_calls=2)
        def lost(*args):
            raise RuntimeError("simulated missing observation")
        journal.start_once("job-1", lost)
        journal.prepare("job-2", PACKET)
        with self.assertRaisesRegex(HarnessError, "in-flight"):
            journal.start_once("job-2", MockProcessTransport())
        self.assertEqual(journal.status()["calls_reserved"], 1)
        self.assertEqual(journal.status()["outstanding_calls"], 1)
        self.assertFalse(journal.start_once("job-1", lost)["dispatched"])

    def test_completion_does_not_refund_call_budget(self):
        journal = self.journal()
        journal.start_once("job-1", MockProcessTransport())
        journal.prepare("job-2", PACKET)
        with self.assertRaisesRegex(HarnessError, "cumulative"):
            journal.start_once("job-2", MockProcessTransport())

    def test_unknown_late_receipt_allows_next_reserved_call_only_once(self):
        journal = self.journal(max_calls=2)
        receipts = []
        def lose_reply(packet, claim):
            receipts.append(MockProcessTransport()(packet, claim))
            raise RuntimeError("reply delivery lost")
        self.assertEqual(journal.start_once("job-1", lose_reply)["state"], "unknown")
        journal = HarnessJournal(self.root)
        self.assertEqual(journal.recover("job-1")["remote_observation"], "unsupported")
        self.assertFalse(journal.start_once("job-1", MockProcessTransport())["dispatched"])
        result = journal.reconcile("job-1", receipts[0], "INERT saved reply")
        self.assertTrue(result["late"])
        self.assertEqual(journal.status()["outstanding_calls"], 0)
        self.assertEqual(journal.status()["calls_reserved"], 1)
        self.assertFalse(journal.reconcile("job-1", receipts[0], "INERT same reply")["recorded"])
        journal.prepare("job-2", PACKET)
        self.assertEqual(journal.start_once("job-2", MockProcessTransport())["state"], "completed")
        self.assertEqual(journal.status()["calls_remaining"], 0)

    def test_abrupt_process_exit_after_claim_recovers_without_execute(self):
        self.journal()
        script = ("import sys, os; "
                  "from victorique.harness.journal import HarnessJournal; "
                  "HarnessJournal(sys.argv[1]).start_once('job-1', lambda packet, claim: os._exit(23))")
        result = subprocess.run([sys.executable, "-I", "-B", "-c", script, str(self.root)],
                                capture_output=True, creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0), text=True, timeout=10)
        self.assertEqual(result.returncode, 23, result.stderr)
        reopened = HarnessJournal(self.root)
        self.assertEqual(reopened.job_status("job-1")["state"], "entered")
        self.assertEqual(reopened.recover("job-1")["state"], "unknown")
        self.assertFalse(reopened.start_once("job-1", MockProcessTransport())["dispatched"])
        self.assertEqual(reopened.status()["calls_reserved"], 1)

    def test_recover_racing_live_return_is_late_observation(self):
        journal = self.journal()
        entered, release = threading.Event(), threading.Event()
        def execute(packet, claim):
            entered.set()
            if not release.wait(5):
                raise RuntimeError("test wait expired")
            return MockProcessTransport()(packet, claim)
        with ThreadPoolExecutor(max_workers=1) as pool:
            future = pool.submit(journal.start_once, "job-1", execute)
            self.assertTrue(entered.wait(5))
            self.assertEqual(HarnessJournal(self.root).recover("job-1")["state"], "unknown")
            self.assertFalse(journal.start_once("job-1", MockProcessTransport())["dispatched"])
            release.set()
            self.assertEqual(future.result(timeout=5)["state"], "completed")
        self.assertEqual(journal.export()["events"][-1]["kind"], "late_receipt")

    def test_malformed_nonzero_and_truncated_keep_raw_evidence(self):
        journal = self.journal(max_calls=3)
        for index, mode in enumerate(("malformed", "nonzero", "truncated"), start=1):
            job_id = f"job-{index}"
            journal.prepare(job_id, PACKET)
            result = journal.start_once(job_id, MockProcessTransport(mode))
            self.assertEqual(result["state"], "failed")
            receipt = result["receipt"]
            self.assertTrue(receipt["stdout"] or receipt["stderr"])
            self.assertIsNone(receipt["output"])
            self.assertIsNone(receipt["host_session_id"])
        self.assertEqual(journal.status()["calls_reserved"], 3)

    def test_timeout_is_unknown_even_when_local_child_was_killed(self):
        journal = self.journal()
        result = journal.start_once("job-1", MockProcessTransport("timeout", timeout_seconds=0.5))
        self.assertEqual(result["state"], "unknown")
        self.assertTrue(result["receipt"]["process"]["kill_requested"])
        self.assertTrue(result["receipt"]["process"]["timed_out"])
        self.assertIsNotNone(result["receipt"]["process"]["exit_code"])
        self.assertFalse(HarnessJournal(self.root).start_once("job-1", MockProcessTransport())["dispatched"])

    def test_invalid_callback_receipt_keeps_raw_and_unknown(self):
        journal = self.journal()
        invalid = {"INERT": "not a receipt"}
        self.assertEqual(journal.start_once("job-1", lambda *_: invalid)["state"], "unknown")
        event = next(e for e in journal.export()["events"] if e["kind"] == "invalid_receipt")
        self.assertEqual(event["detail"]["raw_receipt"], invalid)

    def test_wrong_receipt_dispatch_cannot_resolve_unknown(self):
        journal = self.journal()
        receipts = []
        def lost(packet, claim):
            receipts.append(MockProcessTransport()(packet, claim))
            raise RuntimeError()
        journal.start_once("job-1", lost)
        foreign = copy.deepcopy(receipts[0])
        foreign["dispatch_id"] = "different"
        with self.assertRaisesRegex(HarnessError, "dispatch_id"):
            journal.reconcile("job-1", foreign, "INERT wrong dispatch")
        self.assertEqual(journal.job_status("job-1")["state"], "unknown")

    def test_late_receipt_after_close_does_not_reopen(self):
        journal = self.journal(max_calls=2)
        receipts = []
        def lost(packet, claim):
            receipts.append(MockProcessTransport()(packet, claim))
            raise RuntimeError()
        journal.start_once("job-1", lost)
        journal.prepare("job-2", PACKET)
        journal.close("INERT stopping boundary")
        result = journal.reconcile("job-1", receipts[0], "INERT late observation")
        self.assertTrue(result["late"])
        self.assertTrue(journal.status()["closed"])
        self.assertEqual(journal.status()["calls_reserved"], 1)
        self.assertTrue(journal.export()["events"][-1]["detail"]["after_close"])
        with self.assertRaisesRegex(HarnessError, "closed"):
            journal.start_once("job-2", MockProcessTransport())

    def test_conflicting_terminal_receipt_is_audited_not_overwritten(self):
        journal = self.journal()
        receipt = journal.start_once("job-1", MockProcessTransport())["receipt"]
        altered = copy.deepcopy(receipt)
        altered["output"]["response"] = "INERT_DIFFERENT"
        result = journal.reconcile("job-1", altered, "INERT conflicting reply")
        self.assertTrue(result["conflict"])
        self.assertEqual(journal.export()["jobs"][0]["receipt"], receipt)
        self.assertEqual(journal.export()["events"][-1]["kind"], "conflicting_receipt")

    def test_fixture_cannot_claim_real_model_or_cost(self):
        journal = self.journal()
        receipt = journal.start_once("job-1", MockProcessTransport())["receipt"]
        for key, value in (("actual_model", "claimed-real"), ("usage", {"tokens": 1}), ("cost", 0)):
            altered = copy.deepcopy(receipt)
            altered[key] = value
            with self.assertRaises(HarnessError):
                journal.reconcile("job-1", altered, "INERT invalid accounting")

    def test_non_inert_and_nonfinite_packet_rejected_before_claim(self):
        journal = HarnessJournal.create(self.root)
        for packet in ({}, {**PACKET, "inert": False}, {**PACKET, "data_classification": "host_recorded"},
                       {**PACKET, "payload": float("nan")}, {**PACKET, "requested_model": 3}):
            with self.assertRaises(HarnessError):
                journal.prepare("invalid", packet)
        self.assertEqual(journal.status()["calls_reserved"], 0)

    def test_new_root_and_valid_owned_database_required(self):
        self.root.mkdir()
        with self.assertRaises(HarnessError):
            HarnessJournal.create(self.root)
        with self.assertRaises(HarnessError):
            HarnessJournal(self.root)
        (self.root / "harness.sqlite3").write_text("not sqlite", encoding="utf-8")
        with self.assertRaises(HarnessError):
            HarnessJournal(self.root)

    def test_budget_and_mock_arguments_must_be_finite_bounded(self):
        for value in (0, -1, True, 1.5, 10001):
            with self.assertRaises(HarnessError):
                HarnessJournal.create(self.root, max_calls=value)
        with self.assertRaises(HarnessError):
            HarnessJournal.create(self.root, max_calls=1, max_in_flight=2)
        for value in (float("nan"), float("inf"), True, 0, 20):
            with self.assertRaises(HarnessError):
                MockProcessTransport(timeout_seconds=value)
        with self.assertRaises(HarnessError):
            MockProcessTransport("codex")

    def test_existing_journal_with_hardlink_alias_is_rejected(self):
        journal = self.journal()
        alias_root = Path(self.temp.name) / "other-root"
        alias_root.mkdir()
        os.link(journal.path, alias_root / "harness.sqlite3")
        with self.assertRaisesRegex(HarnessError, "hardlink"):
            HarnessJournal(alias_root)
        with self.assertRaisesRegex(HarnessError, "hardlink"):
            journal.prepare("job-2", PACKET)

    def test_status_excludes_packet_and_raw_text(self):
        journal = self.journal()
        journal.start_once("job-1", MockProcessTransport())
        status = journal.status()
        self.assertNotIn("packet", status["jobs"][0])
        self.assertNotIn("stdout", json.dumps(status))
        self.assertFalse(status["research_gate_projection"])
        self.assertEqual(status["actual_model_calls"], 0)

    def test_close_and_recovery_are_idempotent_without_new_actions(self):
        journal = self.journal()
        before = journal.export()
        self.assertEqual(journal.recover("job-1")["state"], "prepared")
        self.assertEqual(journal.export(), before)
        self.assertEqual(journal.close("first"), journal.close("second"))
        self.assertEqual(journal.status()["calls_reserved"], 0)


if __name__ == "__main__":
    unittest.main()
