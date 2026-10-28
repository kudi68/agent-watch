#!/usr/bin/env python3
"""Unit tests for agent-watch v7 status logic, tabs, quota, and screen output."""

import asyncio
import contextlib
import html
import io
import inspect
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

from textual.widgets import TabbedContent
import watch

from watch import (
    ARCHITECTURE_DATA,
    RECENT_DONE_SECONDS,
    AgentInfo,
    AgentWatchApp,
    LogParser,
    QuotaService,
    clean_text,
    determine_status,
    display_width,
    extract_model_from_line,
    format_completion_time,
    format_elapsed,
    get_worker_status,
    infer_project,
    is_node_active,
    is_noise,
    is_valid_model,
    load_all_agents,
    load_architecture_data,
    last_status_from_content,
    parse_quota_json,
    parse_timestamp,
    parse_command_identity,
    read_log_metadata,
    resolve_run_identity,
    render_active_flow,
    render_architecture_view,
    render_data_errors,
    render_done_tasks,
    render_full_quota_view,
    render_recent_done_tasks,
    split_recent_done,
    task_bucket,
)

watch.ensure_architecture_data()


def write_log(path: Path, start_time: float, *messages: str) -> None:
    stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(start_time))
    lines = [f"{stamp} session start", *messages]
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    os.utime(path, (start_time, start_time))


def set_mtime(path: Path, timestamp: float) -> None:
    os.utime(path, (timestamp, timestamp))


def snapshot_dir(fallback: Path) -> Path:
    """Write snapshots to a temp dir unless AGENT_WATCH_UPDATE_SHOTS is set."""
    if os.environ.get("AGENT_WATCH_UPDATE_SHOTS"):
        target = Path(__file__).parent / "shots"
    else:
        target = fallback
    target.mkdir(parents=True, exist_ok=True)
    return target


def git_output(repo: Path, *args: str) -> str:
    result = subprocess.run(
        ["git", "-C", str(repo), "-c", "user.name=agent-watch-test",
         "-c", "user.email=agent-watch-test@example.invalid", *args],
        capture_output=True,
        text=True,
        check=True,
    )
    return result.stdout.strip()


class IsolatedState:
    def setUp(self):
        super().setUp()
        directory = tempfile.TemporaryDirectory(prefix="agent-watch-state-test-")
        self.addCleanup(directory.cleanup)
        setting = patch.dict(os.environ, {"AGENT_WATCH_STATE": str(Path(directory.name) / "state.json")})
        setting.start()
        self.addCleanup(setting.stop)


class TestAgentWatch(IsolatedState, unittest.TestCase):

    def test_clean_text(self):
        raw = "\x1b[31mHello\x1b[0m\r\n\tWorld"
        cleaned = clean_text(raw)
        self.assertEqual(cleaned, "Hello\n    World")

    def test_noise_filtering(self):
        noise_lines = [
            "2026-10-24T14:30:48Z ERROR codex_api::endpoint::responses_websocket: failed",
            "hook: pre-tool hook changed the arguments",
            "user config: `disable_response_storage` is ignored",
            "failed to connect to websocket: HTTP error: 426 Upgrade Required",
        ]
        for line in noise_lines:
            self.assertTrue(is_noise(line), f"Should be filtered as noise: {line}")

        normal_line = "Running unit tests for module A"
        self.assertFalse(is_noise(normal_line))

    def test_model_extraction(self):
        # Plain log and prompt text are not trustworthy model sources.
        self.assertIsNone(extract_model_from_line("model: astra"))
        self.assertIsNone(extract_model_from_line("--model astra"))

        # Structured JSON metadata is an allowed source.
        json_init = '{"event":"init","init":{"model":"gemini-3.8-flash"}}'
        self.assertEqual(extract_model_from_line(json_init), "gemini-3.8-flash")
        json_direct = '{"model": "deepseek-coder-v2"}'
        self.assertEqual(extract_model_from_line(json_direct), "deepseek-coder-v2")

    def test_command_identity_reads_executable_and_model_options_only(self):
        cases = (
            (
                "/opt/homebrew/bin/opencode run --model provider-a/model-x "
                "'astra is only the task name'",
                ("OpenCode", "provider-a/model-x"),
            ),
            ("claude --model sonnet 'please do not use --model astra'", ("Claude Code", "sonnet")),
            ("pi -m qwen3-coder 'implement a task'", ("Pi Agent", "qwen3-coder")),
            ("agy -m gemini-2.5-flash 'implement a task'", ("agy", "gemini-2.5-flash")),
            ("codex exec 'the prompt says model: astra'", ("Codex", "未知")),
            ("opencode run 'the prompt says --model astra'", ("OpenCode", "未知")),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                self.assertEqual(parse_command_identity(command), expected)

    def test_log_metadata_reads_only_structured_preamble(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            structured = root / "structured.log"
            structured.write_text(
                "# run metadata\n"
                "# harness: OpenCode\n"
                "# model: provider-a/model-x\n"
                "task prompt: model: astra\n",
                encoding="utf-8",
            )
            self.assertEqual(
                read_log_metadata(structured),
                ("OpenCode", "provider-a/model-x"),
            )

            ordinary = root / "ordinary.log"
            ordinary.write_text("task prompt: model: astra\n", encoding="utf-8")
            self.assertEqual(read_log_metadata(ordinary), ("未知", "未知"))

    def test_live_pid_identity_never_reads_process_arguments(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = Path(tmpdir) / "task.log"
            log.write_text("prompt says model: astra\n", encoding="utf-8")
            result = Mock(
                returncode=0,
                stdout=(
                    "/opt/homebrew/bin/opencode run --model "
                    "provider-a/model-x 'prompt says model: astra'\n"
                ),
            )
            with patch("watch.subprocess.run", return_value=result) as run:
                identity = resolve_run_identity(log, 66713, None, None, True)

        self.assertEqual(identity, ("未知", "未知"))
        run.assert_not_called()

    def test_ended_pid_uses_structured_log_metadata_without_ps(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = Path(tmpdir) / "task.log"
            log.write_text(
                '{"type":"session_meta","metadata":{"harness":"Claude Code","model":"sonnet"}}\n'
                "prompt: model: astra\n",
                encoding="utf-8",
            )
            with patch("watch.subprocess.run") as run:
                identity = resolve_run_identity(log, 66713, None, None, False)

        self.assertEqual(identity, ("Claude Code", "sonnet"))
        run.assert_not_called()

    def test_tsv_accepts_legacy_and_harness_model_columns(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            rows = []
            for name, header in (
                ("Legacy3", "model: astra\n"),
                ("Legacy4", "# harness: Pi Agent\n# model: qwen3-coder\n"),
                ("Harness5", "# harness: OpenCode\n# model: opencode-go/model-x\n"),
                ("Explicit6", "model: astra\n"),
            ):
                log = root / f"{name}.log"
                log.write_text(header, encoding="utf-8")
                last = root / f"{name}-LAST.md"
                last.write_text("DONE\n", encoding="utf-8")
                set_mtime(last, time.time() + 2)
                rows.append((name, log, last))
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text(
                "# name<TAB>log<TAB>last<TAB>pid<TAB>harness<TAB>model\n"
                f"Legacy3\t{rows[0][1]}\t{rows[0][2]}\n"
                f"Legacy4\t{rows[1][1]}\t{rows[1][2]}\t12345\n"
                f"Harness5\t{rows[2][1]}\t{rows[2][2]}\t12346\tOpenCode\n"
                f"Explicit6\t{rows[3][1]}\t{rows[3][2]}\t12347\tClaude Code\tsonnet\n",
                encoding="utf-8",
            )
            done.write_text("", encoding="utf-8")

            with patch("watch.running_pid_set", return_value=set()):
                agents, errors = load_all_agents(runs, done, LogParser(), time.time())

        self.assertEqual(errors, [])
        agents_by_name = {agent.name: agent for agent in agents}
        self.assertEqual(
            (agents_by_name["Legacy3"].harness, agents_by_name["Legacy3"].model),
            ("未知", "未知"),
        )
        self.assertEqual(
            (agents_by_name["Legacy4"].harness, agents_by_name["Legacy4"].model),
            ("Pi Agent", "qwen3-coder"),
        )
        self.assertEqual(
            (agents_by_name["Harness5"].harness, agents_by_name["Harness5"].model),
            ("OpenCode", "opencode-go/model-x"),
        )
        self.assertEqual(
            (agents_by_name["Explicit6"].harness, agents_by_name["Explicit6"].model),
            ("Claude Code", "sonnet"),
        )

    def test_work_and_done_cards_show_harness_model_and_progress(self):
        agent = AgentInfo(
            name="example-project 用量調查",
            log_path=Path("/tmp/run.log"),
            last_path=Path("/tmp/REPORT.md"),
            status="running",
            model="provider-a/model-x",
            start_time=None,
            elapsed="時間未知",
            latest="正在整理報告",
            harness="OpenCode",
            stage="P2 核對模型來源",
        )
        for rendered in (render_active_flow([agent]), render_done_tasks([agent])):
            self.assertIn("OpenCode", rendered)
            self.assertIn("provider-a/model-x", rendered)
            self.assertIn("P2 核對模型來源", rendered)

    def test_is_valid_model(self):
        self.assertTrue(is_valid_model("gemini-3.8-flash"))
        self.assertTrue(is_valid_model("provider-b/model-y"))
        self.assertTrue(is_valid_model("claude-3-7-sonnet-20250219"))
        self.assertFalse(is_valid_model("model"))
        self.assertFalse(is_valid_model("snapshot.model"))
        self.assertFalse(is_valid_model("self.model"))
        self.assertFalse(is_valid_model("inherit"))
        self.assertFalse(is_valid_model(""))

    def test_elapsed_time(self):
        start = 1000.0
        now_1h = 1000.0 + 3661.0
        self.assertEqual(format_elapsed(start, now_1h), "01:01:01")
        self.assertEqual(format_elapsed(None, now_1h), "時間未知")
        self.assertEqual(format_elapsed(0, now_1h), "時間未知")

    def test_parse_timestamp(self):
        ts_str = "2026-10-24T14:30:48Z"
        ts = parse_timestamp(ts_str)
        self.assertIsNotNone(ts)
        self.assertGreater(ts, 0)

        ts_str_offset = "2026-10-24T22:30:48+08:00"
        ts2 = parse_timestamp(ts_str_offset)
        self.assertIsNotNone(ts2)

        self.assertIsNone(parse_timestamp("no timestamp here"))

    def test_quota_json_parsing(self):
        sample_quota = [
            {
                "provider": "codex",
                "asOf": "2026-10-25T06:00:00Z",
                "windows": [
                    {"name": "5 hours", "used": 7.4},
                    {"name": "7 days", "used": 17.2},
                ],
            },
            {
                "provider": "claude",
                "asOf": "2026-10-25T06:00:00Z",
                "windows": [
                    {"name": "7 days", "used": 43.1},
                ],
            },
            {
                "provider": "opencode-go",
                "asOf": "2026-10-25T06:00:00Z",
                "windows": [
                    {"name": "7 days", "used": 12.0},
                ],
            },
        ]
        text = parse_quota_json(sample_quota)
        self.assertIn("Codex 5h 7% 7d 17%", text)
        self.assertIn("Claude 7d 43%", text)
        self.assertIn("OpenCode Go 7d 12%", text)

        # Quota empty or malformed
        self.assertEqual(parse_quota_json([]), "額度：無資料")
        self.assertEqual(parse_quota_json({"not": "a list"}), "額度：無資料")
        self.assertEqual(parse_quota_json(None), "額度：無資料")
        no_time = [{"provider": "codex", "windows": [{"name": "5 hours", "used": 7}]}]
        self.assertNotIn("7%", parse_quota_json(no_time))

        month = [{
            "provider": "opencode-go",
            "asOf": "2026-10-25T06:00:00Z",
            "windows": [{"name": "month", "used": 30}],
        }]
        self.assertIn("OpenCode Go 月 30%", parse_quota_json(month))
        self.assertIn("月 ", render_full_quota_view(month))

    def test_determine_status(self):
        now = 100000.0

        st = determine_status(
            is_done_file=False,
            last_valid=True,
            last_mtime=now - 10,
            start_time=now - 100,
            log_mtime=now - 20,
            is_null_log=False,
            data_error="",
            now=now,
            last_status="complete",
        )
        self.assertEqual(st, "complete")

        st = determine_status(
            is_done_file=False,
            last_valid=False,
            last_mtime=0.0,
            start_time=now - 300,
            log_mtime=now - 60,
            is_null_log=False,
            data_error="",
            now=now,
        )
        self.assertEqual(st, "running")

        st = determine_status(
            is_done_file=False,
            last_valid=False,
            last_mtime=0.0,
            start_time=now - 4000,
            log_mtime=now - 2000,  # > 1800s ago
            is_null_log=False,
            data_error="",
            now=now,
        )
        self.assertEqual(st, "waiting")

        st = determine_status(
            is_done_file=False,
            last_valid=True,
            last_mtime=now - 20,
            start_time=now - 100,
            log_mtime=now - 10,
            is_null_log=False,
            data_error="",
            now=now,
            last_status="failed",
            init_time=now - 15,
        )
        # A new init after this report makes it stale for the current run.
        # Later log writes alone do not: Claude's result event always follows
        # the REPORT it describes (M1).
        self.assertEqual(st, "running")

        st = determine_status(
            is_done_file=False,
            last_valid=False,
            last_mtime=0.0,
            start_time=now - 100,
            log_mtime=now - 10,
            is_null_log=False,
            data_error="檔案不存在",
            now=now,
        )
        self.assertEqual(st, "error")

        # Log failure text is not an input to status classification.
        self.assertEqual(last_status_from_content("DONE\n工作摘要"), "complete")
        self.assertEqual(last_status_from_content("FAILED\n工作摘要"), "failed")
        self.assertEqual(last_status_from_content("BLOCKED\n工作摘要"), "blocked")
        self.assertEqual(last_status_from_content("PARTIAL\n工作摘要"), "partial")
        self.assertIsNone(last_status_from_content("# 工作摘要\n曾經出現 FAILED"))
        self.assertEqual(
            last_status_from_content("# 工作摘要\n狀態：DONE\n- FAILED 是較早的重試"),
            "complete",
        )
        self.assertIsNone(last_status_from_content("# 工作摘要\n目前結果是 DONE。"))
        self.assertIsNone(last_status_from_content("# 工作摘要\n工作已完成。"))
        self.assertIsNone(last_status_from_content("# 工作摘要\n目前結果不是 DONE。"))
        self.assertIsNone(last_status_from_content("# 工作摘要\n工作尚未完成。"))
        self.assertIsNone(last_status_from_content("# 工作摘要\n工作沒有完成。"))

    def test_load_all_agents_integration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)

            log1 = tmppath / "agent1.log"
            log1.write_text(
                "2026-10-24T10:00:00Z Session start\n"
                "model: provider-b/model-y\n"
                "Step 1: processing data\n"
                "model = snapshot.model\n"
                "10:05:00 Step 2: finishing up\n",
                encoding="utf-8",
            )

            log2 = tmppath / "agent2.jsonl"
            log2.write_text(
                '{"event":"init","init":{"model":"gemini-3.8-flash"}}\n'
                '{"event":"step_update","step_update":{"step_type":"tool","state":"DONE","tool_name":"read_file"}}\n',
                encoding="utf-8",
            )

            last1 = tmppath / "agent1-LAST.md"
            last1.write_text("# 成果摘要\n任務圓滿完成", encoding="utf-8")

            last2 = tmppath / "agent2-LAST.md"

            runs_tsv = tmppath / "agent-runs.tsv"
            runs_tsv.write_text(
                f"Agent Two\t{log2}\t{last2}\n",
                encoding="utf-8",
            )

            done_tsv = tmppath / "agent-runs-done.tsv"
            done_tsv.write_text(
                f"Agent One\t{log1}\t{last1}\n",
                encoding="utf-8",
            )

            parser = LogParser()
            now = time.time()
            agents, errors = load_all_agents(runs_tsv, done_tsv, parser, now)

            self.assertEqual(len(errors), 0)
            self.assertEqual(len(agents), 2)

            a1 = next(a for a in agents if a.name == "Agent One")
            self.assertEqual(a1.status, "reported")
            self.assertEqual(a1.harness, "未知")
            self.assertEqual(a1.model, "未知")
            self.assertIn("任務圓滿完成", a1.latest)

            a2 = next(a for a in agents if a.name == "Agent Two")
            self.assertEqual(a2.status, "running")
            self.assertEqual(a2.model, "gemini-3.8-flash")
            self.assertIn("read_file", a2.latest)

    def test_log_failure_then_retry_stays_running(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            now = time.time()
            log = root / "retry.log"
            write_log(log, now - 60, "command failed with exit code 1", "Retrying patch", "Running unit tests")
            last = root / "LAST.md"
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text(f"Retry task\t{log}\t{last}\n", encoding="utf-8")
            done.write_text("", encoding="utf-8")

            agents, errors = load_all_agents(runs, done, LogParser(), now)

            self.assertEqual(errors, [])
            self.assertEqual(agents[0].status, "running")
            self.assertIn("command failed", "\n".join(agents[0].log_lines))
            self.assertIn("Running unit tests", agents[0].latest)

    def test_fresh_last_failed_is_red(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            now = time.time()
            log = root / "failed.log"
            write_log(log, now - 60, "Finished final checks")
            last = root / "LAST.md"
            last.write_text("FAILED\nBuild did not finish\n", encoding="utf-8")
            set_mtime(last, now - 5)
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text(f"Failed task\t{log}\t{last}\n", encoding="utf-8")
            set_mtime(runs, now - 30)
            done.write_text("", encoding="utf-8")

            agents, _ = load_all_agents(runs, done, LogParser(), now)

            self.assertEqual(agents[0].status, "failed")
            self.assertIn("#b8324b", render_active_flow(agents))

    def test_last_partial_is_yellow(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            now = time.time()
            log = root / "partial.log"
            write_log(log, now - 60, "Partial output recorded")
            last = root / "LAST.md"
            last.write_text("PARTIAL\nSome work remains\n", encoding="utf-8")
            set_mtime(last, now - 5)
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text(f"Partial task\t{log}\t{last}\n", encoding="utf-8")
            set_mtime(runs, now - 30)
            done.write_text("", encoding="utf-8")

            agents, _ = load_all_agents(runs, done, LogParser(), now)

            self.assertEqual(agents[0].status, "partial")
            self.assertIn("#e0af68", render_active_flow(agents))

    def test_old_last_does_not_complete_new_run(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            now = time.time()
            start = now - 100
            log = root / "new-run.log"
            write_log(log, start, "New run still working")
            last = root / "LAST.md"
            last.write_text("DONE\nOld result\n", encoding="utf-8")
            set_mtime(last, start - 10)
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text(f"New run\t{log}\t{last}\n", encoding="utf-8")
            done.write_text("", encoding="utf-8")

            agents, _ = load_all_agents(runs, done, LogParser(), now)

            self.assertEqual(agents[0].status, "running")

    def test_old_last_does_not_complete_reused_log_after_row_registration(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            old_start = 1_700_000_000.0
            old_finished = old_start + 100
            registration = old_start + 200
            now = old_start + 300
            log = root / "reused.log"
            write_log(log, old_start, "Previous run")
            with log.open("a", encoding="utf-8") as stream:
                stream.write(
                    f"{time.strftime('%Y-%m-%dT%H:%M:%SZ', time.gmtime(registration))} New run started\n"
                )
                stream.write("Current run is active\n")
            set_mtime(log, registration + 1)
            last = root / "LAST.md"
            last.write_text("DONE\nPrevious run result\n", encoding="utf-8")
            set_mtime(last, old_finished)
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text(f"Reused log\t{log}\t{last}\t{os.getpid()}\n", encoding="utf-8")
            set_mtime(runs, registration)
            done.write_text("", encoding="utf-8")

            with patch("watch.running_pid_set", return_value={os.getpid()}):
                agents, _ = load_all_agents(
                    runs, done, LogParser(process_start_reader=None), now,
                )

            self.assertEqual(agents[0].status, "running")

    def test_dead_pid_without_last_shows_no_report(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            now = time.time()
            log = root / "dead.log"
            write_log(log, now - 60, "Worker stopped")
            last = root / "missing-LAST.md"
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text(f"Dead worker\t{log}\t{last}\t2147483647\n", encoding="utf-8")
            done.write_text("", encoding="utf-8")

            with patch("watch.running_pid_set", return_value=set()):
                agents, _ = load_all_agents(runs, done, LogParser(), now)

            self.assertEqual(agents[0].status, "no_report")
            card = render_active_flow(agents)
            self.assertIn("已結束、無結果報告", card)
            self.assertIn("#ff9e3b", card)

    def test_completion_time_freezes_elapsed(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            start = 1_700_000_000.0
            finished = start + 150
            log = root / "done.log"
            write_log(log, start, "Final result")
            last = root / "LAST.md"
            last.write_text("DONE\nCompleted\n", encoding="utf-8")
            set_mtime(last, finished)

            runs = self._write_tsvs(root, f"Task\t{log}\t{last}\n")
            set_mtime(runs, start - 1)
            first = load_all_agents(
                runs,
                root / "done.tsv",
                LogParser(),
                start + 500,
            )[0][0]
            later = load_all_agents(
                root / "runs.tsv",
                root / "done.tsv",
                LogParser(),
                start + 4_500,
            )[0][0]

            self.assertEqual(first.status, "complete")
            self.assertEqual(first.elapsed, "~00:02:30")
            self.assertEqual(later.elapsed, "~00:02:30")
            self.assertEqual(first.completion_time, finished)
            self.assertRegex(format_completion_time(0), r"^1970/01/01 08:00（\d+ 天前）$")

    def test_archived_completion_time_stays_frozen(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            start = 1_700_000_000.0
            finished = start + 240
            log = root / "archived.log"
            write_log(log, start, "Final result")
            last = root / "LAST.md"
            last.write_text("DONE\nArchived result\n", encoding="utf-8")
            set_mtime(last, finished)
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text("", encoding="utf-8")
            done.write_text(f"Archived\t{log}\t{last}\n", encoding="utf-8")

            first = load_all_agents(runs, done, LogParser(), start + 500)[0][0]
            later = load_all_agents(runs, done, LogParser(), start + 4_500)[0][0]

            self.assertEqual(first.status, "complete")
            self.assertEqual(first.completion_time, finished)
            self.assertEqual(first.elapsed, "~00:04:00")
            self.assertEqual(later.elapsed, first.elapsed)
            self.assertRegex(render_done_tasks([later]), r"完成：(\d{4}/)?\d{2}/\d{2} \d{2}:\d{2}（[^）]+）")

    def test_completion_time_shows_taipei_date_and_relative_age(self):
        now = watch.quota_epoch("2026-10-27T02:29:00+08:00")
        self.assertEqual(watch.format_relative_time(now - 30, now), "剛剛")
        self.assertEqual(watch.format_relative_time(now - 12 * 60, now), "12 分鐘前")
        self.assertEqual(watch.format_relative_time(now - 65 * 60, now), "1 小時 5 分前")
        self.assertEqual(watch.format_relative_time(now - 3 * 86400, now), "3 天前")
        # Today keeps its date: the boss must see how old the run is, not just a clock.
        self.assertEqual(format_completion_time(now - 12 * 60, now), "10/27 02:17（12 分鐘前）")
        self.assertEqual(format_completion_time(None, now), "時間未確認")

    def test_done_row_keeps_the_completion_time_when_columns_are_tight(self):
        now = watch.quota_epoch("2026-10-27T02:29:00+08:00")
        agent = AgentInfo(
            "長名稱任務", Path("/tmp/run.log"), Path("/tmp/REPORT.md"), "complete",
            "model-x", now - 3600, "~01:00:00", "成果" * 80,
            completion_time=now - 12 * 60, stage="階段" * 30,
            harness="codex", project="agent-watch", project_source="標籤",
        )
        markup = render_done_tasks([agent], width=70, now=now)
        self.assertIn("完成：10/27 02:17（12 分鐘前）", markup)
        self.assertIn("…", markup)

    def test_completion_list_sorts_newest_first(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            base = 1_700_100_000.0
            entries = []
            for name, delta in (("Older", 100), ("Newest", 300)):
                log = root / f"{name}.log"
                last = root / f"{name}-LAST.md"
                write_log(log, base, f"{name} result")
                last.write_text("DONE\nComplete\n", encoding="utf-8")
                set_mtime(last, base + delta)
                entries.append(f"{name}\t{log}\t{last}")
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text("\n".join(entries) + "\n", encoding="utf-8")
            set_mtime(runs, base - 1)
            done.write_text("", encoding="utf-8")

            agents, _ = load_all_agents(runs, done, LogParser(), base + 500)

            self.assertEqual([a.name for a in agents], ["Newest", "Older"])
            markup = render_done_tasks(agents)
            self.assertLess(markup.index("Newest"), markup.index("Older"))

    def test_project_inference_uses_declared_table_and_safe_token_match(self):
        cases = (
            ("personal-ai-os 狀態整理", "/tmp/work/run.log", "personal-ai-os"),
            ("pai 合併後狀態同步", "/tmp/pai-sync/run.log", "personal-ai-os"),
            ("ios-shortcuts 編譯修復", "/tmp/work/run.log", "ios-shortcuts"),
            ("sc-pkg6 通知觸發", "/tmp/work/run.log", "ios-shortcuts"),
            ("c1 退役備份", "/tmp/work/run.log", "c1-runtime"),
            ("M5-setup 盤點", "/tmp/work/run.log", "M5-setup"),
            ("pair task", "/tmp/other/run.log", "other"),
            ("完全無關的任務", "/tmp/elsewhere/run.log", "other"),
        )
        for name, log_path, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(infer_project(name, Path(log_path)), expected)

    def test_recent_done_is_top_highlighted_and_grouped(self):
        now = 1_700_200_000.0
        recent_agent = AgentInfo(
            name="agent-watch 剛完成",
            log_path=Path("/tmp/agent-watch/run.log"),
            last_path=Path("/tmp/agent-watch/LAST.md"),
            status="complete",
            model="model-x",
            start_time=now - 300,
            elapsed="00:05:00",
            latest="DONE",
            harness="Codex",
            completion_time=now - 60,
            project="agent-watch",
        )
        older_agent = AgentInfo(
            name="ios-shortcuts 舊完成",
            log_path=Path("/tmp/project-d/run.log"),
            last_path=Path("/tmp/project-d/LAST.md"),
            status="complete",
            model="model-x",
            start_time=now - 20_000,
            elapsed="01:00:00",
            latest="DONE",
            harness="Codex",
            completion_time=now - RECENT_DONE_SECONDS - 1,
            project="ios-shortcuts",
        )
        recent_magpie_agent = AgentInfo(
            name="ios-shortcuts 也剛完成",
            log_path=Path("/tmp/project-d/run.log"),
            last_path=Path("/tmp/project-d/LAST.md"),
            status="complete",
            model="model-x",
            start_time=now - 300,
            elapsed="00:05:00",
            latest="DONE",
            harness="Codex",
            completion_time=now - 120,
            project="ios-shortcuts",
        )
        recent, older = split_recent_done([older_agent, recent_magpie_agent, recent_agent], now)
        self.assertEqual(
            [agent.name for agent in recent],
            ["agent-watch 剛完成", "ios-shortcuts 也剛完成"],
        )
        self.assertEqual([agent.name for agent in older], ["ios-shortcuts 舊完成"])

        markup = render_recent_done_tasks([older_agent, recent_magpie_agent, recent_agent], now=now)
        self.assertIn("剛完成", markup)
        self.assertIn("agent-watch (1)", markup)
        self.assertIn("剛完成 [/]", markup)
        self.assertLess(markup.index("agent-watch 剛完成"), markup.index("ios-shortcuts 也剛完成"))
        self.assertNotIn("ios-shortcuts 舊完成", markup)
        self.assertIn("iOS 捷徑 (1)", render_done_tasks(older))

    def test_future_completion_within_clock_skew_stays_recent(self):
        now = time.time()
        agent = AgentInfo(
            name="ios-shortcuts 時鐘偏差",
            log_path=Path("/tmp/project-d/run.log"),
            last_path=Path("/tmp/project-d/LAST.md"),
            status="complete",
            model="model-x",
            start_time=now - 300,
            elapsed="00:05:00",
            latest="DONE",
            harness="Codex",
            completion_time=now + 60,
            project="ios-shortcuts",
        )
        self.assertEqual(task_bucket(agent, now), "recent")
        self.assertEqual(task_bucket(agent, now + 300), "recent")
        self.assertEqual(
            task_bucket(agent, now + RECENT_DONE_SECONDS + 120), "history"
        )

    def test_active_work_is_grouped_by_project(self):
        agents = [
            AgentInfo(
                name=name,
                log_path=Path(f"/tmp/{project}/run.log"),
                last_path=Path(f"/tmp/{project}/LAST.md"),
                status="running",
                model="model-x",
                start_time=None,
                elapsed="時間未知",
                latest="執行中",
                project=project,
            )
            for name, project in (
                ("agent-watch task", "agent-watch"),
                ("ios-shortcuts task", "ios-shortcuts"),
            )
        ]
        markup = render_active_flow(agents)
        self.assertIn("agent-watch (1)", markup)
        self.assertIn("iOS 捷徑 (1)", markup)
        self.assertLess(markup.index("agent-watch (1)"), markup.index("iOS 捷徑 (1)"))

    def test_live_pid_overrides_failed_report_and_log_text(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            now = time.time()
            log = root / "codex.log"
            write_log(log, now - 60, "command failed; retrying")
            last = root / "LAST.md"
            last.write_text("FAILED\nRetry is still running\n", encoding="utf-8")
            set_mtime(last, now - 5)
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text(f"Codex task\t{log}\t{last}\t12345\n", encoding="utf-8")
            set_mtime(runs, now - 30)
            done.write_text("", encoding="utf-8")

            with patch("watch.running_pid_set", return_value={12345}):
                agents, errors = load_all_agents(
                    runs, done, LogParser(process_start_reader=None), now,
                )

            self.assertEqual(errors, [])
            self.assertEqual(agents[0].status, "running")

    def test_report_body_and_report_fallback_show_completion_and_stage(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            now = time.time()
            log = root / "task.log"
            write_log(log, now - 180, "Implementation finished")
            report = root / "REPORT.md"
            report.write_text("# 工作摘要\n- 工作已完成，結果已保存。\n", encoding="utf-8")
            set_mtime(report, now - 5)
            progress = root / "PROGRESS.md"
            progress.write_text("P1 盤點完成\nP2 回歸驗證中\n", encoding="utf-8")
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text(f"Report fallback\t{log}\t{root / 'LAST.md'}\n", encoding="utf-8")
            set_mtime(runs, now - 30)
            done.write_text("", encoding="utf-8")

            with patch("watch.running_pid_set", return_value=set()):
                agents, errors = load_all_agents(runs, done, LogParser(), now)
            agent = agents[0]
            markup = render_done_tasks(agents)

            self.assertEqual(errors, [])
            self.assertEqual(agent.status, "reported")
            self.assertEqual(agent.last_path, report)
            self.assertEqual(agent.stage, "P2 回歸驗證中")
            self.assertIn("目前階段：P2 回歸驗證中", markup)
            self.assertRegex(markup, r"完成：(\d{4}/)?\d{2}/\d{2} \d{2}:\d{2}（[^）]+）")

    def test_dead_pid_with_new_branch_commit_is_complete(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            git_output(root, "init", "-q")
            git_output(root, "commit", "-q", "--allow-empty", "-m", "base")
            base = git_output(root, "rev-parse", "HEAD")
            git_output(root, "update-ref", "refs/remotes/origin/main", base)
            git_output(root, "checkout", "-q", "-b", "task")
            git_output(root, "commit", "-q", "--allow-empty", "-m", "task result")
            expected_commit_time = float(git_output(root, "log", "-1", "--format=%ct", "origin/main..HEAD"))

            now = time.time()
            log = root / "task.log"
            start = now - 300
            write_log(log, start, "A log line containing failed during a retry")
            last = root / "LAST.md"
            last.write_text("# 工作摘要\n結果整理中。\n", encoding="utf-8")
            set_mtime(last, start - 1)
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text(f"Committed task\t{log}\t{last}\t12345\n", encoding="utf-8")
            set_mtime(runs, start + 10)
            done.write_text("", encoding="utf-8")

            with patch("watch.running_pid_set", return_value=set()):
                agents, errors = load_all_agents(runs, done, LogParser(), now)

            self.assertEqual(errors, [])
            self.assertEqual(agents[0].status, "complete")
            self.assertEqual(agents[0].completion_time, expected_commit_time)

    def test_tsv_log_errors_and_markup_are_visible(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            missing_runs = root / "missing-runs.tsv"
            done = root / "done.tsv"
            done.write_text("bad row\n", encoding="utf-8")

            agents, errors = load_all_agents(missing_runs, done, LogParser(), time.time())

            self.assertTrue(any("TSV 不存在" in error for error in errors))
            self.assertTrue(any("格式錯誤" in error for error in errors))
            self.assertEqual(agents, [])

            runs = root / "runs.tsv"
            missing_log = root / "missing.log"
            last = root / "LAST.md"
            runs.write_text(f"[bold red]名稱[/]\t{missing_log}\t{last}\n", encoding="utf-8")
            done.write_text("", encoding="utf-8")
            agents, _ = load_all_agents(runs, done, LogParser(), time.time())
            self.assertEqual(agents[0].status, "error")
            self.assertIn("log 檔案不存在", render_data_errors([], agents, 80))
            escaped_markup = render_active_flow(agents)
            self.assertIn(r"\[bold red]", escaped_markup)
            self.assertIn(r"\[/]", escaped_markup)

    def test_log_parser_reuses_unchanged_file_and_reads_append(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            log = Path(tmpdir) / "incremental.log"
            write_log(log, time.time() - 20, "First line")
            parser = LogParser()

            first = parser.parse(log)
            cached_size = parser.cache[str(log.resolve())]["size"]
            same = parser.parse(log)
            self.assertEqual(first, same)
            self.assertEqual(parser.cache[str(log.resolve())]["size"], cached_size)

            with log.open("a", encoding="utf-8") as stream:
                stream.write("Second line\n")
            appended = parser.parse(log)
            self.assertIn("Second line", appended[2][-1])

    def test_quota_background_failure_clears_value_and_throttles(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            service = QuotaService(interval=60, sessions_path=Path(tmpdir))
            service.cached_summary = "codex 5h 88%"
            service.cached_raw = [{"provider": "codex", "windows": [{"name": "5 hours", "used": 88}]}]
            failed = Mock(returncode=1, stdout="", stderr="unavailable")

            with patch("watch.subprocess.run", return_value=failed) as run:
                self.assertTrue(service.refresh_in_background())
                deadline = time.monotonic() + 2
                while service._fetching and time.monotonic() < deadline:
                    time.sleep(0.005)

                summary, raw, stale, error = service.snapshot()
                self.assertFalse(service.refresh_in_background())

        self.assertEqual(run.call_count, 1)
        self.assertIn("magpie 讀取失敗", summary)
        self.assertNotIn("88%", summary)
        self.assertNotIn("88%", render_full_quota_view(raw))
        self.assertTrue(stale)
        self.assertIn("結束碼 1", error)

    def test_timer_only_change_does_not_redraw_static_data(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            root = Path(tmpdir)
            now = time.time()
            log = root / "timer.log"
            write_log(log, now - 60, "Still working")
            last = root / "missing-LAST.md"
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text(f"Timer task\t{log}\t{last}\n", encoding="utf-8")
            done.write_text("", encoding="utf-8")
            app = AgentWatchApp(runs_path=runs, done_path=done)

            self.assertTrue(app.update_data(now=now))
            initial_redraws = app.redraw_count
            initial_timer_updates = app.timer_update_count
            self.assertFalse(app.update_data(now=now + 5))

            self.assertEqual(app.redraw_count, initial_redraws)
            self.assertGreater(app.timer_update_count, initial_timer_updates)

    def _write_tsvs(self, root: Path, runs_content: str) -> Path:
        runs = root / "runs.tsv"
        done = root / "done.tsv"
        runs.write_text(runs_content, encoding="utf-8")
        done.write_text("", encoding="utf-8")
        return runs

    # ==========================================================================
    # v5 UI Tests
    # ==========================================================================

    def test_data_unchanged_no_redraw(self):
        """Verify that when data is unchanged, update_data skips redrawing (redraw_count remains unchanged)."""
        with tempfile.TemporaryDirectory() as tmpdir:
            tmppath = Path(tmpdir)
            log = tmppath / "test.log"
            log.write_text("2026-10-24T10:00:00Z Initial log line\n", encoding="utf-8")
            last = tmppath / "test-LAST.md"
            last.write_text("# DONE\n", encoding="utf-8")

            runs_tsv = tmppath / "runs.tsv"
            runs_tsv.write_text(f"Task 1\t{log}\t{last}\n", encoding="utf-8")
            done_tsv = tmppath / "done.tsv"
            done_tsv.write_text("", encoding="utf-8")

            app = AgentWatchApp(runs_path=runs_tsv, done_path=done_tsv)

            # Fixed timestamp to isolate data changes
            t0 = 1000000.0

            # First update -> data loaded, redraw happens
            ret1 = app.update_data(now=t0)
            self.assertTrue(ret1)
            self.assertEqual(app.redraw_count, 1)

            # Second update with exact same data -> no redraw
            ret2 = app.update_data(now=t0)
            self.assertFalse(ret2)
            self.assertEqual(app.redraw_count, 1, "資料沒變時不應該重畫 (redraw_count 應保持不變)")

            # Third update with exact same data -> still no redraw
            ret3 = app.update_data(now=t0)
            self.assertFalse(ret3)
            self.assertEqual(app.redraw_count, 1)

            # Modify log file to simulate data update
            time.sleep(0.01)
            log.write_text("2026-10-24T10:00:00Z Initial log line\nNew log update line\n", encoding="utf-8")
            ret4 = app.update_data(now=t0)
            self.assertTrue(ret4, "資料有變時應該更新")
            self.assertEqual(app.redraw_count, 2)

    def test_four_tabs_exist_and_switchable(self):
        """Verify v10 tab order and the retained architecture controls."""
        async def run_pilot_tabs():
            with tempfile.TemporaryDirectory() as tmpdir:
                tmppath = Path(tmpdir)
                runs_tsv = tmppath / "runs.tsv"
                runs_tsv.write_text("", encoding="utf-8")
                done_tsv = tmppath / "done.tsv"
                done_tsv.write_text("", encoding="utf-8")

                app = AgentWatchApp(runs_path=runs_tsv, done_path=done_tsv)
                app.quota_svc.refresh_in_background = lambda force=False: False
                async with app.run_test() as pilot:
                    # Four v10 tabs retain the existing architecture pane.
                    tc = app.query_one("#main-tabs", TabbedContent)
                    self.assertIsNotNone(tc)
                    self.assertIsNotNone(app.query_one("#tab-work"))
                    self.assertIsNotNone(app.query_one("#tab-history"))
                    self.assertIsNotNone(app.query_one("#tab-arch"))
                    self.assertIsNotNone(app.query_one("#tab-quota"))

                    # Default active tab is 'tab-work'
                    self.assertEqual(tc.active, "tab-work")

                    await pilot.press("2")
                    self.assertEqual(tc.active, "tab-history")
                    await pilot.press("4")
                    self.assertEqual(tc.active, "tab-arch")
                    self.assertEqual(app.architecture_view_id, "now")
                    self.assertEqual(app.architecture_project_id, "personal-ai-os")
                    await pilot.press("p")
                    self.assertEqual(app.architecture_project_id, "agent-watch")
                    await pilot.press("f")
                    self.assertEqual(app.architecture_view_id, "future")
                    await pilot.press("n")
                    self.assertEqual(app.architecture_view_id, "now")

                    # Press '3' to switch to Quota tab
                    await pilot.press("3")
                    self.assertEqual(tc.active, "tab-quota")

                    # Press '1' to switch back to Work tab
                    await pilot.press("1")
                    self.assertEqual(tc.active, "tab-work")

        asyncio.run(run_pilot_tabs())

    def test_headless_screenshots_saved_at_requested_sizes(self):
        async def capture_all():
            temp_root = tempfile.TemporaryDirectory(prefix="agent-watch-shots-")
            root = Path(temp_root.name)
            now = time.time()
            active_log = root / "active.log"
            write_log(active_log, now - 600, *(f"Log update {idx}" for idx in range(1, 10)), "TAIL END V8")
            active_last = root / "missing-LAST.md"
            done_log = root / "done.log"
            write_log(done_log, now - 900, "Completed successfully")
            done_last = root / "done-LAST.md"
            done_last.write_text("DONE\nScreenshot fixture complete\n", encoding="utf-8")
            set_mtime(done_last, now - 120)
            runs = root / "runs.tsv"
            done = root / "done.tsv"
            runs.write_text(
                f"example-project active\t{active_log}\t{active_last}\t\tAgent A\tmodel-x\n",
                encoding="utf-8",
            )
            done.write_text(
                f"example-project review\t{done_log}\t{done_last}\t\tReviewer\tmodel-w\n",
                encoding="utf-8",
            )
            set_mtime(active_log, now + 1)

            shots_dir = snapshot_dir(root / "shots")
            captures = (
                ("work-80x30.svg", (80, 30), "tab-work"),
                ("work-100x35.svg", (100, 35), "tab-work"),
                ("arch-now-80x30.svg", (80, 30), "tab-arch"),
                ("arch-future-80x30.svg", (80, 30), "tab-arch"),
                ("arch-now-100x35.svg", (100, 35), "tab-arch"),
                ("arch-future-100x35.svg", (100, 35), "tab-arch"),
                ("quota-100x35.svg", (100, 35), "tab-quota"),
            )
            exported = {}
            for filename, size, tab_id in captures:
                app = AgentWatchApp(runs_path=runs, done_path=done)
                app.quota_svc.cached_summary = "codex 5h 23% 7d 19%"
                app.quota_svc.cached_raw = [
                    {
                        "provider": "codex",
                        "name": "Codex",
                        "plan": "Plus",
                        "asOf": "2026-10-25T06:00:00Z",
                        "windows": [
                            {"name": "5 hours", "used": 23, "resetsAt": "2026-10-25T03:35:00+08:00"},
                            {"name": "7 days", "used": 19, "resetsAt": "2026-10-31T02:27:00+08:00"},
                        ],
                    }
                ]
                app.quota_svc.refresh_in_background = lambda force=False: False
                async with app.run_test(size=size) as pilot:
                    # Wait for the background run reader to land and for its
                    # deferred row focus to settle; a late TaskRow focus would
                    # otherwise switch the tab back to work after the key press.
                    for _ in range(200):
                        if app.data_svc.has_snapshot and app._row_widgets:
                            break
                        await pilot.pause(0.01)
                    for _ in range(3):
                        await pilot.pause()
                    if tab_id != "tab-work":
                        await pilot.press({"tab-arch": "4", "tab-quota": "3"}[tab_id])
                    if filename.startswith("arch-"):
                        await pilot.press("f" if "future" in filename else "n")
                    await pilot.pause()
                    self.assertEqual(app.query_one("#main-tabs").active, tab_id, filename)
                    svg = app.export_screenshot(title=filename)
                    (shots_dir / filename).write_text(svg, encoding="utf-8")
                    exported[filename] = svg
                    self.assertGreater(len(svg), 500)
                    self.assertTrue((shots_dir / filename).is_file())
            temp_root.cleanup()
            return exported

        exported = asyncio.run(capture_all())
        for work_image in ("work-80x30.svg", "work-100x35.svg"):
            screen_text = html.unescape(exported[work_image]).replace("\xa0", " ")
            self.assertIn("TAIL END V8", screen_text)
            self.assertIn("Log update 9", screen_text)
            self.assertIn("剛完成", screen_text)
        arch_text = html.unescape(exported["arch-now-100x35.svg"]).replace("\xa0", " ")
        future_text = html.unescape(exported["arch-future-100x35.svg"]).replace("\xa0", " ")
        arch80_text = html.unescape(exported["arch-now-80x30.svg"]).replace("\xa0", " ")
        future80_text = html.unescape(exported["arch-future-80x30.svg"]).replace("\xa0", " ")
        quota_text = html.unescape(exported["quota-100x35.svg"]).replace("\xa0", " ")
        self.assertIn("現在", arch_text)
        self.assertIn("專案教練", arch_text)
        self.assertIn("Mobile entry", arch_text)
        self.assertIn("watcher.sh", arch_text)
        self.assertIn("┌", arch_text)
        self.assertIn("▼", arch_text)
        self.assertIn("未來", future_text)
        self.assertIn("專案教練", future_text)
        self.assertIn("┌", future_text)
        self.assertIn("▼", future_text)
        self.assertIn("現在", arch80_text)
        self.assertIn("專案教練", arch80_text)
        self.assertIn("┌", arch80_text)
        self.assertIn("▼", arch80_text)
        self.assertIn("專案教練", future80_text)
        self.assertIn("┌", future80_text)
        self.assertIn("▼", future80_text)
        self.assertIn("服務額度用量", quota_text)
        self.assertIn("2026-10-25 14:00:00", quota_text)

    def test_v8_launcher_and_text_snapshots(self):
        root = Path(__file__).parent
        launcher = root / "agent-watch-v8"
        self.assertTrue(os.access(launcher, os.X_OK))
        help_result = subprocess.run(
            [str(launcher), "--help"],
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
            env=dict(os.environ, AGENT_WATCH_PYTHON=sys.executable),
        )
        self.assertEqual(help_result.returncode, 0)
        self.assertIn("agent-watch v8", help_result.stdout)
        for filename, width in (("once-80.txt", 80), ("once-120.txt", 120)):
            snapshot = (root / "shots" / filename).read_text(encoding="utf-8")
            self.assertIn("AGENT WATCH v8", snapshot)
            self.assertIn("額度", snapshot)
            for line in snapshot.splitlines():
                self.assertLessEqual(display_width(line), width, filename)

    def test_architecture_adapts_to_narrow_width(self):
        """Architecture labels remain readable at 80 columns and narrow widths."""
        empty: List[AgentInfo] = []
        wide = render_architecture_view(
            empty, width=100, view_id="future", project_id="agent-watch"
        )
        self.assertIn("專案教練", wide)
        self.assertIn("獨立審查", wide)

        normal = render_architecture_view(
            empty, width=80, view_id="future", project_id="agent-watch"
        )
        self.assertIn("專案教練", normal)
        self.assertIn("獨立審查", normal)
        for line in normal.splitlines():
            plain = re.sub(r"\[/?[^\[\]]*\]", "", line)
            self.assertLessEqual(display_width(plain), 80)

        narrow = render_architecture_view(
            empty, width=26, view_id="future", project_id="agent-watch"
        )
        self.assertIn("專案教練", narrow)
        self.assertIn("┌", narrow)

        now_80 = render_architecture_view(
            empty, width=80, view_id="now", project_id="agent-watch"
        )
        self.assertIn("OpenCode", now_80)
        self.assertIn("Codex", now_80)
        self.assertIn("Claude Code", now_80)
        self.assertIn("專案教練", now_80)
        self.assertIn("watcher", now_80)

    def test_architecture_node_status(self):
        """Verify worker state, model, role and progress come from TSV agents."""
        dummy_agents = [
            AgentInfo(
                name="example-project OpenCode",
                log_path=Path("/dev/null"),
                last_path=Path("/dev/null"),
                status="running",
                harness="OpenCode",
                model="group/night-flash",
                start_time=100.0,
                elapsed="00:10:00",
                latest="修正架構圖",
                stage="P4 架構",
                project="agent-watch",
            ),
            AgentInfo(
                name="example-project Codex",
                log_path=Path("/dev/null"),
                last_path=Path("/dev/null"),
                status="complete",
                harness="Codex",
                model="codex-sol-flash",
                start_time=100.0,
                elapsed="00:08:00",
                latest="主要實作完成",
                stage="P3 實作",
                completion_time=time.time() - 120,
                project="agent-watch",
            ),
            AgentInfo(
                name="example-project Claude Code audit",
                log_path=Path("/dev/null"),
                last_path=Path("/dev/null"),
                status="running",
                harness="Claude",
                model="sonnet-5-5",
                start_time=100.0,
                elapsed="00:05:00",
                latest="updating parser",
                stage="P2 調查",
                project="agent-watch",
            ),
            AgentInfo(
                name="example-project agy research",
                log_path=Path("/dev/null"),
                last_path=Path("/dev/null"),
                status="complete",
                harness="agy",
                model="gemini-research",
                start_time=100.0,
                elapsed="00:03:00",
                latest="done",
                completion_time=time.time() - 60,
                project="agent-watch",
            ),
        ]

        opencode_active, opencode_count, opencode_text = get_worker_status("opencode", dummy_agents)
        self.assertTrue(opencode_active)
        self.assertEqual(opencode_count, 1)
        self.assertIn("執行中", opencode_text)
        self.assertTrue(is_node_active("opencode", dummy_agents))

        agy_active, agy_count, agy_text = get_worker_status("agy", dummy_agents)
        self.assertFalse(agy_active)
        self.assertEqual(agy_count, 0)
        self.assertIn("待命中", agy_text)
        self.assertFalse(is_node_active("agy", dummy_agents))

        claude_active, claude_count, _ = get_worker_status("claude-code", dummy_agents)
        self.assertTrue(claude_active)
        self.assertEqual(claude_count, 1)
        self.assertTrue(is_node_active("claude-code", dummy_agents))

        now_text = render_architecture_view(
            dummy_agents, width=80, view_id="now", project_id="agent-watch"
        )
        wide_text = render_architecture_view(
            dummy_agents, width=180, view_id="now", project_id="agent-watch"
        )
        future_text = render_architecture_view(
            dummy_agents, width=80, view_id="future", project_id="agent-watch"
        )
        self.assertIn("專案教練", now_text)
        self.assertIn("Mobile", now_text)
        self.assertIn("watcher", now_text)
        self.assertIn("● running", now_text)
        self.assertIn("✓ done", now_text)
        self.assertIn("P4 架構", now_text)
        self.assertIn("group/night-flash", wide_text)
        self.assertIn("跑 small 唯讀調查與文件修改", wide_text)
        self.assertIn("sonnet-5-5", wide_text)
        self.assertIn("跑 review 與 claude 任務", wide_text)
        self.assertIn("最近事件", now_text)
        self.assertIn("修正架構圖", now_text)
        self.assertIn("┌", now_text)
        self.assertIn("▼", now_text)
        self.assertIn("▲", now_text)
        self.assertIn("專案教練", future_text)
        self.assertIn("獨立審查", future_text)
        self.assertIn("agy", future_text)
        self.assertIn("┌", future_text)
        self.assertIn("▼", future_text)

    def test_architecture_json_is_external_and_has_both_views(self):
        data = load_architecture_data(Path(__file__).with_name("arch.json"))
        self.assertEqual(data, ARCHITECTURE_DATA)
        self.assertEqual(set(data["architectures"]), {"now", "future"})
        self.assertEqual(
            set(data["projects"]),
            {"personal-ai-os", "agent-watch", "ios-shortcuts", "c1-runtime",
             "m5-coding-canary", "M5-setup", "agentq-canary", "K1", "hermes",
             "night", "other"},
        )
        self.assertEqual(
            set(data["projects"]["night"]),
            {"label", "aliases", "match_keywords", "path_keywords", "repo_paths",
             "repo_names", "default_view", "dispatch_only"},
        )
        self.assertEqual(
            data["architectures"]["now"]["layers"][-1],
            ["opencode", "codex", "claude-code", "agy"],
        )
        self.assertEqual(
            data["architectures"]["now"]["nodes"]["codex"]["model"],
            "codex-sol-flash / sub-all-smart / codex-main",
        )
        self.assertEqual(
            data["architectures"]["future"]["layers"][2],
            ["opencode", "codex", "claude-code", "agy"],
        )
        for project in data["projects"].values():
            self.assertIn(project["default_view"], data["architectures"])

    def test_each_project_can_render_its_own_architecture(self):
        for project_id, project in ARCHITECTURE_DATA["projects"].items():
            with self.subTest(project=project_id):
                rendered = render_architecture_view(
                    [], width=80, view_id="now", project_id=project_id
                )
                self.assertIn(str(project["label"]), rendered)
                self.assertIn("專案教練", rendered)
                self.assertIn("OpenCode", rendered)
                self.assertIn("最近事件", rendered)
                self.assertIn("┌", rendered)
                self.assertIn("▼", rendered)

    def test_quota_uses_one_threshold_source(self):
        """The legacy >80/>85 helpers are gone; quota_window_status is the only rule."""
        for name in ("get_quota_color_and_tag", "get_quota_color", "render_quota_bar"):
            self.assertFalse(hasattr(watch, name), name + " must not come back")
        self.assertEqual(watch.quota_window_status(84.9, None), ("#9ece6a", ""))
        self.assertEqual(watch.quota_window_status(85.0, 10), ("#f7768e", "告急"))
        self.assertEqual(watch.quota_window_status(50.0, 20), ("#e0af68", "燒太快"))
        chart = watch.render_quota_chart(90.0, 50.0, 10)
        self.assertIn("#f7768e", chart)
        self.assertNotIn("#e0af68", chart)


class StageOneScreenTests(IsolatedState, unittest.TestCase):
    def test_t2_t5_t10_pinned_quota_board_and_expand_at_both_widths(self):
        async def check_screens():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                now = time.time()
                log = root / "active.log"
                write_log(log, now - 120, "Current phase")
                runs, done = root / "runs.tsv", root / "done.tsv"
                runs.write_text(
                    f"example-project active\t{log}\t{root / 'missing.md'}\t{os.getpid()}\tAgent A\tmodel-x\n"
                )
                rows = []
                for index in range(6):
                    report = root / ("result-%d.txt" % index)
                    report.write_text("DONE\nNeutral fixture result\n")
                    set_mtime(report, now - 10 - index)
                    rows.append(f"example-project result-{index}\t{log}\t{report}")
                done.write_text("\n".join(rows) + "\n")
                payload = (Path(__file__).parent / "tests/fixtures/magpie_quota.json").read_text()
                snapshots = snapshot_dir(root / "shots")
                for width in (80, 120):
                    app = AgentWatchApp(runs_path=runs, done_path=done)
                    app.quota_svc.sessions_path = root
                    with patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout=payload)):
                        app.quota_svc.fetch(force=True)
                    app.quota_svc.refresh_in_background = lambda force=False: False
                    async with app.run_test(size=(width, 40)) as pilot:
                        await pilot.pause()
                        text = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
                        first = "\n".join(text.splitlines()[:20])
                        for name in ("Claude", "Codex", "OpenCode Go", "OpenRouter", "example-project active"):
                            self.assertIn(name, first)
                        quota = app.query_one("#compact-quota-line")
                        self.assertLessEqual(quota.size.height, 3)
                        self.assertNotIn("…", "\n".join(text.splitlines()[3:3 + quota.size.height]))
                        self.assertNotIn("work-scroll", [parent.id for parent in quota.ancestors])
                        self.assertIn("另 3 筆", text)
                        self.assertNotIn("result-3", text)
                        redraws = app.redraw_count
                        self.assertFalse(app.update_data(now=now + 60))
                        await pilot.pause()
                        timer_text = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
                        self.assertIn("已跑 ~3m", timer_text)
                        self.assertEqual(app.redraw_count, redraws)
                        await pilot.press("e")
                        await pilot.pause()
                        expanded = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
                        for index in range(6):
                            self.assertIn("result-%d" % index, expanded)
                        await pilot.press("e")
                        await pilot.pause()
                        self.assertFalse(app.ui_error)
                        snapshot = "\n".join(line.rstrip() for line in text.splitlines()) + "\n"
                        (snapshots / ("v9-s1-%dx40.txt" % width)).write_text(snapshot)
                        scroll = app.query_one("#work-scroll")
                        scroll.scroll_end(animate=False)
                        await pilot.pause()
                        scrolled = "\n".join(strip.text for strip in app.screen._compositor.render_strips())
                        self.assertIn("OpenRouter", "\n".join(scrolled.splitlines()[:10]))
        asyncio.run(check_screens())

    def test_once_uses_board_counts_and_balance_details(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            now = time.time()
            log, report = root / "run.log", root / "result.txt"
            write_log(log, now - 60, "Current phase")
            report.write_text("# Delivery notes\nReady for review\n")
            set_mtime(report, now - 5)
            runs, done = root / "runs.tsv", root / "done.tsv"
            active_name = "example-project active " + "long task title " * 5
            active_name = active_name.strip()
            runs.write_text(f"{active_name}\t{log}\t{root / 'missing.md'}\t{os.getpid()}\n")
            done.write_text(f"example-project review\t{log}\t{report}\n")
            payload = (Path(__file__).parent / "tests/fixtures/magpie_quota.json").read_text()
            output = io.StringIO()
            with patch("watch.running_pid_set", return_value={os.getpid()}), \
                    patch("watch.read_process_start", return_value=None), \
                    patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout=payload)):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(watch.run_once_summary(runs, done), 0)
            text = output.getvalue()
            for name in ("Claude", "Codex", "OpenCode Go", "OpenRouter", active_name):
                self.assertIn(name, "\n".join(text.splitlines()[:25]))
            self.assertIn("需處理 0 │ 進行中 1 │ 剛完成(2h) 1 │ 歷史 0 │ 總計 2", text)
            self.assertIn("已回報", text)
            self.assertIn("帳戶餘額 $19.58", text)
            self.assertNotIn("Harness", text)


class V10ScreenAndWorkerTests(IsolatedState, unittest.TestCase):
    def test_poll_data_stays_under_200ms_with_blocked_io_and_tabs_work(self):
        async def run():
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                runs, done = root / "agent-runs.tsv", root / "agent-runs-done.tsv"
                runs.write_text("")
                done.write_text("")
                entered, release = threading.Event(), threading.Event()
                reader_threads = []

                def slow_reader(*args, **kwargs):
                    reader_threads.append(threading.get_ident())
                    entered.set()
                    release.wait(timeout=3)
                    return [], []

                app = AgentWatchApp(runs_path=runs, done_path=done)
                app.quota_svc.refresh_in_background = lambda force=False: False
                with patch("watch.load_all_agents", side_effect=slow_reader):
                    try:
                        async with app.run_test(size=(80, 40)) as pilot:
                            await pilot.pause()
                            self.assertTrue(entered.is_set())
                            measured = []
                            for _ in range(5):
                                started = time.perf_counter()
                                app.poll_data()
                                measured.append((time.perf_counter() - started) * 1000)
                            for key, expected in (
                                ("2", "tab-history"), ("3", "tab-quota"),
                                ("4", "tab-arch"), ("1", "tab-work"),
                            ):
                                await pilot.press(key)
                                self.assertEqual(app.query_one("#main-tabs", TabbedContent).active, expected)
                            self.assertLess(max(measured), 200)
                            self.assertNotIn(threading.get_ident(), reader_threads)
                            self.assertEqual(len(reader_threads), 1)
                            print("v10 poll_data max_ms=%.3f (blocked fixture I/O)" % max(measured))
                            release.set()
                            await pilot.pause()
                    finally:
                        release.set()
        asyncio.run(run())

    def test_v10_fixture_snapshots_and_registry_mtimes(self):
        async def run():
            fixtures = Path(__file__).parent / "tests" / "fixtures"
            now = watch.parse_timestamp("2026-10-25T10:12:00Z")
            with tempfile.TemporaryDirectory() as tmp:
                root = Path(tmp)
                for name in ("claude_stream.jsonl", "opencode_stream.jsonl"):
                    (root / name).write_bytes((fixtures / name).read_bytes())
                runs, done = root / "agent-runs.tsv", root / "agent-runs-done.tsv"
                rows = (fixtures / "runs_v10.tsv").read_text()
                rows = rows.replace("opencode_stream.jsonl", str(root / "opencode_stream.jsonl"))
                rows = rows.replace("claude_stream.jsonl", str(root / "claude_stream.jsonl"))
                rows = rows.replace("opencode-result.txt", str(root / "opencode-result.txt"))
                rows = rows.replace("claude-result.txt", str(root / "claude-result.txt"))
                runs.write_text(rows)
                done.write_text("")
                before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in (runs, done)]
                snapshots = snapshot_dir(root / "shots")
                for width in (80, 120):
                    app = AgentWatchApp(runs_path=runs, done_path=done)
                    app.quota_svc.refresh_in_background = lambda force=False: False
                    with patch("watch.time.time", return_value=now), patch("watch.notify_completion"):
                        async with app.run_test(size=(width, 40)) as pilot:
                            await pilot.pause()
                            text = "\n".join(strip.text.rstrip() for strip in app.screen._compositor.render_strips()) + "\n"
                            self.assertIn("已跑 12m", text)
                            self.assertIn("Claude fixture", text)
                            self.assertIn("OpenCode fixture", text)
                            self.assertIn("Claude fixture 最新一句", text)
                            self.assertIn("OpenCode fixture 最新一句", text)
                            self.assertNotIn("'type'", text)
                            self.assertFalse(app.ui_error)
                            (snapshots / ("v10-s1-%dx40.txt" % width)).write_text(text)
                            await pilot.press("4")
                            await pilot.pause()
                            arch = "\n".join(strip.text.rstrip() for strip in app.screen._compositor.render_strips()) + "\n"
                            (snapshots / ("v10-s1-arch-%dx40.txt" % width)).write_text(arch)
                self.assertEqual(before, [(p.stat().st_mtime_ns, p.read_bytes()) for p in (runs, done)])
                print("v10 fixture agent-runs*.tsv mtime_ns and bytes UNCHANGED at 80x40 and 120x40")
        asyncio.run(run())

    def test_terminal_and_waiting_transitions_each_notify_once(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = AgentWatchApp(root / "unused-runs", root / "unused-done")
            agent = AgentInfo("fixture", root / "run.log", root / "REPORT.md", "running",
                              "fixture", time.time() - 60, "00:01:00", "fixture", key="fixture")
            with patch("watch.load_all_agents", return_value=([agent], [])), patch("watch.notify_completion") as notify:
                app.update_data()
                for status in ("waiting", "partial", "failed", "blocked", "complete"):
                    agent.status = status
                    app.update_data()
                    app.update_data()
                self.assertEqual(notify.call_count, 5)

    def test_architecture_ignores_ended_history(self):
        old = AgentInfo("example-project Worker A", Path("/dev/null"), Path("/dev/null"),
                        "complete", "fixture", 100, "00:01:00", "old task must disappear",
                        project="agent-watch", completion_time=time.time() - 8000)
        self.assertNotIn("old task must disappear", render_architecture_view([old], 120))


class V10MainPageTests(IsolatedState, unittest.IsolatedAsyncioTestCase):
    """Fixture-only UI checks; no real TSV, quota service or process lookup."""

    def setUp(self):
        super().setUp()
        self.now = parse_timestamp("2026-10-25T10:00:00Z")
        clock = patch("watch.time.time", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def fixture_agents(self):
        fixtures = Path(__file__).parent / "tests" / "fixtures"

        def agent(key, status, project="agent-watch", **values):
            defaults = dict(
                name=key, log_path=fixtures / "unused.log",
                last_path=fixtures / "unused-report", key=key, status=status,
                harness="Codex", model="codex-sol-flash",
                start_time=self.now - 750, elapsed="00:12:30",
                latest="fixture 最新進度", log_lines=[key + " 第一行", key + " 第二行", key + " 最新一行"],
                log_mtime=self.now - 1, project=project,
            )
            defaults.update(values)
            return AgentInfo(**defaults)

        return [
            agent("failed-review", "failed", latest="測試失敗", completion_time=self.now - 30),
            agent("missing-report", "no_report", "c1-runtime", latest="已結束、無報告"),
            agent("blocked-task", "blocked", latest="等待必要資料"),
            agent("quiet-task", "waiting", latest="日誌暫無更新"),
            agent("answer-hook", "question", latest="第⑤段等回答接點"),
            agent("工作頁排版", "running", stage="P2 實作主頁", model="deepseek-v4.1-" + "very-long-model-" * 4),
            agent("preview-follows-cursor", "running", "c1-runtime", stage="P3 測試", log_mtime=self.now + 100),
            *[agent("recent-%d" % index, "complete", completion_time=self.now - 100 - index)
              for index in range(4)],
            agent("archived-task", "complete", completion_time=self.now - 9000),
        ]

    def fixture_app(self, agents=None):
        agents = self.fixture_agents() if agents is None else agents
        root = Path(__file__).parent / "tests" / "fixtures"
        directory = tempfile.TemporaryDirectory(prefix="agent-watch-ui-test-")
        self.addCleanup(directory.cleanup)
        app = AgentWatchApp(root / "not-read-runs.tsv", root / "not-read-done.tsv",
                            state_path=Path(directory.name) / "state.json")
        app.data_svc.snapshot = lambda: (list(agents), [])
        app.data_svc.refresh_in_background = lambda: False
        app.quota_svc.refresh_in_background = lambda force=False: False
        app.quota_svc.cached_raw = [
            {"provider": "codex", "windows": [{"name": "5 hours", "used": 23}]},
            {"provider": "claude", "windows": [{"name": "5 hours", "used": 45}]},
            {"provider": "opencode-go", "windows": [{"name": "5 hours", "used": 10}]},
            {"provider": "openrouter", "balance": "$19.58"},
        ]
        return app

    @staticmethod
    def screen_text(app):
        return "\n".join(strip.text for strip in app.screen._compositor.render_strips())

    async def test_elapsed_column_survives_80_columns_and_long_identity(self):
        agent = self.fixture_agents()[5]
        columns = []
        for item in self.fixture_agents()[:7]:
            line = watch.render_task_row(item, 76, self.now).plain.splitlines()[0]
            columns.append(display_width(line.split("已跑", 1)[0]))
        self.assertEqual(len(set(columns)), 1, "Elapsed must occupy the same column in every active row")
        for elapsed, start in (("123:45:00", self.now - 445500), ("時間未知", None)):
            agent.start_time, agent.elapsed = start, elapsed
            for width in (72, 76, 80):
                text = watch.render_task_row(agent, width, self.now)
                self.assertIn("已跑 " + watch.compact_elapsed(agent), text.plain.splitlines()[0])
                self.assertTrue(all(display_width(line) <= width for line in text.plain.splitlines()))
                self.assertEqual(len(text.plain.splitlines()), 2)
        app = self.fixture_app([agent])
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            screen = self.screen_text(app)
            self.assertIn("已跑 時間未知", screen)
            self.assertIn("進行中", screen)
            self.assertFalse(app.ui_error)

    async def test_resize_at_120_changes_geometry_and_preserves_cursor(self):
        app = self.fixture_app()
        async with app.run_test(size=(119, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j")
            selected = app.selected_key
            left, preview = app.query_one("#work-scroll"), app.query_one("#work-log-box")
            self.assertEqual(app.layout_mode, "stacked")
            self.assertGreaterEqual(preview.region.y, left.region.bottom)
            await pilot.resize_terminal(120, 40)
            await pilot.pause()
            self.assertEqual(app.layout_mode, "split")
            self.assertEqual(preview.region.y, left.region.y)
            self.assertGreaterEqual(preview.region.x, left.region.right)
            self.assertEqual(app.selected_key, selected)
            await pilot.resize_terminal(80, 40)
            await pilot.pause()
            self.assertEqual(app.layout_mode, "stacked")
            self.assertGreaterEqual(preview.region.y, left.region.bottom)
            self.assertEqual(app.selected_key, selected)
            self.assertFalse(app.ui_error)

    async def test_four_status_colors_and_green_selected_frame(self):
        agents = self.fixture_agents()
        expected = ((agents[0], "#f7768e"), (agents[5], "#e0af68"),
                    (agents[7], "#9ece6a"), (agents[-1], "#737b91"))
        for agent, color in expected:
            text = watch.render_task_row(agent, 76, self.now)
            self.assertEqual(text.style, color)
        app = self.fixture_app([agents[5], agents[6], agents[-1]])
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.query_one(".project-section.selected").styles.border_top[1].hex, "#9ECE6A")
            await pilot.press("j")
            await pilot.pause()
            self.assertEqual(len(app.query(".project-section.selected")), 1)
            self.assertEqual(app.query_one(".project-section.selected").styles.border_top[1].hex, "#9ECE6A")
            await pilot.press("2")
            await pilot.pause()
            # Completed history is rendered in gray, independently of its old status.
            self.assertEqual(app.query_one("#main-tabs", TabbedContent).active, "tab-history")
            self.assertIn("archived-task", self.screen_text(app))
            self.assertIn("已完成", self.screen_text(app))
            for key, tab_id in (("3", "tab-quota"), ("4", "tab-arch"), ("1", "tab-work")):
                await pilot.press(key)
                await pilot.pause()
                self.assertEqual(app.query_one("#main-tabs", TabbedContent).active, tab_id)
            self.assertEqual(app.selected_key, agents[6].key)

    async def test_attention_collects_failed_and_missing_reports_across_projects(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            log = root / "run.log"
            write_log(log, self.now - 120, "finished")
            failed = root / "FAIL.md"
            failed.write_text("FAIL\n測試失敗\n")
            set_mtime(failed, self.now - 10)
            runs, done = root / "runs.tsv", root / "done.tsv"
            runs.write_text("")
            done.write_text(
                f"failed-review\t{log}\t{failed}\t\tCodex\tmodel-x\tagent-watch\n"
                f"missing-report\t{log}\t{root / 'missing'}\t\tClaude\tmodel-y\tpersonal-ai-os\n"
            )
            before = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (runs, done)}
            agents, errors = load_all_agents(runs, done, LogParser(process_start_reader=None), self.now)
            self.assertEqual(errors, [])
            self.assertEqual({a.status for a in agents}, {"failed", "no_report"})
            app = self.fixture_app(agents)
            async with app.run_test(size=(80, 40)) as pilot:
                await pilot.pause()
                attention = app.query_one("#attention-list")
                self.assertEqual({row.agent.key for row in attention.query(watch.TaskRow)}, {a.key for a in agents})
                self.assertFalse(list(app.query_one("#work-board").query(watch.TaskRow)))
                self.assertIn("失敗", self.screen_text(app))
                self.assertIn("無報告", self.screen_text(app))
            after = {p: (p.stat().st_mtime_ns, p.read_bytes()) for p in (runs, done)}
            self.assertEqual(before, after)

    async def test_attention_keeps_old_issues_until_they_are_resolved(self):
        agents = self.fixture_agents()[:5]
        for agent in agents:
            agent.completion_time = self.now - RECENT_DONE_SECONDS - 300
            agent.log_mtime = agent.completion_time
            self.assertEqual(watch.board_bucket(agent, self.now), "attention")
        app = self.fixture_app(agents)
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(len(app.query_one("#attention-list").query(watch.TaskRow)), 5)
            self.assertIn("需處理 5 │ 已接手 0 │ 進行中 0 │ 剛完成 0 │ 歷史 0 │ 隱藏 0 │ 總計 5", self.screen_text(app))
            await pilot.press("2")
            await pilot.pause()
            self.assertIn("目前沒有歷史任務", self.screen_text(app))
            for agent in agents:
                self.assertNotIn(agent.name, self.screen_text(app))

    async def test_x_cannot_clear_unanswered_question(self):
        question_dir = tempfile.TemporaryDirectory(prefix="aw-question-x-")
        self.addCleanup(question_dir.cleanup)
        question_root = Path(question_dir.name)
        (question_root / "QUESTION.md").write_text(
            "# QUESTION\n"
            "id: q1\n"
            "asked_at: 2026-10-24T00:00:00Z\n"
            "question: 要先修 A 還是 B？\n"
            "options:\n"
            "- A: 先修 A\n"
            "- B: 先修 B\n",
            encoding="utf-8",
        )
        question_path = question_root / "QUESTION.md"
        set_mtime(
            question_path,
            self.now - (watch.QUESTION_CLEARABLE_SECONDS - 60),
        )
        agent = self.fixture_agents()[4]
        agent.status = "question"
        agent.completion_time = self.now - 60
        agent.question = watch.read_task_question(question_root)
        app = self.fixture_app([agent])

        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            self.assertTrue(watch.question_is_active(agent.question, agent.completion_time))
            self.assertEqual(app.state.bucket(agent, self.now), "attention")
            self.assertEqual(
                watch.question_wait_seconds(agent.question, self.now),
                23 * 3600 + 59 * 60,
            )
            await pilot.press("x")
            await pilot.pause()
            self.assertEqual(
                app.state_message, "尚未收到這題的有效 ANSWER.md，不能清除",
            )
            self.assertNotIsInstance(app.screen, watch.DismissReasonScreen)
            self.assertEqual(app.state.bucket(agent, self.now), "attention")
            self.assertFalse(app.state.task(agent.key).get("dismissed", False))
            self.assertEqual(app.state.dismiss_stale([agent], self.now), 0)
            self.assertFalse((question_root / "ANSWER.md").exists())

            self.assertTrue(watch.write_answer_file(
                question_root, agent.question, "A", now=self.now,
            )[0])
            agent.question = watch.read_task_question(question_root)
            agent.status = "complete"
            self.assertFalse(watch.question_is_active(agent.question, agent.completion_time))
            self.assertNotEqual(app.state.bucket(agent, self.now), "attention")

    async def test_x_clears_overdue_question_but_shift_x_leaves_it_alone(self):
        question_dir = tempfile.TemporaryDirectory(prefix="aw-question-overdue-x-")
        self.addCleanup(question_dir.cleanup)
        question_root = Path(question_dir.name)
        question_path = question_root / "QUESTION.md"
        answer_path = question_root / "ANSWER.md"
        question_path.write_text(
            "# QUESTION\nid: q1\nquestion: 要先修 A 還是 B？\n"
            "options:\n- A: 先修 A\n- B: 先修 B\n",
            encoding="utf-8",
        )
        set_mtime(
            question_path,
            self.now - (watch.QUESTION_CLEARABLE_SECONDS + 60),
        )
        answer_path.write_text(
            "# ANSWER\nid: old-question\nanswered_at: 2026-10-24T00:00:00Z\n"
            "choice: A\n",
            encoding="utf-8",
        )
        paths = (question_path, answer_path)
        before = {
            path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths
        }
        agent = self.fixture_agents()[4]
        agent.status = "question"
        agent.completion_time = self.now - 60
        agent.question = watch.read_task_question(question_root)
        app = self.fixture_app([agent])

        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            self.assertTrue(watch.question_is_active(agent.question, agent.completion_time))
            self.assertEqual(
                watch.question_wait_seconds(agent.question, self.now),
                24 * 3600 + 60,
            )
            await pilot.press("shift+x")
            await pilot.pause()
            self.assertFalse(app.state.task(agent.key).get("dismissed", False))
            self.assertEqual(app.state.bucket(agent, self.now), "attention")
            self.assertEqual(app.state.dismiss_stale([agent], self.now), 0)
            await pilot.press("x")
            await pilot.pause()
            self.assertIsInstance(app.screen, watch.DismissReasonScreen)
            await pilot.press("enter")
            await pilot.pause()
            self.assertTrue(app.state.task(agent.key)["dismissed"])
            self.assertEqual(app.state.bucket(agent, self.now), "history")
            self.assertEqual(app.state.stale_info(agent, self.now), "已清除")

        self.assertEqual(
            before,
            {path: (path.read_bytes(), path.stat().st_mtime_ns) for path in paths},
        )

    async def test_x_clears_malformed_question_after_showing_why(self):
        question_dir = tempfile.TemporaryDirectory(prefix="aw-question-malformed-")
        self.addCleanup(question_dir.cleanup)
        question_root = Path(question_dir.name)
        question_path = question_root / "QUESTION.md"
        question_path.write_text("# QUESTION\nid: q1\n", encoding="utf-8")
        agent = self.fixture_agents()[0]
        agent.status = "failed"
        agent.completion_time = self.now - 60
        agent.question = watch.read_task_question(question_root)
        app = self.fixture_app([agent])

        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            self.assertFalse(
                watch.question_is_active(agent.question, agent.completion_time),
                "a malformed question can never be answered, so it cannot gate x",
            )
            self.assertEqual(app.state.bucket(agent, self.now), "attention")
            self.assertIn("malformed", self.screen_text(app))
            await pilot.press("x")
            await pilot.pause()
            self.assertIsInstance(app.screen, watch.DismissReasonScreen)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.state.bucket(agent, self.now), "history")
            self.assertEqual(
                question_path.read_text(encoding="utf-8"), "# QUESTION\nid: q1\n",
                "clearing the card never edits QUESTION.md",
            )

    async def test_x_optional_reason_text_empty_and_cancel(self):
        cases = (
            (("r", "e", "t", "r", "y"), "retry"),
            ((), ""),
            ((), None),
        )
        for index, (keys, expected) in enumerate(cases):
            with self.subTest(expected=expected):
                agent = self.fixture_agents()[0]
                agent.key += "-" + str(index)
                app = self.fixture_app([agent])
                before = app.state.data.copy()
                async with app.run_test(size=(80, 40)) as pilot:
                    await pilot.pause()
                    await pilot.press("x")
                    await pilot.pause()
                    self.assertIsInstance(app.screen, watch.DismissReasonScreen)
                    if keys:
                        await pilot.press(*keys)
                    await pilot.press("enter" if expected is not None else "escape")
                    await pilot.pause()
                    if expected is None:
                        self.assertEqual(app.state.data, before)
                    else:
                        self.assertTrue(app.state.task(agent.key)["dismissed"])
                        self.assertEqual(
                            app.state.task(agent.key)["dismissed_reason"], expected,
                        )

    async def test_x_reason_input_stops_at_200_characters(self):
        agent = self.fixture_agents()[0]
        app = self.fixture_app([agent])

        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            await pilot.press("x")
            await pilot.pause()
            self.assertIsInstance(app.screen, watch.DismissReasonScreen)
            reason_input = app.screen.query_one("#dismiss-reason-input", watch.Input)
            reason_input.insert_text_at_cursor("a" * 200)
            reason_input.insert_text_at_cursor("a")
            self.assertEqual(len(reason_input.value), 200)
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(
                app.state.task(agent.key)["dismissed_reason"], "a" * 200,
            )

    async def test_x_refuses_running_and_already_cleared_without_modal(self):
        running, cleared = self.fixture_agents()[5], self.fixture_agents()[0]
        agents = [running, cleared]
        app = self.fixture_app(agents)
        app.state.reconcile(agents, self.now)
        self.assertTrue(app.state.apply(
            "dismiss", cleared, agents, self.now, reason="previous",
        )[0])

        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            for agent, message in (
                (running, "進行中的任務不能清除"),
                (cleared, "目前項目不在需處理區，也不是已過期或被取代項目"),
            ):
                app.selected_key = agent.key
                before = app.state.data.copy()
                await pilot.press("x")
                await pilot.pause()
                self.assertNotIsInstance(app.screen, watch.DismissReasonScreen)
                self.assertEqual(app.state_message, message)
                self.assertEqual(app.state.data, before)

    async def test_preview_tracks_keyboard_mouse_and_refresh_without_stealing_selection(self):
        agents = self.fixture_agents()[5:7]
        agents[0].log_lines = [
            '{"type":"assistant","message":{"content":[{"type":"text","text":"Claude 白話預覽"}]}}'
        ]
        app = self.fixture_app(agents)
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(app.selected_key, agents[0].key)
            self.assertIn("Claude 白話預覽", self.screen_text(app))
            await pilot.press("down")
            await pilot.pause()
            self.assertEqual(app.selected_key, agents[1].key)
            self.assertEqual(app.query_one("#bottom-log-title").render().plain, agents[1].name)
            self.assertIn(agents[1].key + " 最新一行", self.screen_text(app))
            agents[0].log_lines.append("另一任務最新更新不可搶走預覽")
            agents[0].log_mtime += 1000
            app.update_data(now=self.now)
            await pilot.pause()
            self.assertEqual(app.selected_key, agents[1].key)
            self.assertEqual(app.focused.agent.key, agents[1].key)
            await pilot.press("k")
            await pilot.pause()
            self.assertEqual(app.selected_key, agents[0].key)
            await pilot.click(app._row_widgets[agents[1].key])
            await pilot.pause()
            self.assertEqual(app.selected_key, agents[1].key)
            agents[1].log_lines = ["long line " * 20, "wrapped line " * 20, "預覽最末行必須可見"]
            app.update_data(now=self.now)
            await pilot.pause()
            # scroll_end() applies its offset on the following frame, so a single
            # pause can still show the old scroll position (pre-existing flake).
            await pilot.pause()
            self.assertIn("預覽最末行必須可見", self.screen_text(app))
            self.assertFalse(app.ui_error)

    async def test_project_recent_limit_expand_and_selection_fallback(self):
        agents = self.fixture_agents()[5:]
        app = self.fixture_app(agents)
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            self.assertNotIn("recent-3", app._row_widgets)
            self.assertIn("另 1 筆", self.screen_text(app))
            await pilot.press("e")
            await pilot.pause()
            self.assertIn("recent-3", app._row_widgets)
            app._row_widgets["recent-3"].focus()
            await pilot.pause()
            self.assertEqual(app.selected_key, "recent-3")
            await pilot.press("e")
            await pilot.pause()
            self.assertNotEqual(app.selected_key, "recent-3")
            self.assertIn(app.selected_key, app._row_widgets)
            self.assertFalse(app.ui_error)

    async def test_project_box_title_shows_recent_count_including_zero(self):
        agents = self.fixture_agents()[5:]
        app = self.fixture_app(agents)
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            titles = {section.border_title for section in app.query(".project-section")}
            self.assertEqual(titles, {
                f"{watch.project_label('agent-watch')} · 進行中 1 · 剛完成 4 · 依 未判斷 5",
                f"{watch.project_label('c1-runtime')} · 進行中 1 · 剛完成 0 · 依 未判斷 1",
            })
            # Collapsing only changes the visible rows; the count stays at the total.
            self.assertIn("另 1 筆", self.screen_text(app))
            await pilot.press("e")
            await pilot.pause()
            titles = {section.border_title for section in app.query(".project-section")}
            self.assertIn(
                f"{watch.project_label('agent-watch')} · 進行中 1 · 剛完成 4 · 依 未判斷 5",
                titles,
            )
            self.assertNotIn("另 1 筆", self.screen_text(app))
            self.assertFalse(app.ui_error)

    async def test_shortcut_table_keeps_dashboard_shortcuts_inert(self):
        agents = self.fixture_agents()
        app = self.fixture_app(agents)
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            project, view = app.architecture_project_id, app.architecture_view_id
            selected = app.selected_key
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertIsInstance(app.screen, watch.HelpScreen)
            await pilot.press("e", "p", "n", "f", "1", "2", "3", "4")
            await pilot.pause()
            self.assertIsInstance(app.screen, watch.HelpScreen, "Global keys must not close the table")
            self.assertEqual(app.query_one("#main-tabs", TabbedContent).active, "tab-work")
            self.assertFalse(app.expand_recent)
            self.assertEqual(app.architecture_project_id, project)
            self.assertEqual(app.architecture_view_id, view)
            self.assertEqual(app.selected_key, selected)
            # The guard itself must reject the actions, not just rely on Textual
            # truncating the binding chain at the modal screen.
            for action in ("toggle_recent", "cycle_project", "show_architecture_now",
                           "show_architecture_future", "switch_tab('tab-history')"):
                self.assertFalse(await app.run_action(action), action)
            self.assertFalse(app.expand_recent)
            # q stays live: the table itself advertises it as the way out.
            self.assertTrue(app.check_action("quit", ()))
            await pilot.press("escape")
            await pilot.pause()
            await pilot.press("e")
            await pilot.pause()
            self.assertTrue(app.expand_recent, "Shortcuts work again once the table closes")
            self.assertFalse(app.ui_error)
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertIsInstance(app.screen, watch.HelpScreen)
            await pilot.press("q")
            self.assertFalse(app.is_running, "q must still quit while the table is open")

    async def test_help_full_screen_and_fixed_key_bar(self):
        app = self.fixture_app()
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            selected = app.selected_key
            bar = app.query_one("#key-bar")
            self.assertEqual(bar.region.height, 1)
            self.assertEqual(bar.region.bottom, 40)
            await pilot.press("question_mark")
            await pilot.pause()
            self.assertIsInstance(app.screen, watch.HelpScreen)
            self.assertEqual(app.screen.size, app.size)
            self.assertIn("快捷鍵", self.screen_text(app))
            app.update_data(now=self.now + 60)
            self.assertFalse(app.ui_error, "Background refresh must remain safe with the help screen open")
            await pilot.press("escape")
            await pilot.pause()
            self.assertEqual(app.selected_key, selected)
            self.assertEqual(app.focused.agent.key, selected)
            app.query_one("#work-scroll").scroll_end(animate=False)
            await pilot.pause()
            self.assertEqual(bar.region.bottom, 40)
            await pilot.press("question_mark", "question_mark")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, watch.HelpScreen)

    async def test_empty_board_and_live_task_removal_keep_preview_consistent(self):
        agents = []
        app = self.fixture_app(agents)
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            await pilot.press("j", "k", "down", "up", "e")
            self.assertIsNone(app.selected_key)
            self.assertIn("目前沒有可預覽的任務", self.screen_text(app))
            agents.extend(self.fixture_agents()[5:7])
            app.update_data(now=self.now)
            await pilot.pause()
            selected = app.selected_key
            agents[:] = [agent for agent in agents if agent.key != selected]
            app.update_data(now=self.now)
            await pilot.pause()
            self.assertEqual(app.selected_key, agents[0].key)
            self.assertEqual(app.query_one("#bottom-log-title").render().plain, agents[0].name)
            self.assertFalse(app.ui_error)

    async def test_deferred_row_focus_does_not_steal_an_explicit_tab(self):
        """A pending restore_selection focus must not pull the view back to 工作."""
        app = self.fixture_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            for tab_id in ("tab-arch", "tab-quota", "tab-tree"):
                with self.subTest(tab=tab_id):
                    app.action_switch_tab("tab-work")
                    await pilot.pause()
                    app.restore_selection(True)  # leaves a deferred row focus
                    app.action_switch_tab(tab_id)  # user switches in the same tick
                    for _ in range(3):
                        await pilot.pause()
                    self.assertEqual(app.query_one("#main-tabs").active, tab_id)
            # Staying on the work tab must still focus the selected row.
            app.action_switch_tab("tab-work")
            await pilot.pause()
            app.set_focus(None)
            await pilot.pause()
            self.assertIsNone(app.focused)
            app.restore_selection(True)
            for _ in range(3):
                await pilot.pause()
            self.assertIsInstance(app.focused, watch.TaskRow)
            self.assertFalse(app.ui_error)

    async def test_queued_row_focus_does_not_steal_an_explicit_tab(self):
        app = self.fixture_app()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = app._row_widgets[app.selected_key]
            for tab_id in ("tab-arch", "tab-quota", "tab-tree"):
                with self.subTest(tab=tab_id):
                    app.action_switch_tab("tab-work")
                    await pilot.pause()
                    app.set_focus(None)
                    app.set_focus(row)  # Focus event is delivered a tick later
                    app.action_switch_tab(tab_id)
                    for _ in range(3):
                        await pilot.pause()
                    self.assertEqual(app.query_one("#main-tabs").active, tab_id)

    async def test_tab_return_scrolls_selected_row_back_into_view(self):
        """Switching back to 工作 must scroll a scrolled-away selection into view."""
        app = self.fixture_app()
        async with app.run_test(size=(80, 24)) as pilot:
            await pilot.pause()
            scroll = app.query_one("#work-scroll")
            row = next(iter(app._row_widgets.values()))
            app.selected_key = row.agent.key
            app.update_selection()
            scroll.scroll_end(animate=False)
            await pilot.pause()
            await pilot.pause()
            self.assertFalse(app.screen.can_view_entire(row))
            app.action_switch_tab("tab-arch")
            await pilot.pause()
            app.action_switch_tab("tab-work")
            for _ in range(3):
                await pilot.pause()
            self.assertTrue(app.screen.can_view_entire(row))
            self.assertIs(app.focused, row)
            self.assertFalse(app.ui_error)

    async def test_background_result_after_shutdown_ignores_cancelled_callback(self):
        app = self.fixture_app([])
        with patch.object(app, "call_from_thread", side_effect=watch.CancelledError()):
            app._data_ready()

    async def test_fixture_snapshots_80_and_120_with_four_quota_providers(self):
        with tempfile.TemporaryDirectory() as tmp:
            shots = snapshot_dir(Path(tmp))
            for width in (80, 120):
                app = self.fixture_app()
                with patch("watch.load_all_agents", side_effect=AssertionError("UI must use fixture snapshot")), \
                     patch("watch.subprocess.run", side_effect=AssertionError("No live quota or process reads")):
                    async with app.run_test(size=(width, 40)) as pilot:
                        await pilot.pause()
                        text = self.screen_text(app)
                        lines = text.splitlines()
                        self.assertEqual(len(lines), 40)
                        self.assertTrue(all(display_width(line) <= width for line in lines))
                        quota = app.query_one("#compact-quota-line")
                        self.assertLessEqual(quota.size.height, 2)
                        quota_lines = "\n".join(lines[1:1 + quota.size.height])
                        names = ("Codex", "Claude", "OpenCode Go", "OpenRouter")
                        for name in names:
                            self.assertIn(name, quota_lines)
                        self.assertEqual(sorted(names, key=quota_lines.index), list(names))
                        self.assertIn("已跑 12m", text)
                        self.assertFalse(app.ui_error)
                        (shots / ("v10-s2-%dx40.txt" % width)).write_text(
                            "\n".join(line.rstrip() for line in lines) + "\n", encoding="utf-8",
                        )


class V10StateTests(unittest.TestCase):
    fixture_agents = V10MainPageTests.fixture_agents

    def setUp(self):
        self.now = parse_timestamp("2026-10-25T10:00:00Z")
        directory = tempfile.TemporaryDirectory(prefix="v10-state-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.path = self.root / "state.json"
        self.state = watch.WatchState(self.path)
        self.agents = self.fixture_agents()
        self.finished = self.agents[7]

    def apply(self, operation, agent=None, **values):
        return self.state.apply(operation, agent or self.finished, self.agents, self.now, **values)

    def test_dismiss_refusal_only_needs_agent_and_time(self):
        self.assertEqual(self.state.dismiss_refusal(self.finished, self.now),
                         "目前項目不在需處理區，也不是已過期或被取代項目")

    def test_dismiss_refusal_has_no_unreachable_cleared_message_branch(self):
        self.assertNotIn('return "此項目已清除"',
                         inspect.getsource(watch.WatchState.dismiss_refusal))

    def test_clean_dismiss_reason_removes_control_characters(self):
        self.assertEqual(watch.WatchState.clean_dismiss_reason(
            "A\x1b\x00\u2028\x0b\x0c\x85\u2029B"), "AB")

    def test_clean_dismiss_reason_does_not_split_zwj_sequences(self):
        family = "\U0001f468\u200d\U0001f469\u200d\U0001f467\u200d\U0001f466"
        self.assertEqual(
            watch.WatchState.clean_dismiss_reason(family * 60),
            family * 28,
        )

    def test_running_and_attention_cannot_hide_or_finish(self):
        for agent in self.agents[:7]:
            for operation in ("hide", "done"):
                self.assertFalse(self.apply(operation, agent)[0])
        self.assertFalse(self.path.exists())
        self.assertEqual(self.state.data["tasks"], {})

    def test_hidden_rerun_returns_home_and_keeps_tags_and_pin(self):
        self.apply("tags", tags=["追蹤"])
        self.apply("pin")
        self.assertTrue(self.apply("hide")[0])
        self.assertEqual(self.state.bucket(self.finished, self.now), "hidden")
        state = watch.WatchState(self.path)
        self.finished.status = "running"
        self.finished.start_time = self.now
        state.reconcile(self.agents, self.now)
        self.assertEqual(state.bucket(self.finished, self.now), "running")
        self.assertFalse(state.task(self.finished.key)["hidden"])
        self.assertEqual(state.task(self.finished.key)["tags"], ["追蹤"])
        self.assertTrue(state.task(self.finished.key)["pinned"])

    def test_sixth_pin_rejected_and_unpin_frees_slot(self):
        for agent in self.agents[:5]:
            self.assertTrue(self.apply("pin", agent)[0])
        before = self.path.read_bytes()
        ok, message = self.apply("pin", self.agents[5])
        self.assertFalse(ok)
        self.assertIn("5", message)
        self.assertEqual(self.path.read_bytes(), before)
        self.apply("pin", self.agents[0])
        self.assertTrue(self.apply("pin", self.agents[5])[0])
        self.assertEqual(sum(bool(t.get("pinned")) for t in self.state.data["tasks"].values()), 5)

    def test_missing_corrupt_and_invalid_schema_are_empty_with_warning(self):
        self.assertIn("不存在", self.state.warning)
        for content in ('{broken', '[]', '{"version":2,"tasks":{},"projects":{}}',
                        '{"version":1,"tasks":{"x":{"tags":3}},"projects":{}}',
                        '{"version":1,"tasks":{"x":{"hidden":"no"}},"projects":{}}'):
            self.path.write_text(content)
            state = watch.WatchState(self.path)
            self.assertEqual(state.data, dict(version=1, tasks={}, projects={}))
            self.assertIn("空狀態", state.warning)
        self.path.write_bytes(b"\xff")
        self.assertIn("空狀態", watch.WatchState(self.path).warning)

    def test_atomic_replace_sees_complete_temp_and_old_target(self):
        self.apply("tags", tags=["原始"])
        before = self.path.read_bytes()
        replace = os.replace
        calls = []

        def inspect(source, destination):
            source = Path(source)
            self.assertEqual(source.parent, self.path.parent)
            self.assertNotEqual(source, self.path)
            self.assertEqual(self.path.read_bytes(), before)
            data = json.loads(source.read_text())
            self.assertEqual(data["tasks"][self.finished.key]["tags"], ["新標籤"])
            calls.append(source)
            replace(source, destination)

        with patch("watch.os.replace", side_effect=inspect):
            self.assertTrue(self.apply("tags", tags=["新標籤"])[0])
        self.assertEqual(len(calls), 1)
        self.assertFalse(calls[0].exists())
        self.assertEqual(watch.WatchState(self.path).task(self.finished.key)["tags"], ["新標籤"])

    def test_failed_atomic_write_preserves_disk_and_memory(self):
        self.apply("tags", tags=["原始"])
        before = self.path.read_bytes()
        with patch("watch.os.replace", side_effect=OSError("fixture permission denied")):
            ok, message = self.apply("hide")
        self.assertFalse(ok)
        self.assertIn("寫入失敗", message)
        self.assertEqual(self.path.read_bytes(), before)
        self.assertFalse(self.state.task(self.finished.key).get("hidden"))
        self.assertEqual(list(self.root.glob("*.tmp")), [])

    def test_collapse_rejects_active_and_new_task_reopens_after_restart(self):
        self.assertFalse(self.apply("collapse")[0])
        project_tasks = [self.finished, self.agents[8]]
        self.assertTrue(self.state.apply("collapse", self.finished, project_tasks, self.now)[0])
        self.assertTrue(all(self.state.bucket(a, self.now) == "history" for a in project_tasks))
        restarted = watch.WatchState(self.path)
        new = self.agents[5]
        new.key = "new-run"
        restarted.reconcile(project_tasks + [new], self.now)
        self.assertFalse(restarted.data["projects"][new.project]["collapsed"])
        self.assertEqual(restarted.bucket(self.finished, self.now), "recent")

    def test_manual_done_restore_and_old_history_returns_home(self):
        self.assertTrue(self.apply("done")[0])
        self.assertEqual(self.state.bucket(self.finished, self.now), "history")
        old = self.agents[-1]
        self.apply("hide", old)
        self.assertTrue(self.apply("restore", old)[0])
        self.assertEqual(self.state.bucket(old, self.now), "recent")
        self.assertFalse(self.state.task(old.key)["hidden"])
        self.assertTrue(watch.WatchState(self.path).task(old.key)["restored"])

    def test_read_and_tags_persist_and_new_completion_is_unread(self):
        self.apply("tags", tags=["要追", "待驗", "要追", " "])
        self.apply("read")
        state = watch.WatchState(self.path)
        entry = state.task(self.finished.key)
        self.assertEqual(entry["tags"], ["要追", "待驗"])
        self.assertTrue(entry["read"])
        self.assertIn("updated_at", entry)
        self.finished.status = "running"
        state.reconcile(self.agents, self.now)
        self.finished.status = "complete"
        state.reconcile(self.agents, self.now)
        self.assertFalse(state.task(self.finished.key)["read"])
        self.assertEqual(state.task(self.finished.key)["tags"], ["要追", "待驗"])

    def test_history_search_filters_and_newest_first(self):
        agents = self.agents[7:]
        for agent in agents:
            self.state.apply("done", agent, agents, self.now)
        agents[1].project = "personal-ai-os"
        self.state.apply("tags", agents[1], agents, self.now, ["追蹤"])
        self.state.apply("read", agents[1], agents, self.now)
        self.state.apply("hide", agents[2], agents, self.now)
        find = lambda **values: history_tasks_for_test(agents, self.state, self.now, **values)
        self.assertEqual(find(), [a.key for a in agents])
        self.assertEqual(find(query="RECENT-1"), [agents[1].key])
        self.assertEqual(find(query="追蹤"), [agents[1].key])
        self.assertEqual(find(project="personal-ai-os"), [agents[1].key])
        self.assertEqual(find(tag="追蹤"), [agents[1].key])
        self.assertEqual(find(read="read"), [agents[1].key])
        self.assertNotIn(agents[1].key, find(read="unread"))
        self.assertEqual(find(hidden="hidden"), [agents[2].key])
        self.assertNotIn(agents[2].key, find(hidden="visible"))
        self.assertEqual(find(query="追蹤", project="personal-ai-os", tag="追蹤", read="read", hidden="visible"),
                         [agents[1].key])
        self.assertEqual(find(query="no-such-result"), [])


def history_tasks_for_test(agents, state, now, **values):
    return [a.key for a in watch.history_tasks(agents, state, now, **values)]


class V10HistoryScreenTests(IsolatedState, unittest.IsolatedAsyncioTestCase):
    fixture_agents = V10MainPageTests.fixture_agents
    fixture_app = V10MainPageTests.fixture_app
    screen_text = staticmethod(V10MainPageTests.screen_text)

    def setUp(self):
        super().setUp()
        self.now = parse_timestamp("2026-10-25T10:00:00Z")
        clock = patch("watch.time.time", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def history_fixture(self):
        agents = self.fixture_agents()[7:]
        for agent in agents:
            agent.project = "personal-ai-os"
        agents[1].project = "agent-watch"
        agents[-1].project = "agent-watch"
        for agent in agents:
            agent.completion_time -= 9000
        app = self.fixture_app(agents)
        app.state.update_task(agents[1].key, tags=["追蹤"], read=True)
        app.state.update_task(agents[2].key, hidden=True)
        return app, agents

    async def test_history_grouping_collapse_and_newest_first(self):
        app, agents = self.history_fixture()
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.press("2", "g")
            self.assertTrue(app.history_grouped)
            self.assertEqual(app.history_rows[0], ("project", "personal-ai-os"))
            self.assertEqual(app.history_rows[1:4], [("task", a.key) for a in (agents[0], agents[2], agents[3])])
            # Switching to grouped mode keeps the cursor on the previously selected task,
            # so select the project header before exercising collapse/expand.
            table = app.query_one("#history-table", watch.DataTable)
            table.move_cursor(row=app.history_rows.index(("project", "personal-ai-os")))
            await pilot.pause()
            self.assertEqual(app.history_rows[table.cursor_row], ("project", "personal-ai-os"))
            await pilot.press("space")
            self.assertIn("personal-ai-os", app.history_collapsed)
            self.assertNotIn(("task", agents[0].key), app.history_rows)
            self.assertEqual(app.history_rows[table.cursor_row], ("project", "personal-ai-os"))
            await pilot.press("enter")
            self.assertNotIn("personal-ai-os", app.history_collapsed)
            self.assertIn(("task", agents[0].key), app.history_rows)
            self.assertFalse(app.ui_error)

    async def test_grouped_space_on_task_row_collapses_project_and_moves_cursor(self):
        app, agents = self.history_fixture()
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.press("2", "g")
            table = app.query_one("#history-table", watch.DataTable)
            # 切到分組模式會保留原本的任務游標，這次刻意不先移到專案列。
            self.assertEqual(app.history_rows[table.cursor_row], ("task", agents[0].key))
            await pilot.press("space")
            self.assertIn("personal-ai-os", app.history_collapsed)
            self.assertNotIn(("task", agents[0].key), app.history_rows)
            self.assertEqual(app.history_rows[table.cursor_row], ("project", "personal-ai-os"))
            self.assertFalse(app.ui_error)

    async def test_grouped_search_input_filters_and_escape_clear(self):
        app, agents = self.history_fixture()
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.press("2", "g", "slash")
            await pilot.press(*"recent-1")
            self.assertEqual(app.history_query, "recent-1")
            self.assertEqual(app.history_rows, [("project", "agent-watch"), ("task", agents[1].key)])
            await pilot.press("enter")
            for field, value in (("project", "agent-watch"), ("tag", "追蹤"), ("read", "read"), ("hidden", "visible")):
                app.query_one("#filter-" + field, watch.Select).value = value
                await pilot.pause()
                self.assertIn(("task", agents[1].key), app.history_rows)
            app.query_one("#filter-hidden", watch.Select).value = "hidden"
            await pilot.pause()
            self.assertEqual(app.history_rows, [])
            await pilot.press("escape")
            self.assertEqual(app.history_query, "")
            self.assertFalse(app.query_one("#history-search").display)
            self.assertFalse(app.ui_error)

    async def test_search_treats_shortcuts_as_literal_text(self):
        app, _ = self.history_fixture()
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.press("2", "slash")
            await pilot.press(*"q1gituhd")
            self.assertTrue(app.is_running)
            self.assertEqual(app.history_query, "q1gituhd")
            self.assertFalse(app.history_grouped)
            self.assertEqual(app.query_one("#main-tabs", TabbedContent).active, "tab-history")
            await pilot.press("escape")
            self.assertEqual(app.history_query, "")

    async def test_restore_hidden_old_task_returns_to_work_with_selection(self):
        app, agents = self.history_fixture()
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.press("2")
            table = app.query_one("#history-table", watch.DataTable)
            table.move_cursor(row=app.history_rows.index(("task", agents[2].key)))
            await pilot.press("r")
            await pilot.pause()
            self.assertEqual(app.query_one("#main-tabs", TabbedContent).active, "tab-work")
            self.assertEqual(app.selected_key, agents[2].key)
            self.assertIn(agents[2].key, app._row_widgets)
            self.assertFalse(app.state.task(agents[2].key)["hidden"])
            self.assertFalse(app.ui_error)

    async def test_work_actions_persist_and_tag_modal_keeps_keys_literal(self):
        agents = self.fixture_agents()[7:9]
        app = self.fixture_app(agents)
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            self.assertTrue(app.state.task(agents[0].key)["read"], "visible preview marks completion read")
            await pilot.press("u")
            self.assertFalse(app.state.task(agents[0].key)["read"], "manual unread survives refresh")
            await pilot.press("i")
            await pilot.pause()
            self.assertTrue(app.state.task(agents[0].key)["pinned"])
            self.assertIn(agents[0].key, [row.agent.key for row in app.query_one("#pinned-box").query(watch.TaskRow)])
            await pilot.press("t")
            self.assertIsInstance(app.screen, watch.TagScreen)
            await pilot.press(*"q1,g")
            await pilot.press("enter")
            await pilot.pause()
            self.assertEqual(app.state.task(agents[0].key)["tags"], ["q1", "g"])
            await pilot.press("d")
            await pilot.pause()
            self.assertEqual(app.state.bucket(agents[0], self.now), "history")
            persisted = watch.WatchState(app.state.path)
            self.assertTrue(persisted.task(agents[0].key)["done_manual"])
            self.assertFalse(app.ui_error)

    async def test_history_highlighting_does_not_mark_read(self):
        app, agents = self.history_fixture()
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.press("2")
            self.assertFalse(app.state.task(agents[0].key).get("read", False))
            await pilot.press("u")
            self.assertTrue(app.state.task(agents[0].key)["read"])
            await pilot.press("u")
            self.assertFalse(app.state.task(agents[0].key)["read"])

    async def test_bad_state_warning_once_and_app_keeps_working(self):
        app, _ = self.history_fixture()
        app.state.path.write_text("{broken")
        app.state = watch.WatchState(app.state.path)
        app.state_message = app.state.warning
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            self.assertEqual(self.screen_text(app).count("state 檔損壞"), 1)
            await pilot.press("2", "g")
            app.update_data()
            await pilot.pause()
            self.assertEqual(self.screen_text(app).count("state 檔損壞"), 1)
            self.assertFalse(app.ui_error)

    async def test_s3_fixture_snapshots_and_registry_mtimes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs, done = root / "agent-runs.tsv", root / "agent-runs-done.tsv"
            runs.write_text("")
            done.write_text("")
            before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in (runs, done)]
            shots = Path(__file__).parent / "shots" if os.environ.get(
                "AGENT_WATCH_UPDATE_S3_SHOTS") else root / "shots"
            shots.mkdir(parents=True, exist_ok=True)
            for width in (80, 120):
                app, agents = self.history_fixture()
                agents.extend(self.fixture_agents()[5:7])
                app.runs_path, app.done_path = runs, done
                with patch("watch.load_all_agents", side_effect=AssertionError("No live registry")), \
                     patch("watch.subprocess.run", side_effect=AssertionError("No live service")):
                    async with app.run_test(size=(width, 40)) as pilot:
                        await pilot.pause()
                        for mode in ("work", "table", "grouped"):
                            if mode == "table":
                                await pilot.press("2")
                            elif mode == "grouped":
                                await pilot.press("g")
                            await pilot.pause()
                            text = self.screen_text(app)
                            self.assertEqual(len(text.splitlines()), 40)
                            self.assertTrue(all(display_width(line) <= width for line in text.splitlines()))
                            self.assertFalse(app.ui_error)
                            if mode != "work":
                                self.assertIn("結果摘要", text)
                                self.assertIn("archived-task", text)
                            (shots / ("v10-s3-%s-%dx40.txt" % (mode, width))).write_text(
                                "\n".join(line.rstrip() for line in text.splitlines()) + "\n",
                                encoding="utf-8",
                            )
            self.assertEqual(before, [(p.stat().st_mtime_ns, p.read_bytes()) for p in (runs, done)])
            print("v10-s3 fixture state isolated; TSV bytes and mtime_ns UNCHANGED")


class HandoffBucketTests(IsolatedState, unittest.IsolatedAsyncioTestCase):
    """N4: a failed card whose fix card is still running sits in 已接手."""

    def setUp(self):
        super().setUp()
        self.now = parse_timestamp("2026-10-25T10:00:00Z")
        clock = patch("watch.time.time", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def agent(self, name, status, project="claude-perm", **values):
        defaults = dict(
            name=name, log_path=Path("/dev/null"), last_path=Path("/dev/null"),
            status=status, model="fixture", harness="Claude", start_time=self.now - 900,
            elapsed="00:15:00", latest=name + " 最新進度", key=name + "::" + project,
            project=project, log_mtime=self.now - 1,
        )
        defaults.update(values)
        return AgentInfo(**defaults)

    def pair(self, replacement_status="running", **values):
        old = self.agent("claude-perm-p2-fix", "failed", completion_time=self.now - 600)
        new = self.agent("claude-perm-p2-fix2", replacement_status, start_time=self.now - 300, **values)
        return old, new

    def make_state(self, agents):
        directory = tempfile.TemporaryDirectory(prefix="aw-handoff-state-")
        self.addCleanup(directory.cleanup)
        state = watch.WatchState(Path(directory.name) / "state.json")
        state.reconcile(agents, self.now)
        return state

    def test_running_fix2_moves_old_card_to_handoff_and_failure_returns_it(self):
        old, new = self.pair()
        state = self.make_state([old, new])
        self.assertEqual(state.bucket(old, self.now), "handoff")
        self.assertEqual(state.stale_info(old, self.now), "")
        self.assertEqual(state.handoff_label(old), "已接手 → claude-perm-p2-fix2")
        new.status, new.completion_time = "failed", self.now - 10
        state.reconcile([old, new], self.now)
        self.assertEqual(state.bucket(old, self.now), "attention")
        self.assertEqual(state.handoff_label(old), "")
        for status, bucket in (("complete", "history"), ("reported", "handoff"),
                               ("partial", "handoff")):
            with self.subTest(status=status):
                new.status, new.completion_time = status, self.now - 10
                state.reconcile([old, new], self.now)
                self.assertEqual(state.bucket(old, self.now), bucket)
                if status == "complete":
                    self.assertEqual(
                        state.stale_info(old, self.now),
                        "已接手完成 → claude-perm-p2-fix2",
                    )

    def review_case(self, status="complete", report=None, review=None, question=None):
        old, fix = self.pair("complete", completion_time=self.now - 10)
        directory = tempfile.TemporaryDirectory(prefix="aw-review-card-")
        self.addCleanup(directory.cleanup)
        folder = Path(directory.name)
        if report is not None:
            (folder / "REPORT.md").write_text(report, encoding="utf-8")
        if review is not None:
            (folder / "REVIEW.md").write_text(review, encoding="utf-8")
        reviewer = self.agent(
            fix.name + "-review", status, last_path=folder / "task.log",
            completion_time=self.now - 5, question=question,
        )
        agents = [old, fix, reviewer]
        return old, fix, reviewer, agents, self.make_state(agents)

    def test_review_pass_clears_old_card(self):
        old, _, _, _, state = self.review_case(review="Review notes\nverdict: PASS\n")
        self.assertEqual(state.bucket(old, self.now), "history")
        self.assertEqual(
            state.stale_info(old, self.now), "已接手完成 → claude-perm-p2-fix2",
        )

    def test_review_fail_returns_old_card_to_attention(self):
        old, _, _, _, state = self.review_case(review="FAIL\n")
        self.assertEqual(state.bucket(old, self.now), "attention")
        self.assertEqual(state.stale_info(old, self.now), "修正卡審查 FAIL")

    def test_running_review_keeps_old_card_in_handoff(self):
        old, _, _, _, state = self.review_case(status="running", review="PASS\n")
        self.assertEqual(state.bucket(old, self.now), "handoff")

    def test_unreadable_verdict_clears_with_note(self):
        old, _, _, _, state = self.review_case(report="DONE\n")
        self.assertEqual(state.bucket(old, self.now), "history")
        self.assertIn("（審查讀不到，DONE 即清）", state.stale_info(old, self.now))

    def test_done_fail_report_first_line_parses_as_fail(self):
        old, _, _, _, state = self.review_case(
            report="DONE — FAIL（P2 × 2、P3 × 4）\n",
        )
        self.assertEqual(state.bucket(old, self.now), "attention")
        self.assertEqual(state.stale_info(old, self.now), "修正卡審查 FAIL")

    def test_auto_clear_recomputes_when_review_flips_to_fail(self):
        old, _, reviewer, agents, state = self.review_case(review="PASS\n")
        self.assertEqual(state.bucket(old, self.now), "history")
        self.assertEqual(state.task(old.key), {})
        (reviewer.last_path.parent / "REVIEW.md").write_text("FAIL\n", encoding="utf-8")
        state.reconcile(agents, self.now)
        self.assertEqual(state.bucket(old, self.now), "attention")
        self.assertEqual(state.stale_info(old, self.now), "修正卡審查 FAIL")

    def test_active_review_question_keeps_old_card_handed_off(self):
        old, _, _, _, state = self.review_case(
            question={"id": "q1", "answered": False},
        )
        self.assertEqual(state.bucket(old, self.now), "handoff")

    def test_review_inputs_are_read_only(self):
        old, _, reviewer, agents, _ = self.review_case(
            report="DONE — FAIL（P2 × 2、P3 × 4）\n", review="FAIL\n",
        )
        folder = reviewer.last_path.parent
        paths = [folder / name for name in (
            "agent-runs.tsv", "agent-runs-done.tsv", "REPORT.md",
            "REVIEW.md", "QUESTION.md", "ANSWER.md",
        )]
        for path in paths:
            path.touch(exist_ok=True)
        (folder / "QUESTION.md").write_text(
            "# QUESTION\nid: q1\noptions:\n- A: A\n", encoding="utf-8",
        )
        before = [(path.stat().st_mtime_ns, path.read_bytes()) for path in paths]
        state = watch.WatchState(folder / "state.json")
        state.reconcile(agents, self.now)
        self.assertEqual(
            before, [(path.stat().st_mtime_ns, path.read_bytes()) for path in paths],
        )

    def test_handoff_row_names_replacement_in_teal(self):
        old, new = self.pair()
        state = self.make_state([old, new])
        row = watch.render_task_row(
            old, 100, self.now, bucket=state.bucket(old, self.now),
            history_label=state.handoff_label(old),
        )
        self.assertIn("已接手 → claude-perm-p2-fix2", row.plain)
        self.assertEqual(row.style, "#73daca")
        self.assertIn("[#73daca]● 已接手[/]", watch.ATTENTION_LEGEND)
        self.assertEqual(
            watch.count_buckets([old], self.now, lambda _agent, _now: "handoff")["handoff"], 1
        )

    def test_handoff_cards_refuse_x_shift_x_hide_and_collapse(self):
        old, new = self.pair()
        stale = self.agent("stale-task", "failed", project="other-project",
                           completion_time=self.now - 48 * 3600)
        agents = [old, new, stale]
        state = self.make_state(agents)
        ok, message = state.apply("dismiss", old, agents, self.now)
        self.assertFalse(ok)
        self.assertIn("已接手的任務不能清除", message)
        for operation in ("hide", "done"):
            self.assertFalse(state.apply(operation, old, agents, self.now)[0])
        self.assertEqual(state.dismiss_stale(agents, self.now), 1)
        self.assertFalse(state.task(old.key).get("dismissed", False))
        self.assertTrue(state.task(stale.key)["dismissed"])
        self.assertEqual(state.bucket(old, self.now), "handoff")
        # A project with a handed-off card still has open work.
        self.assertFalse(state.apply("collapse", old, [old], self.now)[0])

    def handoff_app(self, agents):
        return V10MainPageTests.fixture_app(self, agents)

    async def test_handoff_section_sits_between_attention_and_pins(self):
        old, new = self.pair()
        failed = self.agent("other-failure", "failed", project="agent-watch",
                            completion_time=self.now - 60)
        recent = self.agent("done-task", "complete", project="agent-watch",
                            completion_time=self.now - 60)
        archived = self.agent("old-task", "complete", project="agent-watch",
                              completion_time=self.now - 9000)
        app = self.handoff_app([old, new, failed, recent, archived])
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            attention, handoff, pinned = (
                app.query_one(selector) for selector in ("#attention-box", "#handoff-box", "#pinned-box")
            )
            self.assertTrue(handoff.display)
            self.assertLessEqual(attention.region.bottom, handoff.region.y)
            self.assertLessEqual(handoff.region.bottom, pinned.region.y)
            self.assertEqual([row.agent.key for row in handoff.query(watch.TaskRow)], [old.key])
            self.assertNotIn(old.key, [row.agent.key for row in attention.query(watch.TaskRow)])
            screen = V10MainPageTests.screen_text(app)
            self.assertIn("● 已接手", screen)
            self.assertIn("已接手 → claude-perm-p2-fix2", screen)
            self.assertIn("需處理 1 │ 已接手 1 │ 進行中 1 │ 剛完成 1 │ 歷史 1 │ 隱藏 0 │ 總計 5", screen)
            self.assertFalse(app.ui_error)
            app.action_dismiss_stale()  # Shift+X
            await pilot.pause()
            self.assertIn("已接手 1 筆不能清除", app.state_message)
            app.selected_key = old.key
            app.action_dismiss_task()  # x
            await pilot.pause()
            self.assertIn("已接手的任務不能清除", app.state_message)
            self.assertNotIsInstance(app.screen, watch.DismissReasonScreen)
            self.assertEqual(app.state.bucket(old, self.now), "handoff")

        app = self.handoff_app([failed, recent])
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            self.assertFalse(app.query_one("#handoff-box").display)
            self.assertNotIn("已接手 →", V10MainPageTests.screen_text(app))

    async def test_handoff_screenshot_100x35(self):
        old, new = self.pair()
        app = self.handoff_app([old, new])
        with tempfile.TemporaryDirectory(prefix="agent-watch-handoff-shot-") as tmp:
            shots = snapshot_dir(Path(tmp) / "shots")
            async with app.run_test(size=(100, 35)) as pilot:
                await pilot.pause()
                await pilot.pause()
                self.assertEqual(app.query_one("#main-tabs").active, "tab-work")
                svg = app.export_screenshot(title="work-handoff-100x35.svg")
            (shots / "work-handoff-100x35.svg").write_text(svg, encoding="utf-8")
        self.assertIn("已接手 →", html.unescape(svg).replace("\xa0", " "))


class SupersessionTests(unittest.TestCase):
    def make_agent(self, name, status, *, project="agent-watch", start=100,
                   completion=None, question=None):
        return AgentInfo(
            name=name, log_path=Path("/dev/null"), last_path=Path("/dev/null"),
            status=status, model="fixture", start_time=start, elapsed="00:01:00",
            latest=name, key=f"{name}::{project}::{status}", project=project,
            completion_time=completion, question=question,
        )

    def make_state(self):
        directory = tempfile.TemporaryDirectory(prefix="aw-superseded-state-")
        self.addCleanup(directory.cleanup)
        return watch.WatchState(Path(directory.name) / "state.json")

    def test_suffix_normalization_strips_multiple_layers(self):
        self.assertEqual(
            watch._normalized_task_name("claude-perm-p2-fix2-retry-3"),
            "claude-perm-p2",
        )

    def test_suffix_normalization_keeps_bare_numbers_as_identity(self):
        for name in (
            "na2-n3", "wp6", "awv10-s5", "na2-n3-2", "1",
            "awv10-p1", "awv10-p2", "xxx-v1", "xxx-v2",
        ):
            with self.subTest(name=name):
                self.assertEqual(watch._normalized_task_name(name), name)

    def test_handoff_suffix_variants_share_one_family(self):
        for old_name, new_name in (
            ("claude-perm-p2-fix", "claude-perm-p2-fix2"),
            ("claude-perm-p2", "claude-perm-p2-fix2"),
            ("awv10-p2", "awv10-p2fix"),
            ("task", "task-fix"),
            ("task-p2", "task-p2fix"),
            ("task-fix", "task-fix2-retry-3"),
        ):
            with self.subTest(old=old_name, new=new_name):
                self.assertEqual(
                    watch._normalized_task_name(old_name),
                    watch._normalized_task_name(new_name),
                )

    def test_review_card_names_do_not_supersede_original_card(self):
        old = self.make_agent("task", "failed", start=100, completion=200)
        for name in ("task-review", "task-rereview"):
            with self.subTest(name=name):
                review = self.make_agent(name, "running", start=300)
                self.assertNotIn(old.key, watch.compute_superseded([old, review]))

    def test_numbered_sibling_cards_keep_their_own_status(self):
        for old_name, new_name in (
            ("na2-n3", "na2-n6"), ("wp6", "wp7"), ("awv10-s5", "awv10-s6"),
        ):
            with self.subTest(old=old_name, new=new_name):
                old = self.make_agent(old_name, "failed", start=100, completion=200)
                sibling = self.make_agent(new_name, "running", start=300)
                state = self.make_state()
                state.reconcile([old, sibling], 310)
                self.assertNotIn(
                    old.key, watch.compute_superseded([old, sibling]),
                    "a numbered sibling is not a fix card",
                )
                self.assertEqual(state.bucket(old, 310), "attention")
                self.assertEqual(state.stale_info(old, 310), "")

    def test_phase_and_version_siblings_keep_their_own_status(self):
        for old_name, new_name in (
            ("awv10-p1", "awv10-p2"),
            ("xxx-v1", "xxx-v2"),
        ):
            with self.subTest(old=old_name, new=new_name):
                old = self.make_agent(old_name, "failed", start=100, completion=200)
                sibling = self.make_agent(new_name, "running", start=300)
                state = self.make_state()
                state.reconcile([old, sibling], 310)
                self.assertNotIn(old.key, watch.compute_superseded([old, sibling]))
                self.assertEqual(state.bucket(old, 310), "attention")
                self.assertEqual(state.stale_info(old, 310), "")

    def test_same_phase_repair_suffix_hands_off_failed_card(self):
        old = self.make_agent("awv10-p1", "failed", start=100, completion=200)
        replacement = self.make_agent("awv10-p1-fix", "running", start=300)
        state = self.make_state()
        state.reconcile([old, replacement], 310)
        self.assertEqual(
            watch.compute_superseded([old, replacement]),
            {old.key: replacement.key},
        )
        self.assertEqual(state.bucket(old, 310), "handoff")

    def test_phase_versioned_fix2_hands_off_matching_failed_card(self):
        old = self.make_agent(
            "claude-perm-p2", "failed", project="claude-perm",
            start=100, completion=200,
        )
        replacement = self.make_agent(
            "claude-perm-p2-fix2", "running", project="claude-perm", start=300,
        )
        self.assertEqual(
            watch.compute_superseded([old, replacement]),
            {old.key: replacement.key},
        )

    def test_fix_from_different_phase_cannot_handoff(self):
        old = self.make_agent("awv10-p1", "failed", start=100, completion=200)
        replacement = self.make_agent("awv10-p2-fix", "running", start=300)
        state = self.make_state()
        state.reconcile([old, replacement], 310)
        self.assertNotIn(old.key, watch.compute_superseded([old, replacement]))
        self.assertEqual(state.bucket(old, 310), "attention")
        self.assertEqual(state.stale_info(old, 310), "")

    def test_multi_digit_phase_and_version_normalization(self):
        for name, family in (
            ("awv10-p12fix", "awv10-p12"),
            ("awv10-p12-fix2", "awv10-p12"),
            ("xxx-v10-fix", "xxx-v10"),
            ("xxx-v10fix", "xxx-v10"),
            ("awv10-p12", "awv10-p12"),
            ("xxx-v10", "xxx-v10"),
        ):
            with self.subTest(name=name):
                self.assertEqual(watch._normalized_task_name(name), family)

    def test_multi_digit_phase_repair_hands_off_failed_card(self):
        for old_name, new_name in (
            ("awv10-p12", "awv10-p12fix"),
            ("awv10-p12", "awv10-p12-fix2"),
            ("xxx-v10", "xxx-v10-fix"),
            ("xxx-v10", "xxx-v10fix"),
        ):
            with self.subTest(old=old_name, new=new_name):
                old = self.make_agent(old_name, "failed", start=100, completion=200)
                replacement = self.make_agent(new_name, "running", start=300)
                state = self.make_state()
                state.reconcile([old, replacement], 310)
                self.assertEqual(
                    watch.compute_superseded([old, replacement]),
                    {old.key: replacement.key},
                )
                self.assertEqual(state.bucket(old, 310), "handoff")

    def test_multi_digit_phase_siblings_and_other_phase_fixes_do_not_handoff(self):
        for old_name, new_name in (
            ("awv10-p1", "awv10-p12"),
            ("awv10-p12", "awv10-p13"),
            ("awv10-p1", "awv10-p12fix"),
            ("awv10-p1", "awv10-p12-fix2"),
            ("xxx-v1", "xxx-v10"),
            ("xxx-v1", "xxx-v10-fix"),
            ("xxx-v10", "xxx-v11fix"),
        ):
            with self.subTest(old=old_name, new=new_name):
                old = self.make_agent(old_name, "failed", start=100, completion=200)
                sibling = self.make_agent(new_name, "running", start=300)
                state = self.make_state()
                state.reconcile([old, sibling], 310)
                self.assertNotIn(old.key, watch.compute_superseded([old, sibling]))
                self.assertEqual(state.bucket(old, 310), "attention")
                self.assertEqual(state.stale_info(old, 310), "")

    def test_glued_repair_word_strips_without_hyphen(self):
        # A repair word glued onto a p/v number is stripped even without a
        # hyphen (`wp6fix` -> `wp6`); bare numbered names keep their identity.
        for name, family in (
            ("wp6fix", "wp6"),
            ("wp6fix2", "wp6"),
            ("wp6", "wp6"),
            ("wp7", "wp7"),
        ):
            with self.subTest(name=name):
                self.assertEqual(watch._normalized_task_name(name), family)
        old = self.make_agent("wp6", "failed", start=100, completion=200)
        repair = self.make_agent("wp6fix", "running", start=300)
        sibling_repair = self.make_agent("wp7fix", "running", start=301)
        self.assertEqual(
            watch.compute_superseded([old, repair, sibling_repair]),
            {old.key: repair.key},
        )

    def test_glued_phase_fix_no_longer_hands_off_unphased_parent(self):
        # Behaviour change in fix2: `-p2` is identity, so `task-p2fix`
        # belongs to `task-p2`, not to a bare `task`.
        self.assertNotEqual(
            watch._normalized_task_name("task"),
            watch._normalized_task_name("task-p2fix"),
        )
        old = self.make_agent("task", "failed", start=100, completion=200)
        replacement = self.make_agent("task-p2fix", "running", start=300)
        self.assertNotIn(old.key, watch.compute_superseded([old, replacement]))

    def test_bare_number_suffix_is_not_a_handoff(self):
        old = self.make_agent("na2-n3", "failed", start=100, completion=200)
        renamed = self.make_agent("na2-n3-2", "running", start=300)
        self.assertNotIn(old.key, watch.compute_superseded([old, renamed]))

    def test_spec_example_hands_off_inside_the_claude_perm_project(self):
        old = self.make_agent(
            "claude-perm-p2-fix", "failed", project="claude-perm",
            start=100, completion=200,
        )
        replacement = self.make_agent(
            "claude-perm-p2-fix2", "running", project="claude-perm", start=300,
        )
        self.assertEqual(
            watch.compute_superseded([old, replacement]),
            {old.key: replacement.key},
        )

    def test_running_fix2_hands_off_old_failed_card(self):
        old = self.make_agent(
            "claude-perm-p2-fix", "failed", start=100, completion=200,
        )
        replacement = self.make_agent(
            "claude-perm-p2-fix2", "running", start=201,
        )
        state = self.make_state()
        state.reconcile([old, replacement], 210)
        self.assertEqual(watch.compute_superseded([old, replacement]), {
            old.key: replacement.key,
        })
        self.assertEqual(state.stale_info(old, 210), "")
        self.assertEqual(state.bucket(old, 210), "handoff")

    def test_failed_fix2_does_not_hide_old_failed_card(self):
        old = self.make_agent(
            "claude-perm-p2-fix", "failed", start=100, completion=200,
        )
        replacement = self.make_agent(
            "claude-perm-p2-fix2", "failed", start=201, completion=300,
        )
        state = self.make_state()
        state.reconcile([old, replacement], 310)
        self.assertNotIn(old.key, watch.compute_superseded([old, replacement]))
        self.assertEqual(state.bucket(old, 310), "attention")

    def test_completed_fix2_hands_off_old_failed_card(self):
        old = self.make_agent(
            "claude-perm-p2-fix", "failed", start=100, completion=200,
        )
        replacement = self.make_agent(
            "claude-perm-p2-fix2", "complete", start=201, completion=300,
        )
        state = self.make_state()
        state.reconcile([old, replacement], 310)
        self.assertEqual(state.bucket(old, 310), "history")
        self.assertEqual(state.stale_info(old, 310), "已接手完成 → claude-perm-p2-fix2")

    def test_name_matching_never_crosses_project_boundary(self):
        old = self.make_agent(
            "claude-perm-p2-fix", "failed", start=100, completion=200,
        )
        for name in (old.name, "claude-perm-p2-fix2"):
            with self.subTest(replacement_name=name):
                replacement = self.make_agent(
                    name, "running", project="personal-ai-os", start=300,
                )
                self.assertNotIn(
                    old.key, watch.compute_superseded([old, replacement]),
                )

    def test_name_matching_requires_a_known_project(self):
        for project in (watch.PROJECT_OTHER, ""):
            with self.subTest(project=project):
                old = self.make_agent(
                    "claude-perm-p2-fix", "failed", project=project,
                    start=100, completion=200,
                )
                replacement = self.make_agent(
                    "claude-perm-p2-fix2", "running",
                    project=project, start=300,
                )
                self.assertNotIn(
                    old.key,
                    watch.compute_superseded([old, replacement]),
                )

    def test_unanswered_question_is_neither_source_nor_replacement(self):
        pending = dict(id="q1", answered=False, asked_at_ts=150)
        # A failed card that still asks a question cannot be handed off either.
        old_question = self.make_agent(
            "claude-perm-p2-fix", "failed", start=100, completion=200,
            question=pending,
        )
        running = self.make_agent(
            "claude-perm-p2-fix2", "running", start=201,
        )
        self.assertNotIn(
            old_question.key,
            watch.compute_superseded([old_question, running]),
        )

        # Neither can a still-running fix card that is itself waiting for an
        # answer supersede the card it is supposed to replace.
        failed = self.make_agent(
            "other-task-fix", "failed", start=100, completion=200,
        )
        pending_replacement = self.make_agent(
            "other-task-fix2", "running", start=201, question=pending,
        )
        self.assertNotIn(
            failed.key,
            watch.compute_superseded([failed, pending_replacement]),
        )

    def test_completed_unanswered_question_is_not_a_replacement(self):
        pending = dict(id="q1", answered=False, asked_at_ts=250)
        failed = self.make_agent(
            "complete-task", "failed", start=100, completion=200,
        )
        completed_replacement = self.make_agent(
            "complete-task-fix", "complete", start=201, completion=300,
            question=pending,
        )
        self.assertNotIn(
            failed.key,
            watch.compute_superseded([failed, completed_replacement]),
        )


class TestQuestionFiles(unittest.TestCase):
    """SPEC §3.3: QUESTION.md / ANSWER.md parsing and atomic answering."""

    QUESTION = (
        "# QUESTION\n"
        "id: q1\n"
        "asked_at: 2026-10-25T10:00:00Z\n"
        "question: 要先修 A 還是 B？\n"
        "options:\n"
        "- A: 先修 A\n"
        "- B: 先修 B（推薦）\n"
        "- C: 兩個都先不修\n"
    )

    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="aw-question-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def test_valid_question_parses_id_options_and_wait(self):
        parsed = watch.parse_question(self.QUESTION)
        self.assertEqual(parsed["error"], "")
        self.assertEqual(parsed["id"], "q1")
        self.assertEqual([code for code, _ in parsed["options"]], ["A", "B", "C"])
        self.assertIn("推薦", parsed["options"][1][1])
        self.assertEqual(
            watch.format_question_wait(watch.question_wait_seconds(
                {"asked_at_ts": parsed["asked_at"]}, parsed["asked_at"] + 185)),
            "已等 3m",
        )
        self.assertEqual(watch.format_question_wait(31 * 60), "已等 31m")
        self.assertEqual(watch.format_question_wait(2 * 3600 + 5 * 60), "已等 2h05m")

    def test_missing_id_question_or_options_is_an_error(self):
        for body, expected in (
            ("id: q1\nquestion: x\noptions:\n- A: a\n", "缺少 # QUESTION"),
            ("# QUESTION\nquestion: x\noptions:\n- A: a\n", "缺少 id"),
            ("# QUESTION\nid: q1\noptions:\n- A: a\n", "缺少 question"),
            ("# QUESTION\nid: q1\nquestion: x\n", "缺少 options"),
            ("# QUESTION\nid: q1\nquestion: x\noptions:\n- A\n", "缺少 options"),
            ("# QUESTION\nid: q1\nquestion: x\noptions:\n- A: a\n- A: b\n", "重複"),
        ):
            with self.subTest(body=body):
                self.assertIn(expected, watch.parse_question(body)["error"])
        self.assertTrue(watch.question_is_malformed(watch.parse_question("# QUESTION\nid: q1\n")))
        self.assertFalse(
            watch.question_is_active(watch.parse_question("# QUESTION\nid: q1\n")),
            "a malformed question cannot be answered, so it must not block the card",
        )

    def test_malformed_question_is_marked_and_has_a_clear_exit(self):
        (self.root / "QUESTION.md").write_text("# QUESTION\nid: q1\n", encoding="utf-8")
        question = watch.read_task_question(self.root)
        self.assertTrue(watch.question_is_malformed(question))
        self.assertFalse(watch.question_is_active(question))
        self.assertIn("缺少 question", question["error"])
        self.assertFalse(watch.write_answer_file(self.root, question, "A")[0])
        self.assertFalse((self.root / "ANSWER.md").exists())

        agent = AgentInfo(
            name="malformed-task", log_path=Path("/dev/null"),
            last_path=Path("/dev/null"), status="failed", model="fixture",
            start_time=100, elapsed="00:01:00", latest="失敗", key="malformed",
            completion_time=200, question=question,
        )
        row = watch.render_task_row(agent, 120, 210).plain
        self.assertIn("malformed", row)
        self.assertIn("按 x 清除", row)
        self.assertEqual(
            (self.root / "QUESTION.md").read_text(encoding="utf-8"),
            "# QUESTION\nid: q1\n",
            "showing a malformed question never rewrites the file",
        )

    def test_malformed_question_keeps_the_task_status(self):
        now = parse_timestamp("2026-10-25T10:10:00Z")
        log = self.root / "run.log"
        write_log(log, now - 600, "等回答中")
        (self.root / "QUESTION.md").write_text("# QUESTION\nid: q1\n", encoding="utf-8")
        runs, done = self.root / "runs.tsv", self.root / "done.tsv"
        runs.write_text(
            "bad-question\t%s\t%s\t\tClaude\tmodel-q\taw\n" % (log, self.root / "REPORT.md")
        )
        done.write_text("")
        agents, errors = load_all_agents(
            runs, done, LogParser(process_start_reader=None), now,
        )
        self.assertEqual(errors, [])
        self.assertNotEqual(agents[0].status, "question")
        self.assertTrue(watch.question_is_malformed(agents[0].question))

    def test_answer_requires_matching_id_and_choice(self):
        (self.root / "QUESTION.md").write_text(self.QUESTION)
        question = watch.read_task_question(self.root)
        self.assertTrue(watch.question_is_active(question))
        (self.root / "ANSWER.md").write_text("# ANSWER\nid: q9\nchoice: B\n")
        self.assertFalse(watch.read_task_question(self.root)["answered"], "stale id is not an answer")
        self.assertTrue(watch.question_is_active(watch.read_task_question(self.root)))

        ok, _ = watch.write_answer_file(self.root, question, "D")
        self.assertFalse(ok)
        self.assertFalse((self.root / "ANSWER.md").read_text().endswith("choice: D\n"))

        ok, _ = watch.write_answer_file(self.root, question, "B", "先 B 再 A", now=1792922400)
        self.assertTrue(ok)
        content = (self.root / "ANSWER.md").read_text()
        self.assertEqual(
            content.splitlines()[:4],
            ["# ANSWER", "id: q1", "answered_at: 2026-10-25T18:00:00+08:00", "choice: B"],
        )
        self.assertIn("note: 先 B 再 A", content)
        self.assertEqual(list(self.root.glob("*.tmp")), [])
        self.assertTrue(watch.read_task_question(self.root)["answered"])

    def test_question_stays_active_until_matching_answer_even_after_completion(self):
        (self.root / "QUESTION.md").write_text(self.QUESTION)
        question = watch.read_task_question(self.root)
        self.assertTrue(watch.question_is_active(question, completion_time=question["asked_at"] - 60))
        self.assertTrue(watch.question_is_active(question, completion_time=question["asked_at"] + 60))
        self.assertTrue(watch.question_is_active(
            question, completion_time=question["asked_at"] + watch.STALE_ATTENTION_SECONDS + 60,
        ))
        (self.root / "ANSWER.md").write_text("# ANSWER\nid: q9\nchoice: B\n")
        self.assertTrue(watch.question_is_active(
            watch.read_task_question(self.root), completion_time=question["asked_at"] + 60,
        ))
        self.assertTrue(watch.write_answer_file(self.root, question, "B")[0])
        self.assertFalse(watch.question_is_active(watch.read_task_question(self.root)))

    def test_tsv_question_status_and_answer_clears_attention(self):
        now = parse_timestamp("2026-10-25T10:10:00Z")
        log = self.root / "run.log"
        write_log(log, now - 600, "等回答中")
        (self.root / "QUESTION.md").write_text(self.QUESTION)
        set_mtime(self.root / "QUESTION.md", now - 120)
        runs, done = self.root / "runs.tsv", self.root / "done.tsv"
        runs.write_text("ask-task\t%s\t%s\t\tClaude\tmodel-q\taw\n" % (log, self.root / "REPORT.md"))
        done.write_text("")
        agents, errors = load_all_agents(runs, done, LogParser(process_start_reader=None), now)
        self.assertEqual(errors, [])
        self.assertEqual(agents[0].status, "question")
        self.assertEqual(agents[0].question["id"], "q1")
        self.assertTrue(watch.write_answer_file(self.root, agents[0].question, "A", now=now)[0])
        agents, _ = load_all_agents(runs, done, LogParser(process_start_reader=None), now)
        self.assertNotEqual(agents[0].status, "question")
        self.assertTrue(agents[0].question["answered"])

    def test_escalation_writes_reserved_choice_and_target(self):
        (self.root / "QUESTION.md").write_text(self.QUESTION)
        question = watch.read_task_question(self.root)
        self.assertFalse(question["escalated"])

        ok, _ = watch.write_answer_file(
            self.root, question, "ESCALATE", "交給教練", now=1792922400,
        )
        self.assertTrue(ok)
        content = (self.root / "ANSWER.md").read_text()
        self.assertEqual(
            content.splitlines()[:5],
            ["# ANSWER", "id: q1", "answered_at: 2026-10-25T18:00:00+08:00",
             "choice: ESCALATE", "escalate_to: coach"],
        )
        self.assertIn("note: 交給教練", content)

        question = watch.read_task_question(self.root)
        self.assertTrue(question["answered"])
        self.assertTrue(question["escalated"])
        self.assertEqual(question["escalate_to"], "coach")
        self.assertFalse(watch.question_is_active(question), "an escalation is not Bo-Yuan's queue")
        self.assertTrue(watch.question_escalated(question))

        ok, _ = watch.write_answer_file(
            self.root, question, "ESCALATE", "", now=1792922400, escalate_to="claude-code",
        )
        self.assertTrue(ok)
        self.assertIn("escalate_to: claude-code", (self.root / "ANSWER.md").read_text())

        before = (self.root / "ANSWER.md").read_text()
        ok, message = watch.write_answer_file(
            self.root, question, "ESCALATE", "", escalate_to="bob",
        )
        self.assertFalse(ok)
        self.assertIn("轉介對象", message)
        self.assertEqual((self.root / "ANSWER.md").read_text(), before)

    def test_tsv_escalated_status_leaves_the_question_queue(self):
        now = parse_timestamp("2026-10-25T10:10:00Z")
        log = self.root / "run.log"
        write_log(log, now - 600, "等回答中")
        (self.root / "QUESTION.md").write_text(self.QUESTION)
        set_mtime(self.root / "QUESTION.md", now - 120)
        runs, done = self.root / "runs.tsv", self.root / "done.tsv"
        runs.write_text("ask-task\t%s\t%s\t\tClaude\tmodel-q\taw\n" % (log, self.root / "REPORT.md"))
        done.write_text("")
        agents, _ = load_all_agents(runs, done, LogParser(process_start_reader=None), now)
        self.assertEqual(agents[0].status, "question")
        self.assertTrue(watch.write_answer_file(
            self.root, agents[0].question, "ESCALATE", now=now, escalate_to="coach",
        )[0])

        agents, _ = load_all_agents(runs, done, LogParser(process_start_reader=None), now + 60)
        self.assertEqual(agents[0].status, "escalated")
        self.assertFalse(watch.question_is_active(agents[0].question))
        self.assertTrue(watch.question_escalated(agents[0].question))
        self.assertEqual(watch.task_bucket(agents[0], now + 60), "attention")

        # The upper agent answers with a real choice: the run leaves the queue.
        (self.root / "ANSWER.md").write_text(
            "# ANSWER\nid: q1\nanswered_at: 2026-10-25T10:20:00Z\n" "choice: B\nnote: 上層決定\n"
        )
        agents, _ = load_all_agents(runs, done, LogParser(process_start_reader=None), now + 90)
        self.assertNotIn(agents[0].status, ("question", "escalated"))


class TestEscalationState(IsolatedState, unittest.TestCase):
    """The dashboard remembers a hand-off even after ANSWER.md is overwritten."""

    def make_state(self):
        directory = tempfile.TemporaryDirectory(prefix="aw-escalate-state-")
        self.addCleanup(directory.cleanup)
        return watch.WatchState(Path(directory.name) / "state.json")

    def make_agent(self, status="escalated", question=None):
        agent = AgentInfo(
            "esc-task", Path("/dev/null"), Path("/dev/null"), status,
            "fixture", None, "00:00:00", "等上層", key="esc-task::/dev/null::/dev/null",
        )
        agent.question = question
        return agent

    def test_state_remembers_escalation_for_attribution(self):
        state = self.make_state()
        question = dict(id="q1", answered=True, escalated=True, escalate_to="claude-code")
        agent = self.make_agent(question=question)
        self.assertTrue(state.remember_escalation(agent, question))
        self.assertEqual(state.task(agent.key)["escalate_qid"], "q1")
        self.assertEqual(state.task(agent.key)["escalate_to"], "claude-code")
        self.assertEqual(state.answered_by(agent), "", "still 已轉介, not answered yet")

        # The upper agent overwrote ANSWER.md with a normal choice.
        agent.question = dict(id="q1", answered=True, escalated=False)
        self.assertEqual(state.answered_by(agent), "claude-code")

        # Reloading the state file keeps the attribution.
        self.assertEqual(watch.WatchState(state.path).answered_by(agent), "claude-code")

        # A different question id is not attributed to the old hand-off.
        agent.question = dict(id="q2", answered=True, escalated=False)
        self.assertEqual(state.answered_by(agent), "")

    def test_answer_file_can_carry_the_upper_agent_itself(self):
        state = self.make_state()
        agent = self.make_agent(status="blocked", question=dict(
            id="q1", answered=True, escalated=False, answer=dict(escalate_to="coach"),
        ))
        self.assertEqual(state.answered_by(agent), "coach")


class V10DeepViewTests(IsolatedState, unittest.IsolatedAsyncioTestCase):
    """SPEC §2.4 / §3.3: read-only terminal, question popup, ANSWER.md, 30m alert."""

    fixture_app = V10MainPageTests.fixture_app
    screen_text = staticmethod(V10MainPageTests.screen_text)

    QUESTION = TestQuestionFiles.QUESTION

    def setUp(self):
        super().setUp()
        self.now = parse_timestamp("2026-10-25T10:00:00Z")
        clock = patch("watch.time.time", return_value=self.now)
        clock.start()
        self.addCleanup(clock.stop)

    def deep_dir(self):
        directory = tempfile.TemporaryDirectory(prefix="aw-deep-")
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        log = root / "run.log"
        log.write_text(
            '{"type":"assistant","message":{"content":[{"type":"text","text":"白話第一句"}]}}\n'
            '{"type":"system","subtype":"init","model":"fixture-model","timestamp":"2026-10-25T09:59:00Z"}\n'
            "工具完成\n"
        )
        return root, log

    def write_question(self, root, asked_at, body=None):
        stamp = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(asked_at))
        text = body if body is not None else self.QUESTION.replace(
            "2026-10-25T10:00:00Z", stamp,
        )
        path = root / "QUESTION.md"
        path.write_text(text)
        set_mtime(path, asked_at)

    def question_agent(self, root, log, status="question", question=None, **values):
        if question is None:
            question = watch.read_task_question(root)
        defaults = dict(
            name="question-task", log_path=log, last_path=root / "REPORT.md",
            status=status, harness="Claude", model="claude-fixture",
            start_time=self.now - 900, elapsed="00:15:00", latest="等回答",
            log_lines=["等回答中"], log_mtime=self.now - 1, project="agent-watch",
            key="question-task::%s::%s" % (log, root / "REPORT.md"), question=question,
        )
        defaults.update(values)
        return AgentInfo(**defaults)

    async def test_enter_terminal_escape_keeps_pid_and_log_untouched(self):
        root, log = self.deep_dir()
        agent = self.question_agent(root, log, status="running", question=None, pid=os.getpid())
        before = (log.stat().st_mtime_ns, log.read_bytes())
        app = self.fixture_app([agent])
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, watch.TerminalScreen)
            text = self.screen_text(app)
            self.assertIn("白話第一句", text)
            self.assertIn("初始化 Claude (fixture-model)", text)
            self.assertNotIn('{"type"', text)
            self.assertTrue(app.state.task(agent.key)["read"], "deep view marks the task read")
            app.update_data(now=self.now + 5)
            self.assertIsInstance(app.screen, watch.TerminalScreen)
            self.assertFalse(app.ui_error)
            await pilot.press("escape")
            await pilot.pause()
            self.assertNotIsInstance(app.screen, watch.TerminalScreen)
            self.assertEqual(app.selected_key, agent.key)
            self.assertEqual(app.focused.agent.key, agent.key)
            self.assertEqual(app.current_agents[0].pid, os.getpid())
            with patch("watch.subprocess.run", return_value=Mock(
                returncode=0, stdout=f"{os.getpid()} python\n",
            )):
                self.assertTrue(watch.is_pid_alive(os.getpid()))
        self.assertEqual(before, (log.stat().st_mtime_ns, log.read_bytes()))

    async def test_terminal_search_scroll_and_live_tail(self):
        root, log = self.deep_dir()
        agent = self.question_agent(root, log, status="running", question=None)
        app = self.fixture_app([agent])
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("slash")
            await pilot.pause()
            search = app.screen.query_one("#terminal-search", watch.Input)
            search.value = "工具"
            await pilot.pause()
            screen = app.screen
            self.assertEqual(screen.query_text, "工具")
            rendered = screen.query_one("#terminal-content", watch.Static).render()
            self.assertIn("工具完成", rendered.plain)
            self.assertIn("224,175,104", " ".join(str(span.style) for span in rendered.spans))
            await pilot.press("enter")
            await pilot.pause()
            log.write_text(log.read_text() + "即時追加一行\n")
            screen.refresh_content()
            await pilot.pause()
            self.assertIn(
                "即時追加一行",
                screen.query_one("#terminal-content", watch.Static).render().plain,
            )
            self.assertFalse(app.ui_error)

    async def test_question_popup_writes_answer_with_matching_id(self):
        root, log = self.deep_dir()
        self.write_question(root, self.now - 120)
        agent = self.question_agent(root, log)
        app = self.fixture_app([agent])
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            self.assertIn("等回答", self.screen_text(app))
            self.assertIn("已等 2m", self.screen_text(app))
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, watch.QuestionScreen)
            self.assertIn("要先修 A 還是 B？", self.screen_text(app))
            options = app.screen.query_one("#question-options", watch.OptionList)
            options.highlighted = 1
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            answer = (root / "ANSWER.md").read_text()
            self.assertIn("id: q1", answer)
            self.assertIn("choice: B", answer)
            self.assertIn("answered_at: ", answer)
            self.assertIn("note: ", answer)
            self.assertEqual(list(root.glob("*.tmp")), [])
            self.assertTrue(watch.read_task_question(root)["answered"])
            self.assertFalse(app.ui_error)

    async def test_question_note_input_and_escape_cancel(self):
        root, log = self.deep_dir()
        self.write_question(root, self.now - 60)
        agent = self.question_agent(root, log)
        app = self.fixture_app([agent])
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            await pilot.press("escape")
            self.assertFalse((root / "ANSWER.md").exists(), "Esc cancels without writing")
            await pilot.press("enter")
            await pilot.pause()
            app.screen.query_one("#question-options", watch.OptionList).highlighted = 2
            note = app.screen.query_one("#question-note", watch.Input)
            note.focus()
            note.value = "先 C"
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            answer = (root / "ANSWER.md").read_text()
            self.assertIn("choice: C", answer)
            self.assertIn("note: 先 C", answer)

    async def test_malformed_question_shows_raw_and_cannot_answer(self):
        root, log = self.deep_dir()
        self.write_question(
            root, self.now - 60,
            body="# QUESTION\nid: q9\nquestion: 壞掉的問題\n- 沒有 options 段落\n",
        )
        agent = self.question_agent(root, log)
        app = self.fixture_app([agent])
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            screen = self.screen_text(app)
            self.assertIn("問題格式錯誤", screen)
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, watch.QuestionScreen)
            self.assertFalse(app.screen.query("#question-options"), "no options for bad format")
            self.assertFalse(app.screen.query("#question-note"))
            self.assertIn("只顯示原文", self.screen_text(app))
            self.assertIn("壞掉的問題", self.screen_text(app))
            await pilot.press("escape")
            await pilot.pause()
        self.assertFalse((root / "ANSWER.md").exists())

    async def test_thirty_minute_question_turns_red_and_notifies_once(self):
        old_root, old_log = self.deep_dir()
        self.write_question(old_root, self.now - 31 * 60)
        old = self.question_agent(old_root, old_log)
        text = watch.render_task_row(old, 76, self.now)
        self.assertIn("已等 31m", text.plain)
        self.assertIn("#f7768e", " ".join(str(span.style) for span in text.spans))
        self.assertTrue(all(display_width(line) <= 76 for line in text.plain.splitlines()))

        young_root, young_log = self.deep_dir()
        self.write_question(young_root, self.now - 29 * 60)
        young = self.question_agent(young_root, young_log)
        young_text = watch.render_task_row(young, 76, self.now)
        self.assertIn("已等 29m", young_text.plain)
        self.assertIn("#e0af68", " ".join(str(span.style) for span in young_text.spans))

        app = self.fixture_app([old])
        async with app.run_test(size=(80, 40)) as pilot:
            await pilot.pause()
            app.is_first_load = False
            # The first paint records already-overdue questions without alerting;
            # this test checks the crossing itself.
            app.notified_questions.clear()
            with patch("watch.notify_question_overdue") as notify:
                app.update_data(now=self.now - 2 * 60)  # 29 minutes
                notify.assert_not_called()
                app.update_data(now=self.now)  # 31 minutes
                self.assertEqual(notify.call_count, 1)
                app.update_data(now=self.now + 30)
                self.assertEqual(notify.call_count, 1, "one alert per question id")
            self.assertIn("已等 31m", self.screen_text(app))

    def test_question_and_escalation_colors_stay_distinct(self):
        root, log = self.deep_dir()
        self.write_question(root, self.now - 60)
        pending = self.question_agent(root, log)
        row = watch.render_task_row(pending, 100, self.now)
        self.assertEqual(row.style, watch.QUESTION_PENDING_COLOR)
        self.assertIn("等回答", row.plain)
        self.assertIn("已等 1m", row.plain)

        (root / "ANSWER.md").write_text(
            "# ANSWER\nid: q1\nanswered_at: 2026-10-25T09:59:30Z\n"
            "choice: ESCALATE\nescalate_to: claude-code\nnote: 交給上層\n"
        )
        escalated = self.question_agent(root, log, status="escalated",
                                        question=watch.read_task_question(root))
        row = watch.render_task_row(escalated, 100, self.now)
        self.assertEqual(row.style, watch.ESCALATED_COLOR)
        self.assertIn("已轉介", row.plain)
        self.assertIn("已轉介給 claude-code，等上層回答", row.plain)
        self.assertNotIn("已等", row.plain)

        # Other needs-attention items keep the old red; an overdue question does too.
        failed = AgentInfo("failed-task", Path("/dev/null"), Path("/dev/null"), "failed",
                           "fixture", self.now - 60, "00:01:00", "失敗", key="failed")
        self.assertEqual(watch.render_task_row(failed, 100, self.now).style, "#f7768e")
        old_root, old_log = self.deep_dir()
        self.write_question(old_root, self.now - 31 * 60)
        overdue = self.question_agent(old_root, old_log)
        self.assertEqual(watch.render_task_row(overdue, 100, self.now).style,
                         watch.QUESTION_OVERDUE_COLOR)

        answered = self.question_agent(root, log, status="blocked",
                                       question=watch.read_task_question(root))
        answered_text = watch.render_task_row(answered, 100, self.now, answered_by="claude-code")
        self.assertIn("由 claude-code 回答", answered_text.plain)

    async def test_question_popup_escalates_with_reserved_choice(self):
        root, log = self.deep_dir()
        self.write_question(root, self.now - 60)
        agent = self.question_agent(root, log)
        app = self.fixture_app([agent])
        async with app.run_test(size=(100, 44)) as pilot:
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            self.assertIsInstance(app.screen, watch.QuestionScreen)
            options = app.screen.query_one("#question-options", watch.OptionList)
            self.assertEqual(options.option_count, 5)
            self.assertIn("轉給上層", self.screen_text(app))
            options.highlighted = 3  # escalate-coach
            await pilot.pause()
            await pilot.press("enter")
            await pilot.pause()
            answer = (root / "ANSWER.md").read_text()
            self.assertIn("id: q1", answer)
            self.assertIn("choice: ESCALATE", answer)
            self.assertIn("escalate_to: coach", answer)
            self.assertIn("已轉介給 coach", app.state_message)
            self.assertEqual(app.state.task(agent.key)["escalate_to"], "coach")
            self.assertFalse(app.ui_error)

    async def test_board_names_the_upper_agent_that_answered(self):
        root, log = self.deep_dir()
        self.write_question(root, self.now - 60)
        agent = self.question_agent(root, log)
        app = self.fixture_app([agent])
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            state_path = app.state.path
            self.assertTrue(app.state.remember_escalation(
                agent, watch.read_task_question(root), "claude-code",
            ))
        (root / "ANSWER.md").write_text(
            "# ANSWER\nid: q1\nanswered_at: 2026-10-25T09:59:30Z\nchoice: A\nnote: 上層決定\n"
        )
        answered = self.question_agent(root, log, status="blocked",
                                       question=watch.read_task_question(root))
        app = self.fixture_app([answered])
        app.state = watch.WatchState(state_path)
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            text = self.screen_text(app)
            self.assertIn("由 claude-code 回答", text)
            self.assertNotIn("已轉介給", text)
            self.assertFalse(app.ui_error)

    async def test_escalated_question_stops_notifications_and_shows_legend(self):
        root, log = self.deep_dir()
        self.write_question(root, self.now - 31 * 60)
        question = watch.read_task_question(root)
        self.assertTrue(watch.write_answer_file(
            root, question, "ESCALATE", "交給上層", now=self.now - 31 * 60,
        )[0])
        escalated = self.question_agent(root, log, status="escalated",
                                        question=watch.read_task_question(root))
        app = self.fixture_app([escalated])
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            app.is_first_load = False
            app.notified_questions.clear()
            with patch("watch.notify_question_overdue") as notify:
                app.update_data(now=self.now)
                app.update_data(now=self.now + 60)
                notify.assert_not_called()
            text = self.screen_text(app)
            self.assertIn("已轉介", text)
            self.assertIn("等博元回答", text, "the legend explains the colors")
            self.assertIn("已轉介等上層", text)
            self.assertFalse(app.ui_error)

    async def test_s5_fixture_snapshots_and_registry_mtimes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            runs, done = root / "agent-runs.tsv", root / "agent-runs-done.tsv"
            runs.write_text("")
            done.write_text("")
            before = [(p.stat().st_mtime_ns, p.read_bytes()) for p in (runs, done)]
            shots = Path(__file__).parent / "shots" if os.environ.get(
                "AGENT_WATCH_UPDATE_S5_SHOTS") else root / "shots"
            shots.mkdir(parents=True, exist_ok=True)
            for width in (80, 120):
                cases = []
                work_root, work_log = self.deep_dir()
                self.write_question(work_root, self.now - 31 * 60)
                cases.append(("work-question", self.question_agent(work_root, work_log)))
                term_root, term_log = self.deep_dir()
                cases.append(("terminal", self.question_agent(
                    term_root, term_log, status="running", question=None,
                )))
                q_root, q_log = self.deep_dir()
                self.write_question(q_root, self.now - 300)
                cases.append(("question", self.question_agent(q_root, q_log)))
                for name, agent in cases:
                    app = self.fixture_app([agent])
                    app.runs_path, app.done_path = runs, done
                    with patch("watch.load_all_agents", side_effect=AssertionError("No live registry")), \
                         patch("watch.subprocess.run", side_effect=AssertionError("No live service")):
                        async with app.run_test(size=(width, 40)) as pilot:
                            await pilot.pause()
                            if name != "work-question":
                                await pilot.press("enter")
                                await pilot.pause()
                            text = self.screen_text(app)
                            self.assertEqual(len(text.splitlines()), 40)
                            self.assertTrue(all(display_width(line) <= width for line in text.splitlines()))
                            self.assertFalse(app.ui_error)
                            if name == "terminal":
                                self.assertIn("白話第一句", text)
                            elif name == "question":
                                self.assertIn("要先修 A 還是 B？", text)
                            else:
                                self.assertIn("已等 31m", text)
                            (shots / ("v10-s5-%s-%dx40.txt" % (name, width))).write_text(
                                "\n".join(line.rstrip() for line in text.splitlines()) + "\n",
                                encoding="utf-8",
                            )
            self.assertEqual(before, [(p.stat().st_mtime_ns, p.read_bytes()) for p in (runs, done)])
            print("v10-s5 fixtures isolated; TSV bytes and mtime_ns UNCHANGED")


if __name__ == "__main__":
    unittest.main()
