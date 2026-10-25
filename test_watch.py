#!/usr/bin/env python3
"""Unit tests for agent-watch v7 status logic, tabs, quota, and screen output."""

import asyncio
import contextlib
import html
import io
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
    get_quota_color,
    get_quota_color_and_tag,
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
    render_quota_bar,
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


class TestAgentWatch(unittest.TestCase):

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

            with patch("watch.is_pid_alive", return_value=False):
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
        )
        # Log activity after this report makes it stale for the current run.
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

            with patch("watch.is_pid_alive", return_value=True):
                agents, _ = load_all_agents(runs, done, LogParser(), now)

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

            with patch("watch.is_pid_alive", return_value=False):
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
            self.assertEqual(format_completion_time(0), "08:00")

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
            self.assertRegex(render_done_tasks([later]), r"完成：\d{2}:\d{2}")

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
            ("example-project build", "/tmp/example-project/run.log", "example-project"),
            ("pa 合併後狀態同步", "/tmp/pa-sync/run.log", "project-a"),
            ("shortcut 編譯修復", "/tmp/shortcut-diag/run.log", "project-b"),
            ("kanban 卡片回報規則", "/tmp/project-c-report/run.log", "project-c"),
            ("quota 新增工作組", "/tmp/quota/run.log", "project-d"),
            ("pair task", "/tmp/other/run.log", "other"),
            ("PR #41 第二輪修正", "/tmp/fix2-pr41/run.log", "project-a"),
        )
        for name, log_path, expected in cases:
            with self.subTest(name=name):
                self.assertEqual(infer_project(name, Path(log_path)), expected)

    def test_recent_done_is_top_highlighted_and_grouped(self):
        now = 1_700_200_000.0
        recent_agent = AgentInfo(
            name="example-project 剛完成",
            log_path=Path("/tmp/example-project/run.log"),
            last_path=Path("/tmp/example-project/LAST.md"),
            status="complete",
            model="model-x",
            start_time=now - 300,
            elapsed="00:05:00",
            latest="DONE",
            harness="Codex",
            completion_time=now - 60,
            project="example-project",
        )
        older_agent = AgentInfo(
            name="project-d 舊完成",
            log_path=Path("/tmp/project-d/run.log"),
            last_path=Path("/tmp/project-d/LAST.md"),
            status="complete",
            model="model-x",
            start_time=now - 20_000,
            elapsed="01:00:00",
            latest="DONE",
            harness="Codex",
            completion_time=now - RECENT_DONE_SECONDS - 1,
            project="project-d",
        )
        recent_magpie_agent = AgentInfo(
            name="project-d 也剛完成",
            log_path=Path("/tmp/project-d/run.log"),
            last_path=Path("/tmp/project-d/LAST.md"),
            status="complete",
            model="model-x",
            start_time=now - 300,
            elapsed="00:05:00",
            latest="DONE",
            harness="Codex",
            completion_time=now - 120,
            project="project-d",
        )
        recent, older = split_recent_done([older_agent, recent_magpie_agent, recent_agent], now)
        self.assertEqual(
            [agent.name for agent in recent],
            ["example-project 剛完成", "project-d 也剛完成"],
        )
        self.assertEqual([agent.name for agent in older], ["project-d 舊完成"])

        markup = render_recent_done_tasks([older_agent, recent_magpie_agent, recent_agent], now=now)
        self.assertIn("剛完成", markup)
        self.assertIn("example-project (1)", markup)
        self.assertIn("剛完成 [/]", markup)
        self.assertLess(markup.index("example-project 剛完成"), markup.index("project-d 也剛完成"))
        self.assertNotIn("project-d 舊完成", markup)
        self.assertIn("Project D (1)", render_done_tasks(older))

    def test_future_completion_within_clock_skew_stays_recent(self):
        now = time.time()
        agent = AgentInfo(
            name="project-d 時鐘偏差",
            log_path=Path("/tmp/project-d/run.log"),
            last_path=Path("/tmp/project-d/LAST.md"),
            status="complete",
            model="model-x",
            start_time=now - 300,
            elapsed="00:05:00",
            latest="DONE",
            harness="Codex",
            completion_time=now + 60,
            project="project-d",
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
                ("example-project task", "example-project"),
                ("project-d task", "project-d"),
            )
        ]
        markup = render_active_flow(agents)
        self.assertIn("example-project (1)", markup)
        self.assertIn("Project D (1)", markup)
        self.assertLess(markup.index("example-project (1)"), markup.index("Project D (1)"))

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

            with patch("watch.is_pid_alive", return_value=True):
                agents, errors = load_all_agents(runs, done, LogParser(), now)

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

            with patch("watch.is_pid_alive", return_value=False):
                agents, errors = load_all_agents(runs, done, LogParser(), now)
            agent = agents[0]
            markup = render_done_tasks(agents)

            self.assertEqual(errors, [])
            self.assertEqual(agent.status, "reported")
            self.assertEqual(agent.last_path, report)
            self.assertEqual(agent.stage, "P2 回歸驗證中")
            self.assertIn("目前階段：P2 回歸驗證中", markup)
            self.assertRegex(markup, r"完成：\d{2}:\d{2}")

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

            with patch("watch.is_pid_alive", return_value=False):
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

    def test_three_tabs_exist_and_switchable(self):
        """Verify three tabs exist (Work, Architecture, Quota) and can be switched via keys 1/2/3 headlessly."""
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
                    # Check TabbedContent and 3 tabs exist
                    tc = app.query_one("#main-tabs", TabbedContent)
                    self.assertIsNotNone(tc)
                    self.assertIsNotNone(app.query_one("#tab-work"))
                    self.assertIsNotNone(app.query_one("#tab-arch"))
                    self.assertIsNotNone(app.query_one("#tab-quota"))

                    # Default active tab is 'tab-work'
                    self.assertEqual(tc.active, "tab-work")

                    # Press '2' to switch to Architecture tab
                    await pilot.press("2")
                    self.assertEqual(tc.active, "tab-arch")
                    self.assertEqual(app.architecture_view_id, "now")
                    self.assertEqual(app.architecture_project_id, "example-project")
                    await pilot.press("p")
                    self.assertEqual(app.architecture_project_id, "project-a")
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
                    if tab_id != "tab-work":
                        await pilot.press({"tab-arch": "2", "tab-quota": "3"}[tab_id])
                    if filename.startswith("arch-"):
                        await pilot.press("f" if "future" in filename else "n")
                    await pilot.pause()
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
        self.assertIn("Coordinator", arch_text)
        self.assertIn("Mobile entry", arch_text)
        self.assertIn("watcher.sh", arch_text)
        self.assertIn("┌", arch_text)
        self.assertIn("▼", arch_text)
        self.assertIn("未來", future_text)
        self.assertIn("Planner", future_text)
        self.assertIn("┌", future_text)
        self.assertIn("▼", future_text)
        self.assertIn("現在", arch80_text)
        self.assertIn("Coordinator", arch80_text)
        self.assertIn("┌", arch80_text)
        self.assertIn("▼", arch80_text)
        self.assertIn("Planner", future80_text)
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
            empty, width=100, view_id="future", project_id="example-project"
        )
        self.assertIn("Planner", wide)
        self.assertIn("Reviewer", wide)

        normal = render_architecture_view(
            empty, width=80, view_id="future", project_id="example-project"
        )
        self.assertIn("Planner", normal)
        self.assertIn("Reviewer", normal)
        for line in normal.splitlines():
            plain = re.sub(r"\[/?[^\[\]]*\]", "", line)
            self.assertLessEqual(display_width(plain), 80)

        narrow = render_architecture_view(
            empty, width=26, view_id="future", project_id="example-project"
        )
        self.assertIn("Planner", narrow)
        self.assertIn("┌", narrow)

        now_80 = render_architecture_view(
            empty, width=80, view_id="now", project_id="example-project"
        )
        self.assertIn("Worker A", now_80)
        self.assertIn("Worker B", now_80)
        self.assertIn("Worker C", now_80)
        self.assertIn("Coordinator", now_80)
        self.assertIn("watcher.sh", now_80)

    def test_architecture_node_status(self):
        """Verify worker state, model, role and progress come from TSV agents."""
        dummy_agents = [
            AgentInfo(
                name="example-project Worker A",
                log_path=Path("/dev/null"),
                last_path=Path("/dev/null"),
                status="running",
                harness="Agent A",
                model="model-x",
                start_time=100.0,
                elapsed="00:10:00",
                latest="修正架構圖",
                stage="P4 架構",
                project="example-project",
            ),
            AgentInfo(
                name="example-project Worker B",
                log_path=Path("/dev/null"),
                last_path=Path("/dev/null"),
                status="complete",
                harness="Agent B",
                model="model-y",
                start_time=100.0,
                elapsed="00:08:00",
                latest="主要實作完成",
                stage="P3 實作",
                completion_time=time.time() - 120,
                project="example-project",
            ),
            AgentInfo(
                name="example-project Worker C audit",
                log_path=Path("/dev/null"),
                last_path=Path("/dev/null"),
                status="running",
                harness="Agent C",
                model="model-z",
                start_time=100.0,
                elapsed="00:05:00",
                latest="updating parser",
                stage="P2 調查",
                project="example-project",
            ),
            AgentInfo(
                name="example-project Reviewer",
                log_path=Path("/dev/null"),
                last_path=Path("/dev/null"),
                status="complete",
                harness="Reviewer",
                model="model-w",
                start_time=100.0,
                elapsed="00:03:00",
                latest="done",
                completion_time=time.time() - 60,
                project="example-project",
            ),
        ]

        worker_a_active, worker_a_count, worker_a_text = get_worker_status("worker-a", dummy_agents)
        self.assertTrue(worker_a_active)
        self.assertEqual(worker_a_count, 1)
        self.assertIn("執行中", worker_a_text)
        self.assertTrue(is_node_active("worker-a", dummy_agents))

        reviewer_active, reviewer_count, reviewer_text = get_worker_status("reviewer", dummy_agents)
        self.assertFalse(reviewer_active)
        self.assertEqual(reviewer_count, 0)
        self.assertIn("待命中", reviewer_text)
        self.assertFalse(is_node_active("reviewer", dummy_agents))

        worker_c_active, worker_c_count, _ = get_worker_status("worker-c", dummy_agents)
        self.assertTrue(worker_c_active)
        self.assertEqual(worker_c_count, 1)
        self.assertTrue(is_node_active("worker-c", dummy_agents))

        now_text = render_architecture_view(
            dummy_agents, width=80, view_id="now", project_id="example-project"
        )
        wide_text = render_architecture_view(
            dummy_agents, width=180, view_id="now", project_id="example-project"
        )
        future_text = render_architecture_view(
            dummy_agents, width=80, view_id="future", project_id="example-project"
        )
        self.assertIn("Coordinator", now_text)
        self.assertIn("Mobile entry", now_text)
        self.assertIn("watcher.sh", now_text)
        self.assertIn("● running", now_text)
        self.assertIn("✓ done", now_text)
        self.assertIn("P4 架構", now_text)
        self.assertIn("model-x", wide_text)
        self.assertIn("實作與修正", wide_text)
        self.assertIn("model-z", wide_text)
        self.assertIn("獨立審查", wide_text)
        self.assertIn("最近事件", now_text)
        self.assertIn("修正架構圖", now_text)
        self.assertIn("┌", now_text)
        self.assertIn("▼", now_text)
        self.assertIn("▲", now_text)
        self.assertIn("Planner", future_text)
        self.assertIn("Reviewer", future_text)
        self.assertIn("Worker D", future_text)
        self.assertIn("┌", future_text)
        self.assertIn("▼", future_text)

    def test_architecture_json_is_external_and_has_both_views(self):
        data = load_architecture_data(Path(__file__).with_name("arch.json"))
        self.assertEqual(data, ARCHITECTURE_DATA)
        self.assertEqual(set(data["architectures"]), {"now", "future"})
        self.assertEqual(
            set(data["projects"]),
            {"example-project", "project-a", "project-b", "project-c", "project-d",
             "personal-ai-os", "agent-watch", "ios-shortcuts", "K1", "other"},
        )
        self.assertEqual(
            data["architectures"]["now"]["layers"][-1],
            ["worker-a", "worker-b", "worker-c", "reviewer"],
        )
        self.assertEqual(
            data["architectures"]["now"]["nodes"]["worker-b"]["model"],
            "model-y",
        )
        self.assertEqual(
            data["architectures"]["future"]["layers"][2],
            ["worker-a", "worker-c", "worker-d"],
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
                self.assertIn("Coordinator", rendered)
                self.assertIn("Worker A", rendered)
                self.assertIn("最近事件", rendered)
                self.assertIn("┌", rendered)
                self.assertIn("▼", rendered)

    def test_quota_thresholds(self):
        """Verify quota color thresholds: <= 80% Green, > 80% Yellow, > 85% Red."""
        # Standard green (<= 80%)
        self.assertEqual(get_quota_color(0.0), "green")
        self.assertEqual(get_quota_color(50.0), "green")
        self.assertEqual(get_quota_color(80.0), "green")
        color, hex_code = get_quota_color_and_tag(80.0)
        self.assertEqual(color, "green")
        self.assertEqual(hex_code, "#9ece6a")

        # Yellow (> 80% and <= 85%)
        self.assertEqual(get_quota_color(80.1), "yellow")
        self.assertEqual(get_quota_color(82.0), "yellow")
        self.assertEqual(get_quota_color(85.0), "yellow")
        color, hex_code = get_quota_color_and_tag(85.0)
        self.assertEqual(color, "yellow")
        self.assertEqual(hex_code, "#e0af68")

        # Red (> 85%)
        self.assertEqual(get_quota_color(85.1), "red")
        self.assertEqual(get_quota_color(90.0), "red")
        self.assertEqual(get_quota_color(100.0), "red")
        color, hex_code = get_quota_color_and_tag(90.0)
        self.assertEqual(color, "red")
        self.assertEqual(hex_code, "#f7768e")

        # Bar chart contains threshold colors
        bar_green = render_quota_bar(50.0, 10)
        self.assertIn("#9ece6a", bar_green)

        bar_yellow = render_quota_bar(82.0, 10)
        self.assertIn("#e0af68", bar_yellow)

        bar_red = render_quota_bar(89.0, 10)
        self.assertIn("#f7768e", bar_red)


class StageOneScreenTests(unittest.TestCase):
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
            with patch("watch.is_pid_alive", return_value=True), patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout=payload)):
                with contextlib.redirect_stdout(output):
                    self.assertEqual(watch.run_once_summary(runs, done), 0)
            text = output.getvalue()
            for name in ("Claude", "Codex", "OpenCode Go", "OpenRouter", active_name):
                self.assertIn(name, "\n".join(text.splitlines()[:25]))
            self.assertIn("需處理 0 │ 進行中 1 │ 剛完成(2h) 1 │ 歷史 0 │ 總計 2", text)
            self.assertIn("已回報", text)
            self.assertIn("帳戶餘額 $19.58", text)
            self.assertNotIn("Harness", text)


class V10ScreenAndWorkerTests(unittest.TestCase):
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
                            for key, expected in (("2", "tab-arch"), ("3", "tab-quota"), ("1", "tab-work")):
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
                            await pilot.press("2")
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
                        project="example-project", completion_time=time.time() - 8000)
        self.assertNotIn("old task must disappear", render_architecture_view([old], 120))


if __name__ == "__main__":
    unittest.main()
