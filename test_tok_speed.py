"""tok/s from magpie usage (spec needs-attention-v2 §E, plan N11); no real magpie, no network."""

import subprocess
import tempfile
import time
import unittest
from datetime import datetime
from pathlib import Path
from types import SimpleNamespace

import tok_speed
import watch
from tok_speed import CardAnchor, TokSpeedService

HEADER = ("time,agent,requested_model,model,served_model,output_tokens,reasoning_tokens,"
          "duration_ms,ttft_ms,status,session,rejected")
CODEX_SESSION = "01a17e8a-e821-7e03-baf8-51297bda7485"
CLAUDE_SESSION = "01316d5b-0000-4000-8000-000000000001"
OPENCODE_SESSION = "ses_e8178435abcdefgh"
BASE = "2026-10-28T02:15:00+08:00"
BASE_EPOCH = datetime.fromisoformat(BASE).timestamp()


def csv_row(agent="opencode", requested="group/night-flash", model="deepseek-v4.1-flash",
            output=176, duration=3107, ttft=2397, status=200, session=OPENCODE_SESSION,
            rejected="", when=BASE):
    return ",".join(str(value) for value in (
        when, agent, requested, model, model, output, 0, duration,
        "" if ttft is None else ttft, status, session, rejected,
    ))


def csv_text(*rows):
    return "\n".join((HEADER, *rows)) + "\n"


def rows_of(*lines):
    rows, error = tok_speed.parse_usage_rows(csv_text(*lines))
    assert not error, error
    return rows


# N2's sample rows, repeated to reach the 5-request minimum.
OPENCODE_ROWS = [csv_row()] * 5
CODEX_ROWS = [csv_row(agent="codex", requested="group/tier-mid", model="gpt-6-luna",
                      output=714, duration=14727, ttft=5817, session=CODEX_SESSION)] * 5
CLAUDE_ROWS = [csv_row(agent="claude", requested="group/tier-strong", model="claude-haiku-5-5",
                       output=559, duration=3072, ttft=655, session=CLAUDE_SESSION)] * 5


class FormulaTests(unittest.TestCase):
    def test_n2_fixture_rows_give_formula_values(self):
        for lines, rate, decode in (
            (OPENCODE_ROWS, 176 / 3107 * 1000, 176 / 710 * 1000),
            (CODEX_ROWS, 714 / 14727 * 1000, 714 / 8910 * 1000),
            (CLAUDE_ROWS, 559 / 3072 * 1000, 559 / 2417 * 1000),
        ):
            speed = tok_speed.compute_card_speed(rows_of(*lines))
            self.assertEqual(speed.requests, 5)
            self.assertAlmostEqual(speed.rate, rate, places=6)
            self.assertAlmostEqual(speed.decode_rate, decode, places=6)
        # N2's per-row decode figures: 248 / 80 / 231 tok/s.
        self.assertEqual(round(176 / 710 * 1000), 248)
        self.assertEqual(round(714 / 8910 * 1000), 80)
        self.assertEqual(round(559 / 2417 * 1000), 231)

    def test_weighted_sum_not_mean_of_row_rates(self):
        # Claude shape: a row whose ttft eats the whole duration explodes a per-row average.
        lines = [csv_row(agent="claude", output=1000, duration=8000, ttft=1000)] * 5
        lines.append(csv_row(agent="claude", output=282, duration=4089, ttft=4071))
        rows = rows_of(*lines)
        speed = tok_speed.compute_card_speed(rows)
        self.assertAlmostEqual(speed.rate, 5282 / 44089 * 1000, places=6)
        mean_of_rates = sum(r.output_tokens / (r.duration_ms - r.ttft_ms) * 1000 for r in rows) / len(rows)
        self.assertGreater(mean_of_rates, 2000)
        self.assertLess(speed.rate, 130)
        self.assertGreater(mean_of_rates / speed.rate, 10)
        # The exploding row is dropped from the decode figure, not averaged in.
        self.assertAlmostEqual(speed.decode_rate, 5000 / 35000 * 1000, places=6)

    def test_status_and_rejected_rows_are_not_counted(self):
        rows = rows_of(*OPENCODE_ROWS,
                       csv_row(output=99999, status=429),
                       csv_row(output=99999, status=0),
                       csv_row(output=99999, rejected="true"),
                       csv_row(output=99999, rejected="1"))
        self.assertEqual(len(rows), 5)
        self.assertEqual(tok_speed.compute_card_speed(rows).output_tokens, 176 * 5)
        kept = rows_of(csv_row(rejected="false"), csv_row(rejected="0"))
        self.assertEqual(len(kept), 2)

    def test_decode_guards_drop_impossible_rows(self):
        good = [csv_row(output=1000, duration=6000, ttft=1000)] * 5
        too_fast = csv_row(output=50, duration=1000, ttft=990)        # 5,000 tok/s
        ttft_equal = csv_row(output=50, duration=2000, ttft=2000)     # nothing left to generate
        ttft_over = csv_row(output=50, duration=2000, ttft=2500)
        no_ttft = csv_row(output=50, duration=2000, ttft=None)
        rows = rows_of(*good, too_fast, ttft_equal, ttft_over, no_ttft)
        self.assertAlmostEqual(tok_speed.decode_rate(rows), 5000 / 25000 * 1000, places=6)
        # The main figure still counts every answered row.
        speed = tok_speed.compute_card_speed(rows)
        self.assertEqual(speed.requests, 9)
        self.assertAlmostEqual(speed.rate, 5200 / (30000 + 7000) * 1000, places=6)

    def test_decode_falls_back_when_guards_drop_too_much_output(self):
        good = [csv_row(output=100, duration=2000, ttft=500)] * 5
        bad = csv_row(output=400, duration=1000, ttft=999)  # 400 of 900 output dropped (44%)
        rows = rows_of(*good, bad)
        self.assertIsNone(tok_speed.decode_rate(rows))
        speed = tok_speed.compute_card_speed(rows)
        self.assertIsNotNone(speed.rate)
        self.assertNotIn("解碼", tok_speed.format_card_speed(speed))

    def test_missing_columns_show_dash(self):
        no_duration = "time,agent,output_tokens,status\n%s,codex,500,200\n" % BASE
        rows, error = tok_speed.parse_usage_rows(no_duration)
        self.assertEqual(rows, [])
        self.assertIn("duration_ms", error)
        self.assertEqual(tok_speed.parse_usage_rows("")[1], "magpie usage 沒有輸出資料")
        # Dict rows with a missing value are unusable rather than zero.
        dict_rows, error = tok_speed.parse_usage_rows([
            {"time": BASE, "agent": "codex", "status": "200", "output_tokens": "500"},
        ] * 6)
        self.assertEqual(error, "")
        speed = tok_speed.compute_card_speed(dict_rows)
        self.assertEqual(speed.requests, 0)
        self.assertIsNone(speed.rate)
        self.assertEqual(tok_speed.format_card_speed(speed), "— tok/s")
        # No ttft column: main figure works, decode stays empty.
        no_ttft = "time,agent,output_tokens,duration_ms,status\n" + \
            "".join("%s,codex,500,5000,200\n" % BASE for _ in range(5))
        speed = tok_speed.compute_card_speed(tok_speed.parse_usage_rows(no_ttft)[0])
        self.assertAlmostEqual(speed.rate, 100.0)
        self.assertIsNone(speed.decode_rate)

    def test_subtitle_format(self):
        speed = tok_speed.CardSpeed(requests=14, output_tokens=10324, duration_ms=92100,
                                    decode_rate=None, model="m", tier="t")
        self.assertEqual(tok_speed.format_card_speed(speed),
                         "112 tok/s · output 10,324 ÷ 生成 92.1 s · 14 請求")
        with_decode = tok_speed.CardSpeed(14, 10324, 92100, 343.4, "m", "t")
        self.assertTrue(tok_speed.format_card_speed(with_decode).endswith("· 解碼 343 tok/s"))
        self.assertEqual(tok_speed.format_card_speed(None), "— tok/s")
        few = tok_speed.compute_card_speed(rows_of(*OPENCODE_ROWS[:3]))
        self.assertIsNone(few.rate)
        self.assertEqual(tok_speed.format_card_speed(few), "— tok/s · 樣本不足 3 請求")
        self.assertNotIn(" 0 tok/s", tok_speed.format_card_speed(few))


class JoinTests(unittest.TestCase):
    def all_rows(self):
        return rows_of(*OPENCODE_ROWS, *CODEX_ROWS, *CLAUDE_ROWS)

    def test_session_join(self):
        anchors = [CardAnchor("codex-card", agent="codex", sessions=(CODEX_SESSION,)),
                   CardAnchor("claude-card", agent="claude", sessions=(CLAUDE_SESSION,))]
        result = tok_speed.compute_all(anchors, self.all_rows(), BASE_EPOCH)
        self.assertEqual(result["codex-card"].output_tokens, 714 * 5)
        self.assertEqual(result["codex-card"].model, "gpt-6-luna")
        self.assertEqual(result["codex-card"].tier, "group/tier-mid")
        self.assertEqual(result["claude-card"].output_tokens, 559 * 5)

    def test_no_join_shows_no_number(self):
        anchors = [
            CardAnchor("unknown-session", agent="codex", sessions=("not-in-csv-0000000",)),
            CardAnchor("no-anchor"),
            CardAnchor("no-alias", agent="opencode", start=BASE_EPOCH - 60, end=BASE_EPOCH + 60),
            CardAnchor("other-window", agent="opencode", alias="group/night-flash",
                       start=BASE_EPOCH + 3600, end=BASE_EPOCH + 7200),
        ]
        result = tok_speed.compute_all(anchors, self.all_rows(), BASE_EPOCH)
        self.assertEqual(result, {})
        for anchor in anchors:
            self.assertEqual(tok_speed.format_card_speed(result.get(anchor.key)), "— tok/s")

    def test_time_window_join(self):
        anchor = CardAnchor("oc", agent="opencode", alias="group/night-flash",
                            start=BASE_EPOCH - 60, end=None)
        result = tok_speed.compute_all([anchor], self.all_rows(), BASE_EPOCH + 30)
        self.assertEqual(result["oc"].requests, 5)
        self.assertEqual(result["oc"].tier, "group/night-flash")

    def test_overlapping_windows_are_ambiguous(self):
        anchors = [CardAnchor(key, agent="opencode", alias="group/night-flash",
                              start=BASE_EPOCH - 60, end=BASE_EPOCH + 60) for key in ("a", "b")]
        self.assertEqual(tok_speed.compute_all(anchors, self.all_rows(), BASE_EPOCH), {})

    def test_window_skips_rows_owned_by_a_session_card(self):
        anchors = [
            CardAnchor("by-session", agent="opencode", sessions=(OPENCODE_SESSION,)),
            CardAnchor("by-window", agent="opencode", alias="group/night-flash",
                       start=BASE_EPOCH - 60, end=BASE_EPOCH + 60),
        ]
        result = tok_speed.compute_all(anchors, self.all_rows(), BASE_EPOCH)
        self.assertIn("by-session", result)
        self.assertNotIn("by-window", result)

    def test_extract_session_ids(self):
        codex = "\x1b[1mOpenAI Codex\x1b[0m\nmodel: group/tier-mid\nsession id: %s\n" % CODEX_SESSION
        self.assertEqual(tok_speed.extract_session_ids(codex, "codex"), (CODEX_SESSION,))
        claude = ('{"type":"system","subtype":"init","session_id":"%s"}\n'
                  '{"type":"assistant","session_id":"%s"}\n') % (CLAUDE_SESSION, CLAUDE_SESSION)
        self.assertEqual(tok_speed.extract_session_ids(claude, "claude"), (CLAUDE_SESSION,))
        self.assertEqual(tok_speed.extract_session_ids("printed %s once" % OPENCODE_SESSION, "opencode"),
                         (OPENCODE_SESSION,))
        two = "ses_aaaaaaaaaaaa and ses_bbbbbbbbbbbb"
        self.assertEqual(tok_speed.extract_session_ids(two, "opencode"), ())
        self.assertEqual(tok_speed.extract_session_ids("ses_aaaaaaaaaaaa", "codex"), ())
        self.assertEqual(tok_speed.extract_session_ids("no ids here", "codex"), ())

    def test_magpie_agent_name(self):
        self.assertEqual(tok_speed.magpie_agent_name("Claude Code"), "claude")
        self.assertEqual(tok_speed.magpie_agent_name("OpenCode"), "opencode")
        self.assertEqual(tok_speed.magpie_agent_name("Codex"), "codex")
        self.assertEqual(tok_speed.magpie_agent_name(watch.UNKNOWN_IDENTITY), "")
        self.assertEqual(tok_speed.magpie_agent_name(""), "")


class WrapperTests(unittest.TestCase):
    def test_wrapper_runs_only_magpie_usage_csv_today(self):
        calls = []

        def runner(args, **kwargs):
            calls.append((args, kwargs))
            return SimpleNamespace(returncode=0, stdout=csv_text(*CODEX_ROWS))

        text, error = tok_speed.run_magpie_usage(runner)
        self.assertEqual(error, "")
        self.assertIn("output_tokens", text)
        self.assertEqual(calls[0][0][1:], ["usage", "--csv", "today"])
        self.assertLessEqual(calls[0][1]["timeout"], 10)

    def test_wrapper_failures_become_errors(self):
        def timeout(args, **kwargs):
            raise subprocess.TimeoutExpired(args, kwargs.get("timeout"))

        def missing(args, **kwargs):
            raise FileNotFoundError("magpie")

        def boom(args, **kwargs):
            raise RuntimeError("boom")

        cases = (
            (timeout, "逾時"),
            (missing, "無法執行"),
            (boom, "無法執行"),
            (lambda args, **kwargs: SimpleNamespace(returncode=2, stdout="x"), "結束碼 2"),
            (lambda args, **kwargs: SimpleNamespace(returncode=0, stdout="  "), "沒有輸出資料"),
        )
        for runner, message in cases:
            text, error = tok_speed.run_magpie_usage(runner)
            self.assertEqual(text, "")
            self.assertIn(message, error)

    def test_service_failure_shows_dash_and_never_raises(self):
        anchors = [CardAnchor("codex-card", agent="codex", sessions=(CODEX_SESSION,))]
        ok = TokSpeedService(reader=lambda: (csv_text(*CODEX_ROWS), ""))
        ok.refresh_now(anchors)
        self.assertIsNotNone(ok.result("codex-card").rate)

        def raising():
            raise RuntimeError("reader exploded")

        for reader in (lambda: ("", "magpie usage 逾時"),
                       lambda: ("", "magpie usage 結束碼 1"),
                       raising,
                       lambda: ("not,a,usage\n1,2,3\n", "")):
            service = TokSpeedService(reader=reader)
            service.refresh_now(anchors)
            self.assertIsNone(service.result("codex-card"))
            self.assertNotEqual(service.error, "")
            self.assertEqual(tok_speed.format_card_speed(service.result("codex-card")), "— tok/s")
        # A failed read after a good one clears the old figure.
        ok.reader = lambda: ("", "magpie usage 逾時")
        ok.refresh_now(anchors)
        self.assertIsNone(ok.result("codex-card"))

    def test_service_caches_the_read(self):
        calls = []

        def reader():
            calls.append(1)
            return csv_text(*CODEX_ROWS), ""

        service = TokSpeedService(interval=1, reader=reader)
        self.assertGreaterEqual(service.interval, 5)
        self.assertTrue(service.refresh_in_background([]))
        deadline = time.monotonic() + 5
        while service._fetching and time.monotonic() < deadline:
            time.sleep(0.01)
        self.assertFalse(service.refresh_in_background([]))
        self.assertEqual(len(calls), 1)
        self.assertEqual(service.version, 1)

    def test_service_reads_session_from_log_head(self):
        with tempfile.TemporaryDirectory() as tmp:
            log = Path(tmp) / "run.log"
            log.write_text("model: group/tier-mid\nsession id: %s\n" % CODEX_SESSION, encoding="utf-8")
            service = TokSpeedService(reader=lambda: (csv_text(*CODEX_ROWS, *OPENCODE_ROWS), ""))
            service.refresh_now([CardAnchor("c", agent="codex", log_path=str(log))])
            self.assertEqual(service.result("c").output_tokens, 714 * 5)
            self.assertEqual(service.session_ids(str(log), "codex"), (CODEX_SESSION,))
            self.assertEqual(service.session_ids(str(Path(tmp) / "missing.log"), "codex"), ())


class DailyStatsTests(unittest.TestCase):
    def test_percentiles(self):
        values = [10.0, 20.0, 30.0, 40.0, 50.0]
        self.assertAlmostEqual(tok_speed.percentile(values, 0.1), 14.0)
        self.assertAlmostEqual(tok_speed.percentile(values, 0.5), 30.0)
        self.assertAlmostEqual(tok_speed.percentile(values, 0.9), 46.0)
        self.assertEqual(tok_speed.percentile([7.0], 0.9), 7.0)
        self.assertIsNone(tok_speed.percentile([], 0.5))

    def test_summarize_groups_per_model_and_tier(self):
        def card(rate, model, tier):
            return tok_speed.CardSpeed(5, int(rate * 10), 10000, None, model, tier)

        speeds = [card(r, "gpt-6-luna", "group/tier-mid") for r in (10, 20, 30, 40, 50)]
        speeds.append(card(100, "deepseek-v4.1-flash", "group/tier-mid"))
        speeds.append(tok_speed.CardSpeed(3, 300, 1000, None, "gpt-6-luna", "group/tier-mid"))  # no figure
        groups = {(g, n): rest for g, n, *rest in tok_speed.summarize_groups(speeds)}
        cards, mean, median, p10, p90 = groups[("model", "gpt-6-luna")]
        self.assertEqual(cards, 5)
        self.assertAlmostEqual(mean, 30.0)
        self.assertAlmostEqual(median, 30.0)
        self.assertAlmostEqual(p10, 14.0)
        self.assertAlmostEqual(p90, 46.0)
        self.assertEqual(groups[("model", "deepseek-v4.1-flash")][0], 1)
        self.assertEqual(groups[("tier", "group/tier-mid")][0], 6)
        self.assertEqual(tok_speed.summarize_groups([]), [])

    def test_write_daily_tsv_long_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "sub" / "toks.tsv"
            self.assertTrue(tok_speed.write_daily_tsv(path, "2026-10-27", [
                ("model", "gpt-6-luna", 2, 50.0, 50.0, 41.0, 59.0),
            ]))
            self.assertTrue(tok_speed.write_daily_tsv(path, "2026-10-28", [
                ("model", "gpt-6-luna", 1, 48.48, 48.48, 48.48, 48.48),
                ("tier", "group/tier-mid", 1, None, None, None, None),
            ]))
            # Same day again replaces that day's rows only.
            self.assertTrue(tok_speed.write_daily_tsv(path, "2026-10-28", [
                ("model", "gpt-6-luna", 1, 60.0, 60.0, 60.0, 60.0),
            ]))
            self.assertFalse(tok_speed.write_daily_tsv(path, "2026-10-28", [
                ("model", "gpt-6-luna", 1, 60.0, 60.0, 60.0, 60.0),
            ]))
            lines = path.read_text(encoding="utf-8").splitlines()
            self.assertEqual(lines[0], "date\tgroup\tname\tcards\tmean\tmedian\tp10\tp90")
            self.assertEqual(lines[1:], [
                "2026-10-27\tmodel\tgpt-6-luna\t2\t50.0\t50.0\t41.0\t59.0",
                "2026-10-28\tmodel\tgpt-6-luna\t1\t60.0\t60.0\t60.0\t60.0",
            ])
            self.assertEqual(list(Path(tmp, "sub").iterdir()), [path])

    def test_missing_values_are_empty_cells_not_zero(self):
        content = tok_speed.render_daily_tsv("", "2026-10-28", [("tier", "t", 0, None, None, None, None)])
        self.assertEqual(content.splitlines()[1], "2026-10-28\ttier\tt\t0\t\t\t\t")

    def test_service_writes_daily_tsv_to_given_path(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "toks.tsv"
            service = TokSpeedService(
                tsv_path=path,
                reader=lambda: (csv_text(*CODEX_ROWS, *CLAUDE_ROWS), ""),
                clock=lambda: BASE_EPOCH,
            )
            service.refresh_now([
                CardAnchor("codex-card", agent="codex", sessions=(CODEX_SESSION,)),
                CardAnchor("claude-card", agent="claude", sessions=(CLAUDE_SESSION,)),
            ])
            self.assertEqual(service.error, "")
            date = time.strftime("%Y-%m-%d", time.localtime(BASE_EPOCH))
            rows = [line.split("\t") for line in path.read_text(encoding="utf-8").splitlines()[1:]]
            names = {(row[1], row[2]) for row in rows}
            self.assertEqual(names, {
                ("model", "gpt-6-luna"), ("model", "claude-haiku-5-5"),
                ("tier", "group/tier-mid"), ("tier", "group/tier-strong"),
            })
            self.assertTrue(all(row[0] == date for row in rows))
            luna = next(row for row in rows if row[2] == "gpt-6-luna")
            self.assertEqual(luna[3:], ["1", "48.5", "48.5", "48.5", "48.5"])

    def test_service_without_tsv_path_writes_nothing(self):
        service = TokSpeedService(reader=lambda: (csv_text(*CODEX_ROWS), ""))
        self.assertIsNone(service.tsv_path)
        service.refresh_now([CardAnchor("c", agent="codex", sessions=(CODEX_SESSION,))])
        self.assertEqual(service.error, "")


class WatchIntegrationTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.now = time.time()
        self.tmp = tempfile.TemporaryDirectory(prefix="agent-watch-toks-test-")
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        self.log = self.root / "run.log"
        self.log.write_text("session id: %s\n" % CODEX_SESSION, encoding="utf-8")

    def running_agent(self, key="tok-card"):
        return watch.AgentInfo(
            name=key, log_path=self.log, last_path=self.root / "REPORT.md",
            status="running", model="group/tier-mid", start_time=self.now - 120,
            elapsed="00:02:00", latest="寫測試", key=key, harness="Codex",
        )

    def test_anchor_from_agent(self):
        agent = self.running_agent()
        anchor = watch.toks_anchor(agent)
        self.assertEqual((anchor.key, anchor.agent, anchor.alias), ("tok-card", "codex", "group/tier-mid"))
        self.assertIsNone(anchor.end)
        self.assertEqual(anchor.log_path, str(self.log))
        agent.model = watch.UNKNOWN_IDENTITY
        agent.status, agent.completion_time = "complete", self.now
        anchor = watch.toks_anchor(agent)
        self.assertEqual((anchor.alias, anchor.end), ("", self.now))

    def test_render_task_row_subtitle(self):
        agent = self.running_agent()
        speed = tok_speed.format_card_speed(tok_speed.CardSpeed(14, 10324, 92100, None, "m", "t"))
        for width in (60, 76, 120):
            text = watch.render_task_row(agent, width, self.now, bucket="running", toks_text=speed)
            lines = text.plain.splitlines()
            self.assertEqual(len(lines), 2)
            self.assertTrue(all(watch.display_width(line) <= width for line in lines))
            self.assertTrue(lines[1].startswith("  112 tok/s"))
        wide = watch.render_task_row(agent, 120, self.now, bucket="running", toks_text=speed).plain
        self.assertIn("output 10,324 ÷ 生成 92.1 s · 14 請求", wide)
        self.assertIn("寫測試", wide)
        plain = watch.render_task_row(agent, 120, self.now, bucket="running").plain
        self.assertNotIn("tok/s", plain)
        # Only running rows carry the subtitle.
        agent.status = "failed"
        self.assertNotIn("tok/s", watch.render_task_row(
            agent, 120, self.now, bucket="attention", toks_text=speed).plain)

    def make_app(self, toks_svc):
        app = watch.AgentWatchApp(self.root / "runs.tsv", self.root / "done.tsv",
                                  state_path=self.root / "state.json", toks_svc=toks_svc)
        agents = [self.running_agent()]
        app.data_svc.snapshot = lambda: (list(agents), [])
        app.data_svc.refresh_in_background = lambda: False
        app.quota_svc.refresh_in_background = lambda force=False: False
        return app

    def test_app_without_service_shows_no_toks(self):
        app = self.make_app(None)
        self.assertEqual(app.toks_text(self.running_agent(), "running"), "")

    async def test_app_shows_live_toks_and_dash_on_failure(self):
        service = TokSpeedService(reader=lambda: (csv_text(*CODEX_ROWS), ""))
        app = self.make_app(service)
        service.refresh_now([watch.toks_anchor(self.running_agent())])
        # Keep the 1-second timer from starting a racing background read.
        service.last_fetch = float("inf")
        async with app.run_test(size=(120, 40)) as pilot:
            await pilot.pause()
            row = app._row_widgets.get("tok-card")
            self.assertIsNotNone(row)
            self.assertIn("48 tok/s · output 3,570", row.render().plain)
            self.assertEqual(app.toks_text(self.running_agent(), "recent"), "")
            service.reader = lambda: ("", "magpie usage 逾時")
            service.refresh_now([watch.toks_anchor(self.running_agent())])
            app.poll_toks()
            await pilot.pause()
            self.assertIn("— tok/s", app._row_widgets["tok-card"].render().plain)
            self.assertEqual(app.ui_error, "")


if __name__ == "__main__":
    unittest.main()
