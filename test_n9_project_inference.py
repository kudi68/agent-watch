"""N9: unknown dispatch hints keep inferring, `其他` ratio, and the daily TSV."""
import fcntl
import os
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

import watch

# A second process that reads the TSV slowly, so two writers that do not share
# a lock both build their row from the same stale snapshot.
RACE_WRITER = """
import sys, time
from pathlib import Path
import watch

plain = Path.read_text
Path.read_text = lambda self, *a, **k: (plain(self, *a, **k), time.sleep(0.3))[0]
print("ready", flush=True)
sys.stdin.readline()
watch.write_daily_stats(
    Path(sys.argv[1]), [],
    row={"date": time.strftime("%Y-%m-%d"), sys.argv[2]: sys.argv[3]},
    owned_columns=(sys.argv[2],),
)
"""

# Logs every TSV read and lock call; "paused" waits on stdin after its read.
RACE_IPC_WRITER = """
import fcntl, sys, time
from pathlib import Path
import watch
mode, target, column, value = sys.argv[1:5]
read, lock = Path.read_text, fcntl.flock
def read_text(self, *args, **kwargs):
    text = read(self, *args, **kwargs)
    print("read", time.time(), flush=True)
    if mode == "paused":
        sys.stdin.readline()
    return text
def flock(fd, operation):
    print("flock", operation, time.time(), flush=True)
    return lock(fd, operation)
Path.read_text, fcntl.flock = read_text, flock
watch.write_daily_stats(Path(target), [], owned_columns=(column,),
                        row={"date": time.strftime("%Y-%m-%d"), column: value})
"""


def add_project(test, project_id, **config):
    """Register a throwaway project for one test and always remove it again."""
    watch.ensure_architecture_data()
    watch.ARCHITECTURE_DATA["projects"][project_id] = {
        "label": project_id,
        "aliases": [],
        "repo_paths": [],
        "repo_names": [],
        "default_view": "now",
        **config,
    }
    test.addCleanup(watch.ARCHITECTURE_DATA["projects"].pop, project_id, None)


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

    def test_arch_json_names_are_globally_unique(self):
        """No id/alias/repo name is shared, so inference can never be ambiguous."""
        watch.ensure_architecture_data()
        owners = {}
        for project_id, config in watch.ARCHITECTURE_DATA["projects"].items():
            if not isinstance(config, dict):
                continue
            names = [project_id, *config.get("aliases", []), *config.get("repo_names", [])]
            names += [
                str(path).strip("/").rsplit("/", 1)[-1]
                for path in config.get("repo_paths", [])
            ]
            for name in names:
                key = watch._repo_key(name)
                if key:
                    owners.setdefault(key, set()).add(project_id)
        collisions = {key: sorted(ids) for key, ids in owners.items() if len(ids) > 1}
        self.assertEqual(collisions, {})

    def test_unknown_hint_falls_through_to_git_without_a_warning(self):
        folder = self.task_dir("aw-cleanup")
        self.add_worktree(folder, "agent-watch")
        project, source, warning = self.infer(folder, "aw-cleanup", hint="DONE")
        self.assertEqual((project, source), ("agent-watch", "git"))
        # Classified cards must not keep the "資料有錯" row (P2-1).
        self.assertEqual(warning, "")

    def test_unknown_hint_falls_through_to_keywords_without_a_warning(self):
        folder = self.task_dir("medtool-import")
        project, source, warning = self.infer(folder, "medtool-import", hint="foo")
        self.assertEqual((project, source), ("medical-tools", "關鍵字"))
        self.assertEqual(warning, "")

    def test_unknown_hint_falls_through_to_a_label_file_without_a_warning(self):
        # Third success path with an unknown hint: the folder's own label wins.
        folder = self.task_dir("labelled")
        (folder / "PROMPT.md").write_text("# 卡\nproject: ios\n", encoding="utf-8")
        project, source, warning = self.infer(folder, "沒有語意的任務名", hint="DONE")
        self.assertEqual((project, source), ("ios-shortcuts", "標籤"))
        self.assertEqual(warning, "")

    def test_unknown_hint_falls_through_to_a_path_without_a_warning(self):
        folder = self.task_dir("path-only")
        (folder / "REPORT.md").write_text(
            "改 ~/Projects/agent-watch/watch.py\n" * 3, encoding="utf-8"
        )
        project, source, warning = self.infer(folder, "沒有語意的任務名", hint="DONE")
        self.assertEqual((project, source), ("agent-watch", "路徑"))
        self.assertEqual(warning, "")

    def test_unknown_hint_naming_a_dispatch_repo_keeps_dispatch_without_a_warning(self):
        # `weak_hint` is a dispatch-only label: it is used, but it is not a
        # project, so the card is reported under 派工 and not as a data error.
        add_project(self, "night-copy", repo_names=["night-queue"], dispatch_only=True)
        folder = self.task_dir("dispatch-repo-task")
        project, source, warning = self.infer(
            folder, "沒有語意的任務名", hint="night-queue"
        )
        self.assertEqual((project, source), ("night-copy", "派工"))
        self.assertEqual(warning, "")

    def test_dispatch_only_hint_is_dispatch_not_a_data_error(self):
        folder = self.task_dir("night-queue-task")
        project, source, warning = self.infer(folder, "沒有語意的任務名", hint="night")
        self.assertEqual((project, source, warning), ("night", "派工", ""))

    def test_unknown_hint_without_evidence_stays_other(self):
        folder = self.task_dir("misc")
        project, source, warning = self.infer(folder, "misc", hint="foo")
        self.assertEqual((project, source), ("other", "未判斷"))
        self.assertEqual(warning, "未知專案：foo")

    def test_unknown_hint_naming_a_repo_is_classified(self):
        cases = {
            "med-knowledge": "medknowledge",
            "Projects/MedKnowledge/": "medknowledge",
            "personal_capture": "personal-capture",
            "agent-ops-host-pinned-bb7a091": "agent-ops-pinned",
        }
        for index, (hint, expected) in enumerate(cases.items()):
            with self.subTest(hint=hint):
                folder = self.task_dir(f"task-{index}")
                project, source, warning = self.infer(folder, "沒有語意的任務名", hint=hint)
                self.assertEqual((project, source), (expected, "標籤"))
                # A repo-name hint that placed the card is not a data error.
                self.assertEqual(warning, "")

    def test_repo_suffix_is_stripped_once(self):
        self.assertEqual(watch._project_for_repo_hint("agent-watch-fix2"), "agent-watch")
        self.assertEqual(watch._project_for_repo_hint("agent-watch-review"), "agent-watch")

    def test_numbered_suffix_is_part_of_the_name(self):
        # The old rule also dropped `-\\d+` / `-v\\d+`, which turned these into
        # the aliases `c1` / `aw` and misclassified them (P2-3).
        self.assertEqual(watch._project_for_repo_hint("c1-2"), "")
        self.assertEqual(watch._project_for_repo_hint("aw-v2"), "")
        folder = self.task_dir("misc-numbered")
        project, source, warning = self.infer(folder, "misc-numbered", hint="c1-2")
        self.assertEqual((project, source), ("other", "未判斷"))
        self.assertEqual(warning, "未知專案：c1-2")

    def test_full_repo_name_wins_over_the_stripped_name(self):
        add_project(self, "plain-repo", repo_names=["shadow"])
        add_project(self, "decorated-repo", repo_names=["shadow-fix"])
        self.assertEqual(watch._project_for_repo_hint("shadow"), "plain-repo")
        # Full name first: `shadow-fix` is its own repo, not `shadow` plus a suffix.
        self.assertEqual(watch._project_for_repo_hint("shadow-fix"), "decorated-repo")

    def test_repo_name_shared_by_two_projects_is_not_guessed(self):
        add_project(self, "twin-one", repo_names=["twin-repo"])
        add_project(self, "twin-two", repo_names=["twin-repo"])
        self.assertEqual(watch._project_for_repo_name("twin-repo"), "")
        self.assertEqual(watch._project_for_repo_hint("twin-repo"), "")
        folder = self.task_dir("twin-task")
        project, source, warning = self.infer(folder, "twin-task", hint="twin-repo")
        self.assertEqual((project, source), ("other", "未判斷"))
        self.assertEqual(warning, "未知專案：twin-repo")

    def test_repo_name_colliding_with_another_alias_does_not_reroute_labels(self):
        # `pai` is personal-ai-os' alias; a repo of that name must not steal it.
        add_project(self, "pai-copy", repo_names=["pai"])
        self.assertEqual(watch.resolve_project("pai")[0], "personal-ai-os")
        self.assertEqual(watch._project_for_repo_name("pai"), "")
        self.assertEqual(watch._project_for_repo_hint("pai"), "")

    def test_repo_names_are_not_labels(self):
        add_project(
            self, "repo-only", repo_names=["only-in-repo"],
            repo_paths=["Projects/only-in-repo"],
        )
        # The global label lookup no longer eats repo names (P2-2)…
        project, error = watch.resolve_project("only-in-repo")
        self.assertEqual(project, "other")
        self.assertIn("未知專案", error)
        # …the explicit repo lookup still finds it…
        self.assertEqual(watch._project_for_repo_name("only-in-repo"), "repo-only")
        # …and a ~/Projects path keeps working through the path stage.
        folder = self.task_dir("repo-only-task")
        (folder / "REPORT.md").write_text(
            "改 ~/Projects/only-in-repo/a.py\n" * 3, encoding="utf-8"
        )
        project, source, warning = self.infer(folder, "repo-only-task")
        self.assertEqual((project, source), ("repo-only", "路徑"))
        self.assertEqual(warning, "")

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
        add_project(
            self, "status",
            repo_paths=["Projects/status"], repo_names=["status"],
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
    def agent(self, project, source, age_seconds=0, now=None, start=True):
        current = time.time() if now is None else now
        return watch.AgentInfo(
            name=f"{project}-{source or 'none'}",
            log_path=Path("/tmp/a"),
            last_path=Path("/tmp/a"),
            status="running",
            model="m",
            start_time=(current - age_seconds) if start else None,
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
        self.assertEqual(
            watch.project_other_summary(agents, now), "其他 2（50.0%，近 14 天）"
        )

    def test_other_summary_ignores_cards_older_than_the_window(self):
        now = time.time()
        agents = [
            self.agent("agent-watch", "標籤", now=now),
            self.agent("other", "未判斷", age_seconds=20 * 86400, now=now),
        ]
        self.assertEqual(
            watch.project_other_summary(agents, now), "其他 0（0.0%，近 14 天）"
        )

    def test_other_summary_keeps_cards_without_any_timestamp(self):
        # An undated card cannot be aged out, so it stays in the denominator (P3-2).
        now = time.time()
        agents = [
            self.agent("agent-watch", "標籤", now=now),
            self.agent("other", "未判斷", now=now, start=False),
        ]
        self.assertEqual(
            watch.project_other_summary(agents, now), "其他 1（50.0%，近 14 天）"
        )

    def test_other_summary_names_its_window(self):
        now = time.time()
        agents = [
            self.agent("agent-watch", "標籤", now=now),
            self.agent("other", "未判斷", now=now),
        ]
        self.assertEqual(
            watch.project_other_summary(agents, now, window_days=7),
            "其他 1（50.0%，近 7 天）",
        )

    def test_other_summary_is_in_the_plain_summary_text(self):
        now = time.time()
        agents = [
            self.agent("agent-watch", "標籤", now=now),
            self.agent("other", "未判斷", now=now),
        ]
        text = watch.format_bucket_counts(agents, now)
        self.assertIn("其他 1（50.0%，近 14 天）", text)


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

    def test_two_writers_in_two_processes_keep_both_columns(self):
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "daily" / "agent-watch-daily.tsv"
            procs = [
                subprocess.Popen(
                    [sys.executable, "-B", "-c", RACE_WRITER, str(target), column, value],
                    cwd=str(Path(__file__).resolve().parent), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
                for column, value in (("writer_a", "a1"), ("writer_b", "b1"))
            ]
            try:
                for proc in procs:
                    self.assertEqual(proc.stdout.readline().strip(), b"ready")
                for proc in procs:
                    proc.stdin.write(b"\n")
                    proc.stdin.flush()
                for proc in procs:
                    _, errors = proc.communicate(timeout=60)
                    self.assertEqual(proc.returncode, 0, errors.decode("utf-8"))
            finally:
                for proc in procs:
                    if proc.poll() is None:
                        proc.kill()
                        proc.wait()
            self.assertTrue(target.with_name(target.name + ".lock").is_file())
            header, *rows = target.read_text(encoding="utf-8").splitlines()
            self.assertEqual(len(rows), 1)
            values = dict(zip(header.split("\t"), rows[0].split("\t")))
            self.assertEqual((values.get("writer_a"), values.get("writer_b")), ("a1", "b1"))

    def test_second_writer_reads_only_after_the_first_writer_unlocks(self):
        now = self.day_start() + 8 * 3600
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "daily.tsv"
            watch.write_daily_stats(
                target, [], owned_columns=("base",),
                row={"date": time.strftime("%Y-%m-%d", time.localtime(now)), "base": "b0"},
            )

            def line(proc):
                return proc.stdout.readline().decode().split()

            def start(mode, column, value):
                return subprocess.Popen(
                    [sys.executable, "-B", "-c", RACE_IPC_WRITER,
                     mode, str(target), column, value],
                    cwd=str(Path(__file__).resolve().parent), stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                )
            paused = start("paused", "writer_a", "a1")
            live = None
            try:
                self.assertEqual(line(paused)[0], "flock")
                self.assertEqual(line(paused)[0], "read")
                live = start("live", "writer_b", "b1")
                self.assertEqual(line(live)[0], "flock", "the second writer read while locked")
                self.assertEqual(line(live)[0], "flock")
                rest = {}
                for proc in (paused, live):
                    output, errors = proc.communicate(timeout=60)
                    self.assertEqual(proc.returncode, 0, errors.decode("utf-8"))
                    rest[proc] = output.decode().splitlines()
            finally:
                for proc in (paused, live):
                    if proc is not None and proc.poll() is None:
                        proc.kill()
                        proc.wait()
            unlock = [float(l.split()[2]) for l in rest[paused]
                      if l.split()[:2] == ["flock", str(fcntl.LOCK_UN)]]
            read_at = [float(l.split()[1]) for l in rest[live] if l.split()[:1] == ["read"]]
            self.assertTrue(unlock and read_at, rest)
            self.assertGreater(read_at[0], unlock[0])
            header, *rows = target.read_text(encoding="utf-8").splitlines()
            values = dict(zip(header.split("\t"), rows[0].split("\t")))
            self.assertEqual([values.get(k) for k in ("base", "writer_a", "writer_b")],
                             ["b0", "a1", "b1"])

    def test_update_data_does_not_wait_for_a_lock_held_by_another_process(self):
        now = self.day_start() + 8 * 3600
        agent = self.agent("other", "未判斷", now)
        agent.start_source = "tsv"
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "agent-watch-daily.tsv"
            app = watch.AgentWatchApp(
                runs_path=root / "runs.tsv", done_path=root / "done.tsv",
                state_path=root / "state.json", decisions_path=root / "decisions",
                daily_stats_path=target,
            )
            def round(now_at):
                with patch.object(watch, "load_all_agents", return_value=([agent], [])), \
                        patch.object(watch, "notify_completion"):
                    return app.update_data(now=now_at)

            self.assertTrue(round(now))
            before = target.read_bytes()
            holder = subprocess.Popen(
                [sys.executable, "-B", "-c", RACE_IPC_WRITER, "paused", str(target), "base", "b0"],
                cwd=str(Path(__file__).resolve().parent), stdin=subprocess.PIPE,
                stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            )
            try:
                self.assertEqual(holder.stdout.readline().decode().split()[0], "flock")
                self.assertEqual(holder.stdout.readline().decode().split()[0], "read")
                with self.assertLogs("watch", level="WARNING") as logged:
                    started = time.monotonic()
                    round(now + 1)
                    elapsed = time.monotonic() - started
                self.assertTrue(logged.output[0].endswith("skipped"), logged.output[0])
                self.assertLess(elapsed, 0.2)
                self.assertEqual(target.read_bytes(), before)
            finally:
                holder.communicate(timeout=60)
            released = target.read_bytes()
            agent.status = "blocked"
            self.assertTrue(round(now + 2))
            self.assertNotEqual(target.read_bytes(), released)

    def test_writer_skips_the_round_while_another_process_holds_the_lock(self):
        day = self.day_start()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "daily" / "agent-watch-daily.tsv"
            watch.write_daily_stats(
                target, [self.agent("other", "未判斷", day + 3600)],
                day_start=day, now=day + 86400,
            )
            before = target.read_text(encoding="utf-8")
            lock_path = target.with_name(target.name + ".lock")
            with open(lock_path, "a+") as held:
                fcntl.flock(held.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
                with patch.object(watch, "DAILY_STATS_LOCK_TIMEOUT", 0.05), \
                        self.assertLogs("watch", level="WARNING") as logged:
                    watch.write_daily_stats(
                        target, [self.agent("agent-watch", "標籤", day + 7200)],
                        day_start=day, now=day + 86400,
                    )
            self.assertIn("lock busy", logged.output[0])
            self.assertEqual(target.read_text(encoding="utf-8"), before)

    def test_daily_attention_classifies_today_and_empty_ratio(self):
        day = self.day_start()
        now = day + 8 * 3600

        def card(name, status, started, question=None, source="tsv", mtime=None):
            agent = self.agent("p", name, started)
            agent.key, agent.status = name, status
            agent.start_source = source
            agent.log_mtime = started if mtime is None else mtime
            agent.question, agent.completion_time = question, None
            return agent

        def question(age, escalated=False):
            return {"mtime": now - age, "answered": False, "escalated": escalated}

        agents = [
            card("waiting-q", "question", now, question(60)),
            card("overdue-q", "question", now, question(watch.QUESTION_OVERDUE_SECONDS)),
            card("escalated-q", "question", now, question(60, True)),
            card("blocked", "blocked", now), card("failed", "failed", now),
            card("no-report", "no_report", now), card("waiting", "waiting", now),
            card("malformed", "error", now, {"error": "bad"}),
            card("handed", "failed", now), card("clear-reason", "failed", now),
            card("clear", "blocked", now), card("done", "complete", now),
            card("mtime-fallback", "complete", day - 86400, source="pid", mtime=now),
            card("old-mtime", "failed", now, source="log", mtime=day - 86400),
        ]
        with tempfile.TemporaryDirectory() as tmp:
            state = watch.WatchState(Path(tmp) / "state.json")
            state._handoff["handed"] = "replacement"
            cleared_agents = {agent.key: agent for agent in agents}
            for key, reason in (("clear-reason", "reason"), ("clear", "")):
                state.data["tasks"][key] = {
                    "dismissed": True,
                    "dismissed_at": watch.datetime.fromtimestamp(now).astimezone().isoformat(timespec="seconds"),
                    "dismissed_reason": reason,
                    "dismissed_sig": state.dismissed_signature(cleared_agents[key]),
                }
            row = watch.daily_attention_row(agents, state, now)
            self.assertEqual(
                {key: row[key] for key in watch.N7_DAILY_STATS_COLUMNS[1:]},
                {
                    "total_cards": 13, "q_waiting": 1, "q_overdue": 1,
                    "escalated": 1, "blocked": 1, "failed": 1, "no_report": 1,
                    "attention_other": 2, "handed_off": 1, "manual_cleared": 2,
                    "manual_with_reason": 1, "attention_ratio": "61.5",
                },
            )
            empty = watch.daily_attention_row([], state, now)
            self.assertEqual((empty["total_cards"], empty["attention_ratio"]), (0, ""))

    def test_daily_attention_prefers_tsv_started_at_to_log_mtime(self):
        day = self.day_start()
        now = day + 8 * 3600
        agent = self.agent("other", "未判斷", now)
        agent.key = "started-at-priority"
        agent.status = "failed"
        agent.start_source = "tsv"
        agent.log_mtime = day - 86400
        with tempfile.TemporaryDirectory() as tmp:
            state = watch.WatchState(Path(tmp) / "state.json")
            row = watch.daily_attention_row([agent], state, now)

        self.assertEqual(row["total_cards"], 1)
        self.assertEqual(row["failed"], 1)
        self.assertEqual(row["attention_ratio"], "100.0")

    def test_daily_attention_suppresses_only_matching_dismissed_signature(self):
        day = self.day_start()
        now = day + 8 * 3600
        completion_changed = self.agent("other", "未判斷", now)
        completion_changed.key = "completion-changed"
        completion_changed.status = "failed"
        completion_changed.start_source = "tsv"
        completion_changed.completion_time = now - 30

        question_changed = self.agent("other", "未判斷", now)
        question_changed.key = "question-changed"
        question_changed.status = "question"
        question_changed.start_source = "tsv"
        question_changed.completion_time = None
        question_changed.question = {
            "id": "new-question", "mtime": now - 60, "answered": False,
        }

        matching = self.agent("other", "未判斷", now)
        matching.key = "matching"
        matching.status = "blocked"
        matching.start_source = "tsv"

        agents = [completion_changed, question_changed, matching]
        dismissed_at = watch.datetime.fromtimestamp(now).astimezone().isoformat(
            timespec="seconds"
        )
        with tempfile.TemporaryDirectory() as tmp:
            state = watch.WatchState(Path(tmp) / "state.json")
            state.data["tasks"] = {
                "completion-changed": {
                    "dismissed": True, "dismissed_at": dismissed_at,
                    "dismissed_reason": "resolved",
                    "dismissed_sig": f"failed||{now - 90}",
                },
                "question-changed": {
                    "dismissed": True, "dismissed_at": dismissed_at,
                    "dismissed_reason": "resolved",
                    "dismissed_sig": "question|old-question|",
                },
                "matching": {
                    "dismissed": True, "dismissed_at": dismissed_at,
                    "dismissed_sig": state.dismissed_signature(matching),
                },
            }
            row = watch.daily_attention_row(agents, state, now)

        self.assertEqual(row["manual_cleared"], 3)
        self.assertEqual(row["manual_with_reason"], 2)
        self.assertEqual(row["failed"], 1)
        self.assertEqual(row["q_waiting"], 1)
        self.assertEqual(row["blocked"], 0)
        self.assertEqual(row["attention_ratio"], "66.7")

    def test_daily_writer_merges_n7_n9_columns_and_keeps_older_row(self):
        day = self.day_start()
        older = day - 86400
        now = day + 8 * 3600
        old_agent = self.agent("other", "old", older + 60)
        today_agent = self.agent("other", "today", day + 60)
        today_agent.start_source = "tsv"
        for order in (("n7", "n9"), ("n9", "n7")):
            with tempfile.TemporaryDirectory() as tmp:
                target = Path(tmp) / "daily.tsv"
                state = watch.WatchState(Path(tmp) / "state.json")
                n7_row = watch.daily_attention_row([today_agent], state, now)
                watch.write_daily_stats(target, [old_agent], older, now)
                old_line = target.read_text(encoding="utf-8").splitlines()[1]
                for writer in order:
                    if writer == "n7":
                        watch.write_daily_stats(
                            target, [today_agent], day, now, row=n7_row,
                            owned_columns=watch.N7_DAILY_STATS_COLUMNS,
                        )
                    else:
                        watch.write_daily_stats(target, [today_agent], day, now)
                lines = target.read_text(encoding="utf-8").splitlines()
                header = lines[0].split("\t")
                values = dict(zip(header, lines[-1].split("\t")))
                self.assertEqual(lines[1], old_line)
                self.assertTrue(set(watch.N7_DAILY_STATS_COLUMNS) <= set(header))
                self.assertEqual(values["total_cards"], "1")
                self.assertEqual(values["other_count"], "1")
                self.assertEqual(sum(line.startswith("2026-10-28\t") for line in lines), 1)

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

    def test_writer_rejects_a_row_for_yesterday(self):
        day = self.day_start()
        yesterday = day - 86400
        yesterday_date = time.strftime("%Y-%m-%d", time.localtime(yesterday))
        old_line = yesterday_date + "\t5\t1\t20.0"
        original = "\t".join(watch.DAILY_STATS_COLUMNS) + "\n" + old_line + "\n"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "agent-watch-daily.tsv"
            target.write_text(original, encoding="utf-8")
            stale_row = {"date": yesterday_date}
            with self.assertLogs("watch", level="WARNING") as logged:
                watch.write_daily_stats(
                    target, [], day_start=day, now=day + 8 * 3600,
                    row=stale_row, owned_columns=watch.N7_DAILY_STATS_COLUMNS,
                )
            self.assertIn("not today", logged.output[0])
            self.assertEqual(target.read_text(encoding="utf-8"), original)

    def test_older_column_rows_are_kept_verbatim(self):
        # A column-set upgrade must not erase earlier days (P3-3).
        legacy = "2026-10-20\t5\t1\t20.0"
        day = self.day_start()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "agent-watch-daily.tsv"
            target.write_text(
                "\t".join(watch.DAILY_STATS_COLUMNS) + "\n" + legacy + "\n",
                encoding="utf-8",
            )
            agents = [self.agent("other", "未判斷", day + 3600)]
            watch.write_daily_stats(target, agents, day_start=day, now=day + 86400)
            watch.write_daily_stats(target, agents, day_start=day, now=day + 86400)
            lines = target.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0].split("\t"), list(watch.DAILY_STATS_COLUMNS))
            # Rows stay in date order even though the column sets differ (P3-2).
            self.assertEqual(
                [line.split("\t")[0] for line in lines[1:]], ["2026-10-20", "2026-10-28"]
            )
            self.assertEqual(lines[1], legacy)
            self.assertEqual(sum(1 for line in lines if line == legacy), 1)

    def test_legacy_row_for_a_recomputed_day_is_superseded(self):
        # The same day in both schemas would put one date in the file twice; the
        # row just recomputed from the cards wins (P3-2).
        legacy = "2026-10-28\t5\t1\t20.0"
        day = self.day_start()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "agent-watch-daily.tsv"
            target.write_text(
                "\t".join(watch.DAILY_STATS_COLUMNS) + "\n" + legacy + "\n",
                encoding="utf-8",
            )
            watch.write_daily_stats(
                target, [self.agent("agent-watch", "標籤", day + 60)],
                day_start=day, now=day + 86400,
            )
            lines = target.read_text(encoding="utf-8").splitlines()
            self.assertEqual([line.split("\t")[0] for line in lines[1:]], ["2026-10-28"])
            self.assertNotIn(legacy, lines)
            self.assertEqual(
                lines[1].split("\t")[watch.DAILY_STATS_COLUMNS.index("other_count")], "0"
            )

    def test_legacy_rows_keep_date_order(self):
        day = self.day_start()
        newer_legacy = "2026-10-21\t5\t1\t20.0"
        older_legacy = "2026-10-19\t4\t2\t50.0"
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "agent-watch-daily.tsv"
            target.write_text(
                "\t".join(watch.DAILY_STATS_COLUMNS) + "\n"
                + newer_legacy + "\n" + older_legacy + "\n",
                encoding="utf-8",
            )
            watch.write_daily_stats(
                target, [self.agent("other", "未判斷", day + 60)],
                day_start=day, now=day + 86400,
            )
            lines = target.read_text(encoding="utf-8").splitlines()
            self.assertEqual(
                [line.split("\t")[0] for line in lines[1:]],
                ["2026-10-19", "2026-10-21", "2026-10-28"],
            )
            self.assertEqual(lines[1], older_legacy)
            self.assertEqual(lines[2], newer_legacy)

    def test_existing_mode_survives_the_atomic_replace(self):
        # `mkstemp` creates 0600; replacing a 0644 TSV must not lock it down (P3-3).
        day = self.day_start()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "agent-watch-daily.tsv"
            target.write_text(
                "\t".join(watch.DAILY_STATS_COLUMNS) + "\n", encoding="utf-8"
            )
            os.chmod(target, 0o644)
            watch.write_daily_stats(
                target, [self.agent("other", "未判斷", day + 60)],
                day_start=day, now=day + 86400,
            )
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_new_file_gets_the_umask_mode(self):
        day = self.day_start()
        previous = os.umask(0o022)
        self.addCleanup(os.umask, previous)
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "agent-watch-daily.tsv"
            watch.write_daily_stats(
                target, [self.agent("other", "未判斷", day + 60)],
                day_start=day, now=day + 86400,
            )
            self.assertEqual(target.stat().st_mode & 0o777, 0o644)

    def test_write_does_not_reuse_a_fixed_temp_name(self):
        day = self.day_start()
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "agent-watch-daily.tsv"
            stale = target.with_name(target.name + ".tmp")
            stale.write_text("another writer's file\n", encoding="utf-8")
            watch.write_daily_stats(
                target, [self.agent("other", "未判斷", day + 3600)],
                day_start=day, now=day + 86400,
            )
            self.assertEqual(stale.read_text(encoding="utf-8"), "another writer's file\n")
            self.assertTrue(target.is_file())


class DailyStatsIntegrationTests(unittest.TestCase):
    def day_start(self, year=2026, month=10, day=28):
        return time.mktime((year, month, day, 0, 0, 0, 0, 0, -1))

    def agent(self, now, status="complete", key="today"):
        return watch.AgentInfo(
            name=key,
            log_path=Path("/tmp") / key / "run.log",
            last_path=Path("/tmp") / key / "REPORT.md",
            status=status,
            model="test",
            start_time=now - 60,
            elapsed="1m",
            latest=status,
            log_mtime=now,
            key=key,
            completion_time=now - 30 if status == "complete" else None,
            project="other",
            project_source="未判斷",
            start_source="tsv",
        )

    def test_daily_path_uses_env_override_and_root_default(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "root"
            override = Path(tmp) / "override.tsv"
            with patch.object(watch, "ROOT_DIR", root), patch.dict(
                os.environ, {"AGENT_WATCH_DAILY": str(override)}
            ):
                self.assertEqual(watch.daily_stats_path_from_env(), override)
            with patch.object(watch, "ROOT_DIR", root), patch.dict(os.environ):
                os.environ.pop("AGENT_WATCH_DAILY", None)
                self.assertEqual(
                    watch.daily_stats_path_from_env(),
                    root / "agent-watch-daily.tsv",
                )

    def test_update_data_writes_changed_today_once_and_preserves_other_columns(self):
        day = self.day_start()
        older_day = day - 86400
        now = day + 8 * 3600
        older_agent = self.agent(older_day + 3600, key="older")
        today_agent = self.agent(now)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            target = root / "agent-watch-daily.tsv"
            watch.write_daily_stats(
                target, [older_agent], day_start=older_day, now=older_day + 86400,
            )
            watch.write_daily_stats(
                target, [today_agent], day_start=day, now=now,
            )
            old_date = time.strftime("%Y-%m-%d", time.localtime(older_day))
            old_line = next(
                line for line in target.read_text(encoding="utf-8").splitlines()
                if line.startswith(old_date + "\t")
            )
            current_agents = [today_agent]
            app = watch.AgentWatchApp(
                runs_path=root / "runs.tsv",
                done_path=root / "done.tsv",
                state_path=root / "state.json",
                decisions_path=root / "decisions",
                daily_stats_path=target,
            )
            with patch.object(
                watch, "load_all_agents",
                side_effect=lambda *args: (current_agents, []),
            ), patch.object(
                watch, "notify_completion",
            ):
                self.assertTrue(app.update_data(now=now))
                first_text = target.read_text(encoding="utf-8")
                first_stat = target.stat()

                self.assertFalse(app.update_data(now=now + 1))
                self.assertEqual(target.read_text(encoding="utf-8"), first_text)
                self.assertEqual(target.stat().st_mtime_ns, first_stat.st_mtime_ns)

                today_agent.status = "blocked"
                today_agent.latest = "blocked"
                self.assertTrue(app.update_data(now=now + 2))

            lines = target.read_text(encoding="utf-8").splitlines()
            header = lines[0].split("\t")
            today_date = time.strftime("%Y-%m-%d", time.localtime(now))
            today_lines = [
                line for line in lines[1:]
                if line.split("\t", 1)[0] == today_date
            ]
            self.assertEqual(len(today_lines), 1)
            values = dict(zip(header, today_lines[0].split("\t")))
            self.assertEqual(values["other_count"], "1")
            self.assertEqual(values["basis_未判斷"], "1")
            self.assertEqual(values["blocked"], "1")
            self.assertTrue(set(watch.N7_DAILY_STATS_COLUMNS) <= set(header))
            self.assertTrue(set(watch.DAILY_STATS_COLUMNS) <= set(header))
            self.assertEqual(
                next(line for line in lines[1:] if line.startswith(old_date + "\t")),
                old_line,
            )
            self.assertNotEqual(target.read_text(encoding="utf-8"), first_text)

    def test_update_data_logs_writer_failure_and_continues(self):
        now = self.day_start() + 8 * 3600
        agent = self.agent(now)
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            app = watch.AgentWatchApp(
                runs_path=root / "runs.tsv",
                done_path=root / "done.tsv",
                state_path=root / "state.json",
                decisions_path=root / "decisions",
                daily_stats_path=root / "agent-watch-daily.tsv",
            )
            with patch.object(
                watch, "load_all_agents", return_value=([agent], [])
            ), patch.object(
                watch, "write_daily_stats", side_effect=OSError("write blocked")
            ), self.assertLogs("watch", level="WARNING") as logged:
                self.assertTrue(app.update_data(now=now))
            self.assertIn("daily stats update failed", logged.output[0])


if __name__ == "__main__":
    unittest.main()
