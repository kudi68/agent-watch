"""346: mainline map grouping + parse_answer `answer:` alias.

The map is the only top-level grouping source: names are matched by literal
prefix, then by the inferred arch project id, and only then does the old
arch.json/infer_project path decide. Editing the TSV alone must change groups.
"""
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import watch


HEADER = "mainline_id\tlabel\tcard_prefixes\tproject_aliases\tnotes\n"
NO_SUCH_MAP = "/nonexistent/agent-watch/MAINLINE-current.tsv"


def agent(name, project="other", status="running", start=1_800_000_000.0, **kwargs):
    fields = dict(
        name=name, status=status, log_path=Path("/tmp/example/run.log"),
        last_path=Path("/tmp/example/LAST.md"), model="model-x", harness="Agent A",
        start_time=start, elapsed="00:02:00", latest="成果摘要",
        completion_time=None, project=project, key=name,
    )
    fields.update(kwargs)
    return watch.AgentInfo(**fields)


class MainlineMapTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)
        self.map_path = self.base / "MAINLINE-current.tsv"

    def write_map(self, rows):
        self.map_path.write_text(HEADER + "".join(rows), encoding="utf-8")
        # mtime_ns is part of the cache signature; nudge it for same-second edits.
        stamp = os.stat(self.map_path).st_mtime + 1
        os.utime(self.map_path, (stamp, stamp))
        return self.map_path

    def with_map(self, rows):
        self.write_map(rows)
        patcher = patch.dict(os.environ, {"AGENT_WATCH_MAINLINE": str(self.map_path)})
        patcher.start()
        self.addCleanup(patcher.stop)
        return self.map_path

    def groups(self, agents):
        return {key: [a.key for a in value]
                for key, value in watch.group_agents_by_project(agents).items()}

    def test_without_map_keeps_old_arch_grouping(self):
        # Red side of the change: no map file -> the old arch project decides.
        with patch.dict(os.environ, {"AGENT_WATCH_MAINLINE": NO_SUCH_MAP}):
            grouped = self.groups([
                agent("hermes-cron-04", "hermes"),
                agent("random-card", "personal-ai-os"),
                agent("no-idea", "other"),
            ])
            self.assertEqual(grouped, {
                "hermes": ["hermes-cron-04"],
                "personal-ai-os": ["random-card"],
                "other": ["no-idea"],
            })
            self.assertEqual(watch.project_label("hermes"), "Hermes")

    def test_map_regroups_by_prefix_and_alias(self):
        self.with_map([
            "m1\t主線1 Hermes＋PAI\thermes,hp0\tpersonal-ai-os\t\n",
            "m2\t主線2 記帳\tab\t\t\n",
            "other\t其他／基建\t\t\t\n",
        ])
        grouped = self.groups([
            agent("hermes-cron-04", "hermes"),
            agent("hp0-t8-baseline", "other"),
            agent("ab-daak-fix", "other"),
            agent("random-card", "personal-ai-os"),
            agent("aw-mainline-map", "other"),
        ])
        self.assertEqual(grouped["m1"], ["hermes-cron-04", "hp0-t8-baseline", "random-card"])
        self.assertEqual(grouped["m2"], ["ab-daak-fix"])
        self.assertEqual(grouped["other"], ["aw-mainline-map"])
        self.assertEqual(watch.project_label("m1"), "主線1 Hermes＋PAI")

    def test_unknown_stays_other(self):
        self.with_map([
            "m1\t主線1 Hermes＋PAI\thermes\t\t\n",
            "other\t其他／基建\t\t\t\n",
        ])
        self.assertEqual(watch.mainline_for("nothing-matches", "unknown-project"), None)
        self.assertEqual(self.groups([agent("nothing-matches", "")]),
                         {"other": ["nothing-matches"]})
        self.assertEqual(watch.project_label("other"), "其他")

    def test_prefix_wins_over_alias(self):
        # A card whose name says P3 belongs to P3 even if arch guessed personal-ai-os.
        self.with_map([
            "m1\t主線1 Hermes＋PAI\t\tpersonal-ai-os\t\n",
            "m3\t主線3 P3\tp3\t\t\n",
        ])
        self.assertEqual(watch.mainline_for("p3a-install", "personal-ai-os")["id"], "m3")

    def test_editing_map_changes_group_without_code_change(self):
        agents = [agent("ab-daak-fix", "other")]
        self.with_map([
            "m1\t主線1 Hermes＋PAI\tab\t\t\n",
            "other\t其他／基建\t\t\t\n",
        ])
        self.assertEqual(list(self.groups(agents)), ["m1"])
        self.write_map([
            "m2\t主線2 記帳\tab\t\t\n",
            "other\t其他／基建\t\t\t\n",
        ])
        self.assertEqual(list(self.groups(agents)), ["m2"])

    def test_mainlines_sort_before_arch_projects(self):
        self.with_map([
            "m1\t主線1 Hermes＋PAI\thermes\t\t\n",
            "other\t其他／基建\t\t\t\n",
        ])
        grouped = self.groups([
            agent("agent-watch-thing", "agent-watch"),
            agent("hermes-cron", "hermes"),
        ])
        self.assertEqual(list(grouped), ["m1", "agent-watch"])

    def test_board_header_shows_mainline_label(self):
        self.with_map([
            "m1\t主線1 Hermes＋PAI\thermes\t\t\n",
        ])
        now = 1_800_000_000.0
        text = watch.render_work_board(
            [agent("hermes-cron", "hermes", start=now - 60)], now=now,
        )
        self.assertIn("主線1 Hermes＋PAI", text)

    def test_default_path_is_the_repo_file(self):
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_WATCH_MAINLINE", None)
            self.assertEqual(
                watch.mainline_path(),
                Path(watch.__file__).with_name(watch.MAINLINE_FILENAME),
            )

    def test_parse_skips_rows_without_id_or_label(self):
        rows = watch.parse_mainline_tsv(
            "mainline_id\tlabel\tcard_prefixes\n"
            "\t沒有 id\tx\n"
            "m9\t\tx\n"
            "m1\t主線1\ta, b\n"
        )
        self.assertEqual(list(rows), ["m1"])
        self.assertEqual(rows["m1"]["card_prefixes"], ["a", "b"])


class ParseAnswerAliasTests(unittest.TestCase):
    def answer(self, body):
        return watch.parse_answer("# ANSWER\n" + body)

    def test_answer_field_is_a_choice_alias(self):
        parsed = self.answer("id: q1\nanswered_at: 2026-11-09T10:00:00+08:00\nanswer: A\n")
        self.assertEqual(parsed["error"], "")
        self.assertEqual(parsed["choice"], "A")

    def test_choice_field_still_works(self):
        parsed = self.answer("id: q1\nchoice: B\n")
        self.assertEqual(parsed["error"], "")
        self.assertEqual(parsed["choice"], "B")

    def test_choice_wins_when_both_are_present(self):
        parsed = self.answer("id: q1\nanswer: A\nchoice: C\n")
        self.assertEqual(parsed["choice"], "C")

    def test_missing_both_is_still_an_error(self):
        parsed = self.answer("id: q1\nnote: 只寫備註\n")
        self.assertEqual(parsed["error"], "缺少 choice")

    def test_alias_answer_counts_as_answered_question(self):
        with tempfile.TemporaryDirectory() as tmp:
            folder = Path(tmp)
            (folder / "QUESTION.md").write_text(
                "# QUESTION\nid: q1\nasked_at: 2026-11-09T10:00:00+08:00\n"
                "question: 選哪個？\noptions:\n- A: 甲\n- B: 乙\n",
                encoding="utf-8",
            )
            (folder / "ANSWER.md").write_text(
                "# ANSWER\nid: q1\nanswer: B\n", encoding="utf-8",
            )
            question = watch.read_task_question(folder)
            self.assertTrue(question["answered"])
            self.assertEqual(question["answer"]["choice"], "B")


if __name__ == "__main__":
    unittest.main()
