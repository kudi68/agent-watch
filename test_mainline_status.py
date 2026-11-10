"""A3: read-only 主線現況 (MAINLINE-STATUS.md) panel: parse, one line per item, placeholder."""
import asyncio
import contextlib
import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import Mock, patch

import mainline_status
import watch


NEW_FORMAT = """# 主線現況（MAINLINE-STATUS）

## 逐項
| 項目 | 狀態 | 離開條件 | 證據路徑 |
|---|---|---|---|
| 1.4 通知 | VERIFIED | 無（已上線） | `20261109/a1/REPORT.md` |
| 1.9 乾淨 ACCEPTANCE | BLOCKED | 等博元決定要不要重跑 | `20261110/a1/REPORT.md` 第 1 行 NO |
| 影子跑 11/11 | UNKNOWN | 11/11 00:00 開跑後補證據 | （尚未產生） |
"""

LEGACY_FORMAT = """## 3. 逐卡表
| 卡 | 目的 | 狀態 | 證據 | 在等什麼 | 負責人 | 期限 |
|---|---|---|---|---|---|---|
| K1 ab-v5-readback | 讀回真消費 | **FAIL** | `20261029/k1-readback-1416/REPORT.md` 第 1 行 FAIL（iCloud 未同步） | 75A 設計卡 | Codex | 11/01 |
| H2 hermes-24h-slice | 24h 切片 | **DONE** | `20261028/hermes-24h-slice/REPORT.md` | 博元套用 | OpenCode | 10/30 |
"""


class ParseStatusTests(unittest.TestCase):
    def test_new_format_table_gives_one_item_per_row(self):
        items = mainline_status.parse_status_document(NEW_FORMAT)
        self.assertEqual([i.item for i in items], ["1.4 通知", "1.9 乾淨 ACCEPTANCE", "影子跑 11/11"])
        self.assertEqual([i.status for i in items], ["VERIFIED", "BLOCKED", "UNKNOWN"])
        self.assertEqual(items[0].exit_condition, "無（已上線）")
        self.assertEqual(items[0].evidence, "20261109/a1/REPORT.md")
        self.assertEqual(items[1].evidence, "20261110/a1/REPORT.md")
        self.assertEqual(items[2].exit_condition, "11/11 00:00 開跑後補證據")

    def test_legacy_card_table_maps_等什麼_and_證據(self):
        items = mainline_status.parse_status_document(LEGACY_FORMAT)
        self.assertEqual(len(items), 2)
        self.assertEqual(items[0].item, "K1 ab-v5-readback")
        self.assertEqual(items[0].status, "BLOCKED")  # FAIL -> BLOCKED, not invented
        self.assertEqual(items[0].evidence, "20261029/k1-readback-1416/REPORT.md")
        self.assertEqual(items[0].exit_condition, "75A 設計卡")
        self.assertEqual(items[1].status, "VERIFIED")  # DONE -> VERIFIED

    def test_pipe_lines_are_the_fallback_when_no_table_exists(self):
        text = "- 1.4 通知｜VERIFIED｜離開：無｜證據：`20261109/a1/REPORT.md`\n"
        items = mainline_status.parse_status_document(text)
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0].item, "1.4 通知")
        self.assertEqual(items[0].status, "VERIFIED")
        self.assertEqual(items[0].exit_condition, "離開：無")
        self.assertEqual(items[0].evidence, "20261109/a1/REPORT.md")

    def test_status_normalisation_keeps_unknown_wording_as_written(self):
        self.assertEqual(mainline_status.normalize_status("**PARTIAL**"), "PARTIAL")
        self.assertEqual(mainline_status.normalize_status("跑中"), "PARTIAL")
        self.assertEqual(mainline_status.normalize_status("待確認"), "待確認")
        self.assertEqual(mainline_status.normalize_status(""), "UNKNOWN")

    def test_table_without_a_status_column_yields_no_items(self):
        text = "| 實作 | 審查（模型） | 結果 |\n|---|---|---|\n| a | b | PASS |\n"
        self.assertEqual(mainline_status.parse_status_document(text), [])

    def test_evidence_prefers_the_backticked_path(self):
        cell = "`20261028/na2-n7b-p35-fix1/REPORT.md` 第 1 行 DONE；另見 /tmp/x"
        self.assertEqual(
            mainline_status.evidence_path(cell),
            "20261028/na2-n7b-p35-fix1/REPORT.md",
        )


class PanelTextTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.base = Path(self._tmp.name)

    def test_each_item_is_one_line_with_status_exit_and_evidence(self):
        source = self.base / "MAINLINE-STATUS.md"
        source.write_text(NEW_FORMAT, encoding="utf-8")
        text = watch.mainline_status_text(200, path=source)
        lines = text.splitlines()
        self.assertEqual(len(lines), 4)  # header + 3 items, no wrapped rows
        self.assertIn("3 項", lines[0])
        self.assertIn("VERIFIED", lines[1])
        self.assertIn("1.4 通知", lines[1])
        self.assertIn("離開：無（已上線）", lines[1])
        self.assertIn("證據：20261109/a1/REPORT.md", lines[1])
        self.assertIn("BLOCKED", lines[2])

    def test_narrow_width_keeps_every_row_on_one_line(self):
        source = self.base / "MAINLINE-STATUS.md"
        source.write_text(NEW_FORMAT, encoding="utf-8")
        for width in (40, 60, 100):
            text = watch.mainline_status_text(width, path=source)
            self.assertTrue(text.splitlines())
            for line in text.splitlines():
                self.assertLessEqual(watch.display_width(line), width, line)

    def test_missing_file_shows_a_placeholder_and_does_not_raise(self):
        missing = self.base / "MAINLINE-STATUS.md"
        text = watch.mainline_status_text(80, path=missing)
        self.assertIn("讀不到", text)
        self.assertIn("檔案不存在", text)
        self.assertIn(str(missing), text)
        self.assertIn("占位", text)
        board = mainline_status.load_status_board(missing)
        self.assertEqual(board.items, ())
        self.assertEqual(board.error, "檔案不存在")

    def test_directory_instead_of_file_is_a_placeholder_too(self):
        text = watch.mainline_status_text(80, path=self.base)
        self.assertIn("路徑是目錄", text)
        self.assertIn("占位", text)

    def test_file_without_status_rows_is_a_placeholder(self):
        source = self.base / "MAINLINE-STATUS.md"
        source.write_text("# 只有說明\n還沒有表格。\n", encoding="utf-8")
        text = watch.mainline_status_text(80, path=source)
        self.assertIn("找不到含狀態欄的項目列", text)
        self.assertIn("占位", text)

    def test_env_override_and_default_path(self):
        source = self.base / "custom-status.md"
        source.write_text(NEW_FORMAT, encoding="utf-8")
        with patch.dict(os.environ, {"AGENT_WATCH_MAINLINE_STATUS": str(source)}):
            self.assertEqual(watch.mainline_status_path(), source)
            self.assertIn("1.4 通知", watch.mainline_status_text(80))
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("AGENT_WATCH_MAINLINE_STATUS", None)
            self.assertEqual(
                watch.mainline_status_path(),
                watch.ROOT_DIR / "MAINLINE-STATUS.md",
            )


class _WiringFixture(unittest.TestCase):
    """Temp runs/state files shared by the 工作 tab and --once entry points."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.root = Path(self._tmp.name)
        self.status = self.root / "MAINLINE-STATUS.md"
        self.status.write_text(NEW_FORMAT, encoding="utf-8")
        self.runs = self.root / "agent-runs.tsv"
        self.done = self.root / "agent-runs-done.tsv"
        self.runs.write_text("", encoding="utf-8")
        self.done.write_text("", encoding="utf-8")
        self.payload = (
            Path(__file__).parent / "tests/fixtures/magpie_quota.json"
        ).read_text()


class PanelWiringTests(_WiringFixture):
    """The panel must reach both entry points: the 工作 tab and --once."""

    def test_once_output_prints_the_read_only_block(self):
        output = io.StringIO()
        with patch.dict(os.environ, {"AGENT_WATCH_MAINLINE_STATUS": str(self.status)}), \
                patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout=self.payload)), \
                contextlib.redirect_stdout(output):
            self.assertEqual(watch.run_once_summary(self.runs, self.done), 0)
        text = output.getvalue()
        self.assertIn("【主線現況 · MAINLINE-STATUS · 唯讀】", text)
        self.assertIn("1.4 通知", text)
        self.assertIn("VERIFIED", text)

    def test_work_tab_shows_the_panel_beside_the_board(self):
        async def run():
            app = watch.AgentWatchApp(
                runs_path=self.runs,
                done_path=self.done,
                state_path=self.root / "state.json",
                mainline_status_file=self.status,
            )
            app.quota_svc.sessions_path = self.root
            app.quota_svc.refresh_in_background = lambda force=False: False
            async with app.run_test(size=(120, 40)) as pilot:
                await pilot.pause()
                screen = "\n".join(
                    strip.text for strip in app.screen._compositor.render_strips()
                )
                self.assertIn("主線現況 · MAINLINE-STATUS", screen)
                self.assertIn("1.4 通知", screen)
                self.assertIn("BLOCKED", screen)
                self.assertFalse(app.ui_error)

        asyncio.run(run())


if __name__ == "__main__":
    unittest.main()


LONG_FORMAT = """# 主線現況（MAINLINE-STATUS）

| 項目 | 狀態 | 離開條件 | 證據路徑 |
|---|---|---|---|
| 1.4 通知 | VERIFIED | 已達成；日後改通知路徑需重驗 | `/Users/kudi68-15mba/Projects/night/20261110/m1-8-rerun-accept-served/REPORT.md`（§3，10 筆 `notified … target=grok`） |
| 1.9 乾淨 ACCEPTANCE | UNVERIFIED | `accept.py` 第 7 行不再原樣印 m1-8 REPORT 首行 `PARTIAL`，重跑得到無尾巴的 `ACCEPTANCE_OK` | `/Users/kudi68-15mba/Projects/night/20261110/m1-9-clean-acceptance/REPORT.md`（§5）＋同卡 `evidence/m19-clean-verdict.txt` |
"""


class WrappedEvidenceTests(unittest.TestCase):
    """F1: a long evidence path must survive the 80-column --once render."""

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.source = Path(self._tmp.name) / "MAINLINE-STATUS.md"
        self.source.write_text(LONG_FORMAT, encoding="utf-8")

    def test_long_evidence_path_is_recoverable_and_never_ellipsised(self):
        text = watch.mainline_status_text(80, path=self.source)
        self.assertNotIn("…", text)
        for line in text.splitlines():
            self.assertLessEqual(watch.display_width(line), 80, line)
        flat = watch.unwrap_mainline_text(text).replace("\n", "")
        board = mainline_status.load_status_board(self.source)
        self.assertEqual(len(board.items), 2)
        for item in board.items:
            self.assertIn(item.evidence, flat)
        self.assertEqual(text.count("證據："), 2)

    def test_every_item_keeps_its_evidence_label_when_wrapped(self):
        text = watch.mainline_status_text(80, path=self.source)
        flat = watch.unwrap_mainline_text(text).replace("\n", "")
        for item in mainline_status.load_status_board(self.source).items:
            self.assertIn(f"證據：{item.evidence}", flat)

    def test_wrap_round_trip_keeps_every_character(self):
        line = (
            "VERIFIED  1.4 通知 │ 離開：已達成；日後改通知路徑需重驗"
            " │ 證據：" + "/Users/kudi68-15mba/Projects/night/" * 4 + "REPORT.md"
        )
        for width in (30, 40, 80, 120):
            lines = watch.wrap_mainline_text(line, width)
            for rendered in lines:
                self.assertLessEqual(watch.display_width(rendered), width, rendered)
            restored = watch.unwrap_mainline_text("\n".join(lines)).replace("\n", "")
            self.assertEqual(restored, line)


class NegatedStatusTests(unittest.TestCase):
    """F2: a negated status word must not be promoted to the positive status."""

    def test_negated_verified_forms_stay_as_written(self):
        for value in (
            "UNVERIFIED",
            "NOT_VERIFIED",
            "UN-VERIFIED",
            "NOT VERIFIED",
            "NON_VERIFIED",
            "未VERIFIED",
            "非VERIFIED",
        ):
            with self.subTest(value=value):
                self.assertNotEqual(mainline_status.normalize_status(value), "VERIFIED")
                self.assertEqual(mainline_status.normalize_status(value), value)

    def test_canonical_statuses_and_legacy_words_are_unchanged(self):
        self.assertEqual(mainline_status.normalize_status("VERIFIED"), "VERIFIED")
        self.assertEqual(mainline_status.normalize_status("PARTIAL"), "PARTIAL")
        self.assertEqual(mainline_status.normalize_status("UNKNOWN"), "UNKNOWN")
        self.assertEqual(mainline_status.normalize_status("BLOCKED"), "BLOCKED")
        self.assertEqual(mainline_status.normalize_status("**PARTIAL**"), "PARTIAL")
        self.assertEqual(mainline_status.normalize_status("DONE"), "VERIFIED")
        self.assertEqual(mainline_status.normalize_status("FAIL"), "BLOCKED")
        self.assertEqual(mainline_status.normalize_status("跑中"), "PARTIAL")
        self.assertEqual(mainline_status.normalize_status("待確認"), "待確認")
        self.assertEqual(mainline_status.normalize_status(""), "UNKNOWN")

    def test_unverified_table_cell_is_not_upgraded(self):
        items = mainline_status.parse_status_document(LONG_FORMAT)
        self.assertEqual([item.status for item in items], ["VERIFIED", "UNVERIFIED"])


class WheelWiringTests(_WiringFixture):
    """F1 through the real entry points: --once at 80 columns and the TUI panel."""

    def test_once_summary_at_80_columns_still_prints_the_whole_path(self):
        self.status.write_text(LONG_FORMAT, encoding="utf-8")
        output = io.StringIO()
        with patch.dict(os.environ, {"AGENT_WATCH_MAINLINE_STATUS": str(self.status)}), \
                patch("watch.subprocess.run", return_value=Mock(returncode=0, stdout=self.payload)), \
                contextlib.redirect_stdout(output):
            self.assertEqual(watch.run_once_summary(self.runs, self.done), 0)
        printed = output.getvalue()
        restored = printed.replace("\n" + watch.MAINLINE_STATUS_INDENT, "")
        for item in mainline_status.load_status_board(self.status).items:
            self.assertIn(item.evidence, restored)
        section = restored.split("【主線現況")[1].split("資料提示")[0]
        self.assertNotIn("…", section)

    def test_work_tab_panel_holds_the_whole_path_for_scrolling(self):
        self.status.write_text(LONG_FORMAT, encoding="utf-8")

        async def run():
            app = watch.AgentWatchApp(
                runs_path=self.runs,
                done_path=self.done,
                state_path=self.root / "state.json",
                mainline_status_file=self.status,
            )
            app.quota_svc.sessions_path = self.root
            app.quota_svc.refresh_in_background = lambda force=False: False
            async with app.run_test(size=(100, 40)) as pilot:
                await pilot.pause()
                body = app.query_one("#mainline-status-body", watch.Static)
                scroller = app.query_one("#mainline-status-scroll", watch.VerticalScroll)
                content = body.content
                self.assertIsInstance(content, str)
                flat = watch.unwrap_mainline_text(content).replace("\n", "")
                for item in mainline_status.load_status_board(self.status).items:
                    self.assertIn(item.evidence, flat)
                self.assertTrue(scroller.is_scrollable)
                self.assertFalse(app.ui_error)

        asyncio.run(run())
