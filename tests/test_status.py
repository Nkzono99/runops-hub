from __future__ import annotations

import contextlib
import io
import os
import tempfile
import time
import unittest
from pathlib import Path

from helpers import load, member


def finished_run(project: Path, run_id: str, day: str, notes: str = "") -> dict:
    run = project / "runs" / "series" / f"{run_id}--x"
    (run / "analysis").mkdir(parents=True)
    (run / "manifest.toml").write_text(
        f'[job]\n[[job.attempts]]\nfinished_at = "{day}T10:00:00+09:00"\n{notes}', encoding="utf-8"
    )
    return {"id": run_id, "status": "completed", "run_dir": f"runs/series/{run_id}--x"}


class StatusTest(unittest.TestCase):
    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.hub = Path(tmp.name) / "hub"
        (self.hub / ".hub").mkdir(parents=True)
        self.status = load("status", self.hub)
        self.project = member(self.hub, "demo")

    def log(self, text: str) -> None:
        (self.project / "LOG.md").write_text(text, encoding="utf-8")

    def test_run_ids_cover_the_ways_a_log_writes_them(self) -> None:
        ids = self.status.run_ids_in
        self.assertEqual(len(ids("R20261104-0016〜0027")), 12)
        self.assertEqual(ids("R20261104-0007/0008"), {"R20261104-0007", "R20261104-0008"})
        self.assertEqual(len(ids("R20261105-000{3..7}--z")), 5)
        self.assertEqual(len(ids("R20261022-0001〜0010 と R20261025-0001〜0008")), 18)
        self.assertEqual(len(ids("R20261104-0004/0005/0006")), 3)
        self.assertEqual(ids("R20261104-0016, job 9037039"), {"R20261104-0016"})

    def test_a_completed_run_nobody_mentions_is_listed(self) -> None:
        run = finished_run(self.project, "R20261105-0001", "2026-11-06")
        self.log("# Log\n\n## 2026-11-01 — setup\n\ncreated R20261105-0001\n")
        self.assertEqual(self.status.unconcluded(self.project, [run]), [("2026-11-06", "R20261105-0001")])

    def test_a_later_log_entry_note_or_report_counts_as_written_up(self) -> None:
        by_log = finished_run(self.project, "R20261105-0001", "2026-11-06")
        by_note = finished_run(
            self.project, "R20261105-0002", "2026-11-06", '[[notes]]\nat = "2026-11-07T09:00:00+09:00"\n'
        )
        by_report = finished_run(self.project, "R20261105-0003", "2026-11-06")
        topic = self.project / "analysis" / "compare"
        topic.mkdir(parents=True)
        (topic / "summary.csv").write_text("run\nR20261105-0003\n", encoding="utf-8")
        self.log("# Log\n\n## 2026-11-01 — setup\n\n## 2026-11-07 — result\n\nR20261105-0001 は定常。\n")
        self.assertEqual(self.status.unconcluded(self.project, [by_log, by_note, by_report]), [])

    def test_a_status_snapshot_entry_does_not_count(self) -> None:
        run = finished_run(self.project, "R20261105-0001", "2026-11-06")
        self.log("# Log\n\n## 2026-11-01 — setup\n\n## 2026-11-07 — hub進捗確認\n\nR20261105-0001 完了。\n")
        self.assertEqual(len(self.status.unconcluded(self.project, [run])), 1)

    def test_an_old_report_does_not_count(self) -> None:
        run = finished_run(self.project, "R20261105-0001", "2026-11-06")
        topic = self.project / "analysis" / "plan"
        topic.mkdir(parents=True)
        plan = topic / "plan.json"
        plan.write_text('["R20261105-0001"]', encoding="utf-8")
        before = time.mktime((2026, 11, 5, 12, 0, 0, 0, 0, -1))
        os.utime(plan, (before, before))
        self.log("# Log\n\n## 2026-11-01 — setup\n")
        self.assertEqual(len(self.status.unconcluded(self.project, [run])), 1)

    def test_changes_are_reported_against_the_saved_look(self) -> None:
        self.status.save_seen({"demo": {"R1": "running", "R2": "completed"}})
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            self.status.print_changes(
                self.status.load_seen(), {"demo": {"R1": "completed", "R2": "completed", "R3": "created"}}
            )
        self.assertIn("demo: completed 1 (R1); created 1 (R3)", out.getvalue())


if __name__ == "__main__":
    unittest.main()
