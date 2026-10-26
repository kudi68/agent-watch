"""Tests for the 專案樹 tab: decisions parsing, tree building, current node, rendering."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import watch


HEADER = (
    "| date | id | parent | kind | spec | summary | decision | evidence "
    "| status | agent | merge_target |"
)
SEPARATOR = "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |"


def decision_row(
    date="2026-10-26",
    node_id="n1",
    parent="-",
    kind="trunk",
    spec="s§1",
    summary="摘要",
    decision="決定",
    evidence="/tmp/REPORT.md",
    status="done",
    agent="-",
    merge_target="-",
):
    cells = [date, node_id, parent, kind, spec, summary, decision, evidence,
             status, agent, merge_target]
    return "| " + " | ".join(cells) + " |"


def decisions_file(*rows, title="# demo 決策節點"):
    return "\n".join([title, "", HEADER, SEPARATOR, *rows, ""])


class DecisionParseTests(unittest.TestCase):
    def test_header_separator_and_values_are_parsed(self):
        nodes, problems, title = watch.parse_decision_table(
            decisions_file(decision_row(node_id="goal", summary="主幹目標"))
        )
        self.assertEqual(problems, [])
        self.assertEqual(title, "demo 決策節點")
        self.assertEqual(len(nodes), 1)
        node = nodes[0]
        self.assertEqual(node.id, "goal")
        self.assertEqual(node.kind, "trunk")
        self.assertEqual(node.summary, "主幹目標")
        self.assertEqual(node.status, "done")

    def test_short_separator_row_is_not_reported_as_bad_columns(self):
        text = "\n".join([
            HEADER,
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
            decision_row(node_id="goal"),
        ])
        nodes, problems, _title = watch.parse_decision_table(text)
        self.assertEqual(problems, [])
        self.assertEqual([node.id for node in nodes], ["goal"])

    def test_wrong_column_count_becomes_problem_not_exception(self):
        text = decisions_file(
            decision_row(node_id="goal"),
            "| 2026-10-26 | broken | goal |",
        )
        nodes, problems, _title = watch.parse_decision_table(text)
        self.assertEqual([node.id for node in nodes], ["goal"])
        self.assertEqual(len(problems), 1)
        self.assertIn("欄數 3", problems[0])
        self.assertIn("L6", problems[0])

    def test_duplicate_id_keeps_last_status_row_in_first_position(self):
        text = decisions_file(
            decision_row(node_id="goal", summary="舊摘要", status="running", agent="Codex"),
            decision_row(node_id="next", parent="goal", kind="branch"),
            decision_row(date="2026-10-27", node_id="goal", summary="新摘要", status="done"),
        )
        nodes, problems, _title = watch.parse_decision_table(text)
        self.assertEqual(problems, [])
        self.assertEqual([node.id for node in nodes], ["goal", "next"])
        self.assertEqual(nodes[0].summary, "新摘要")
        self.assertEqual(nodes[0].status, "done")
        self.assertEqual(nodes[0].date, "2026-10-27")

    def test_duplicate_id_with_changed_parent_is_reported(self):
        text = decisions_file(
            decision_row(node_id="goal"),
            decision_row(node_id="next", parent="goal", kind="branch"),
            decision_row(node_id="next", parent="-", kind="branch"),
        )
        _nodes, problems, _title = watch.parse_decision_table(text)
        self.assertTrue(any("沿用最後一列" in problem for problem in problems))

    def test_bad_id_date_and_status_are_reported(self):
        text = decisions_file(
            decision_row(node_id="Bad_Id"),
            decision_row(node_id="goal", date="2026/10/26", status="shipping"),
        )
        nodes, problems, _title = watch.parse_decision_table(text)
        self.assertEqual([node.id for node in nodes], ["goal"])
        joined = "\n".join(problems)
        self.assertIn("格式不對", joined)
        self.assertIn("日期格式異常", joined)
        self.assertIn("未知狀態", joined)

    def test_missing_id_is_reported(self):
        _nodes, problems, _title = watch.parse_decision_table(
            decisions_file(decision_row(node_id=""))
        )
        self.assertTrue(any("缺少 id" in problem for problem in problems))


class DecisionTreeTests(unittest.TestCase):
    def build(self, *rows):
        return watch.build_decision_line("demo", decisions_file(*rows))

    def test_roots_are_sorted_trunk_first_and_children_are_linked(self):
        line = self.build(
            decision_row(node_id="side", kind="branch"),
            decision_row(node_id="goal", kind="trunk"),
            decision_row(date="2026-10-25", node_id="old", parent="goal", kind="branch"),
            decision_row(date="2026-10-26", node_id="new", parent="goal", kind="branch"),
        )
        self.assertEqual([root.id for root in line.roots], ["goal", "side"])
        self.assertEqual([child.id for child in line.roots[0].children], ["old", "new"])

    def test_missing_parent_moves_node_to_problems(self):
        line = self.build(
            decision_row(node_id="goal"),
            decision_row(node_id="lost", parent="nowhere", kind="branch"),
        )
        self.assertEqual([node.id for node in line.nodes], ["goal"])
        self.assertNotIn("lost", [child.id for child in line.roots[0].children])
        self.assertTrue(any("parent「nowhere」找不到" in p for p in line.problems))
        self.assertIs(line.current, line.nodes[0])

    def test_parent_cycle_is_reported_and_kept_out_of_the_tree(self):
        line = self.build(
            decision_row(node_id="goal"),
            decision_row(node_id="a", parent="b", kind="branch"),
            decision_row(node_id="b", parent="a", kind="branch"),
        )
        self.assertEqual([node.id for node in line.nodes], ["goal"])
        self.assertTrue(any("迴圈" in problem for problem in line.problems))
        self.assertEqual([node.id for node in line.roots], ["goal"])
        self.assertEqual(line.current.id, "goal")

    def test_empty_file_still_builds_a_line(self):
        line = watch.build_decision_line("empty", "# empty\n\n沒有表格\n")
        self.assertEqual(line.nodes, [])
        self.assertEqual(line.problems, [])
        self.assertIsNone(line.current)


class CurrentNodeTests(unittest.TestCase):
    def node(self, node_id, date, status, row):
        return watch.DecisionNode(
            line="demo", row=row, date=date, id=node_id, parent="-", kind="branch",
            spec="-", summary="-", decision="-", evidence="-", status=status,
            agent="-", merge_target="-",
        )

    def test_latest_not_done_node_wins(self):
        nodes = [
            self.node("a", "2026-10-25", "running", 5),
            self.node("b", "2026-10-26", "done", 6),
            self.node("c", "2026-10-24", "waiting", 7),
        ]
        self.assertEqual(watch.pick_current_node(nodes).id, "a")

    def test_same_date_falls_back_to_the_later_row(self):
        nodes = [
            self.node("a", "2026-10-26", "waiting", 5),
            self.node("b", "2026-10-26", "running", 9),
        ]
        self.assertEqual(watch.pick_current_node(nodes).id, "b")

    def test_all_done_falls_back_to_newest_date(self):
        nodes = [
            self.node("a", "2026-10-24", "done", 5),
            self.node("b", "2026-10-26", "done", 6),
        ]
        self.assertEqual(watch.pick_current_node(nodes).id, "b")

    def test_unknown_status_counts_as_not_done(self):
        nodes = [
            self.node("a", "2026-10-24", "done", 5),
            self.node("b", "2026-10-25", "weird", 6),
        ]
        self.assertEqual(watch.pick_current_node(nodes).id, "b")

    def test_empty_list_has_no_current_node(self):
        self.assertIsNone(watch.pick_current_node([]))


class WrapTests(unittest.TestCase):
    def test_ascii_tokens_are_not_split_across_lines(self):
        source = "先跑 unittest 再 push PR #3"
        lines = watch.wrap_decision_text(source, 14)
        self.assertEqual("".join(lines), source)
        for token in ("unittest", "push", "PR", "#3"):
            self.assertTrue(any(token in line for line in lines), token)
            self.assertEqual(sum(1 for line in lines if token in line), 1, token)

    def test_closing_punctuation_does_not_start_a_line(self):
        lines = watch.wrap_decision_text("博元看過後開 PR（未合併）", 12)
        self.assertGreater(len(lines), 1)
        for line in lines[1:]:
            self.assertNotIn(line[0], watch.DECISION_NO_LINE_START, line)

    def test_overlong_token_falls_back_to_characters(self):
        lines = watch.wrap_decision_text("x" * 25, 10)
        self.assertEqual(lines, ["x" * 10, "x" * 10, "x" * 5])

    def test_zero_width_is_safe(self):
        self.assertEqual(watch.wrap_decision_text("abc", 0), [""])


class RenderTests(unittest.TestCase):
    def build_board(self, *rows, name="demo"):
        line = watch.build_decision_line(name, decisions_file(*rows))
        return watch.DecisionBoard(
            directory=Path("/nonexistent"), lines=[line], errors=[], refreshed_at=0.0,
        )

    def test_node_line_shows_date_summary_status_agent_and_current_marker(self):
        board = self.build_board(
            decision_row(node_id="goal", summary="主幹目標"),
            decision_row(node_id="work", parent="goal", kind="branch",
                         summary="目前工作", status="running", agent="Codex"),
        )
        markup = watch.render_decision_tree(board, 100)
        self.assertIn("demo", markup)
        self.assertIn("主幹目標", markup)
        self.assertIn("目前工作", markup)
        self.assertIn("running", markup)
        self.assertIn("@Codex", markup)
        self.assertIn("目前", markup)
        self.assertIn("work", markup)
        self.assertIn("▌ demo", markup)

    def test_current_marker_sits_on_the_not_done_node_only(self):
        board = self.build_board(
            decision_row(node_id="goal", summary="已完成"),
            decision_row(node_id="work", parent="goal", kind="branch",
                         summary="進行中", status="running", agent="Codex"),
        )
        markup = watch.render_decision_tree(board, 100)
        markers = [row for row in markup.splitlines() if "0f1117 on #9ece6a" in row]
        self.assertEqual(len(markers), 1)
        self.assertIn("work", markers[0])

    def test_all_done_line_marks_newest_node(self):
        board = self.build_board(
            decision_row(node_id="goal", summary="舊的", date="2026-10-24"),
            decision_row(node_id="later", parent="goal", kind="branch",
                         summary="新的", date="2026-10-26"),
        )
        markup = watch.render_decision_tree(board, 100)
        self.assertIn("目前", markup)
        self.assertIn("later", markup)

    def test_broken_rows_render_under_unparsable_section(self):
        board = self.build_board(
            decision_row(node_id="goal", summary="主幹"),
            "| 2026-10-26 | broken |",
        )
        markup = watch.render_decision_tree(board, 100)
        self.assertIn("無法解析", markup)
        self.assertIn("欄數 2", markup)
        self.assertIn("主幹", markup)

    def test_merge_back_branch_and_unknown_kind_are_labeled(self):
        board = self.build_board(
            decision_row(node_id="goal"),
            decision_row(node_id="back", parent="goal", kind="merge-back",
                         merge_target="goal"),
            decision_row(node_id="weird", parent="goal", kind="mystery"),
        )
        markup = watch.render_decision_tree(board, 120)
        self.assertIn("merge-back→goal", markup)
        self.assertIn("未知種類", markup)

    def test_render_survives_narrow_widths_and_markup_characters(self):
        board = self.build_board(
            decision_row(node_id="goal", summary="含 [brackets] 與 ｜ 符號"),
            decision_row(node_id="work", parent="goal", kind="branch",
                         summary="長" * 80, status="waiting", agent="-"),
        )
        for width in (30, 40, 80, 120):
            with self.subTest(width=width):
                markup = watch.render_decision_tree(board, width)
                self.assertIn("brackets", markup)

    def test_empty_board_says_no_decisions(self):
        board = watch.DecisionBoard(directory=Path("/nonexistent"), refreshed_at=0.0)
        markup = watch.render_decision_tree(board, 80)
        self.assertIn("找不到決策檔", markup)
        self.assertIn("0 條線", markup)

    def test_directory_errors_are_visible(self):
        board = watch.DecisionBoard(
            directory=Path("/nonexistent"), errors=["decisions 無法讀取"], refreshed_at=0.0,
        )
        markup = watch.render_decision_tree(board, 80)
        self.assertIn("decisions 無法讀取", markup)


class DecisionLoadTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="aw-tree-load-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)

    def write(self, name, text):
        (self.root / name).write_text(text, encoding="utf-8")

    def test_reads_every_md_except_format_and_backups(self):
        self.write("alpha.md", decisions_file(decision_row(node_id="a1", summary="甲線")))
        self.write("beta.md", decisions_file(decision_row(node_id="b1", summary="乙線")))
        self.write("FORMAT.md", "# 格式說明\n")
        self.write("alpha.md.bak-20261026", decisions_file(decision_row(node_id="old")))
        board = watch.load_decision_board(self.root)
        self.assertEqual([line.name for line in board.lines], ["alpha", "beta"])
        self.assertEqual(board.errors, [])

    def test_new_file_appears_without_configuration(self):
        first = watch.load_decision_board(self.root)
        self.assertEqual(first.lines, [])
        self.write("later.md", decisions_file(decision_row(node_id="late", summary="新線")))
        second = watch.load_decision_board(self.root)
        self.assertEqual([line.name for line in second.lines], ["later"])

    def test_unreadable_file_does_not_hide_other_lines(self):
        self.write("good.md", decisions_file(decision_row(node_id="g1")))
        (self.root / "bad.md").write_bytes(b"\xff\xfe\x00bad")
        board = watch.load_decision_board(self.root)
        self.assertEqual([line.name for line in board.lines], ["good"])
        self.assertEqual(len(board.errors), 1)
        self.assertIn("bad.md", board.errors[0])

    def test_missing_directory_is_empty_and_not_an_exception(self):
        board = watch.load_decision_board(self.root / "missing")
        self.assertEqual(board.lines, [])
        self.assertEqual(board.errors, [])

    def test_directory_that_is_a_file_is_reported(self):
        path = self.root / "not-a-dir.md"
        path.write_text("# nope\n", encoding="utf-8")
        board = watch.load_decision_board(path)
        self.assertEqual(board.lines, [])
        self.assertEqual(len(board.errors), 1)
        self.assertIn("不是資料夾", board.errors[0])


class DecisionServiceTests(unittest.TestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="aw-tree-svc-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        self.service = watch.DecisionService(self.root)

    def write(self, name, node_id):
        (self.root / name).write_text(
            decisions_file(decision_row(node_id=node_id)), encoding="utf-8",
        )

    def test_cache_holds_until_sixty_seconds_then_rereads(self):
        self.write("first.md", "first")
        start = 1_000.0
        board = self.service.board(now=start)
        self.assertEqual([line.name for line in board.lines], ["first"])
        self.write("second.md", "second")
        cached = self.service.board(now=start + 59)
        self.assertEqual([line.name for line in cached.lines], ["first"])
        refreshed = self.service.board(now=start + 60)
        self.assertEqual(
            [line.name for line in refreshed.lines], ["first", "second"],
        )

    def test_force_rereads_without_waiting(self):
        self.write("first.md", "first")
        self.service.board(now=1_000.0)
        self.write("second.md", "second")
        board = self.service.board(now=1_001.0, force=True)
        self.assertEqual([line.name for line in board.lines], ["first", "second"])

    def test_default_directory_can_be_overridden_by_env(self):
        with patch.dict(os.environ, {"AGENT_WATCH_DECISIONS": str(self.root)}):
            self.assertEqual(watch.decisions_directory(), self.root)
            service = watch.DecisionService()
            self.assertEqual(service.directory, self.root)


class TreeAppTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        directory = tempfile.TemporaryDirectory(prefix="aw-tree-app-")
        self.addCleanup(directory.cleanup)
        self.root = Path(directory.name)
        setting = patch.dict(
            os.environ, {"AGENT_WATCH_STATE": str(self.root / "state.json")},
        )
        setting.start()
        self.addCleanup(setting.stop)
        self.decisions = self.root / "decisions"
        self.decisions.mkdir()
        (self.decisions / "demo.md").write_text(
            decisions_file(
                decision_row(node_id="goal", summary="主幹目標"),
                decision_row(node_id="work", parent="goal", kind="branch",
                             summary="目前工作進行中", status="running", agent="Codex"),
            ),
            encoding="utf-8",
        )

    def make_app(self):
        app = watch.AgentWatchApp(
            runs_path=self.root / "runs.tsv",
            done_path=self.root / "done.tsv",
            decisions_path=self.decisions,
        )
        app.quota_svc.refresh_in_background = lambda force=False: False
        app.data_svc.refresh_in_background = lambda force=False: False
        return app

    async def screen_text(self, app):
        return "\n".join(strip.text for strip in app.screen._compositor.render_strips())

    async def test_tab_five_shows_the_tree_and_marks_the_current_node(self):
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            self.assertIsNotNone(app.query_one("#tab-tree"))
            await pilot.press("5")
            await pilot.pause()
            self.assertEqual(app.query_one("#main-tabs", watch.TabbedContent).active, "tab-tree")
            text = await self.screen_text(app)
            self.assertIn("專案樹", text)
            self.assertIn("demo", text)
            self.assertIn("目前工作進行中", text)
            self.assertIn("目前", text)

    async def test_sixty_second_refresh_shows_a_brand_new_line(self):
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("5")
            await pilot.pause()
            (self.decisions / "brand-new.md").write_text(
                decisions_file(decision_row(node_id="new-1", summary="全新的線")),
                encoding="utf-8",
            )
            app.refresh_decisions()
            await pilot.pause()
            text = await self.screen_text(app)
            self.assertIn("brand-new", text)
            self.assertIn("全新的線", text)

    async def test_manual_reload_action_shows_a_brand_new_line(self):
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("5")
            await pilot.pause()
            (self.decisions / "manual.md").write_text(
                decisions_file(decision_row(node_id="manual-1", summary="手動重讀的線")),
                encoding="utf-8",
            )
            app.action_reload_decisions()
            await pilot.pause()
            text = await self.screen_text(app)
            self.assertIn("manual", text)
            self.assertIn("手動重讀的線", text)

    async def test_uppercase_r_key_rereads_the_folder(self):
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("5")
            await pilot.pause()
            (self.decisions / "keyed.md").write_text(
                decisions_file(decision_row(node_id="keyed-1", summary="按 R 才看到的線")),
                encoding="utf-8",
            )
            await pilot.press("R")
            await pilot.pause()
            text = await self.screen_text(app)
            self.assertIn("keyed", text)
            self.assertIn("按 R 才看到的線", text)

    async def test_refresh_timer_uses_sixty_seconds(self):
        self.assertEqual(watch.DECISIONS_REFRESH_SECONDS, 60)
        app = self.make_app()
        async with app.run_test(size=(80, 30)) as pilot:
            await pilot.pause()
            try:
                intervals = [timer._interval for timer in app._timers]
            except AttributeError:  # Textual changed its private timer fields
                self.skipTest("timer internals unavailable")
            self.assertIn(float(watch.DECISIONS_REFRESH_SECONDS), intervals)

    async def test_broken_rows_do_not_break_the_page(self):
        (self.decisions / "broken.md").write_text(
            "# broken\n\n" + HEADER + "\n" + SEPARATOR + "\n| 2026-10-26 | only |\n",
            encoding="utf-8",
        )
        app = self.make_app()
        async with app.run_test(size=(100, 40)) as pilot:
            await pilot.pause()
            await pilot.press("5")
            await pilot.pause()
            text = await self.screen_text(app)
            self.assertIn("無法解析", text)
            self.assertIn("主幹目標", text)

    async def test_bindings_stay_unique_after_adding_the_tree_tab(self):
        keys = [binding[0] for binding in watch.AgentWatchApp.BINDINGS]
        self.assertEqual(keys.count("5"), 1)
        self.assertEqual(keys.count("R"), 1)
        self.assertEqual(keys.count("r"), 1)
        self.assertEqual(len(keys), len(set(keys)))
        self.assertIn("5", keys)


if __name__ == "__main__":
    unittest.main()
