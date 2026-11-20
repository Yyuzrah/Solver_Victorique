"""The public run driver keeps observing past one local observation bound (runs without pytest)."""
from datetime import datetime, timezone
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from victorique import api

BOUND = {"phase": "local_observation_bound", "native_endpoint_created_by_this_bound": False}


def _fake_run(tmp):
    problem = Path(tmp) / "problem.md"
    problem.write_text("Prove that n + 0 = n for every integer n.", encoding="utf-8")
    return api._create_run(problem, policy="single", calls=3, out=Path(tmp) / "run", transport="fake")


class ObservationBoundTest(unittest.TestCase):
    def test_run_continues_after_a_local_observation_bound(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, loop = _fake_run(tmp)
            real_run, seen = loop.run, []

            def first_bounded(**kwargs):
                seen.append(kwargs)
                return dict(BOUND, status=loop.status()) if len(seen) == 1 else real_run(**kwargs)

            loop.run = first_bounded
            summary = api._run(directory, loop)
        self.assertEqual(len(seen), 2)
        self.assertEqual(summary["outcome"], "complete")
        self.assertEqual(summary["calls_used"], 3)

    def test_run_stops_at_the_bound_once_the_deadline_has_passed(self):
        with tempfile.TemporaryDirectory() as tmp:
            directory, loop = _fake_run(tmp)
            seen = []

            def always_bounded(**kwargs):
                seen.append(kwargs)
                return dict(BOUND, status=loop.status())

            loop.run = always_bounded
            past = datetime(2000, 1, 1, tzinfo=timezone.utc)
            with mock.patch.object(api, "_deadline", return_value=past):
                summary = api._run(directory, loop)
        self.assertEqual(len(seen), 1)
        self.assertEqual(summary["outcome"], "pending")


if __name__ == "__main__":
    unittest.main()
