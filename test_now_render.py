"""現況頁渲染測試：45 欄、等你／被擋在前、空閒時只有一行。"""

import unittest

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


class TestRealData(unittest.TestCase):
    def test_real_status_and_decisions_render_within_45_columns(self):
        records = line_status.build_snapshots()
        self.assertTrue(records)
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
