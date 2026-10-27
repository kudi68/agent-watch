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

    def test_decision_waiting_or_blocked_overrides_a_done_header(self):
        """A newer node that still needs someone must not be hidden by a stale 完成."""
        self.write(self.status, "stale.md", status_text("舊表頭", status="完成", waiting_on=""))
        self.write(self.decisions, "stale.md", decisions(
            row("wait1", status="waiting", summary="等你選 A 或 B。")))
        self.write(self.status, "stuck.md", status_text("卡住的線", status="告一段落"))
        self.write(self.decisions, "stuck.md", decisions(
            row("stuck1", status="blocked", summary="找不到憑證。")))
        self.write(self.status, "agrees.md", status_text("真的完成", status="完成"))
        self.write(self.decisions, "agrees.md", decisions(
            row("done1", status="done", summary="已經收尾。")))
        records = {record.name: record for record in self.build()}
        self.assertEqual(records["stale"].state, line_status.WAITING)
        self.assertEqual(records["stale"].decision_id, "wait1")
        self.assertEqual(records["stuck"].state, line_status.BLOCKED)
        self.assertEqual(records["agrees"].state, line_status.DONE)
        # The merged states still sort attention first, so the card is never skipped.
        self.assertEqual([record.name for record in self.build()][:2], ["stuck", "stale"])
        # Decision text alone decides when the header says nothing; running never
        # downgrades an explicitly finished header.
        self.assertEqual(line_status.classify_state("", "", "waiting"), line_status.WAITING)
        self.assertEqual(line_status.classify_state("", "", "running"), line_status.ACTIVE)
        self.assertEqual(line_status.classify_state("完成", "", "running"), line_status.DONE)

    def test_summaries_drop_internal_codes_but_keep_evidence(self):
        self.write(self.status, "a.md", status_text("甲線"))
        self.write(self.decisions, "a.md", decisions(
            row("n1", status="running",
                summary="P1-13 交給 tier-flash，K1 驗證。後面不用顯示。")))
        self.write(self.status, "b.md", status_text("乙線", waiting_on="等回覆"))
        self.write(self.decisions, "b.md", decisions(
            row("n2", status="running",
                summary="PR #47、#48 已合併；commit ac52b4d，4.88% 誤判。")))
        records = {record.name: record for record in self.build()}
        doing = records["a"].doing
        self.assertIsNone(line_status.CODE_RE.search(doing), doing)
        self.assertNotIn("P1-13", doing)
        self.assertNotIn("tier-flash", doing)
        self.assertNotIn("K1", doing)
        self.assertIn("驗證", doing)
        self.assertNotIn("後面不用顯示", doing)
        kept = records["b"].doing
        self.assertIsNone(line_status.CODE_RE.search(kept), kept)
        self.assertIn("PR 47", kept)  # PR numbers stay, only the # goes
        self.assertIn("ac52b4d", kept)
        self.assertIn("4.88%", kept)
        # Severity words are human vocabulary: they become words, not blanks.
        self.write(self.status, "d.md", status_text("丁線"))
        self.write(self.decisions, "d.md", decisions(
            row("n4", status="running", summary="修完三個 P1，P1-13 留到下包。")))
        severity = {r.name: r for r in self.build()}["d"].doing
        self.assertIn("三個 必修", severity)
        self.assertNotIn("P1-13", severity)
        self.assertIsNone(line_status.CODE_RE.search(severity), severity)
        # A summary that was nothing but codes falls back to a readable placeholder.
        self.write(self.status, "c.md", status_text("丙線"))
        self.write(self.decisions, "c.md", decisions(row("n3", status="running", summary="K1")))
        self.assertEqual({r.name: r for r in self.build()}["c"].doing,
                         line_status.FALLBACK_DOING[line_status.ACTIVE])

    def test_summaries_keep_paths_and_readable_names(self):
        """The code cleaner must not eat characters out of real paths or product names."""
        for text in ("請看 /tmp/R2.md。",
                     "請看 /tmp/aw-genkyo-c1c2-fix/REPORT.md。",
                     "下一步驗證 USB3 與 S3。",
                     "換到 RTX4090 跑 GPT5。",
                     "分支 agent/aw-genkyo-c2-20261027 已 commit。"):
            self.assertEqual(line_status.human_text(text), text)
        # Removing a code still tidies the hole it left, and only that hole.
        self.assertEqual(line_status.human_text("K1、WP6、G7 都好了。"), "都好了。")
        self.assertEqual(line_status.human_text("P1-13 交給 tier-flash，K1 驗證。"), "交給，驗證。")

    def test_absolute_paths_survive_colon_bracket_and_list_separators(self):
        for text in ("請看：/tmp/report.md。",
                     "證據（/tmp/report.md）",
                     "4.88%、/tmp/report.md。"):
            self.assertEqual(line_status.human_text(text), text)
        self.assertEqual(line_status.human_text("PR #14、/tmp/report.md。"),
                         "PR 14、/tmp/report.md。")  # the # still goes, the 、 and / stay

    def test_paths_survive_the_whole_card(self):
        """Fidelity has to hold through build_snapshots, not only in the helper."""
        self.write(self.status, "a.md",
                   status_text("甲線", next_step="照 /tmp/aw-genkyo-c1c2-fix/REPORT.md 收尾。"))
        self.write(self.decisions, "a.md", decisions(
            row("n1", status="running", summary="K1 已修，證據 /tmp/R2.md。",
                evidence="/tmp/R2.md")))
        record = {item.name: item for item in self.build()}["a"]
        self.assertEqual(record.doing, "已修，證據 /tmp/R2.md。")
        self.assertEqual(record.next_step, "照 /tmp/aw-genkyo-c1c2-fix/REPORT.md 收尾。")
        self.assertEqual(record.report, "/tmp/R2.md")

    def test_code_beside_an_absolute_path_keeps_the_leading_slash(self):
        """A cut code must not take the first / of the path that follows it."""
        self.assertEqual(line_status.human_text("證據 K1 /tmp/report.md。"),
                         "證據 /tmp/report.md。")
        self.assertEqual(line_status.human_text("K1 /tmp/R2.md"), "/tmp/R2.md")
        self.assertEqual(line_status.human_text("K1、/tmp/R2.md"), "/tmp/R2.md")
        self.assertEqual(line_status.human_text("看 /tmp/R2.md K1。"), "看 /tmp/R2.md。")
        self.assertEqual(line_status.human_text("K1 ~/R2.md 與 ./P2.txt。"),
                         "~/R2.md 與 ./P2.txt。")
        # A real list separator between two codes is still tidied.
        self.assertEqual(line_status.human_text("K1＋G7 都好了。"), "都好了。")

    def test_conjunction_cleanup_only_touches_the_hole(self):
        """A 與／及／和／或 inside a URL or path is evidence, not a dangling conjunction."""
        self.assertEqual(line_status.human_text("證據 K1 與 https://example.org/和。"),
                         "證據 與 https://example.org/和。")
        self.assertEqual(line_status.human_text("K1 看 /tmp/與。"), "看 /tmp/與。")
        self.assertEqual(line_status.human_text("K1 看 https://example.org/或，再說。"),
                         "看 https://example.org/或，再說。")
        # A conjunction that really did lose its only object still goes.
        self.assertEqual(line_status.human_text("修好 A 與 K1。"), "修好 A。")
        self.assertEqual(line_status.human_text("修好 A 和 K1，再驗證。"), "修好 A，再驗證。")

    def test_paths_before_pr_and_severity_rewrites_stay_as_written(self):
        self.assertEqual(line_status.human_text("K1 已修，請看 /tmp/P1.md。"),
                         "已修，請看 /tmp/P1.md。")
        self.assertEqual(line_status.human_text("看 ./P2.txt 與 #14。"), "看 ./P2.txt 與 PR 14。")

    def test_slash_path_and_url_survive_the_whole_card(self):
        self.write(self.status, "a.md", status_text(
            "甲線", waiting_on="K1 /tmp/blocked.md", next_step="K1 /tmp/next.md。"))
        self.write(self.decisions, "a.md", decisions(
            row("n1", status="running", summary="證據 K1 /tmp/report.md。")))
        record = {item.name: item for item in self.build()}["a"]
        self.assertEqual(record.doing, "證據 /tmp/report.md。")
        self.assertEqual(record.blocked_on, "/tmp/blocked.md")
        self.assertEqual(record.next_step, "/tmp/next.md。")
        self.write(self.status, "b.md", status_text(
            "乙線", next_step="證據 K1 與 https://example.org/和。"))
        record = {item.name: item for item in self.build()}["b"]
        self.assertEqual(record.next_step, "證據 與 https://example.org/和。")

    def test_leftover_code_check_ignores_protected_evidence(self):
        """The live-data check must not call a kept path or product name a leftover code."""
        def leftover(value):
            bare = line_status.KEEP_AS_WRITTEN_RE.sub("", value)
            return [token.group(0) for token in line_status.CODE_RE.finditer(bare)
                    if line_status._is_step_code(token.group(0))]
        self.assertEqual(leftover(line_status.human_text("K1 看 /tmp/R2.md 與 USB3。")), [])
        self.assertEqual(leftover("獨立的 K1 沒清掉"), ["K1"])

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
            for field_name in ("doing", "blocked_on", "next_step"):
                value = getattr(record, field_name)
                # Paths, URLs and product names are kept on purpose (/tmp/R2.md, USB3).
                bare = line_status.KEEP_AS_WRITTEN_RE.sub("", value)
                left = [token.group(0) for token in line_status.CODE_RE.finditer(bare)
                        if line_status._is_step_code(token.group(0))]
                self.assertEqual(left, [], f"{record.name}.{field_name}: {value}")
            self.assertTrue(record.doing and record.blocked_on and record.next_step)
            self.assertNotIn("\n", record.doing + record.blocked_on + record.next_step)
