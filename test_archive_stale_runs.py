"""346: archive_stale_runs.py must be dry-run by default and only touch copies."""
import tempfile
import time
import unittest
from pathlib import Path

import importlib.util

SCRIPT = Path(__file__).with_name("scripts") / "archive_stale_runs.py"
_spec = importlib.util.spec_from_file_location("archive_stale_runs", SCRIPT)
archive_stale_runs = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(archive_stale_runs)


class ArchiveStaleRunsTests(unittest.TestCase):
    NOW = 1_800_000_000.0

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.done = self.base / "agent-runs-done.tsv"
        self.card_dir = self.base / "20261001" / "old-card"
        self.card_dir.mkdir(parents=True)
        (self.card_dir / "REPORT.md").write_text("done\n", encoding="utf-8")

    def touch(self, path, age_days):
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x\n", encoding="utf-8")
        stamp = self.NOW - age_days * 86400
        import os
        os.utime(path, (stamp, stamp))
        return path

    def write_done(self):
        old_result = self.touch(self.base / "old" / "REPORT.md", 10)
        fresh_result = self.touch(self.base / "fresh" / "REPORT.md", 1)
        rows = [
            "# Completed/archived runs. Columns: name<TAB>log_path<TAB>LAST_or_REPORT",
            f"old-card\t{self.base}/old/run.log\t{old_result}",
            f"fresh-card\t{self.base}/fresh/run.log\t{fresh_result}",
            f"dated-card\t{self.base}/20220101/dated/run.log\t{self.base}/missing/REPORT.md",
            f"unknown-card\t{self.base}/nowhere/run.log\t{self.base}/missing/REPORT.md",
        ]
        self.done.write_text("\n".join(rows) + "\n", encoding="utf-8")
        return rows

    def run_main(self, *extra):
        return archive_stale_runs.main([
            "--done", str(self.done), "--now", str(self.NOW), "--quiet",
            *extra,
        ])

    def test_dry_run_is_default_and_writes_nothing(self):
        original_rows = self.write_done()
        original = self.done.read_text(encoding="utf-8")
        before = sorted(p.name for p in self.base.rglob("*"))
        self.assertEqual(self.run_main(), 0)
        self.assertEqual(self.done.read_text(encoding="utf-8"), original)
        self.assertEqual(original_rows[0], original.splitlines()[0])
        self.assertFalse((self.base / "agent-runs-done.tsv.archive.tsv").exists())
        self.assertEqual([p.name for p in self.base.glob("*.bak-awclean-*")], [])
        self.assertEqual(sorted(p.name for p in self.base.rglob("*")), before)

    def test_apply_backs_up_then_moves_only_stale_rows(self):
        self.write_done()
        original = self.done.read_text(encoding="utf-8")
        self.assertEqual(self.run_main("--apply"), 0)

        backups = list(self.base.glob("agent-runs-done.tsv.bak-awclean-*"))
        self.assertEqual(len(backups), 1)
        self.assertEqual(backups[0].read_text(encoding="utf-8"), original)

        kept = self.done.read_text(encoding="utf-8")
        self.assertIn("fresh-card", kept)
        self.assertIn("unknown-card", kept)  # no timestamp -> never archived
        self.assertNotIn("old-card", kept)
        self.assertNotIn("dated-card", kept)
        self.assertIn("# Completed/archived runs", kept)

        archive = self.base / "agent-runs-done.tsv.archive.tsv"
        archived = archive.read_text(encoding="utf-8")
        self.assertIn("old-card", archived)
        self.assertIn("dated-card", archived)
        self.assertNotIn("fresh-card", archived)
        # The card folder is never deleted or moved.
        self.assertTrue((self.card_dir / "REPORT.md").is_file())

    def test_seven_day_boundary_keeps_exactly_seven_days(self):
        boundary = self.touch(self.base / "boundary" / "REPORT.md", 7)
        stale = self.touch(self.base / "stale" / "REPORT.md", 7.1)
        self.done.write_text(
            f"boundary-card\t{self.base}/boundary/run.log\t{boundary}\n"
            f"stale-card\t{self.base}/stale/run.log\t{stale}\n",
            encoding="utf-8",
        )
        self.assertEqual(self.run_main("--apply"), 0)
        kept = self.done.read_text(encoding="utf-8")
        self.assertIn("boundary-card", kept)
        self.assertNotIn("stale-card", kept)

    def test_archive_append_keeps_previous_run(self):
        self.write_done()
        self.assertEqual(self.run_main("--apply"), 0)
        archive = self.base / "agent-runs-done.tsv.archive.tsv"
        first = archive.read_text(encoding="utf-8")
        # A second apply with nothing stale must not rewrite anything.
        self.assertEqual(self.run_main("--apply"), 0)
        self.assertEqual(archive.read_text(encoding="utf-8"), first)
        self.assertEqual(len(list(self.base.glob("*.bak-awclean-*"))), 1)

    def test_refuses_missing_input(self):
        self.assertEqual(archive_stale_runs.main([
            "--done", str(self.base / "nope.tsv"), "--now", str(self.NOW),
        ]), 2)

    def test_refuses_same_archive_and_done(self):
        self.write_done()
        self.assertEqual(archive_stale_runs.main([
            "--done", str(self.done), "--archive", str(self.done),
            "--now", str(self.NOW),
        ]), 2)


if __name__ == "__main__":
    unittest.main()
