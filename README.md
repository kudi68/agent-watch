# agent-watch

A terminal dashboard for supervising several AI coding-agent runs at once. It reads
plain tab-separated run registries, follows each run's log and result report, and shows
live status, elapsed time, per-project grouping, an editable architecture diagram, and
(optionally) subscription quota usage.

The tool is deliberately file-based: any harness that can append a TSV row and write a
log file can be watched. There is no daemon, database, or network listener.

## Requirements

- Python 3.9 or newer
- [`textual`](https://textual.textualize.io/) and `rich` (installed from
  `requirements.txt`)
- Optional: the [`magpie`](https://usemagpie.ai/) CLI on `PATH` for quota readings
- Optional: a Codex sessions directory (`~/.codex/sessions`) for Codex rate-limit data
- macOS is only needed for the completion sound (`afplay`); elsewhere it is skipped

## Install

```bash
git clone https://github.com/kudi68/agent-watch.git
cd agent-watch
uv venv .venv
uv pip install --python .venv/bin/python -r requirements.txt
./agent-watch-v8
```

A plain `python3 -m venv .venv && .venv/bin/pip install -r requirements.txt` works too.

Run once and print a plain-text summary instead of the full-screen UI:

```bash
./agent-watch-v8 --once
```

## Configuration

Everything machine-specific is read from the environment. CLI flags `--runs` and
`--done` override the two registry paths.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_WATCH_RUNS_ROOT` | `~/Projects/night` | Base directory for relative paths inside the TSVs |
| `AGENT_WATCH_RUNS` | `$AGENT_WATCH_RUNS_ROOT/agent-runs.tsv` | Active-runs registry |
| `AGENT_WATCH_DONE` | `$AGENT_WATCH_RUNS_ROOT/agent-runs-done.tsv` | Archive registry |
| `AGENT_WATCH_CODEX_SESSIONS` | `~/.codex/sessions` | Codex session logs used for quota |
| `AGENT_WATCH_ARCH` | `arch.json` next to `watch.py` | Architecture graph definition |
| `AGENT_WATCH_MAGPIE` | `magpie` | Executable used for `quota --json` |

`scripts/night-watch.sh` uses `AGENT_WATCH_RUNS_ROOT` in its `main_example`; the bash
test accepts `NIGHT_WATCH_SCRIPT` to point at a different copy of that helper.

## TSV format

One run per line, tab-separated, 3 to 6 columns. Blank lines and lines starting with
`#` are ignored. Relative log/report paths are resolved against
`AGENT_WATCH_RUNS_ROOT`.

```text
name<TAB>log_path<TAB>last_report_path<TAB>pid<TAB>harness<TAB>model
```

| Column | Required | Notes |
| --- | --- | --- |
| `name` | yes | Display name; also used for project matching |
| `log_path` | yes | Log file to tail; `/dev/null` is allowed |
| `last_report_path` | yes | `LAST.md` / `REPORT.md` style result file |
| `pid` | no | Process id; a live PID means "running" |
| `harness` | no | Display label, for example `Codex` |
| `model` | no | Display label, for example `sonnet` |

Example:

```text
nightly build	20261025/build/run.log	20261025/build/LAST.md	48213	Codex	gpt-5
docs pass	20261025/docs/run.log	20261025/docs/REPORT.md
```

Project grouping and the architecture tab are configured in `arch.json`. Point
`AGENT_WATCH_ARCH` at your own file to describe your projects and workers; the bundled
file is an example and is validated at startup.

## Tests

```bash
.venv/bin/python -m unittest test_watch test_v8
bash tests/test_night_watch.sh
```

The Python suite covers status inference, TSV parsing, project grouping, quota
parsing, rendering, and Textual screenshots. The bash suite exercises
`scripts/night-watch.sh`, the small helper that decides whether a detached run has
finished.

## Layout

```text
watch.py                  dashboard and status logic
arch.json                 example architecture/project configuration
agent-watch-v8            launcher using .venv/bin/python
test_watch.py             UI and behaviour tests
test_v8.py                status and quota regression tests
tests/test_night_watch.sh bash tests for scripts/night-watch.sh
tests/fixtures/           quota JSON fixtures
scripts/night-watch.sh    PID/result-report helper
shots/                    text snapshot fixtures
ARCH-HISTORY.md           design history of the architecture tab
```

## 繁體中文摘要

agent-watch 是一個純終端機的 AI agent 監控看板。它讀取 TSV 清單、追蹤日誌與結果檔，
顯示執行中／已完成狀態、各專案分組、架構圖與訂閱額度；沒有常駐服務，也不寫資料庫。
所有路徑與外部指令都可用環境變數覆寫，預設值指向 `~/Projects/night`。安裝後執行
`./agent-watch-v8`，或加 `--once` 印出單次摘要。測試指令見上。

## License

MIT. See [LICENSE](LICENSE).
