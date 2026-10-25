# 架構圖沿革

## 已查證

- **v4 有方框與連線圖。** `v4/PROMPT.md:9` 明確要求用方框與箭頭畫組織圖；`v4/LAST.md:54-59` 記錄博元、Grok Bot、Codex、Claude Code、agy、agent-watch 及 K1 看板的圖形呈現。`v4-review/shots/arch-100x35.svg` 是當時保存的畫面。
- **v5 保留架構分頁，但程式改成文字狀態清單。** `v5/watch.py` 的 `render_architecture_view` 逐行輸出標題、worker 狀態與 K1 說明，沒有方框和連線。`v5/LAST.md` 仍列有架構頁 SVG，表示分頁保留。
- **v6 沿用文字清單。** `v6/watch.py` 的 `render_architecture_view` 仍以一般文字列出 worker 和 K1 說明，沒有恢復圖形。
- **v5 的工作範圍採納了縮短架構頁的 review 建議。** `v5/PROMPT.md:4` 要求照 v4 review 的前十項建議處理；`v4-review/REVIEW.md:81` 建議縮短架構頁、分開固定說明與動態狀態，並縮短或收合 K1 資訊。
- **v7 起始狀態沿用文字清單。** 原本的 `watch.py` 將架構內容寫在 Python 常數中，只有一種視圖。

## 為何改成文字清單

**推論，未有明文紀錄：** v5 把方框圖改成較短的狀態清單，時間點與 review 建議相符；可能是在落實「縮短架構頁」時簡化過頭。v4、v5、v7 都沒有記下「因為某項理由而移除方框圖」的明確決策。v7 沒有 Git metadata，因此也無法用 commit diff 確認原作者意圖。

## v7 調整

- `arch.json` 保存「現在」與「未來」的節點、分層與連線。
- 架構分頁以方框和連線呈現兩種視圖，按 `N` 或 `F` 切換；現在視圖的 Codex、Claude Code、OpenCode 節點會顯示即時工作狀態。
- 本紀錄只追溯本機版本文件與截圖；沒有讀取或變更共用 TSV，也沒有查詢 live 派工服務。
