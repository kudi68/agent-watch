"""現況頁渲染測試：45 欄、等你／被擋在前、空閒時只有一行。"""

import tempfile
import unittest
from pathlib import Path

import line_status
import now_render
import watch
from line_status import ACTIVE, BLOCKED, DONE, IDLE, WAITING, LineSnapshot


def snap(title, state, doing="正在做一件很長但正常的事。", blocked_on="沒有卡點。",
         next_step="沒有寫下一步。", updated="2026-10-27"):
    return LineSnapshot(name=title, title=title, state=state, doing=doing,
                        blocked_on=blocked_on, next_step=next_step, last_updated=updated)


def widths(text):
    return [watch.display_width(line) for line in text.splitlines()]


class TestQuietBoard(unittest.TestCase):
    def test_empty_board_is_one_line(self):
        text = now_render.render_now([], 45)
        self.assertEqual(text, now_render.QUIET_LINE)
        self.assertNotIn("\n", text)
        self.assertIn("沒事", text)

    def test_done_and_idle_only_do_not_paint_cards(self):
        records = [snap("舊的一條", DONE), snap("閒的一條", IDLE), snap("也完成", DONE)]
        text = now_render.render_now(records, 45)
        self.assertEqual(text, now_render.QUIET_LINE)
        self.assertNotIn("[完成]", text)
        self.assertNotIn("[空閒]", text)

    def test_quiet_board_fits_every_supported_width(self):
        """One honest line, but it has to fit 36/30/24 too, not just 45."""
        boards = ([], [snap("已完成的事", DONE)], [snap("閒著", IDLE)],
                  [snap("完成甲", DONE), snap("閒著", IDLE)])
        for width in (45, 36, 30, 24):
            for records in boards:
                text = now_render.render_now(records, width)
                self.assertNotIn("\n", text)
                self.assertIn("沒事", text)
                self.assertLessEqual(watch.display_width(text), width,
                                     f"{width}: {text!r}")
        self.assertEqual(now_render.render_now([], 45), now_render.QUIET_LINE)
        self.assertNotEqual(now_render.render_now([], 24), now_render.QUIET_LINE)


class TestOrdering(unittest.TestCase):
    def test_waiting_and_blocked_render_above_active(self):
        records = [
            snap("已完成的事", DONE),
            snap("正在跑的事", ACTIVE, doing="正在收尾。"),
            snap("等你回覆的事", WAITING, doing="停在等你回覆。", blocked_on="等你選 A 或 B。"),
            snap("卡住的事", BLOCKED, doing="卡住了。", blocked_on="找不到憑證。"),
        ]
        lines = now_render.render_now(records, 45).splitlines()
        indexes = {state: next(i for i, line in enumerate(lines) if badge in line)
                   for state, badge in
                   ((BLOCKED, "[被擋]"), (WAITING, "[等你]"), (ACTIVE, "[進行中]"))}
        self.assertLess(indexes[BLOCKED], indexes[WAITING])
        self.assertLess(indexes[WAITING], indexes[ACTIVE])
        # Live cards come before the compact finished list.
        finished = next(i for i, line in enumerate(lines) if "已完成" in line)
        self.assertLess(indexes[ACTIVE], finished)

    def test_order_inside_one_state_keeps_data_layer_order(self):
        records = [snap("先", WAITING, blocked_on="等你一"), snap("後", WAITING, blocked_on="等你二")]
        text = now_render.render_now(records, 45)
        self.assertLess(text.index("[等你] 先"), text.index("[等你] 後"))


class TestNarrowTerminal(unittest.TestCase):
    def test_every_line_fits_45_columns_even_with_long_cjk(self):
        long_text = ("這是一段很長的中文說明，裡面還有 line_status.py 這種路徑與 "
                     "commit ac52b4d 這種代號，最後用 END 收尾。")
        records = [
            snap("很長標題的一條線，名稱故意寫得很長超過欄寬也要切好", BLOCKED,
                 doing=long_text, blocked_on=long_text, next_step=long_text),
            snap("等你的一條", WAITING, doing="短的。", blocked_on="等你決定。",
                 next_step="再跑一次。"),
            snap("進行中的一條", ACTIVE, doing="慢慢做。", next_step="收尾。"),
        ]
        for width in (30, 36, 40, 45):
            text = now_render.render_now(records, width)
            self.assertTrue(all(line <= width for line in widths(text)),
                            f"{width}: {[line for line in widths(text) if line > width]}")
            self.assertIn("END", text)  # nothing is dropped by wrapping
            self.assertFalse(any(line.lstrip().startswith(("，", "。"))
                                 for line in text.splitlines()))

    def test_default_width_is_phone_friendly(self):
        text = now_render.render_now([snap("等你的一條", WAITING, blocked_on="等你決定。")])
        self.assertLessEqual(max(widths(text)), 45)

    def test_very_narrow_width_still_renders(self):
        text = now_render.render_now([snap("等你的一條", WAITING, blocked_on="等你決定。")], 10)
        self.assertLessEqual(max(widths(text)), now_render.MIN_WIDTH)


class TestPlainLanguage(unittest.TestCase):
    def test_titles_and_labels_avoid_internal_codes_and_field_names(self):
        records = [snap("K1 單卡驗證", WAITING, doing="等在跑單卡驗證。",
                        blocked_on="等你按執行。", next_step="跑完記一筆。")]
        text = now_render.render_now(records, 45)
        self.assertNotIn("K1", text)
        self.assertIn("單卡驗證", text)
        for label in ("現在", "卡在", "下一步"):
            self.assertIn(label, text)
        for internal in ("doing", "blocked_on", "next_step", "waiting_on"):
            self.assertNotIn(internal, text)

    def test_placeholder_sentences_are_not_printed(self):
        text = now_render.render_now([snap("進行中的一條", ACTIVE, doing="正在跑測試。")], 45)
        self.assertIn("正在跑測試。", text)
        self.assertNotIn("沒有卡點。", text)
        self.assertNotIn("沒有寫下一步。", text)

    def test_repeated_sentence_is_shown_once(self):
        same = "等你回電腦重開 agent-watch。"
        text = now_render.render_now(
            [snap("等你的一條", WAITING, doing=same, blocked_on=same, next_step=same)], 45)
        self.assertEqual(text.count("等你回電腦重開"), 1)

    def test_finished_lines_are_a_compact_list(self):
        records = [snap("等你的一條", WAITING, blocked_on="等你決定。"),
                   snap("完成甲", DONE), snap("完成乙", DONE), snap("完成丙", DONE)]
        text = now_render.render_now(records, 45)
        self.assertNotIn("[完成]", text)
        self.assertIn("已完成 3 條", text)
        for title in ("完成甲", "完成乙", "完成丙"):
            self.assertEqual(text.count(title), 1)


HEADER = ("| date | id | parent | kind | spec | summary | decision | evidence "
          "| status | agent | merge_target |")
SEPARATOR = "| --- | " * 10 + "--- |"


def status_text(name, status="進行中", waiting_on="", next_step="把下一件事做完。",
                updated="2026-10-27"):
    return (f'<!-- dashboard:v1\nname: "{name}"\nproject: "pai"\nstatus: "{status}"\n'
            f'last_updated: "{updated}"\nnext_step: "{next_step}"\n'
            f'waiting_on: "{waiting_on}"\n-->\n# 內文不該被讀成表頭\n')


def decision_row(node_id="n1", status="done", summary="完成一件小事",
                 evidence="/tmp/REPORT.md", date="2026-10-27"):
    cells = [date, node_id, "-", "branch", "s§1", summary, "決定", evidence,
             status, "-", "-"]
    return "| " + " | ".join(cells) + " |"


class TestFixtureBoard(unittest.TestCase):
    """End to end on fixture files, so the test never depends on this Mac's status."""

    def test_fixture_board_keeps_attention_first_and_fits_45_columns(self):
        long_note = ("這是一段很長的中文說明，裡面還有 line_status.py 這種路徑與 "
                     "commit ac52b4d 這種代號，最後用 END 收尾。")
        with tempfile.TemporaryDirectory(prefix="aw-now-status-") as status, \
                tempfile.TemporaryDirectory(prefix="aw-now-board-") as board:
            status_path, board_path = Path(status), Path(board)
            # A stale 完成 header with a newer 等你 node: must still show a card.
            (status_path / "stale.md").write_text(
                status_text("表頭已寫完成的一條", status="完成", next_step=long_note),
                encoding="utf-8")
            (board_path / "stale.md").write_text(
                "\n".join(["# stale", "", HEADER, SEPARATOR,
                           decision_row("wait1", status="waiting",
                                        summary="等你選 A 或 B。"), ""]),
                encoding="utf-8")
            (status_path / "live.md").write_text(
                status_text("正在跑的一條", status="進行中"), encoding="utf-8")
            (status_path / "old.md").write_text(
                status_text("早就完成的一條", status="告一段落"), encoding="utf-8")
            records = line_status.build_snapshots(status_path, board_path)
            self.assertEqual(len(records), 3)
            text = now_render.render_now(records, 45)
            self.assertNotEqual(text, now_render.QUIET_LINE)
            self.assertIn("[等你]", text)
            self.assertIn("[進行中]", text)
            self.assertLess(text.index("[等你]"), text.index("[進行中]"))
            self.assertLessEqual(max(widths(text)), 45)
            self.assertIn("已完成 1 條", text)
            for width in (36, 30):
                narrow = now_render.render_now(records, width)
                self.assertLessEqual(max(widths(narrow)), width)


class TestRealData(unittest.TestCase):
    @unittest.skipUnless((Path.home() / "Projects" / "status").is_dir(), "no real status data")
    def test_real_status_and_decisions_render_within_45_columns(self):
        records = line_status.build_snapshots()
        if not records:
            self.skipTest("this machine has no status records")
        text = now_render.render_now(records, 45)
        self.assertLessEqual(max(widths(text)), 45)
        live = [item for item in records
                if item.state in (BLOCKED, WAITING, ACTIVE)]
        if live:
            # Every attention line is carded before the finished summary.
            finished_at = text.find("\n  已完成 ")
            for item in live:
                first = text.find(item.title)
                self.assertNotEqual(first, -1, item.title)
                if finished_at != -1:
                    self.assertLess(first, finished_at)


if __name__ == "__main__":
    unittest.main()
