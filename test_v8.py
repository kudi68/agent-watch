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
            self.assertEqual(a.latest, "主要成果")

    def test_summary_skips_pure_status_lines(self):
        for marker in ("DONE", "STATUS: DONE", "判定：**DONE**", "# DONE", "PARTIAL"):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                a = self.load(Path(tmp), marker + "\n已完成備份打包\n")[0]
                self.assertEqual(a.latest, "已完成備份打包")
        with tempfile.TemporaryDirectory() as tmp:
            self.assertEqual(self.load(Path(tmp), "DONE\n")[0].latest, "完成")

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
        with patch("watch.os.kill", side_effect=OSError("inspection unavailable")):
            self.assertIsNone(watch.is_pid_alive(1234))

    def test_parent_pid_is_alive(self):
        self.assertTrue(watch.is_pid_alive(os.getppid()))
        with patch("watch.os.kill", side_effect=ProcessLookupError):
            self.assertFalse(watch.is_pid_alive(1234))
        with patch("watch.os.kill", side_effect=PermissionError):
            self.assertTrue(watch.is_pid_alive(1234))
        self.assertIsNone(watch.is_pid_alive(0))


def example_agent(name, status, now, project="example-project", **kwargs):
    fields = dict(
        name=name, status=status, log_path=Path("/tmp/example/run.log"),
        last_path=Path("/tmp/example/LAST.md"), model="model-x", harness="Agent A",
        start_time=now - 120, elapsed="00:02:00", latest="成果摘要",
        completion_time=None, project=project, key=name,
    )
    fields.update(kwargs)
    return watch.AgentInfo(**fields)


class BoardRegressions(unittest.TestCase):
    def test_t4_projects_prioritize_attention_running_recent_then_history(self):
        now = 1_800_000_000
        agents = [
            example_agent("old-result", "complete", now, "project-a", completion_time=now - 8000),
            example_agent("active-build", "running", now, "example-project"),
            example_agent("recent-result", "reported", now, "project-c", completion_time=now - 10),
            example_agent("needs-input", "waiting", now, "project-b"),
        ]
        text = watch.render_work_board(agents, now=now)
        self.assertLess(text.index("Project B"), text.index("example-project"))
        self.assertLess(text.index("example-project"), text.index("Project C"))
        self.assertNotIn("old-result", text)
        self.assertIn("歷史 1 筆（project-a 1）", text)

    def test_buckets_cover_statuses_and_expire_old_failures(self):
        now = 1_800_000_000
        for status in ("waiting", "blocked", "failed", "no_report", "unconfirmed", "error"):
            with self.subTest(status=status):
                a = example_agent("issue", status, now)
                self.assertEqual(watch.task_bucket(a, now), "attention")
                a.completion_time = now - 7201
                self.assertEqual(watch.task_bucket(a, now), "history")
        for status in ("complete", "reported", "partial"):
            a = example_agent("result", status, now, completion_time=now - 7200)
            self.assertEqual(watch.task_bucket(a, now), "recent")
            self.assertEqual(watch.task_bucket(a, now + 1), "history")
        a = example_agent("active", "running", now, completion_time=now - 8000)
        self.assertEqual(watch.task_bucket(a, now), "running")

    def test_t5_recent_limit_and_timing_stay_with_their_cards(self):
        now = 1_800_000_000
        agents = [
            example_agent("result-%d" % i, "partial" if i == 0 else "complete", now,
                          completion_time=now - i)
            for i in range(6)
        ]
        agents += [
            example_agent("older-active", "running", now, start_time=now - 180, elapsed="00:03:00"),
            example_agent("newer-active", "running", now),
        ]
        text = watch.render_work_board(agents, now=now)
        self.assertIn("另 3 筆", text)
        self.assertIn("⚠", text)
        self.assertNotIn("result-3", text)
        self.assertNotIn("Harness", text)
        self.assertNotIn("▼", text)
        self.assertLess(text.index("newer-active"), text.index("older-active"))
        self.assertLess(text.index("older-active"), text.index("result-0"))
        self.assertIn("Agent A · model-x · 已跑 3m", text)
        self.assertEqual(sum("result-" in line for line in text.splitlines()), 3)
        expanded = watch.render_work_board(agents, now=now, expand_recent=True)
        self.assertEqual(sum("result-" in line for line in expanded.splitlines()), 6)

    def test_t6_counts_partition_64_tasks(self):
        now = 1_800_000_000
        agents = [example_agent("active-%d" % i, "running", now) for i in range(3)]
        agents += [example_agent("recent-%d" % i, ("complete", "reported", "partial")[i % 3],
                                 now, completion_time=now - i) for i in range(26)]
        agents += [example_agent("old-%d" % i, ("complete", "failed", "blocked")[i % 3],
                                 now, completion_time=now - 8000 - i) for i in range(35)]
        counts = watch.count_buckets(agents, now)
        self.assertEqual(counts, dict(attention=0, running=3, recent=26, history=35, total=64))
        self.assertEqual(sum(counts[key] for key in ("attention", "running", "recent", "history")), 64)

    def test_project_ties_use_last_finish_and_long_cards_keep_timer(self):
        now = 1_800_000_000
        agents = [
            example_agent("active-a", "running", now, "project-a", log_mtime=now),
            example_agent("active-b", "running", now, "project-b", log_mtime=now - 100),
            example_agent("result-a", "complete", now, "project-a", completion_time=now - 500),
            example_agent("result-b", "complete", now, "project-b", completion_time=now - 50),
        ]
        text = watch.render_work_board(agents, now=now)
        self.assertLess(text.index("Project B"), text.index("project-a"))
        long = example_agent("Very long task name " * 5, "running", now)
        from rich.text import Text
        lines = Text.from_markup(watch.render_board_active([long], 80)).plain.splitlines()
        self.assertIn("Agent A · model-x · 已跑 2m", lines[0])
        self.assertTrue(all(watch.display_width(line) <= 76 for line in lines))

    def test_snapshot_changes_at_two_hour_boundary(self):
        now = 1_800_000_000
        agents = [example_agent("edge", "complete", now, completion_time=now - 7200)]
        app = watch.AgentWatchApp()
        with patch("watch.time.time", return_value=now):
            first = app.make_snapshot(agents, "", [])
        with patch("watch.time.time", return_value=now + 1):
            later = app.make_snapshot(agents, "", [])
        self.assertNotEqual(first, later)


class QuotaRegressions(unittest.TestCase):
    FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "magpie_quota.json"

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.sessions = Path(self._tmp.name)

    def test_missing_provider_keeps_its_place_in_home_summary(self):
        service = watch.QuotaService(sessions_path=self.sessions)
        with patch("watch.subprocess.run", return_value=Mock(
            returncode=0, stdout='[{"provider":"openrouter","balance":"$0.00"}]',
        )):
            summary, raw = service.fetch(force=True)
        for name in ("Claude", "Codex", "OpenCode Go", "OpenRouter"):
            self.assertIn(name, summary)
        self.assertIn("Claude：無資料", summary)
        self.assertIn("OpenRouter $0.00", summary)
        self.assertEqual(len(raw), 4)

    def test_magpie_fixture_parses_every_provider_window(self):
        payload = self.FIXTURE.read_text(encoding="utf-8")
        service = watch.QuotaService(sessions_path=self.sessions)
        with patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout=payload)):
            summary, raw = service.fetch(force=True)
        providers = [item["provider"] for item in raw]
        self.assertEqual(providers, ["claude", "codex", "opencode-go", "openrouter"])
        self.assertIn("Claude 5h 19%", summary)
        self.assertIn("7d 46%", summary)
        self.assertIn("Codex 5h 11%", summary)
        self.assertIn("7d 2%", summary)
        self.assertIn("OpenCode Go 5h 4% 7d 14% 月 8%", summary)
        self.assertIn("OpenRouter $19.58", summary)
        self.assertNotIn("無資料", summary)
        self.assertNotIn("重置", summary)
        self.assertNotIn("資料時間", summary)
        self.assertEqual(summary.count("更新"), 1)
        self.assertEqual(raw[-1]["kind"], "balance")
        self.assertEqual(raw[-1]["balance"], "$19.58")
        details = watch.render_full_quota_view(raw)
        self.assertIn("帳戶餘額 $19.58", details)
        self.assertIn("資料時間：magpie 未提供（本次讀取", details)
        self.assertIn("帳戶餘額 $19.58", "\n".join(watch.render_plain_quota_details(raw)))
        wrapped = watch.render_compact_quota(summary, 80)
        for name in ("Claude", "Codex", "OpenCode Go", "OpenRouter"):
            self.assertIn(name, wrapped)
        self.assertNotIn("…", wrapped)
        self.assertLessEqual(len(wrapped.splitlines()), 3)
        self.assertTrue(all(watch.display_width(line) <= 80 for line in wrapped.splitlines()))

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
        self.assertIn("Claude 5h 30%", summary)
        self.assertIn("Codex：無資料", summary)

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
        self.assertIn("OpenCode Go 5h 4%", summary)
        self.assertIn("7d 14%", summary)
        self.assertIn("月 8%", summary)
        self.assertIsNone(raw[0]["asOf"])
        self.assertIsNotNone(watch.quota_epoch(raw[0]["fetchedAt"]))

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
                self.assertIn("Claude：magpie 讀取失敗", summary)
                self.assertIn("OpenRouter：magpie 讀取失敗", summary)
                self.assertEqual(len(raw), 4)
                self.assertTrue(service.stale)

    def test_magpie_timeout_shows_no_data(self):
        service = watch.QuotaService(sessions_path=self.sessions)
        with patch("watch.subprocess.run", side_effect=watch.subprocess.TimeoutExpired("magpie", 10)):
            summary, raw = service.fetch(force=True)
        self.assertIn("magpie 讀取失敗", summary)
        self.assertEqual(len(raw), 4)
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
            self.assertIn("Codex 5h 12% 7d 34%", summary)

            with patch("watch.subprocess.run", return_value=Mock(returncode=1, stdout="")):
                summary, raw = service.fetch(force=True)
            self.assertIn("Codex 5h 12% 7d 34%", summary)
            self.assertIn("Claude：magpie 讀取失敗", summary)
            self.assertTrue(service.stale)
            self.assertIn("12%", watch.render_full_quota_view(raw, stale=True))

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
            self.assertIn("Codex 5h 55%", summary)
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
            self.assertIn("magpie 讀取失敗", summary)
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
