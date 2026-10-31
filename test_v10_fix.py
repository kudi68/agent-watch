import json
import os
import stat
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from unittest.mock import Mock, patch

import watch


class TsvNoteTests(unittest.TestCase):
    def load_row(self, fields):
        directory = tempfile.TemporaryDirectory()
        self.addCleanup(directory.cleanup)
        root = Path(directory.name)
        log = root / "run.log"
        report = root / "REPORT.md"
        log.write_text("work\n", encoding="utf-8")
        report.write_text("DONE\n", encoding="utf-8")
        row = [fields[0], str(log), str(report), *fields[3:]]
        path = root / "runs.tsv"
        path.write_text("\t".join(row) + "\n", encoding="utf-8")
        return watch.load_tsv_agents(
            path, True, watch.LogParser(process_start_reader=None), 1_800_000_000,
        )

    def test_nine_columns_load_note_without_format_error(self):
        fields = ["task-a", "", "", "", "", "", "", "", "ordinary note"]
        agents, errors = self.load_row(fields)
        self.assertEqual(errors, [])
        self.assertEqual(len(agents), 1)
        self.assertEqual(agents[0].note, "ordinary note")

    def test_superseded_note_extracts_replacement_name(self):
        fields = ["task-a", "", "", "", "", "", "", "", "SUPERSEDED(by x; q1=C)"]
        agents, errors = self.load_row(fields)
        self.assertEqual(errors, [])
        self.assertEqual(agents[0].superseded_hint, "x")

    def test_three_and_eight_column_rows_remain_compatible(self):
        for width in (3, 8):
            fields = ["task-a", "", "", "", "", "", "", ""][:width]
            with self.subTest(width=width):
                agents, errors = self.load_row(fields)
                self.assertEqual(errors, [])
                self.assertEqual(len(agents), 1)
                self.assertEqual(agents[0].note, "")
                self.assertEqual(agents[0].superseded_hint, "")

    def test_ten_columns_are_rejected(self):
        fields = ["task-a", "", "", "", "", "", "", "", "note", "extra"]
        agents, errors = self.load_row(fields)
        self.assertEqual(agents, [])
        self.assertEqual(len(errors), 1)
        self.assertIn("需 3 至 9 欄", errors[0])


class ProcessInventoryTests(unittest.TestCase):
    def test_load_all_agents_reads_pgrep_once_for_100_rows_and_skips_done_pid(self):
        with tempfile.TemporaryDirectory(prefix="aw-pid-cache-tests-") as tmp:
            root = Path(tmp)
            runs, done = root / "runs.tsv", root / "done.tsv"
            pids = list(range(70000, 70099))
            done_pid = 70099
            runs.write_text(
                "".join(f"run-{index}\t/dev/null\t/dev/null\t{pid}\n"
                        for index, pid in enumerate(pids)),
                encoding="utf-8",
            )
            report = root / "done.md"
            report.write_text("DONE\n", encoding="utf-8")
            done.write_text(f"done-run\t/dev/null\t{report}\t{done_pid}\n",
                            encoding="utf-8")
            output = "".join(f"{pid} worker\n" for pid in (*pids, done_pid))
            with patch("watch.subprocess.run", return_value=Mock(
                returncode=0, stdout=output,
            )) as run:
                agents, errors = watch.load_all_agents(
                    runs, done, watch.LogParser(process_start_reader=None), 1_800_000_000,
                )

        self.assertEqual(errors, [])
        self.assertEqual(len(agents), 100)
        self.assertEqual(run.call_count, 1)
        self.assertEqual(run.call_args.args[0], ["pgrep", "-l", "."])
        active = next(agent for agent in agents if agent.name == "run-0")
        archived = next(agent for agent in agents if agent.name == "done-run")
        self.assertTrue(active.process_alive)
        self.assertEqual(active.status, "running")
        self.assertFalse(archived.process_alive)
        self.assertEqual(archived.status, "complete")


class AttentionStateTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="aw-attention-tests-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.state_path = self.root / "state.json"
        self.state = watch.WatchState(self.state_path)
        self.now = 1_800_000_000

    def test_orphan_state_cleanup_waits_for_registry_and_backs_up_before_pruning(self):
        old = datetime.fromtimestamp(self.now - 31 * 86400).astimezone().isoformat()
        recent = datetime.fromtimestamp(self.now - 29 * 86400).astimezone().isoformat()
        initial = {
            "version": 1,
            "tasks": {
                "orphan-old": {"read": True, "updated_at": old},
                "present-old": {"hidden": True, "updated_at": old},
                "orphan-recent": {"updated_at": recent},
                "orphan-invalid": {"updated_at": "not-a-date"},
            },
            "projects": {},
        }
        original = (json.dumps(initial, ensure_ascii=False) + "\n").encode()
        self.state_path.write_bytes(original)
        os.chmod(self.state_path, 0o600)
        original_mode = stat.S_IMODE(self.state_path.stat().st_mode)
        state = watch.WatchState(self.state_path)
        present = self.agent("present-old", "still-listed", "complete",
                             completion=self.now - 60)

        state.reconcile([present], self.now)
        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(list(self.root.glob("state.json.bak-*")), [])

        state.reconcile([present], self.now, registry_complete=True)
        backup = self.state_path.with_name(
            self.state_path.name + ".bak-" + datetime.fromtimestamp(self.now).astimezone().strftime("%Y%m%d")
        )
        self.assertEqual(backup.read_bytes(), original)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), original_mode)
        saved = json.loads(self.state_path.read_text(encoding="utf-8"))
        self.assertNotIn("orphan-old", saved["tasks"])
        self.assertIn("present-old", saved["tasks"])
        self.assertIn("orphan-recent", saved["tasks"])
        self.assertIn("orphan-invalid", saved["tasks"])

    def test_cleanup_does_not_overwrite_existing_backup_or_delete_entries(self):
        old = datetime.fromtimestamp(self.now - 31 * 86400).astimezone().isoformat()
        initial = {"version": 1, "tasks": {"orphan": {"updated_at": old}}, "projects": {}}
        original = (json.dumps(initial) + "\n").encode()
        self.state_path.write_bytes(original)
        backup = self.state_path.with_name(
            self.state_path.name + ".bak-" + datetime.fromtimestamp(self.now).astimezone().strftime("%Y%m%d")
        )
        backup.write_bytes(b"existing backup\n")
        state = watch.WatchState(self.state_path)

        state.reconcile([], self.now, registry_complete=True)

        self.assertEqual(self.state_path.read_bytes(), original)
        self.assertEqual(backup.read_bytes(), b"existing backup\n")
        self.assertIn("當日備份已存在", state.warning)
        self.assertIn("orphan", state.data["tasks"])

    def test_cleanup_leaves_corrupt_or_changed_state_file_untouched(self):
        old = datetime.fromtimestamp(self.now - 31 * 86400).astimezone().isoformat()
        corrupt = b"{broken\n"
        self.state_path.write_bytes(corrupt)
        state = watch.WatchState(self.state_path)
        state.data["tasks"]["orphan"] = {"updated_at": old}
        state.reconcile([], self.now, registry_complete=True)
        self.assertEqual(self.state_path.read_bytes(), corrupt)
        self.assertEqual(list(self.root.glob("state.json.bak-*")), [])
        self.assertIn("不可讀", state.warning)

        state_path = self.root / "changed.json"
        original = {"version": 1, "tasks": {"orphan": {"updated_at": old}}, "projects": {}}
        state_path.write_text(json.dumps(original), encoding="utf-8")
        changed_state = watch.WatchState(state_path)
        external = {"version": 1, "tasks": {"newer": {"updated_at": "2026-10-26T00:00:00+08:00"}},
                    "projects": {}}
        external_bytes = (json.dumps(external) + "\n").encode()
        state_path.write_bytes(external_bytes)
        changed_state.reconcile([], self.now, registry_complete=True)
        self.assertEqual(state_path.read_bytes(), external_bytes)
        self.assertEqual(list(self.root.glob("changed.json.bak-*")), [])
        self.assertIn("已變更", changed_state.warning)

    def test_stale_hours_override_and_invalid_values_use_default(self):
        cases = {"12.5": 45_000, "0.5": 1_800, "bad": 86_400,
                 "-1": 86_400, "inf": 86_400, "1e308": 86_400}
        for raw, expected in cases.items():
            with self.subTest(raw=raw), patch.dict(os.environ, {
                "AGENT_WATCH_STALE_HOURS": raw,
            }):
                self.assertEqual(watch._stale_attention_seconds_from_env(), expected)

    def agent(self, key, name, status, *, project=watch.PROJECT_OTHER,
              start=None, completion=None, log_mtime=None,
              question=None, process_alive=None, superseded_hint=""):
        return watch.AgentInfo(
            name=name,
            log_path=self.root / (key + ".log"),
            last_path=self.root / (key + ".md"),
            status=status,
            model="test",
            start_time=start,
            elapsed="",
            latest=name,
            log_mtime=log_mtime or self.now - 60,
            key=key,
            completion_time=completion,
            question=question,
            process_alive=process_alive,
            superseded_hint=superseded_hint,
            project=project,
        )

    def orphan_question(self, name, age_seconds, answer_content=None):
        root = self.root / name
        root.mkdir()
        question_path = root / "QUESTION.md"
        question_path.write_text(
            "# QUESTION\nid: q1\nquestion: 要先修 A 還是 B？\n"
            "options:\n- A: 先修 A\n- B: 先修 B\n",
            encoding="utf-8",
        )
        os.utime(question_path, (self.now - age_seconds, self.now - age_seconds))
        answer_path = root / "ANSWER.md"
        if answer_content is not None:
            answer_path.write_text(answer_content, encoding="utf-8")
        return root, question_path, answer_path, watch.read_task_question(root)

    def test_old_failures_expire_but_recent_failures_stay_attention(self):
        old = self.agent("old", "old-task", "failed", completion=self.now - 48 * 3600)
        recent = self.agent("recent", "recent-task", "failed", completion=self.now - 23 * 3600)
        self.state.reconcile([old, recent], self.now)
        self.assertEqual(watch.board_bucket(old, self.now), "attention")
        self.assertEqual(self.state.bucket(old, self.now), "history")
        self.assertEqual(self.state.stale_info(old, self.now), "已過期")
        self.assertEqual(self.state.bucket(recent, self.now), "attention")
        self.assertEqual(self.state.stale_info(recent, self.now), "")

    def test_explicit_hint_uses_successful_replacement_but_not_failed_one(self):
        old = self.agent("old", "k1-s4s5", "failed", start=self.now - 500,
                         completion=self.now - 400, superseded_hint="k1-s4s5-claude")
        failed_later = self.agent("later", "k1-s4s5-claude", "failed",
                                  start=self.now - 300, completion=self.now - 200)
        self.assertEqual(watch.compute_superseded([old, failed_later]), {})
        successful_later = self.agent("later-ok", "k1-s4s5-claude", "complete",
                                      start=self.now - 300, completion=self.now - 100)
        mapping = watch.compute_superseded([old, successful_later])
        self.assertEqual(mapping, {"old": "later-ok"})
        self.state.reconcile([old, successful_later], self.now)
        self.assertEqual(self.state.stale_info(old, self.now), "已接手完成 → k1-s4s5-claude")

    def test_retry_suffix_replacement_and_review_number_guard(self):
        original = self.agent("foo", "foo", "failed", project="agent-watch",
                              start=self.now - 500,
                              completion=self.now - 400)
        retry = self.agent("foo-retry", "foo-retry", "complete",
                           project="agent-watch", start=self.now - 300,
                           completion=self.now - 100)
        review2 = self.agent("review2", "foo-review2", "complete",
                             project="agent-watch", completion=self.now - 50)
        review = self.agent("review", "foo-review", "complete",
                            project="agent-watch", completion=self.now - 20)
        mapping = watch.compute_superseded([original, retry, review2, review])
        self.assertEqual(mapping, {"foo": "foo-retry"})

    def test_dismiss_persists_and_restore_clears_dismissed(self):
        agent = self.agent("failed", "fresh-failure", "failed",
                           completion=self.now - 60)
        self.state.reconcile([agent], self.now)
        raw_reason = "  retry\tfailed\nagain\r" + "x" * 210
        ok, _ = self.state.apply(
            "dismiss", agent, [agent], self.now, reason=raw_reason,
        )
        self.assertTrue(ok)
        self.assertTrue(self.state.task(agent.key)["dismissed"])
        self.assertEqual(len(self.state.task(agent.key)["dismissed_reason"]), 200)
        self.assertTrue(
            self.state.task(agent.key)["dismissed_reason"].startswith("retryfailedagain"),
        )
        reloaded = watch.WatchState(self.state_path)
        reloaded.reconcile([agent], self.now)
        self.assertEqual(reloaded.bucket(agent, self.now), "history")
        self.assertEqual(reloaded.stale_info(agent, self.now), "已清除")
        self.assertTrue(reloaded.apply("restore", agent, [agent], self.now)[0])
        self.assertFalse(reloaded.task(agent.key)["dismissed"])
        self.assertEqual(reloaded.task(agent.key)["dismissed_reason"], "")
        self.assertEqual(reloaded.bucket(agent, self.now), "attention")

    def test_new_question_id_clears_old_dismissal_signature(self):
        question = {"id": "q1", "asked_at_ts": self.now - 60, "answered": False}
        agent = self.agent("question", "question-task", "question", question=question)
        self.state.reconcile([agent], self.now)
        ok, message = self.state.apply("dismiss", agent, [agent], self.now)
        self.assertFalse(ok)
        self.assertIn("尚未收到這題的有效 ANSWER.md", message)
        agent.question = {"id": "q2", "asked_at_ts": self.now - 30, "answered": False}
        self.state.reconcile([agent], self.now)
        self.assertEqual(self.state.bucket(agent, self.now), "attention")

    def test_malformed_question_card_is_attention_but_can_be_dismissed(self):
        question = {"id": "q-bad", "asked_at_ts": self.now - 60, "answered": False,
                    "error": "缺少 options 或沒有可用選項"}
        self.assertTrue(watch.question_is_malformed(question))
        self.assertFalse(watch.question_is_active(question, self.now - 60))
        agent = self.agent("malformed", "malformed-task", "failed",
                           completion=self.now - 60, question=question)
        self.state.reconcile([agent], self.now)
        self.assertEqual(self.state.bucket(agent, self.now), "attention")
        self.assertEqual(self.state.stale_info(agent, self.now), "")

        ok, message = self.state.apply("dismiss", agent, [agent], self.now)
        self.assertTrue(ok, "a malformed question must not be unclearable")
        self.assertIn("malformed", message)
        self.assertEqual(self.state.bucket(agent, self.now), "history")

    def test_running_rerun_clears_dismissal_and_running_cannot_be_dismissed(self):
        agent = self.agent("rerun", "rerun-task", "failed", start=self.now - 500,
                           completion=self.now - 400)
        self.state.reconcile([agent], self.now)
        self.assertTrue(self.state.apply(
            "dismiss", agent, [agent], self.now, reason="rerun reason",
        )[0])
        self.assertEqual(self.state.task(agent.key)["dismissed_reason"], "rerun reason")
        agent.status = "running"
        agent.start_time = self.now
        agent.process_alive = True
        self.state.reconcile([agent], self.now)
        self.assertFalse(self.state.task(agent.key)["dismissed"])
        self.assertEqual(self.state.task(agent.key)["dismissed_reason"], "")
        self.assertEqual(self.state.bucket(agent, self.now), "running")
        ok, _ = self.state.apply("dismiss", agent, [agent], self.now)
        self.assertFalse(ok)

    def test_orphan_question_older_than_24_hours_remains_attention(self):
        question = {"id": "q-orphan", "asked_at_ts": self.now - 25 * 3600,
                    "answered": False}
        agent = self.agent("orphan", "orphan-task", "question", question=question)
        self.state.reconcile([agent], self.now)
        self.assertTrue(watch.question_is_active(question, agent.completion_time))
        self.assertEqual(self.state.bucket(agent, self.now), "attention")
        self.assertEqual(self.state.stale_info(agent, self.now), "")
        self.state.update_task(
            agent.key, dismissed=True,
            dismissed_sig=self.state.dismissed_signature(agent),
            run_start=agent.start_time,
        )
        self.assertEqual(self.state.bucket(agent, self.now), "attention")
        self.assertEqual(self.state.stale_info(agent, self.now), "")
        self.assertFalse(self.state.apply("dismiss", agent, [agent], self.now)[0])
        self.assertEqual(self.state.dismiss_stale([agent], self.now), 0)

    def test_old_state_without_dismissed_and_corrupt_state_load_safely(self):
        self.state_path.write_text(
            '{"version":1,"tasks":{"old":{"hidden":false}},"projects":{}}',
            encoding="utf-8",
        )
        old = watch.WatchState(self.state_path)
        self.assertFalse(old.warning)
        self.assertNotIn("dismissed", old.task("old"))
        self.state_path.write_text("{broken", encoding="utf-8")
        corrupt = watch.WatchState(self.state_path)
        self.assertEqual(corrupt.data, {"version": 1, "tasks": {}, "projects": {}})
        self.assertIn("損壞", corrupt.warning)

    def test_dismissing_question_does_not_change_question_files(self):
        question_path = self.root / "QUESTION.md"
        answer_path = self.root / "ANSWER.md"
        question_path.write_text("question bytes\n", encoding="utf-8")
        answer_path.write_text("answer bytes\n", encoding="utf-8")
        question = {"id": "q-files", "asked_at_ts": self.now - 60,
                    "answered": False, "path": str(question_path)}
        agent = self.agent("question-files", "question-files", "question", question=question)
        self.state.reconcile([agent], self.now)
        ok, message = self.state.apply("dismiss", agent, [agent], self.now)
        self.assertFalse(ok)
        self.assertIn("尚未收到這題的有效 ANSWER.md", message)
        self.assertEqual(question_path.read_text(encoding="utf-8"), "question bytes\n")
        self.assertEqual(answer_path.read_text(encoding="utf-8"), "answer bytes\n")

    def test_question_at_23h59_cannot_be_cleared_and_state_is_unchanged(self):
        _, _, _, question = self.orphan_question(
            "question-23h59", watch.QUESTION_CLEARABLE_SECONDS - 60,
        )
        agent = self.agent("question-23h59", "question-23h59", "question",
                           question=question)
        self.state.reconcile([agent], self.now)
        before = (
            self.state_path.exists(),
            self.state_path.read_bytes() if self.state_path.exists() else None,
            self.state.data.copy(),
        )

        ok, message = self.state.apply("dismiss", agent, [agent], self.now)

        self.assertEqual(watch.question_wait_seconds(question, self.now), 23 * 3600 + 59 * 60)
        self.assertFalse(ok)
        self.assertEqual(message, "尚未收到這題的有效 ANSWER.md，不能清除")
        self.assertEqual(
            before,
            (
                self.state_path.exists(),
                self.state_path.read_bytes() if self.state_path.exists() else None,
                self.state.data.copy(),
            ),
        )

    def test_overdue_question_needs_matching_answer_before_manual_clear(self):
        question_root, _, _, question = self.orphan_question(
            "question-late-answer", watch.QUESTION_CLEARABLE_SECONDS + 60,
        )
        agent = self.agent(
            "question-late-answer", "question-late-answer", "question",
            start=self.now - 300, question=question,
        )
        self.state.reconcile([agent], self.now)
        self.assertFalse(self.state.apply("dismiss", agent, [agent], self.now)[0])
        self.assertFalse(self.state.task(agent.key).get("dismissed", False))
        self.assertEqual(self.state.bucket(agent, self.now), "attention")

        self.assertTrue(watch.write_answer_file(
            question_root, agent.question, "A", now=self.now + 1,
        )[0])
        agent.question = watch.read_task_question(question_root)
        agent.status = "failed"
        agent.completion_time = self.now + 1
        self.assertFalse(watch.question_is_active(agent.question, agent.completion_time))
        self.state.reconcile([agent], self.now + 1)
        self.assertTrue(self.state.apply("dismiss", agent, [agent], self.now + 1)[0])
        self.assertTrue(self.state.task(agent.key)["dismissed"])
        self.assertEqual(self.state.bucket(agent, self.now + 1), "history")

        agent.status = "running"
        agent.start_time = self.now + 2
        agent.process_alive = True
        self.state.reconcile([agent], self.now + 2)
        self.assertFalse(self.state.task(agent.key)["dismissed"])
        self.assertEqual(self.state.bucket(agent, self.now + 2), "running")

    def test_bulk_dismiss_only_targets_stale_or_superseded(self):
        stale = self.agent("stale", "stale-task", "failed",
                           completion=self.now - 48 * 3600)
        replaced = self.agent("replaced", "foo", "failed", project="agent-watch",
                              start=self.now - 500,
                              completion=self.now - 400)
        winner = self.agent("winner", "foo-retry", "complete",
                            project="agent-watch", start=self.now - 300,
                            completion=self.now - 100)
        fresh = self.agent("fresh", "fresh-task", "failed",
                           completion=self.now - 23 * 3600)
        agents = [stale, replaced, winner, fresh]
        self.state.reconcile(agents, self.now)
        self.assertEqual(self.state.dismiss_stale(agents, self.now), 1)
        self.assertEqual(self.state.stale_info(stale, self.now), "已清除")
        self.assertEqual(
            self.state.stale_info(replaced, self.now), "已接手完成 → foo-retry",
        )
        self.assertFalse(self.state.task(replaced.key).get("dismissed", False))
        self.assertFalse(self.state.task(fresh.key).get("dismissed", False))
        self.assertEqual(self.state.bucket(fresh, self.now), "attention")
        row = watch.render_task_row(
            stale, 80, self.now, bucket=self.state.bucket(stale, self.now),
            history_label=self.state.stale_info(stale, self.now),
        )
        self.assertIn("已清除", row.plain)

    def test_clear_bindings_do_not_conflict(self):
        keys = [binding[0] for binding in watch.AgentWatchApp.BINDINGS]
        self.assertEqual(keys.count("x"), 1)
        self.assertEqual(keys.count("shift+x"), 1)
        self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
