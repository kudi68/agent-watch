# agent-watch

A terminal dashboard for supervising several AI coding-agent runs at once. It reads
plain tab-separated run registries, follows each run's log and result report, and shows
live status, elapsed time, per-project grouping, an editable architecture diagram, and
(optionally) subscription quota usage.

The tool is deliberately file-based: any harness that can append a TSV row and write a
log file can be watched. There is no daemon, database, or network listener.

## Requirements

- Python 3.9 or newer (verified on CPython 3.9.6 on macOS; the test suite also
  passes on 3.12)
- [`textual`](https://textual.textualize.io/) and `rich` (installed from
  `requirements.txt`)
- Optional: the [`magpie`](https://usemagpie.ai/) CLI on `PATH` for quota readings
- Optional: a Codex sessions directory (`~/.codex/sessions`) for Codex rate-limit data
- macOS is only needed for the completion sound (`afplay`); elsewhere it is skipped

## Install

The launcher `./agent-watch-v8` runs `.venv/bin/python watch.py`, so a virtual
environment named `.venv` **must exist in the repository root** before the first
launch.

1. Install [`uv`](https://docs.astral.sh/uv/) (skip to the pip fallback if you
   prefer not to use it):

   ```bash
   curl -LsSf https://astral.sh/uv/install.sh | sh
   ```

2. Clone, create the environment, install dependencies:

   ```bash
   git clone https://github.com/kudi68/agent-watch.git
   cd agent-watch
   uv venv .venv
   uv pip install --python .venv/bin/python -r requirements.txt
   ./agent-watch-v8
   ```

   Without `uv`:

   ```bash
   python3 -m venv .venv
   .venv/bin/python -m pip install -r requirements.txt
   ./agent-watch-v8
   ```

## First run

The dashboard is empty until at least one runs registry exists. The defaults are:

| Path | Default |
| --- | --- |
| runs root | `~/Projects/night` |
| active registry | `~/Projects/night/agent-runs.tsv` |
| archive registry | `~/Projects/night/agent-runs-done.tsv` |

`~/Projects/night` is **only a default**, not a hardcoded location: set
`AGENT_WATCH_RUNS_ROOT` (or `AGENT_WATCH_RUNS` / `AGENT_WATCH_DONE`) to point
somewhere else, or pass `--runs` / `--done`.

If a registry is missing, the tool still starts and exits cleanly, but `--once`
prints `資料提示: TSV 不存在：…` and the TUI shows the same warning with empty
lists. Create empty files first, or point the variables at your own directory:

```bash
mkdir -p ~/Projects/night
: > ~/Projects/night/agent-runs.tsv
: > ~/Projects/night/agent-runs-done.tsv
```

```bash
export AGENT_WATCH_RUNS_ROOT="$HOME/agent-runs"
mkdir -p "$AGENT_WATCH_RUNS_ROOT"
: > "$AGENT_WATCH_RUNS_ROOT/agent-runs.tsv"
: > "$AGENT_WATCH_RUNS_ROOT/agent-runs-done.tsv"
```

Then add one TSV row per run (see [TSV format](#tsv-format)).

## Usage

Start the full-screen dashboard in an interactive terminal:

```bash
./agent-watch-v8
```

| Key | Action |
| --- | --- |
| `1` | Work tab (running / recent runs) |
| `2` | Architecture tab |
| `3` | Quota tab |
| `P` | Cycle the architecture tab's project |
| `N` | Architecture tab: "now" view |
| `F` | Architecture tab: "future" view |
| `Q` | Quit |

Run once and print a plain-text summary instead of the full-screen UI (works
without a TTY, useful for logs and CI):

```bash
./agent-watch-v8 --once
./agent-watch-v8 --help
```

If neither `magpie` nor a Codex sessions directory is available, the quota line
shows `無資料` (no data). That is expected and not an error; every other part of
the dashboard keeps working.

## Configuration

Everything machine-specific is read from the environment. CLI flags `--runs` and
`--done` override the two registry paths.

| Variable | Default | Meaning |
| --- | --- | --- |
| `AGENT_WATCH_RUNS_ROOT` | `~/Projects/night` (default only) | Base directory for relative paths inside the TSVs |
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
所有路徑與外部指令都可用環境變數覆寫，預設值指向 `~/Projects/night`（只是預設，可用
`AGENT_WATCH_RUNS_ROOT` 改）。第一次使用先建好 `.venv`（`./agent-watch-v8` 需要它），
再建立空的 `agent-runs.tsv`（或指定自己的目錄），否則只會看到「TSV 不存在」提示。
安裝後執行 `./agent-watch-v8`；按鍵 `1`/`2`/`3` 切換工作、架構、額度分頁，`P` 切換
專案，`N`/`F` 切換架構頁的現在／未來視圖，`Q` 離開。或加 `--once` 印出單次摘要。
沒安裝 magpie 或沒有 Codex sessions 時，額度列顯示「無資料」屬正常。測試指令見上。

## License

MIT. See [LICENSE](LICENSE).
