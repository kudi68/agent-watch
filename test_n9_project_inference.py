"""N9: unknown dispatch hints keep inferring, `其他` ratio, and the daily TSV."""
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import watch


class UnknownHintInferenceTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.runs = self.base / "runs"
        self.runs.mkdir()
        patcher = patch.object(watch, "ROOT_DIR", self.runs)
        patcher.start()
        self.addCleanup(patcher.stop)

    def task_dir(self, name="task"):
        folder = self.runs / "20261028" / name
        folder.mkdir(parents=True)
        (folder / "run.log").write_text("工作紀錄\n", encoding="utf-8")
        return folder

    def add_worktree(self, task_dir, repo_name, worktree="repo"):
        owner = self.base / "Projects" / repo_name
        gitdir = owner / ".git" / "worktrees" / worktree
        gitdir.mkdir(parents=True)
        checkout = task_dir / worktree
        checkout.mkdir(parents=True, exist_ok=True)
        (checkout / ".git").write_text(f"gitdir: {gitdir}\n", encoding="utf-8")
        return checkout

    def infer(self, folder, name, hint=None):
        return watch.infer_project_details(
            name,
            folder / "run.log",
            folder / "REPORT.md",
            task_dir=folder,
            project_hint=hint,
        )

    def test_unknown_hint_falls_through_to_git(self):
        folder = self.task_dir("aw-cleanup")
        self.add_worktree(folder, "agent-watch")
        project, source, warning = self.infer(folder, "aw-cleanup", hint="DONE")
        self.assertEqual((project, source), ("agent-watch", "git"))
        self.assertIn("未知專案", warning)

    def test_unknown_hint_falls_through_to_keywords(self):
        folder = self.task_dir("medtool-import")
        project, source, warning = self.infer(folder, "medtool-import", hint="foo")
        self.assertEqual((project, source), ("medical-tools", "關鍵字"))
        self.assertIn("foo", warning)

    def test_unknown_hint_without_evidence_stays_other(self):
        folder = self.task_dir("misc")
        project, source, warning = self.infer(folder, "misc", hint="foo")
        self.assertEqual((project, source), ("other", "未判斷"))
        self.assertIn("未知專案", warning)

    def test_unknown_hint_naming_a_repo_is_classified(self):
        cases = {
            "med-knowledge": "medknowledge",
            "Projects/MedKnowledge/": "medknowledge",
            "personal_capture": "personal-capture",
            "agent-ops-host-pinned-bb7a091": "agent-ops-pinned",
        }
        for hint, expected in cases.items():
            with self.subTest(hint=hint):
                folder = self.task_dir(f"task-{expected}-{abs(hash(hint))}")
                project, source, warning = self.infer(folder, "沒有語意的任務名", hint=hint)
                self.assertEqual((project, source), (expected, "標籤"))
                # A hint spelling that is not a registered id keeps its warning.
                self.assertTrue(warning == "" or "未知專案" in warning)

    def test_valid_hint_is_unchanged(self):
        folder = self.task_dir("shortcut")
        project, source, warning = self.infer(folder, "shortcut", hint="ios")
        self.assertEqual((project, source, warning), ("ios-shortcuts", "標籤", ""))

    def test_status_path_is_meta_and_never_wins(self):
        folder = self.task_dir("aw-status-update")
        (folder / "REPORT.md").write_text(
            "更新 ~/Projects/status/night.md\n" * 3
            + "改 ~/Projects/agent-watch/watch.py\n" * 3,
            encoding="utf-8",
        )
        project, source, _ = self.infer(folder, "aw-status-update")
        self.assertNotEqual(project, "status")
        self.assertEqual((project, source), ("agent-watch", "路徑"))

    def test_status_stays_meta_even_if_someone_registers_it(self):
        watch.ensure_architecture_data()
        watch.ARCHITECTURE_DATA["projects"]["status"] = {
            "label": "status", "aliases": [], "repo_paths": ["Projects/status"],
            "repo_names": ["status"], "default_view": "now",
        }
        self.addCleanup(
            lambda: watch.ARCHITECTURE_DATA["projects"].pop("status", None)
        )
        watch._PROJECT_PATH_CACHE.clear()
        folder = self.task_dir("看板筆記")
        (folder / "REPORT.md").write_text(
            "更新 ~/Projects/status/night.md\n" * 8, encoding="utf-8"
        )
        project, source, _ = self.infer(folder, "看板筆記")
        self.assertEqual((project, source), ("other", "未判斷"))

    def test_status_only_mentions_are_not_evidence(self):
        folder = self.task_dir("status-only")
        (folder / "REPORT.md").write_text(
            "更新 ~/Projects/status/night.md\n" * 6, encoding="utf-8"
        )
        project, source, _ = self.infer(folder, "status-only")
        self.assertEqual((project, source), ("other", "未判斷"))


class OtherRatioTextTests(unittest.TestCase):
    def agent(self, project, source, age_seconds=0, now=None):
        current = time.time() if now is None else now
        return watch.AgentInfo(
            name=f"{project}-{source or 'none'}",
            log_path=Path("/tmp/a"),
            last_path=Path("/tmp/a"),
            status="running",
            model="m",
            start_time=current - age_seconds,
            elapsed="0",
            latest="l",
            project=project,
            project_source=source,
        )

    def test_other_summary_counts_and_ratio(self):
        now = time.time()
        agents = [
            self.agent("agent-watch", "標籤", now=now),
            self.agent("ios-shortcuts", "git", now=now),
            self.agent("other", "未判斷", now=now),
            self.agent("other", "未判斷", now=now),
        ]
        self.assertEqual(watch.project_other_summary(agents, now), "其他 2（50.0%）")

    def test_other_summary_ignores_cards_older_than_the_window(self):
        now = time.time()
        agents = [
            self.agent("agent-watch", "標籤", now=now),
            self.agent("other", "未判斷", age_seconds=20 * 86400, now=now),
        ]
        self.assertEqual(watch.project_other_summary(agents, now), "其他 0（0.0%）")

    def test_other_summary_is_in_the_plain_summary_text(self):
        now = time.time()
        agents = [
            self.agent("agent-watch", "標籤", now=now),
            self.agent("other", "未判斷", now=now),
        ]
        text = watch.format_bucket_counts(agents, now)
        self.assertIn("其他 1（50.0%）", text)


class DailyStatsTests(unittest.TestCase):
    def agent(self, project, source, when):
        return watch.AgentInfo(
            name=f"{project}-{source}",
            log_path=Path("/tmp/a"),
            last_path=Path("/tmp/a"),
            status="complete",
            model="m",
            start_time=when,
            elapsed="0",
            latest="DONE",
            completion_time=when,
            project=project,
            project_source=source,
        )

    def day_start(self, year=2026, month=10, day=28):
        return time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))

    def test_row_has_every_required_column(self):
        day = self.day_start()
        agents = [
            self.agent("agent-watch", "標籤", day + 3600),
            self.agent("other", "未判斷", day + 7200),
        ]
        row = watch.daily_stats_row(agents, day, now=day + 86400)
        for column in (
            "other_count", "other_ratio", "basis_標籤", "basis_派工",
            "basis_關鍵字", "basis_git", "basis_路徑", "basis_未判斷",
        ):
            self.assertIn(column, row)
            self.assertIn(column, watch.DAILY_STATS_COLUMNS)
        self.assertEqual(row["total"], "2")
        self.assertEqual(row["other_count"], "1")
        self.assertEqual(row["other_ratio"], "50.0")
        self.assertEqual(row["basis_標籤"], "1")
        self.assertEqual(row["basis_未判斷"], "1")
        self.assertEqual(row["basis_git"], "0")

    def test_daily_row_is_rewritten_idempotently(self):
        day = self.day_start()
        agents = [self.agent("other", "未判斷", day + 3600)]
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "daily" / "agent-watch-daily.tsv"
            watch.write_daily_stats(target, agents, day_start=day, now=day + 86400)
            first = target.read_text(encoding="utf-8")
            watch.write_daily_stats(target, agents, day_start=day, now=day + 86400)
            self.assertEqual(target.read_text(encoding="utf-8"), first)
            lines = [line for line in first.splitlines() if line.strip()]
            self.assertEqual(len(lines), 2)  # header + one day
            self.assertEqual(lines[0].split("\t"), list(watch.DAILY_STATS_COLUMNS))
            self.assertEqual(lines[1].split("\t")[0], "2026-10-28")

    def test_other_days_are_preserved_and_sorted(self):
        day = self.day_start()
        older = day - 86400
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "agent-watch-daily.tsv"
            watch.write_daily_stats(
                target, [self.agent("other", "未判斷", older + 60)],
                day_start=older, now=older + 86400,
            )
            watch.write_daily_stats(
                target, [self.agent("agent-watch", "標籤", day + 60)],
                day_start=day, now=day + 86400,
            )
            rows = target.read_text(encoding="utf-8").splitlines()[1:]
            self.assertEqual([row.split("\t")[0] for row in rows], ["2026-10-27", "2026-10-28"])
            self.assertEqual(rows[0].split("\t")[watch.DAILY_STATS_COLUMNS.index("other_count")], "1")
            self.assertEqual(rows[1].split("\t")[watch.DAILY_STATS_COLUMNS.index("other_count")], "0")


if __name__ == "__main__":
    unittest.main()
