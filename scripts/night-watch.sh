#!/bin/bash
# night-watch 範本（本機、零 AI 額度）。
# 判斷一個夜跑是否結束與結果的規則：
#   1. 先看程序：pid 還活著 → RUNNING（不看任何內容）。
#   2. 程序結束後，只看結果檔（LAST.md / REPORT.md / REVIEW.md），絕不看 run.log 的字眼。
#      a. 前 20 行裡「狀態位置」的關鍵字（行首，或「狀態/status/結論/result:」後面）優先。
#      b. 沒有的話，前 20 行任何大寫獨立字 FAILED/BLOCKED/PARTIAL/DONE（壞的優先，避免誤報成功）。
#      c. 都沒有：結果檔非空且分支有新 commit → DONE；結果檔非空但沒 commit → REPORTED（要人看）。
#      d. 沒結果檔或是空的 → NO_REPORT。
#   3. 給了 run 開始時間時，比開始時間舊的結果檔不算數（舊 LAST 不替新 run 判完成）。
# 用法：
#   source night-watch.sh            # 只載入函式
#   night-watch.sh --self-test       # 跑內建測試
# 範例主迴圈在檔案最後（main_example），複製成當晚的腳本再改 pid 與路徑。

nw_alive() {  # $1=pid；活著回 0
  local pid="${1:-}"
  [ -n "$pid" ] || return 1
  case "$pid" in *[!0-9]*) return 1;; esac
  kill -0 "$pid" 2>/dev/null && return 0
  # kill -0 被拒（別的使用者的程序）時仍算活著
  ps -p "$pid" >/dev/null 2>&1
}

nw_mtime() { stat -f %m "$1" 2>/dev/null || stat -c %Y "$1" 2>/dev/null || echo 0; }

# $1=結果檔 [$2=git 目錄 $3=比較基準 ref $4=run 開始 epoch]；印出 DONE/PARTIAL/FAILED/BLOCKED/REPORTED/NO_REPORT/STALE
nw_result() {
  local f="${1:-}" repo="${2:-}" base="${3:-}" start="${4:-0}" head20 kw commits
  if [ -z "$f" ] || [ ! -s "$f" ]; then echo NO_REPORT; return; fi
  if [ "${start:-0}" -gt 0 ] && [ "$(nw_mtime "$f")" -le "$start" ]; then echo STALE; return; fi
  # 去掉 markdown 記號：** ` # > - 和行首空白
  head20=$(head -n 20 "$f" | sed -e 's/[*`]//g' -e 's/^[[:space:]#>-]*//')
  # Explicit result labels are stronger than status words in report details.
  kw=$(printf '%s\n' "$head20" | grep -Eio '(狀態|status|結論|result)[[:space:]]*[:：][[:space:]]*(DONE|COMPLETE|PARTIAL|FAILED|BLOCKED)' \
       | grep -Eio '(DONE|COMPLETE|PARTIAL|FAILED|BLOCKED)$' | head -n 1 | tr '[:lower:]' '[:upper:]')
  if [ -z "$kw" ]; then
    kw=$(printf '%s\n' "$head20" | grep -Eo '^(DONE|COMPLETE|PARTIAL|FAILED|BLOCKED)\b' | head -n 1)
  fi
  if [ -z "$kw" ]; then
    local w
    for w in FAILED BLOCKED PARTIAL DONE; do
      if printf '%s\n' "$head20" | grep -Eq "(^|[^A-Za-z_])$w([^A-Za-z_]|$)"; then kw=$w; break; fi
    done
  fi
  [ "$kw" = COMPLETE ] && kw=DONE
  if [ -n "$kw" ]; then echo "$kw"; return; fi
  commits=""
  if [ -n "$repo" ] && [ -n "$base" ]; then
    commits=$(git -C "$repo" log --oneline "$base..HEAD" 2>/dev/null | head -n 1)
  fi
  if [ -n "$commits" ]; then echo DONE; else echo REPORTED; fi
}

# $1=pid $2=結果檔 [$3 $4 $5 同 nw_result]；印出 RUNNING 或結果
nw_state() {
  local pid="$1"; shift
  if nw_alive "$pid"; then echo RUNNING; return; fi
  nw_result "$@"
}

nw_self_test() {
  local t fail=0 repo got
  t=$(mktemp -d "${TMPDIR:-/tmp}/nwtest.XXXXXX")
  check() { if [ "$2" = "$3" ]; then echo "ok   $1"; else echo "FAIL $1: 期望 $2 實得 $3"; fail=1; fi; }
  # 假 repo：base 之後多 1 個 commit
  repo="$t/repo"; mkdir -p "$repo"
  git -C "$repo" init -q && git -C "$repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
  git -C "$repo" branch -q base
  git -C "$repo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m work
  norepo="$t/norepo"; mkdir -p "$norepo"
  git -C "$norepo" init -q && git -C "$norepo" -c user.email=t@t -c user.name=t commit -q --allow-empty -m base
  git -C "$norepo" branch -q base

  printf '**結論**\n- 已完成文件。\n- 全套測試 302/302 PASS\n- 失敗嘗試：首次 git add 被拒\n' > "$t/b.md"
  check "B 型：沒 DONE 首行但有 commit → DONE" DONE "$(nw_result "$t/b.md" "$repo" base)"
  check "B 型：沒 DONE 首行也沒 commit → REPORTED" REPORTED "$(nw_result "$t/b.md" "$norepo" base)"
  printf 'DONE — 成功編譯\n測試 94 passed\n' > "$t/a.md"
  check "首行 DONE" DONE "$(nw_result "$t/a.md")"
  printf '# 報告\n\n**PARTIAL**：69/70\n' > "$t/p.md"
  check "標題下 PARTIAL" PARTIAL "$(nw_result "$t/p.md" "$repo" base)"
  printf '# 結果\n\nStatus: FAILED\n' > "$t/f.md"
  check "狀態位置 FAILED 不能被 commit 蓋過" FAILED "$(nw_result "$t/f.md" "$repo" base)"
  printf '摘要\n第一次 FAILED，重試後 DONE\n' > "$t/m.md"
  check "同時有 FAILED 和 DONE → 壞的優先" FAILED "$(nw_result "$t/m.md")"
  printf 'done 小寫不算關鍵字\n' > "$t/l.md"
  check "小寫 done 不當狀態" REPORTED "$(nw_result "$t/l.md")"
  : > "$t/e.md"
  check "空檔 → NO_REPORT" NO_REPORT "$(nw_result "$t/e.md")"
  check "沒檔 → NO_REPORT" NO_REPORT "$(nw_result "$t/none.md")"
  printf 'DONE\n' > "$t/old.md"; touch -t 202001010000 "$t/old.md"
  check "比 run 開始舊 → STALE" STALE "$(nw_result "$t/old.md" "" "" "$(date +%s)")"
  sleep 30 & local sp=$!
  check "程序還活著 → RUNNING（不看結果檔）" RUNNING "$(nw_state "$sp" "$t/f.md")"
  kill "$sp" 2>/dev/null; wait "$sp" 2>/dev/null
  check "程序結束 → 看結果檔" FAILED "$(nw_state "$sp" "$t/f.md")"
  check "沒 pid → 看結果檔" DONE "$(nw_state "" "$t/a.md")"
  rm -rf "$t"
  [ $fail = 0 ] && echo "SELF-TEST PASS" || { echo "SELF-TEST FAIL"; return 1; }
}

main_example() {
  # 複製本檔成當晚腳本後再改這裡。每 5 分鐘看一次，全部結束或 07:00 停。
  local runs_root="${AGENT_WATCH_RUNS_ROOT:-$HOME/Projects/night}"
  local LOG="$runs_root/YYYYMMDD/night-watch.log"
  say() { echo "$(date '+%H:%M') $*" >> "$LOG"; }
  say start
  while true; do
    local s; s=$(nw_state 12345 "$runs_root/YYYYMMDD/run-a/LAST.md" "/path/to/worktree" origin/main)
    say "A=$s"
    [ "$s" != RUNNING ] && { say "all done"; return 0; }
    [ "$(date +%H%M)" -ge 0700 ] && [ "$(date +%H)" -lt 12 ] && { say "07:00 stop"; return 0; }
    sleep 300
  done
}

if [ "${BASH_SOURCE[0]}" = "$0" ]; then
  case "${1:-}" in
    --self-test) nw_self_test ;;
    *) echo "用法：source $0 載入函式；$0 --self-test 跑測試"; exit 2 ;;
  esac
fi
