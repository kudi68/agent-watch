"""v8 regressions at TSV, quota and rendered-output boundaries."""
import json
import os
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import watch


class StatusRegressions(unittest.TestCase):
    def load(self, root, report, log="2026-10-25T01:00:00Z session start\n", duplicate=False):
        runs, done = root / "runs.tsv", root / "done.tsv"
        runs.write_text("")
        log_path, result = root / "run.log", root / "LAST.md"
        if log is not None:
            log_path.write_text(log)
        if report is not None:
            result.write_text(report)
        row = f"Example\t{log_path}\t{result}\n"
        done.write_text(row * (2 if duplicate else 1))
        return watch.load_all_agents(runs, done)[0]

    def test_real_partial_first_lines_override_completed_body(self):
        cases = {
            "pkg-2": "判定：**PARTIAL**——轉換器已完成\n18/18 PASS\n",
            "pkg-3": "判定：**PARTIAL** — 兩份檔案已寫好\n已完成測試\n",
            "pkg-3b": "判定：**PARTIAL** — 未完成建置\n20/20 通過\n",
            "pkg-4": "判定：**PARTIAL**；已完成本機 commit\n",
            "task-f": "判定：**PARTIAL**；本輪未套用 live\n",
        }
        for name, content in cases.items():
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                a = self.load(Path(tmp), content)[0]
                self.assertEqual(a.status, "partial")
                self.assertIn("PARTIAL", a.latest)

    def test_done_report_survives_missing_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.load(Path(tmp), "DONE\n正確報告\n", log=None)[0]
            self.assertEqual(a.status, "complete")
            self.assertTrue(a.error)

    def test_archived_missing_report_and_duplicate_v3(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = self.load(Path(tmp), None, duplicate=True)
            self.assertEqual(len(agents), 1)
            self.assertEqual(agents[0].status, "no_report")

    def test_unfinished_delivery_is_partial_and_spec_is_only_reported(self):
        for text, expected in (
            ("狀態：已建立程式；尚未完成交付。\n", "partial"),
            ("# 介面需求規格\n", "reported"),
        ):
            with self.subTest(text=text), tempfile.TemporaryDirectory() as tmp:
                a = self.load(Path(tmp), text)[0]
                self.assertEqual(a.status, expected)

    def test_quoted_dates_never_become_log_start(self):
        for name in ("pr-fix-39-40", "codex-budget-check", "fix-pr43"):
            with self.subTest(name=name), tempfile.TemporaryDirectory() as tmp:
                a = self.load(Path(tmp), "DONE\n", log="prompt: old event 2026-10-14T11:05:30Z\n")[0]
                self.assertLess(time.time() - a.start_time, 60)

    def test_report_conclusion_is_first_line_not_trailing_caveat(self):
        with tempfile.TemporaryDirectory() as tmp:
            a = self.load(Path(tmp), "DONE\n主要成果\n\n歷史失敗但已修正\n")[0]
            self.assertEqual(a.latest, "DONE")

    def test_editing_another_tsv_row_does_not_invalidate_fresh_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            self.load(root, "DONE\n")
            row = (root / "done.tsv").read_text()
            (root / "runs.tsv").write_text(row)
            (root / "done.tsv").write_text("")
            future = time.time() + 100
            os.utime(root / "runs.tsv", (future, future))
            self.assertEqual(watch.load_all_agents(root / "runs.tsv", root / "done.tsv")[0][0].status, "complete")

    def test_process_inventory_failure_never_means_dead(self):
        result = Mock(returncode=3, stdout="", stderr="Cannot get process list")
        with patch("watch.subprocess.run", return_value=result):
            self.assertIsNone(watch.is_pid_alive(1234))


class QuotaRegressions(unittest.TestCase):
    FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "magpie_quota.json"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sessions = Path(self._tmp.name)

    def test_magpie_fixture_parses_every_provider_window(self):
        payload = self.FIXTURE.read_text(encoding="utf-8")
        service = watch.QuotaService(sessions_path=self.sessions)
        with patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout=payload)):
            summary, raw = service.fetch(force=True)
        providers = [item["provider"] for item in raw]
        self.assertEqual(providers, ["claude", "codex", "opencode-go"])
        self.assertIn("claude 5h 19%", summary)
        self.assertIn("7d 46%", summary)
        self.assertIn("codex 5h 11%", summary)
        self.assertIn("7d 2%", summary)
        self.assertIn("opencode-go 5h 4%", summary)
        self.assertIn("month 8%", summary)
        self.assertRegex(summary, r"重置 \d{2}:\d{2}|\d{2}/\d{2} \d{2}:\d{2}")
        self.assertIn("資料時間", summary)

    def test_magpie_partial_row_shows_no_data_only_for_that_provider(self):
        payload = json.dumps([
            {"provider": "claude", "readAt": "2026-10-25T15:00:50+08:00",
             "windows": [{"name": "5 hours", "used": 30, "resetsAt": "2026-10-25T16:59:00+08:00"}]},
            {"provider": "codex", "readAt": "2026-10-25T15:00:52+08:00",
             "windows": [{"name": "5 hours", "remaining": 89}]},
        ])
        service = watch.QuotaService(sessions_path=self.sessions)
        with patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout=payload)):
            summary, _ = service.fetch(force=True)
        self.assertIn("claude 5h 30%", summary)
        self.assertIn("codex：無資料", summary)

    def test_magpie_row_without_readat_uses_fetch_time(self):
        payload = json.dumps([{
            "provider": "opencode-go",
            "name": "OpenCode Go",
            "windows": [
                {"name": "5 hours", "used": 4, "remaining": 96},
                {"name": "7 days", "used": 14, "remaining": 86},
                {"name": "Month", "used": 8, "remaining": 92},
            ],
        }])
        service = watch.QuotaService(sessions_path=self.sessions)
        with patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout=payload)):
            summary, raw = service.fetch(force=True)
        self.assertIn("opencode-go 5h 4%", summary)
        self.assertIn("7d 14%", summary)
        self.assertIn("month 8%", summary)
        self.assertIsNotNone(watch.quota_epoch(raw[0]["asOf"]))

    def test_magpie_failure_shows_no_data_without_crashing(self):
        for result in (
            Mock(returncode=1, stdout="", stderr="unavailable"),
            Mock(returncode=0, stdout="not json"),
            Mock(returncode=0, stdout=""),
        ):
            with self.subTest(result=result):
                service = watch.QuotaService(sessions_path=self.sessions)
                with patch("watch.subprocess.run", return_value=result):
                    summary, raw = service.fetch(force=True)
                self.assertEqual("額度：無資料", summary)
                self.assertEqual([], raw)
                self.assertTrue(service.stale)

    def test_magpie_timeout_shows_no_data(self):
        service = watch.QuotaService(sessions_path=self.sessions)
        with patch("watch.subprocess.run", side_effect=watch.subprocess.TimeoutExpired("magpie", 10)):
            summary, raw = service.fetch(force=True)
        self.assertEqual("額度：無資料", summary)
        self.assertEqual([], raw)
        self.assertIn("逾時", service.last_error)

    def test_codex_session_limits_used_when_magpie_omits_codex(self):
        with tempfile.TemporaryDirectory() as tmp:
            session = Path(tmp) / "session.jsonl"
            session.write_text(json.dumps({
                "timestamp": "2026-10-25T06:00:00Z",
                "payload": {"rate_limits": {
                    "primary": {"used_percent": 12, "window_minutes": 300},
                    "secondary": {"used_percent": 34, "window_minutes": 10080},
                }},
            }) + "\n")
            service = watch.QuotaService(sessions_path=Path(tmp))
            magpie = Mock(returncode=0, stdout=json.dumps([{
                "provider": "claude",
                "readAt": "2026-10-25T06:01:00Z",
                "windows": [{"name": "5 hours", "used": 20}],
            }]))
            with patch("watch.subprocess.run", return_value=magpie):
                summary, raw = service.fetch(force=True)
            codex = [item for item in raw if item["provider"] == "codex"][0]
            self.assertEqual(codex["asOf"], "2026-10-25T06:00:00Z")
            self.assertEqual([window["used"] for window in codex["windows"]], [12.0, 34.0])
            self.assertIn("codex 5h 12%｜7d 34%", summary)

    def test_magpie_codex_value_replaces_old_cached_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "new.jsonl").write_text(json.dumps({
                "timestamp": "2026-10-25T06:00:00Z",
                "payload": {"rate_limits": None},
            }) + "\n")
            service = watch.QuotaService(sessions_path=root)
            service.cached_raw = [{"provider": "codex", "windows": [{"name": "5 hours", "used": 88}]}]
            magpie = Mock(returncode=0, stdout=json.dumps([{"provider": "codex",
                "readAt": "2026-10-25T06:01:00Z",
                "windows": [{"name": "5 hours", "used": 55}]}]))
            with patch("watch.subprocess.run", return_value=magpie):
                summary, raw = service.fetch(force=True)
            self.assertIn("codex 5h 55%", summary)
            self.assertNotIn("88%", summary)
            self.assertEqual(raw[0]["asOf"], "2026-10-25T06:01:00Z")

    def test_quota_numbers_require_valid_value_and_source_time(self):
        invalid = [None, "bad", True, float("nan"), -2, 101]
        for value in invalid:
            with self.subTest(value=value):
                text = watch.render_full_quota_view([{"provider": "claude",
                    "asOf": "2026-10-25T06:00:00Z", "windows": [{"name": "5 hours", "used": value}]}])
                self.assertIn("無資料", text)
                self.assertNotIn("%", watch.clean_text(text).split("claude")[-1])
        valid = [{"provider": "codex", "asOf": "2026-10-25T06:00:00Z",
                  "windows": [{"name": "5 hours", "used": 12}]}]
        text = watch.render_full_quota_view(valid)
        self.assertIn("12%", text)
        self.assertIn("2026-10-25 14:00:00", text)
        valid[0].pop("asOf")
        self.assertNotIn("12%", watch.render_full_quota_view(valid))

    def test_failed_refresh_clears_previous_numbers(self):
        with tempfile.TemporaryDirectory() as tmp:
            service = watch.QuotaService(sessions_path=Path(tmp))
            service.cached_summary = "codex 5h 88%"
            service.cached_raw = [{"provider": "codex", "windows": [{"name": "5 hours", "used": 88}]}]
            with patch("watch.subprocess.run", return_value=Mock(returncode=1, stdout="")):
                summary, raw = service.fetch(force=True)
            self.assertNotIn("88%", summary)
            self.assertIn("無資料", summary)
            self.assertNotIn("88%", watch.render_full_quota_view(raw))


class ProjectGroupingRegressions(unittest.TestCase):
    """Every configured grouping rule gets at least one concrete example."""

    def test_project_a_rules(self):
        cases = (
            ("alpha 退役備份（步驟 1-3a）", "/tmp/alpha-retire/run.log"),
            ("PR #42 第二輪修正", "/tmp/fix2-pr42/run.log"),
            ("PR #41-43 複查", "/tmp/review-pr41-43/run.log"),
            ("pa 合併後狀態同步", "/tmp/pa-sync/run.log"),
            ("project-a 狀態整理", "/tmp/work/run.log"),
            ("example-runtime 清理", "/tmp/project-a/example-runtime/run.log"),
            ("alpha-migration 檢查", "/tmp/work/run.log"),
            ("雜項任務", "/tmp/project-a/run.log"),
            ("雜項任務", "/tmp/pr-43-fix/run.log"),
        )
        for name, log_path in cases:
            with self.subTest(name=name, log_path=log_path):
                self.assertEqual(
                    watch.infer_project(name, Path(log_path)), "project-a"
                )

    def test_example_project_rule(self):
        self.assertEqual(
            watch.infer_project("example-project v8.1 修正", Path("/tmp/run.log")),
            "example-project",
        )

    def test_project_b_rules(self):
        cases = (
            ("sc-pkg6 通知觸發", "/tmp/work/run.log"),
            ("shortcut 編譯修復", "/tmp/work/run.log"),
            ("shortcut 匯入檢查", "/tmp/work/run.log"),
            ("project-b 收件流程", "/tmp/work/run.log"),
            ("雜項任務", "/tmp/project-b/run.log"),
        )
        for name, log_path in cases:
            with self.subTest(name=name, log_path=log_path):
                self.assertEqual(
                    watch.infer_project(name, Path(log_path)), "project-b"
                )

    def test_project_c_rules(self):
        cases = (
            ("kanban 卡片回報規則", "/tmp/work/run.log"),
            ("雜項任務", "/tmp/project-c/run.log"),
        )
        for name, log_path in cases:
            with self.subTest(name=name, log_path=log_path):
                self.assertEqual(
                    watch.infer_project(name, Path(log_path)), "project-c"
                )

    def test_project_d_rule(self):
        cases = (
            ("quota 新增工作組", "/tmp/work/run.log"),
            ("雜項任務", "/tmp/project-d/run.log"),
        )
        for name, log_path in cases:
            with self.subTest(name=name, log_path=log_path):
                self.assertEqual(
                    watch.infer_project(name, Path(log_path)), "project-d"
                )

    def test_unmatched_task_stays_other(self):
        self.assertEqual(
            watch.infer_project("完全無關的任務", Path("/tmp/elsewhere/run.log")),
            "other",
        )


if __name__ == "__main__":
    unittest.main()
