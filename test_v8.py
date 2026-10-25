"""v8 regressions at TSV, quota and rendered-output boundaries."""
import asyncio
import json
import os
import subprocess
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from rich.text import Text

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
        with patch("watch.subprocess.run", side_effect=OSError("inspection unavailable")):
            self.assertIsNone(watch.is_pid_alive(1234))

    def test_parent_pid_is_alive(self):
        parent = os.getppid()
        with patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout=f"{parent} parent\n")):
            self.assertTrue(watch.is_pid_alive(parent))
        with patch("watch.subprocess.run", return_value=Mock(returncode=1, stdout="")) as run:
            self.assertFalse(watch.is_pid_alive(1234))
            self.assertEqual(run.call_args.args[0], ["pgrep", "-l", "."])
        with patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout="1234 worker\n")):
            self.assertTrue(watch.is_pid_alive(1234))
        self.assertIsNone(watch.is_pid_alive(0))

    def test_real_ps_lstart_matches_this_process(self):
        started = watch.read_process_start(os.getpid())
        self.assertIsNotNone(started)
        self.assertEqual(started, int(started))
        self.assertLessEqual(started, time.time() + 1)
        self.assertGreater(started, time.time() - 3600)
        self.assertEqual(started, watch.read_process_start(os.getpid()))


class V10DataRegressions(unittest.TestCase):
    fixtures = Path(__file__).parent / "tests" / "fixtures"
    now = watch.parse_timestamp("2026-10-25T10:12:00Z")
    start = watch.parse_timestamp("2026-10-25T10:00:00Z")

    def load_fixture(self, root, parser=None):
        for name in ("claude_stream.jsonl", "opencode_stream.jsonl", "runs_v10.tsv"):
            (root / name).write_bytes((self.fixtures / name).read_bytes())
        runs, done = root / "runs_v10.tsv", root / "agent-runs-done.tsv"
        done.write_text("")
        before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in (runs, done)]
        with patch("watch.ROOT_DIR", root):
            agents, errors = watch.load_all_agents(runs, done, parser, self.now)
        self.assertEqual(errors, [])
        self.assertEqual(before, [(p.stat().st_mtime_ns, p.read_bytes()) for p in (runs, done)])
        return {a.name: a for a in agents}

    def test_fixture_open_code_and_claude_have_elapsed_and_plain_latest(self):
        with tempfile.TemporaryDirectory() as tmp:
            agents = self.load_fixture(Path(tmp))
        for name in ("OpenCode", "Claude"):
            agent = agents[name + " fixture"]
            self.assertEqual(agent.elapsed, "00:12:00")
            self.assertIn(name + " fixture 最新一句", agent.latest)
            self.assertNotIn("'type'", agent.latest)
        self.assertEqual(agents["Claude fixture"].model, "claude-sonnet-fixture")
        self.assertEqual(agents["Claude fixture"].harness, "Claude Code")
        self.assertEqual(agents["OpenCode fixture"].project, "agent-watch")
        self.assertEqual(agents["Claude fixture"].project, "personal-ai-os")

    def test_strict_status_does_not_accept_completion_in_prose(self):
        for body in ("工作已完成", "the earlier run was DONE", "完成測試，仍需實作", "example: Status: DONE"):
            with self.subTest(body=body):
                self.assertIsNone(watch.last_status_from_content("# Notes\n" + body))

    def test_keyword_boundaries_reject_embedded_long_keywords(self):
        self.assertFalse(watch.keyword_matches("shortcut", "myshortcutting"))
        self.assertFalse(watch.keyword_matches("sc-", "misc-task"))
        self.assertFalse(watch.keyword_matches("k1", "desk1-task"))
        self.assertTrue(watch.keyword_matches("sc-", "sc-pkg6"))

    def test_no_timestamp_line_does_not_invent_mtime_clock(self):
        self.assertEqual(watch.LogParser._summarize(b"plain progress", self.start)[2], "plain progress")

    def test_elapsed_sources_precedence_and_reused_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log, report = root / "run.log", root / "REPORT.md"
            log.write_text("2026-10-20T10:00:00Z session start\nold log\n")
            report.write_text("DONE\nold result\n")
            os.utime(report, (self.start - 60, self.start - 60))
            os.utime(log, (self.start + 5, self.start + 5))
            reader = Mock(return_value=self.start + 60)
            parser = watch.LogParser(process_start_reader=reader)
            with patch("watch.is_pid_alive", return_value=True):
                exact = watch.build_agent("Task", str(log), str(report), False, parser, self.now,
                                          pid=1234, started_at=self.start)
                reader.assert_not_called()
                pid = watch.build_agent("Task", str(log), str(report), False, parser, self.now, pid=1234)
                unknown = watch.build_agent("Task", str(log), str(report), False,
                                             watch.LogParser(process_start_reader=None), self.now, pid=1234)
            self.assertEqual(exact.elapsed, "00:12:00")
            self.assertEqual(pid.elapsed, "00:11:00")
            self.assertEqual(pid.start_source, "pid")
            self.assertEqual(unknown.elapsed, "時間未知")
            self.assertEqual(exact.status, "running")

    def test_process_lstart_parsing_and_ps_invocation(self):
        expected = time.mktime(time.strptime("2026-10-25 10:00:00", "%Y-%m-%d %H:%M:%S"))
        with patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout="Sun Oct 25 10:00:00 2026\n")) as run:
            self.assertEqual(watch.read_process_start(4321), expected)
        self.assertEqual(run.call_args.args[0], ["ps", "-o", "lstart=", "-p", "4321"])
        self.assertEqual(run.call_args.kwargs["env"]["LC_ALL"], "C")
        self.assertEqual(
            watch.parse_process_lstart("Sun Oct  5 09:08:07 2026"),
            time.mktime(time.strptime("2026-10-05 09:08:07", "%Y-%m-%d %H:%M:%S")),
        )
        self.assertIsNone(watch.parse_process_lstart(""))
        self.assertIsNone(watch.parse_process_lstart("not a start time"))

    def test_process_start_reader_failure_modes_return_none(self):
        for result in (
            Mock(returncode=1, stdout=""),
            Mock(returncode=0, stdout=""),
            Mock(returncode=0, stdout="garbage output"),
        ):
            with self.subTest(stdout=result.stdout, returncode=result.returncode), \
                    patch("watch.subprocess.run", return_value=result):
                self.assertIsNone(watch.read_process_start(4321))
        with patch("watch.subprocess.run", side_effect=OSError("ps unavailable")):
            self.assertIsNone(watch.read_process_start(4321))
        with patch("watch.subprocess.run", side_effect=subprocess.TimeoutExpired("ps", 3)):
            self.assertIsNone(watch.read_process_start(4321))
        with patch("watch.subprocess.run") as run:
            self.assertIsNone(watch.read_process_start(0))
            run.assert_not_called()

    def test_default_priority_is_tsv_then_live_pid_then_log(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log, report = root / "run.log", root / "REPORT.md"
            log.write_text("2026-10-25T10:00:00Z session start\nworking\n")
            report.write_text("DONE\n")
            ps_reader = Mock(return_value=self.start + 120)
            # Default wiring, no injection: the parser itself must reach `ps`.
            with patch("watch.read_process_start", ps_reader), \
                    patch("watch.is_pid_alive", return_value=True):
                parser = watch.LogParser()
                via_tsv = watch.build_agent("Task", str(log), str(report), False, parser,
                                            self.now, pid=1234, started_at=self.start)
                ps_reader.assert_not_called()
                via_pid = watch.build_agent("Task", str(log), str(report), False, parser,
                                            self.now, pid=1234)
            self.assertEqual(via_tsv.start_source, "tsv")
            self.assertEqual(via_tsv.elapsed, "00:12:00")
            self.assertEqual(via_pid.start_source, "pid")
            self.assertFalse(via_pid.elapsed_estimated)
            self.assertEqual(via_pid.elapsed, "00:10:00")

    def test_pid_start_source_is_skipped_unless_pid_is_alive(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log, report = root / "run.log", root / "REPORT.md"
            log.write_text("2026-10-25T10:00:00Z session start\nworking\n")
            report.write_text("DONE\n")
            reader = Mock(return_value=self.start + 120)
            parser = watch.LogParser(process_start_reader=reader)
            for alive in (False, None):
                with self.subTest(alive=alive), patch("watch.is_pid_alive", return_value=alive):
                    agent = watch.build_agent("Task", str(log), str(report), False, parser,
                                              self.now, pid=1234)
                self.assertNotEqual(agent.start_source, "pid")
                reader.assert_not_called()

    def test_pid_start_time_is_read_once_and_bad_values_are_dropped(self):
        reader = Mock(return_value=self.start)
        parser = watch.LogParser(process_start_reader=reader)
        self.assertEqual(parser.pid_start_time(1234), self.start)
        self.assertEqual(parser.pid_start_time(1234), self.start)
        reader.assert_called_once_with(1234)
        self.assertIsNone(parser.pid_start_time(None))
        # A failed lookup is retried on the next poll instead of being cached.
        retry = Mock(side_effect=[None, self.start])
        recovering = watch.LogParser(process_start_reader=retry)
        self.assertIsNone(recovering.pid_start_time(4321))
        self.assertEqual(recovering.pid_start_time(4321), self.start)
        future = watch.LogParser(process_start_reader=Mock(return_value=time.time() + 3600))
        self.assertIsNone(future.pid_start_time(4321))
        broken = watch.LogParser(process_start_reader=Mock(side_effect=OSError("denied")))
        self.assertIsNone(broken.pid_start_time(4321))
        disabled = watch.LogParser(process_start_reader=None)
        self.assertIsNone(disabled.pid_start_time(4321))

    def test_live_process_uses_ps_lstart_end_to_end(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log, report = root / "run.log", root / "REPORT.md"
            # A stale log start proves the live PID wins over the estimate.
            log.write_text("2026-10-20T10:00:00Z stale session start\nworking\n")
            runs, done = root / "runs.tsv", root / "done.tsv"
            proc = subprocess.Popen(["sleep", "60"])
            try:
                runs.write_text(f"Live pid\t{log}\t{report}\t{proc.pid}\n")
                done.write_text("")
                expected = watch.read_process_start(proc.pid)
                agents, errors = watch.load_all_agents(runs, done)
            finally:
                proc.terminate()
                proc.wait()
        self.assertEqual(errors, [])
        self.assertEqual(agents[0].start_source, "pid")
        self.assertFalse(agents[0].elapsed_estimated)
        self.assertEqual(agents[0].start_time, expected)
        self.assertNotIn("~", agents[0].elapsed)

    def test_estimate_and_all_sources_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "run.log"
            log.write_text("2026-10-25T10:00:00Z session start\nworking\n")
            agent = watch.build_agent("Task", str(log), str(root / "missing"), False, watch.LogParser(), self.now)
            self.assertEqual(agent.elapsed, "~00:12:00")
            self.assertEqual(watch.compact_elapsed(agent), "~12m")
            unknown = watch.build_agent("Task", "/dev/null", str(root / "missing"), False, watch.LogParser(), self.now)
            self.assertEqual(unknown.elapsed, "時間未知")

    def test_old_report_with_new_log_activity_is_not_complete(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log, report = root / "run.log", root / "REPORT.md"
            log.write_text("2026-10-20T10:00:00Z session start\nnew activity\n")
            report.write_text("DONE\nold result\n")
            os.utime(log, (self.now - 10, self.now - 10))
            os.utime(report, (self.start - 1, self.start - 1))
            with patch("watch.find_new_branch_commit", return_value=(False, None)):
                agent = watch.build_agent("Task", str(log), str(report), False, watch.LogParser(), self.now)
            self.assertEqual(agent.status, "running")
            self.assertEqual(agent.elapsed, "時間未知")

    def test_progress_requires_newer_mtime_in_log_directory(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            progress = root / "PROGRESS.md"
            progress.write_text("P1 previous invocation\n")
            os.utime(progress, (self.start - 1, self.start - 1))
            self.assertEqual(watch.read_progress_stage(root, self.start), "")
            self.assertEqual(watch.read_progress_stage(root, None), "")
            os.utime(progress, (self.start + 1, self.start + 1))
            self.assertEqual(watch.read_progress_stage(root, self.start), "P1 previous invocation")

    def test_old_ahead_commit_needs_to_be_newer_than_current_run(self):
        answers = [
            Mock(returncode=0, stdout="task\n"),
            Mock(returncode=0, stdout="origin/main\n"),
            Mock(returncode=0, stdout=str(self.start - 100)),
        ]
        with patch("watch.find_git_root", return_value=Path("/fixture/repo")), patch("watch.subprocess.run", side_effect=answers):
            self.assertEqual(watch.find_new_branch_commit(Path("/fixture/repo"), self.start), (False, None))

    def test_claude_result_success_error_and_pid_priority(self):
        for failed, expected in ((False, "complete"), (True, "failed")):
            with self.subTest(failed=failed), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                log = root / "run.log"
                log.write_bytes((self.fixtures / "claude_stream.jsonl").read_bytes())
                with log.open("a") as stream:
                    stream.write(json.dumps({
                        "type": "result", "subtype": "error_during_execution" if failed else "success",
                        "is_error": failed, "timestamp": "2026-10-25T10:02:00Z", "result": "fixture outcome",
                    }) + "\n")
                with patch("watch.is_pid_alive", return_value=False):
                    agent = watch.build_agent("Task", str(log), str(root / "missing"), False, watch.LogParser(), self.now,
                                              pid=1234, started_at=self.start)
                self.assertEqual(agent.status, expected)
                self.assertIn("fixture outcome", agent.latest)
                self.assertEqual(agent.elapsed, "00:02:00")
                with patch("watch.is_pid_alive", return_value=True):
                    live = watch.build_agent("Task", str(log), str(root / "missing"), False, watch.LogParser(), self.now,
                                             pid=1234, started_at=self.start)
                self.assertEqual(live.status, "running")

    def test_claude_result_after_report_keeps_report_status(self):
        """M1: the result event is written after REPORT.md and must not override it."""
        for marker, expected in (("FAIL", "failed"), ("PARTIAL", "partial"), ("DONE", "complete")):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                log, report = root / "run.log", root / "REPORT.md"
                log.write_bytes((self.fixtures / "claude_stream.jsonl").read_bytes())
                with log.open("a") as stream:
                    stream.write(json.dumps({
                        "type": "result", "subtype": "success", "is_error": False,
                        "timestamp": "2026-10-25T10:06:00Z", "result": "fixture outcome",
                    }) + "\n")
                report.write_text(f"{marker}\n主要成果\n")
                # Claude saves the report first; the result event lands later.
                os.utime(report, (self.start + 300, self.start + 300))
                os.utime(log, (self.start + 360, self.start + 360))
                with patch("watch.is_pid_alive", return_value=False):
                    agent = watch.build_agent("Task", str(log), str(report), False,
                                              watch.LogParser(), self.now, pid=1234,
                                              started_at=self.start)
                self.assertEqual(agent.status, expected)
                self.assertEqual(agent.latest, "主要成果")

    def test_report_before_current_init_is_not_used(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log, report = root / "run.log", root / "REPORT.md"
            log.write_bytes((self.fixtures / "claude_stream.jsonl").read_bytes())
            with log.open("a") as stream:
                stream.write(json.dumps({
                    "type": "result", "subtype": "success", "is_error": False,
                    "timestamp": "2026-10-25T10:06:00Z", "result": "new Claude outcome",
                }) + "\n")
            # Older than the current run's init event, newer than started_at.
            report.write_text("FAIL\n舊報告\n")
            os.utime(report, (self.start - 120, self.start - 120))
            os.utime(log, (self.start + 360, self.start + 360))
            with patch("watch.is_pid_alive", return_value=False), patch(
                "watch.find_new_branch_commit", return_value=(False, None)
            ):
                agent = watch.build_agent("Task", str(log), str(report), False,
                                          watch.LogParser(), self.now, pid=1234,
                                          started_at=self.start - 180)
            self.assertEqual(agent.status, "complete")
            self.assertIn("new Claude outcome", agent.latest)

    def test_report_after_init_without_timestamp_keeps_status(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log, report = root / "run.log", root / "REPORT.md"
            log.write_text('{"type":"system","subtype":"init","model":"claude-fixture"}\n'
                           '{"type":"result","subtype":"success","result":"fixture outcome"}\n')
            report.write_text("FAIL\n建置失敗\n")
            os.utime(report, (self.start + 300, self.start + 300))
            os.utime(log, (self.start + 360, self.start + 360))
            with patch("watch.is_pid_alive", return_value=False):
                agent = watch.build_agent("Task", str(log), str(report), False,
                                          watch.LogParser(), self.now, pid=1234,
                                          started_at=self.start)
            self.assertEqual(agent.status, "failed")

    def test_result_event_still_used_without_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "run.log"
            log.write_bytes((self.fixtures / "claude_stream.jsonl").read_bytes())
            with log.open("a") as stream:
                stream.write(json.dumps({
                    "type": "result", "subtype": "success", "is_error": False,
                    "timestamp": "2026-10-25T10:06:00Z", "result": "fixture outcome",
                }) + "\n")
            os.utime(log, (self.start + 360, self.start + 360))
            with patch("watch.is_pid_alive", return_value=False):
                agent = watch.build_agent("Task", str(log), str(root / "missing"), False,
                                          watch.LogParser(), self.now, pid=1234,
                                          started_at=self.start)
            self.assertEqual(agent.status, "complete")
            self.assertIn("fixture outcome", agent.latest)

    def test_done_file_report_status_beats_result_event(self):
        for marker, expected in (("FAIL", "failed"), ("PARTIAL", "partial"), ("DONE", "complete")):
            with self.subTest(marker=marker), tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                log, report = root / "run.log", root / "REPORT.md"
                log.write_bytes((self.fixtures / "claude_stream.jsonl").read_bytes())
                with log.open("a") as stream:
                    stream.write(json.dumps({
                        "type": "result", "subtype": "success", "is_error": False,
                        "timestamp": "2026-10-25T10:06:00Z", "result": "fixture outcome",
                    }) + "\n")
                report.write_text(f"{marker}\n主要成果\n")
                os.utime(report, (self.start + 300, self.start + 300))
                os.utime(log, (self.start + 360, self.start + 360))
                runs, done = root / "runs.tsv", root / "done.tsv"
                runs.write_text("")
                done.write_text("\t".join([
                    "Task", str(log), str(report), "", "", "", "", "2026-10-25T10:00:00Z",
                ]) + "\n")
                agents, errors = watch.load_all_agents(runs, done, watch.LogParser(), self.now)
                self.assertEqual(errors, [])
                self.assertEqual(len(agents), 1)
                self.assertEqual(agents[0].is_done_file, True)
                self.assertEqual(agents[0].status, expected)
                self.assertEqual(agents[0].latest, "主要成果")

    def test_claude_tool_and_partial_records_and_reused_init(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.log"
            parser = watch.LogParser()
            log.write_text('{"type":"system","subtype":"init","timestamp":"2026-10-20T10:00:00Z"}\n')
            parser.parse(log)
            tool = '{"type":"assistant","message":{"content":[{"type":"tool_use","name":"Read","input":{"file_path":"a.py"}}]}}'
            with log.open("a") as stream:
                stream.write(tool[:35])
            self.assertNotIn("工具：Read", "\n".join(parser.parse(log)[2]))
            with log.open("a") as stream:
                stream.write(tool[35:] + "\n")
            self.assertIn("工具：Read", parser.parse(log)[2][-1])
            with log.open("a") as stream:
                stream.write('{"type":"system","subtype":"init","timestamp":"2026-10-25T10:00:00Z"}\n')
            self.assertEqual(parser.parse(log)[0], self.start)

    def test_claude_result_without_timestamp_and_new_activity_clear_completion(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "run.log"
            log.write_text('{"type":"system","subtype":"init","model":"claude-fixture"}\n'
                           '{"type":"result","subtype":"success","result":"fixture result"}\n')
            os.utime(log, (self.start + 120, self.start + 120))
            parser = watch.LogParser()
            with patch("watch.is_pid_alive", return_value=False):
                result = watch.build_agent("Task", str(log), str(root / "missing"), False, parser, self.now,
                                           pid=1234, started_at=self.start)
                self.assertEqual(result.status, "complete")
                self.assertEqual(result.elapsed, "00:02:00")
                self.assertEqual(result.log_lines[-1], "完成：fixture result")
                with log.open("a") as stream:
                    stream.write('{"type":"assistant","message":{"content":[{"type":"text","text":"new run"}]}}\n')
                os.utime(log, (self.now - 1, self.now - 1))
                rerun = watch.build_agent("Task", str(log), str(root / "missing"), False, parser, self.now,
                                          pid=1234, started_at=self.start + 180)
            self.assertEqual(rerun.status, "no_report")

    def test_claude_new_result_summary_does_not_reuse_old_report(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log, report = root / "run.log", root / "REPORT.md"
            report.write_text("DONE\nold report from previous invocation\n")
            os.utime(report, (self.start - 1, self.start - 1))
            log.write_text('{"type":"result","subtype":"success","timestamp":"2026-10-25T10:02:00Z","result":"new Claude outcome"}\n')
            os.utime(log, (self.start + 120, self.start + 120))
            with patch("watch.is_pid_alive", return_value=False), patch("watch.find_new_branch_commit", return_value=(False, None)):
                agent = watch.build_agent("Task", str(log), str(report), False, watch.LogParser(), self.now,
                                          pid=1234, started_at=self.start)
            self.assertEqual(agent.status, "complete")
            self.assertIn("new Claude outcome", agent.latest)
            self.assertNotIn("old report", agent.latest)

    def test_project_alias_override_unknown_and_legacy_widths(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            rows = [
                "personal-ai-os check\t/dev/null\tmissing\t\t\t\tK1",
                "explicit\t/dev/null\tmissing\t\t\t\tpai",
                "unknown\t/dev/null\tmissing\t\t\t\tno-such-project",
                "invalid-time\t/dev/null\tmissing\t\t\t\taw\t2026-10-25T10:00:00",
            ] + ["legacy-%d\t/dev/null\tmissing%s" % (width, "\t" * (width - 3)) for width in range(3, 7)]
            runs = root / "runs.tsv"
            runs.write_text("\n".join(rows) + "\n")
            with patch("watch.ROOT_DIR", root):
                agents, errors = watch.load_tsv_agents(runs, False, watch.LogParser(), self.now)
            self.assertEqual(len(agents), 8)
            self.assertEqual([a.project for a in agents[:3]], ["K1", "personal-ai-os", "other"])
            self.assertIn("未知專案", agents[2].error)
            self.assertEqual(len(errors), 1)
            self.assertIn("started_at", errors[0])

    def test_architecture_fallback_and_adjacent_layers_validation(self):
        saved = dict(watch.ARCHITECTURE_DATA)
        warnings = list(watch.ARCHITECTURE_ERRORS)
        try:
            for body in ("{invalid", "[]", '{"projects": {}, "architectures": {}}'):
                with self.subTest(body=body), tempfile.TemporaryDirectory() as tmp:
                    path = Path(tmp) / "broken.json"
                    path.write_text(body)
                    watch.ARCHITECTURE_DATA.clear()
                    with patch("watch.ARCHITECTURE_PATH", path):
                        watch.ensure_architecture_data()
                        watch.ensure_architecture_data()
                        self.assertIn("Operator", watch.render_architecture_view([], 80))
                    self.assertEqual(len(watch.ARCHITECTURE_ERRORS), 1)
            graph = json.loads((Path(__file__).parent / "arch.json").read_text())
            graph["architectures"]["future"]["edges"].append(["user", "reviewer"])
            with tempfile.TemporaryDirectory() as tmp:
                path = Path(tmp) / "broken.json"
                path.write_text(json.dumps(graph))
                with self.assertRaises(ValueError):
                    watch.load_architecture_data(path)
        finally:
            watch.ARCHITECTURE_DATA.clear()
            watch.ARCHITECTURE_DATA.update(saved)
            watch.ARCHITECTURE_ERRORS[:] = warnings


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
        with tempfile.TemporaryDirectory() as tmp:
            app = watch.AgentWatchApp(state_path=Path(tmp) / "state.json")
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
        self.assertIn("Claude 5h 50%", summary)
        self.assertIn("7d 46%", summary)
        self.assertIn("Codex 5h 85%", summary)
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
        self.assertIn("資料時間：2026-10-25 15:00:54", details)
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
                self.assertNotIn("▓", text)
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

    @staticmethod
    def plain(markup):
        return "\n".join(Text.from_markup(line).plain for line in markup.splitlines())

    def fixture_raw(self):
        payload = self.FIXTURE.read_text(encoding="utf-8")
        service = watch.QuotaService(sessions_path=self.sessions)
        with patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout=payload)):
            _, raw = service.fetch(force=True)
        return raw

    def test_fixture_verifies_burn_ahead_critical_colours_and_order(self):
        now = watch.quota_epoch("2026-10-25T14:30:00+08:00")
        self.assertIsNotNone(now)
        markup = watch.render_full_quota_view(self.fixture_raw(), 80, now=now)
        lines = self.plain(markup).splitlines()
        positions = [
            lines.index(name)
            for name in ("Codex (plus)", "Claude (pro)", "OpenCode Go", "OpenRouter")
        ]
        self.assertEqual(positions, sorted(positions))
        codex_row = markup.splitlines()[positions[0] + 1]
        claude_row = markup.splitlines()[positions[1] + 1]
        # Fixture at 14:30: Codex 5h is 85% (critical), Claude 5h is
        # 50% used vs 25% elapsed (25 points ahead of the clock).
        self.assertIn("85% 告急", Text.from_markup(codex_row).plain)
        self.assertIn("#f7768e", codex_row)
        self.assertIn("50% 燒太快", Text.from_markup(claude_row).plain)
        self.assertIn("#e0af68", claude_row)
        self.assertIn("│", Text.from_markup(codex_row).plain)
        self.assertTrue(all(watch.display_width(line) <= 80 for line in lines))

    def test_pacing_thresholds_are_constants_and_critical_wins(self):
        self.assertEqual(
            (watch.QUOTA_BURN_AHEAD_PERCENT, watch.QUOTA_CRITICAL_PERCENT), (15, 85)
        )
        self.assertEqual(watch.quota_window_status(45, 30), ("#e0af68", "燒太快"))
        self.assertEqual(watch.quota_window_status(44.9, 30), ("#9ece6a", ""))
        self.assertEqual(watch.quota_window_status(85, 0), ("#f7768e", "告急"))
        self.assertEqual(watch.quota_window_status(84, 0), ("#e0af68", "燒太快"))
        self.assertEqual(watch.quota_window_status(85, 90), ("#f7768e", "告急"))

    def test_quota_chart_marks_time_progress_inside_the_bar(self):
        now = watch.quota_epoch("2026-10-25T14:30:00+08:00")
        self.assertAlmostEqual(
            watch.quota_time_progress(
                {"name": "5 hours", "resetsAt": "2026-10-25T18:15:00+08:00"}, now
            ),
            25.0,
        )
        self.assertAlmostEqual(
            watch.quota_time_progress(
                {"name": "7 days", "resetsAt": "2026-11-01T14:11:51+08:00"}, now
            ),
            0.18,
            places=2,
        )
        self.assertIsNone(
            watch.quota_time_progress(
                {"name": "mystery", "resetsAt": "2026-10-25T18:15:00+08:00"}, now
            )
        )
        chart = Text.from_markup(watch.render_quota_chart(50, 25, 12)).plain
        self.assertEqual(chart, "▓▓▓│▓▓░░░░░░")
        self.assertEqual(chart.count("│"), 1)

    def test_unknown_window_length_never_judges_burn_ahead(self):
        now = watch.quota_epoch("2026-10-25T14:30:00+08:00")
        item = {
            "provider": "codex",
            "asOf": "2026-10-25T14:29:00+08:00",
            "windows": [{
                "name": "mystery", "used": 60,
                "resetsAt": "2026-10-26T14:30:00+08:00",
            }],
        }
        markup = watch.render_full_quota_view([item], 80, now=now)
        plain = self.plain(markup)
        self.assertIn("時間進度未知", plain)
        self.assertNotIn("燒太快", plain)
        self.assertIn("60%", plain)
        self.assertNotIn("燒太快", watch.render_home_quota([item], 80, now=now))

    def test_missing_readat_reports_when_this_read_happened(self):
        fetched = watch.quota_epoch("2026-10-25T15:00:00+08:00")
        item = {
            "provider": "opencode-go",
            "name": "OpenCode Go",
            "fetchedAt": fetched,
            "windows": [{
                "name": "5 hours", "used": 4,
                "resetsAt": "2026-10-25T16:14:48+08:00",
            }],
        }
        plain = self.plain(watch.render_full_quota_view(
            [item], 80, now=watch.quota_epoch("2026-10-25T14:30:00+08:00"),
        ))
        self.assertIn("資料時間：未提供（本次讀取 15:00:00）", plain)
        self.assertNotIn("magpie 未提供", plain)

    def test_home_compact_bars_share_pacing_colours_and_fit_80(self):
        now = watch.quota_epoch("2026-10-25T14:30:00+08:00")
        raw = self.fixture_raw()
        markup = watch.render_home_quota(raw, 78, now=now)
        lines = [Text.from_markup(line).plain for line in markup.splitlines()]
        self.assertEqual(len(lines), 2)
        self.assertTrue(all(watch.display_width(line) <= 78 for line in lines))
        joined = " ".join(lines)
        positions = [joined.index(name) for name in ("Codex", "Claude", "OpenCode Go", "OpenRouter")]
        self.assertEqual(positions, sorted(positions))
        self.assertIn("85% 告急", joined)
        self.assertIn("50% 燒太快", joined)
        self.assertIn("#f7768e", markup)
        self.assertIn("#e0af68", markup)
        self.assertNotIn("…", markup)
        wide = watch.render_home_quota(raw, 118, now=now)
        self.assertEqual(len(wide.splitlines()), 1)
        self.assertIn("OpenRouter $19.58", Text.from_markup(wide).plain)


class QuotaSnapshotTests(unittest.IsolatedAsyncioTestCase):
    FIXTURE = Path(__file__).parent / "tests" / "fixtures" / "magpie_quota.json"
    NOW = watch.quota_epoch("2026-10-25T14:30:00+08:00")

    @staticmethod
    def screen_text(app):
        return "\n".join(strip.text for strip in app.screen._compositor.render_strips())

    async def test_s4_quota_snapshots_keep_four_providers_at_80_and_120(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs, done = root / "agent-runs.tsv", root / "agent-runs-done.tsv"
            runs.write_text("")
            done.write_text("")
            state = root / "state.json"
            state.write_text('{"version": 1, "tasks": {}, "projects": {}}')
            payload = self.FIXTURE.read_text(encoding="utf-8")
            service = watch.QuotaService(sessions_path=root / "sessions")
            with patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout=payload)):
                summary, raw = service.fetch(force=True)
            before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in (runs, done)]
            shots = Path(__file__).parent / "shots" if os.environ.get(
                "AGENT_WATCH_UPDATE_S4_SHOTS") else root / "shots"
            shots.mkdir(parents=True, exist_ok=True)
            for width in (80, 120):
                app = watch.AgentWatchApp(runs_path=runs, done_path=done, state_path=state)
                app.quota_svc.cached_summary = summary
                app.quota_svc.cached_raw = raw
                app.quota_svc.refresh_in_background = lambda force=False: False
                app.data_svc.refresh_in_background = lambda: False
                async with app.run_test(size=(width, 40)) as pilot:
                    await pilot.press("3")
                    await pilot.pause()
                    app.update_data(now=self.NOW)
                    await pilot.pause()
                    text = self.screen_text(app)
                    lines = text.splitlines()
                    self.assertEqual(len(lines), 40)
                    self.assertTrue(all(watch.display_width(line) <= width for line in lines))
                    self.assertFalse(app.ui_error)
                    for name in ("Codex", "Claude", "OpenCode Go", "OpenRouter"):
                        self.assertIn(name, text)
                    self.assertIn("85% 告急", text)
                    self.assertIn("50% 燒太快", text)
                    self.assertIn("資料時間：", text)
                    self.assertNotIn("…", text)
                    (shots / ("v10-s4-quota-%dx40.txt" % width)).write_text(
                        "\n".join(line.rstrip() for line in lines) + "\n",
                        encoding="utf-8",
                    )
            self.assertEqual(
                before,
                [(p.stat().st_mtime_ns, p.read_bytes()) for p in (runs, done)],
            )
            print("v10-s4 quota snapshots 80x40/120x40; TSV bytes and mtime_ns UNCHANGED")


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
