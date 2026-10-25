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
| dashboard state | `~/Projects/night/agent-watch-state.json` |

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
| `2` | History tab |
| `3` | Quota tab |
| `4` | Architecture tab |
| `j` / `k`, arrows | Select next / previous task |
| `Enter` | Work: open the full-screen read-only terminal; with `QUESTION.md` opens the options popup |
| `e` | Work: expand / collapse recent completions |
| `h` / `d` | Hide / move to completed history (ended tasks without attention only) |
| `x` | Work: clear the selected needs-attention item; question files stay unchanged |
| `X` | Work: confirm, then clear expired or superseded items only |
| `c` | Collapse the selected task's project; a new running task reopens it |
| `i` | Pin / unpin selected task (maximum 5; the sixth is rejected) |
| `t` | Edit free-form tags; commas separate tags; empty input clears them |
| `u` | Toggle read / unread |
| `/` | History: search name, project, tags and result summary |
| `Tab` | History: focus project / tag / read / hidden filters |
| `g` | History: switch table / project groups |
| `Space` / `Enter` | History: collapse / expand selected project group (Space on a task row collapses its project) |
| `r` | History: bring selected task back to Work |
| `p` / `n` / `f` | Architecture: cycle project / "now" / "future" view |
| `?` | Full shortcut table; `Esc` / `?` returns |
| `q` | Quit |

### 歷史頁與看板狀態（v10 第③段）

`2` 開啟歷史頁。欄位是時間、專案、標籤、名稱、結果摘要、已讀。主頁超過兩小時的
完成任務、手動歸到已完成、隱藏及整個專案收起的任務都會出現在這裡。
`/` 搜尋，不分英文大小寫；Enter 回清單，Esc 清除搜尋。上方四個下拉選單可以同時
篩選專案、自由標籤、已讀／未讀、已隱藏／未隱藏。Tab 移動焦點，方向鍵與 Enter 選擇。

`g` 在表格與依專案分組之間切換。每組內按完成時間由新到舊排序；選中綠色專案列，
按 Space 或 Enter 收合／展開。分組模式沿用相同搜尋與篩選，收合狀態在本次開啟期間保留。
在分組模式下，選中任務列按 Space 也會收合它所屬的專案，游標會跳到該專案列。
選中任務按 `r` 取消隱藏、手動完成或清除狀態，展開所屬專案並回到工作頁。超過兩小時的
任務也會留在工作頁，直到手動隱藏、歸到已完成或收起專案。

工作頁選中已結束且無需處理的任務，`h` 隱藏，`d` 歸到已完成；進行中及需處理任務會拒絕。
`x` 清除所選需處理項目，只寫入看板 state，不會刪除或修改 `QUESTION.md`／`ANSWER.md`。
`X` 會先顯示筆數並要求確認，只清除已過期或已被取代的項目；未滿 24 小時的失敗任務保留。
失敗類項目預設超過 24 小時後移入歷史，可用 `AGENT_WATCH_STALE_HOURS` 調整期限。
`c` 收起整個專案，專案有進行中／需處理任務時會拒絕；新任務開跑時自動重新出現。
隱藏或手動完成的任務重跑時也會自動回主頁。`i` 釘選最多五個任務；隱藏／收起的釘選
及手動完成的釘選不顯示在工作頁，但仍佔名額，可從歷史頁取消。
需處理任務維持在紅框，不會因釘選重複顯示。
`t` 設自由標籤，不會改 TSV 的專案欄。未讀任務顯示 `●`；完成後預設未讀，
工作頁預覽實際顯示時自動已讀。歷史頁只選中列不會已讀，`u` 可手動切換。

看板只將操作狀態寫入 `~/Projects/night/agent-watch-state.json`，可用 `AGENT_WATCH_STATE`
另指定檔案。格式為 version 1，`tasks` 以既有的 `name::log::result` 為 key，保存
hidden、done_manual、read、pinned、tags、updated_at；`projects` 保存 collapsed／collapsed_at。
額外的 `restored`、`dismissed`、`dismissed_at`、`dismissed_sig` 與 `run_start` 用於撈回主頁、
清除需處理項目與辨認重跑。清除簽章會隨狀態、問題 id 或完成時間改變而失效。寫入先建立同目錄暫存檔、flush／fsync，
再 atomic rename；寫入失敗時保留原檔與記憶體狀態。不存在或格式損壞時使用空狀態，
畫面只保留一則提示，不會反覆跳通知。第一次成功操作才建立／替換 state 檔。
本版不處理多個看板同時寫入的鎖定，最後寫入者覆蓋較早的狀態。
看板不寫入派工 TSV，`--once` 維持原本的唯讀摘要，不讀寫操作狀態。

### 深入畫面與回答選項（v10 第⑤段）

在工作頁選中任務按 `Enter`，進入全螢幕唯讀終端機：內容是該任務 `run.log` 的尾端
（最多 400 行），每 1 秒自動更新，Claude／Codex 的 JSON 事件會翻成白話。
`j`／`k`、↑↓ 捲動，PgUp／PgDn 翻頁，`/` 搜尋（Enter 套用；有搜尋字時
Esc 先清除搜尋，再按一次 Esc 回工作頁）。
`Esc` 回到工作頁，選取位置不變；看板只讀檔，不會送訊號給任務，PID 與 log 都不受影響。
進過全螢幕的任務會標成已讀。

任務資料夾（`run.log` 旁）出現 `QUESTION.md` 且沒有相同 `id` 的 `ANSWER.md` 時，
任務會進「需處理」並顯示「已等 Xm」。按 `Enter` 開選項彈窗，↑↓ 選、Enter 送出；
也可 Tab 到備註欄留一行。看板依照 SPEC §3.3 格式在同一個資料夾寫 `ANSWER.md`
（先寫暫存檔再 atomic rename），`id` 沿用問題的 `id`。格式錯誤（缺 `id`、
`question`、`options`）或選項壞掉時，只顯示「問題格式錯誤」與原文，不給選項、
也不寫檔。`asked_at` 無法解析時以檔案時間起算。

等超過 30 分鐘（`QUESTION_OVERDUE_SECONDS`）時，「已等」會變紅，並沿用完成通知
機制跳一次桌面通知＋聲音；同一個問題只通知一次。`ANSWER.md` 出現且 `id` 相符後，
任務隔一次輪詢就離開需處理區。看板不會叫醒或重啟 agent，也不會替人選答案。
若舊的 `QUESTION.md` 比已完成的報告還舊，視為上一輪留下的檔案，不會讓完成的任務
卡在需處理。

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
| `AGENT_WATCH_STATE` | `~/Projects/night/agent-watch-state.json` | Dashboard-owned state; independent of runs root |
| `AGENT_WATCH_CODEX_SESSIONS` | `~/.codex/sessions` | Codex session logs used for quota |
| `AGENT_WATCH_ARCH` | `arch.json` next to `watch.py` | Architecture graph definition |
| `AGENT_WATCH_MAGPIE` | `magpie` | Executable used for `quota --json` |

`scripts/night-watch.sh` uses `AGENT_WATCH_RUNS_ROOT` in its `main_example`; the bash
test accepts `NIGHT_WATCH_SCRIPT` to point at a different copy of that helper.

## TSV format

One run per line, tab-separated, 3 to 9 columns. Blank lines and lines starting with
`#` are ignored. Relative log/report paths are resolved against
`AGENT_WATCH_RUNS_ROOT`.

```text
name<TAB>log_path<TAB>last_report_path<TAB>pid<TAB>harness<TAB>model<TAB>project<TAB>started_at<TAB>note
```

| Column | Required | Notes |
| --- | --- | --- |
| `name` | yes | Display name; also used for project matching |
| `log_path` | yes | Log file to tail; `/dev/null` is allowed |
| `last_report_path` | yes | `LAST.md` / `REPORT.md` style result file |
| `pid` | no | Process id; a live PID means "running" |
| `harness` | no | Display label, for example `agent-a` |
| `model` | no | Display label, for example `model-x` |
| `project` | no | Configured project ID or case-insensitive alias; explicit values override inference |
| `started_at` | no | ISO 8601 timestamp with timezone, for example `2026-10-25T18:00:00+08:00` |
| `note` | no | Free-form note; `SUPERSEDED(by name; ...)` identifies a replacement run. |

Example:

```text
example-project build	example-project/build/run.log	example-project/build/LAST.md	48213	agent-a	model-x
docs pass	example-project/docs/run.log	example-project/docs/REPORT.md
```

Project grouping and the architecture tab are configured in `arch.json`. Point
`AGENT_WATCH_ARCH` at your own file to describe your projects and workers; the bundled
file is an example and is validated at startup.

The bundled project table includes `personal-ai-os` (`pai`), `agent-watch` (`aw`),
`ios-shortcuts` (`ios`), and `K1`, plus the original example projects. Unknown explicit
projects go to `other` with one data warning. Inferred keywords use alphanumeric
boundaries (`misc-task` does not match `sc-`, and `desk1` does not match `K1`);
a keyword ending in punctuation such as `sc-` is a delimited prefix.
Optional `match_patterns` contain regular expressions for names and paths.
Missing, invalid, or unreadable architecture JSON falls back to an internal graph
with one warning. Edges must still join adjacent layers.

Elapsed time prefers column 8, then a live PID's start time from
`ps -o lstart= -p <pid>` (queried once per PID, with `LC_ALL=C` so the output parses),
then log timestamps or file birthtime. The PID source is only consulted while
`pgrep -l` reports the process as alive, and a failed or unparsable `ps` lookup
falls through to the estimate instead of guessing a start. Tests can still inject
`LogParser(process_start_reader=...)` or pass `None` to disable the source.
Log/birthtime estimates display `~12m`; missing evidence displays `時間未知`.
Reused log prefixes do not establish a new invocation's start after an older
report. A new Claude init event resets the start. Lines without timestamps have
no fabricated clock prefix.

Claude `--print --output-format stream-json --verbose` logs support `system`,
`assistant`, `user`, and `result`: text becomes the preview, tool calls show a
short name/argument summary, and init supplies the model. Result events classify
completion or failure after the PID ends; a live PID always wins. Timestamp-free
result events use the log's modification time for completion freshness, without
adding that time to individual lines. No Claude session directory is scanned.
Result reports require an explicit status line; prose mentioning completion is
only `reported`. New log activity invalidates an older report, branch commits
must postdate this invocation, and `PROGRESS.md` beside the log must be newer
than its start. Terminal and waiting transitions notify once per run/status.

The Textual timer schedules one background data reader for TSV/log/report/git
I/O. Rendering and elapsed counters consume snapshots, so a slow disk or git
command does not block tab changes.

## Tests

```bash
.venv/bin/python -m unittest test_watch test_v8
bash tests/test_night_watch.sh
```

The Python suite covers status inference, TSV parsing, project grouping, quota
parsing, rendering, and Textual screenshots. The bash suite exercises
`scripts/night-watch.sh`, the small helper that decides whether a detached run has
finished.

v10 fixtures are `tests/fixtures/runs_v10.tsv`, `claude_stream.jsonl`, and
`opencode_stream.jsonl`; they do not depend on the dispatcher's concurrent work.
To persist v10's Textual 80x40 / 120x40 work and architecture text snapshots:

```bash
AGENT_WATCH_UPDATE_SHOTS=1 .venv/bin/python -m unittest \
  test_watch.V10ScreenAndWorkerTests.test_v10_fixture_snapshots_and_registry_mtimes
```

Deep-view (stage ⑤) terminal and question-popup snapshots:

```bash
AGENT_WATCH_UPDATE_S5_SHOTS=1 .venv/bin/python -m unittest \
  test_watch.V10DeepViewTests.test_s5_fixture_snapshots_and_registry_mtimes
```

The fixture tests compare registry bytes and nanosecond mtimes before/after reads.
The worker test blocks simulated file I/O while measuring `poll_data < 200ms`
and switching tabs. It proves headless responsiveness, not a live ten-minute run.

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
shots/                    neutral example output snapshots
ARCH-HISTORY.md           design history of the architecture tab
```

## 繁體中文摘要

agent-watch 是一個純終端機的 AI agent 監控看板。它讀取 TSV 清單、追蹤日誌與結果檔，
顯示執行中／已完成狀態、各專案分組、架構圖與訂閱額度；沒有常駐服務，也不寫資料庫。
所有路徑與外部指令都可用環境變數覆寫，預設值指向 `~/Projects/night`（只是預設，可用
`AGENT_WATCH_RUNS_ROOT` 改）。第一次使用先建好 `.venv`（`./agent-watch-v8` 需要它），
再建立空的 `agent-runs.tsv`（或指定自己的目錄），否則只會看到「TSV 不存在」提示。
安裝後執行 `./agent-watch-v8`；按鍵 `1`/`2`/`3`/`4` 切換工作、歷史、額度、架構分頁，`E`
展開／收起剛完成清單，`P` 切換專案，`N`/`F` 切換架構頁的現在／未來視圖，`?` 開關
快捷鍵表，`Q` 離開。或加 `--once` 印出單次摘要。
沒安裝 magpie 或沒有 Codex sessions 時，額度列顯示「無資料」屬正常。測試指令見上。

## License

MIT. See [LICENSE](LICENSE).
