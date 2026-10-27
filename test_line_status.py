"""Tests for the 現況 data layer: status frontmatter + decisions -> one record per line."""

import tempfile
import unittest
from pathlib import Path

import line_status

HEADER = ("| date | id | parent | kind | spec | summary | decision | evidence "
          "| status | agent | merge_target |")
SEPARATOR = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"


def status_text(name, status="進行中", waiting_on="", next_step="把下一件事做完。",
                updated="2026-10-27"):
    return (f'<!-- dashboard:v1\nname: "{name}"\nproject: "pai"\nstatus: "{status}"\n'
            f'last_updated: "{updated}"\nnext_step: "{next_step}"\n'
            f'waiting_on: "{waiting_on}"\n-->\n# 內文不該被讀成表頭\n')


def row(node_id="n1", parent="-", status="done", summary="完成一件小事",
        evidence="/tmp/REPORT.md", date="2026-10-27"):
    cells = [date, node_id, parent, "branch", "s§1", summary, "決定", evidence,
             status, "-", "-"]
    return "| " + " | ".join(cells) + " |"


def decisions(*rows, title="# demo 決策節點"):
    return "\n".join([title, "", HEADER, SEPARATOR, *rows, ""])


class LineStatusTests(unittest.TestCase):
    def setUp(self):
        self.status = tempfile.TemporaryDirectory(prefix="aw-line-status-")
        self.decisions = tempfile.TemporaryDirectory(prefix="aw-line-decisions-")
        self.addCleanup(self.status.cleanup)
        self.addCleanup(self.decisions.cleanup)

    def write(self, folder, name, text):
        (Path(folder.name) / name).write_text(text, encoding="utf-8")

    def build(self):
        return line_status.build_snapshots(self.status.name, self.decisions.name)

    def test_waiting_outranks_active_and_sorts_first(self):
        fields = line_status.parse_frontmatter(status_text("日報", waiting_on="等博元選 A"))
        self.assertEqual((fields["status"], fields["waiting_on"]), ("進行中", "等博元選 A"))
        self.assertNotIn("內文", fields)
        self.assertEqual(line_status.parse_frontmatter("# 沒有表頭"), {})
        self.assertEqual(line_status.one_sentence("第一句。第二句"), "第一句。")
        self.assertTrue(line_status.one_sentence("字" * 300).endswith("…"))
        cases = [("a.md", "甲線", "進行中", "", "2026-10-27"),
                 ("b.md", "乙線", "進行中", "等博元選 A 或 B", "2026-10-26"),
                 ("c.md", "丙線", "等待中", "等回覆", "2026-10-25"),
                 ("d.md", "丁線", "告一段落", "", "2026-10-24"),
                 ("e.md", "戊線", "被擋", "缺資料", "2026-10-23")]
        for name, title, state, waiting, updated in cases:
            self.write(self.status, name,
                       status_text(title, status=state, waiting_on=waiting, updated=updated))
        records = self.build()
        self.assertEqual([r.state for r in records],
                         ["blocked", "waiting", "waiting", "active", "done"])
        self.assertEqual(records[0].blocked_on, "缺資料")
        self.assertEqual(records[1].blocked_on, "等博元選 A 或 B")
        self.assertEqual(line_status.STATE_LABEL[records[1].state], "等你")

    def test_titles_never_keep_internal_codes(self):
        for raw in ("K1 單卡驗證", "P1-13 修正", "tier-flash 練習", "C1 切換"):
            title = line_status.human_title(raw, "pai")
            self.assertTrue(title)
            self.assertIsNone(line_status.CODE_RE.search(title), title)
        self.assertEqual(line_status.human_title("K1", ""), "未命名線")
        self.assertEqual(line_status.human_title("agent-watch v10", ""), "agent-watch v10")

    def test_decisions_current_node_and_decisions_only_line(self):
        self.write(self.status, "a.md", status_text("甲線"))
        self.write(self.decisions, "a.md", decisions(
            row("n1", status="done", summary="舊的完成事項"),
            row("n2", parent="n1", status="running",
                summary="正在修表格。後面不用顯示。", evidence="/tmp/R2.md")))
        self.write(self.decisions, "solo.md", decisions(row("s1", summary="只有決策檔")))
        records = {record.name: record for record in self.build()}
        self.assertEqual((records["a"].state, records["a"].doing, records["a"].decision_id),
                         ("active", "正在修表格。", "n2"))
        self.assertEqual(records["a"].report, "/tmp/R2.md")
        self.assertEqual((records["solo"].state, records["solo"].title), ("done", "demo"))

    @unittest.skipUnless((Path.home() / "Projects" / "status").is_dir(), "no real status")
    def test_real_status_and_decisions_produce_one_record_per_line(self):
        folder = Path.home() / "Projects" / "status"
        expected = {path.stem for path in folder.glob("*.md")
                    if ".bak-" not in path.name
                    and line_status.parse_frontmatter(path.read_text(encoding="utf-8"))}
        records = line_status.build_snapshots()
        names = [record.name for record in records]
        self.assertTrue(expected)
        self.assertEqual(len(names), len(set(names)))
        self.assertTrue(expected <= set(names))
        states = [record.state for record in records]
        self.assertEqual(states, sorted(states, key=line_status.STATE_ORDER.__getitem__))
        for record in records:
            self.assertIsNone(line_status.CODE_RE.search(record.title), record.title)
            self.assertTrue(record.doing and record.blocked_on and record.next_step)
            self.assertNotIn("\n", record.doing + record.blocked_on + record.next_step)
