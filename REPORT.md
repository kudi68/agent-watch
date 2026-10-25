PARTIAL

第 ① 段實作已提交；資料、背景 I/O、fixtures 與快照驗證通過。
預設 PID 啟動時間來源尚未接上：本次只允許 `pgrep -l`，不能取得 SPEC 要求的 `ps lstart`。
第二層優先序以注入來源測試通過；正式預設採第 8 欄，再退回標記 `~` 的 log／birthtime 估計，不宣稱三層完整落地。

## 分支與提交

- 基準：`agent/v9-s1-sort-quota`，`4302760c8be62c418fe617a4a4c30d41942ddb27`。
- 工作分支：`agent/v10-s1-data`。
- 實作 commit：`685b2047571741ec322b107b33da6bec226029c6`。
- 提交訊息：`fix(data): isolate v10 run state and parsing`。
- 本報告與最終 PROGRESS 更新另作文件 commit；實作 hash 不包含自我引用。
- 同步狀態：LOCAL_ONLY；沒有 push、merge、PR，也沒有執行下一段。

## 已驗證

| 項目 | 結果與證據 |
| --- | --- |
| TSV | 支援 3–8 欄；第 7 欄專案優先、別名不分大小寫、未知專案歸其他並提示；第 8 欄要求含時區 ISO 8601 |
| 專案推斷 | 英數字邊界排除 `misc-task`／`desk1`／嵌在長字中的關鍵字；支援 `match_patterns`；保留原範例設定以相容既有資料 |
| 已跑時間 | 第 8 欄優先於注入 PID 起點，優先於 log／birthtime；估計顯示 `~12m`，無可信起點顯示「時間未知」 |
| 重用 log | 新 Claude init 重設起點；舊報告後有新活動時排除舊結果，無新起點證據不沿用超長時間 |
| Claude | system／assistant／user／result、text／tool_use、model、成功／失敗摘要；無 timestamp 的 result 使用檔案時間判斷完成新鮮度，不為行內容加假時間 |
| 狀態 | 只認明確狀態行；舊 ahead commit 必須晚於 run_start；PID 存活優先；程序查詢失敗保持 unconfirmed |
| PROGRESS | 只讀 log 同目錄，且 mtime 必須晚於本次起點 |
| arch.json | 執行期載入；壞檔回退內建圖並提示一次；保留相鄰層 edge 驗證；架構頁排除已結束歷史 |
| I/O | 單一背景 reader 讀 TSV／log／report／git；UI 讀 snapshot；同時最多一個 reader |
| 通知 | complete／partial／failed／blocked／waiting 轉換各通知一次；成功、失敗與其他狀態選不同聲音；通知以 mock 驗證，未播放實機聲音 |
| 摘要 | 新 Claude result 不顯示前次舊報告的摘要 |

## SPEC §6 共同驗收 1–4

1. PASS：`/Users/kudi68-15mba/Projects/agent-watch/.venv/bin/python -m unittest test_watch test_v8`：96/96，6.512 秒。
   最後單行「排除舊報告摘要」修正後，另跑新增的 `test_v8.V10DataRegressions.test_claude_new_result_summary_does_not_reuse_old_report`：1/1 PASS，0.002 秒。
   目前共 97 個測試有 PASS 證據；遵守不重跑已 PASS 驗收，沒有在最後這項修正後重跑完整套件。
   `/bin/bash /Users/kudi68-15mba/Projects/agent-watch/tests/test_night_watch.sh`：6/6，`NIGHT-WATCH TESTS PASS`。
2. PASS：新增 19 個回歸測試；基準 `4302760` 已重現寬鬆完成比對、長關鍵字誤中、mtime 假時鐘、Claude 字典外洩四個原缺陷。
3. PASS：Textual `run_test(size=(80, 40))` 與 `(120, 40)` 的工作頁及架構頁快照已保存：
   `shots/v10-s1-80x40.txt`、`shots/v10-s1-120x40.txt`、
   `shots/v10-s1-arch-80x40.txt`、`shots/v10-s1-arch-120x40.txt`。
   四份各 40 行，文字寬度不超出指定欄數；工作頁兩個 fixture 都顯示已跑與最新一句，沒有 `{'type': …`。
4. PASS（隔離 fixture）：兩個 `agent-runs*.tsv` 的 nanosecond mtime 與內容在兩種畫面測試前後均不變；資料載入 fixture 亦有同樣斷言。
   未載入、修改正式 TSV；⓪ 同時進行，因此不宣稱其他 agent 會維持正式 TSV 的 mtime。

其他驗證：

- 模擬 I/O 阻塞時，5 次 `poll_data` 最慢 0.027ms（門檻 200ms），`1/2/3` 分頁切換正常；讀取執行緒與 UI 執行緒不同。
- Python 3.9.6 以內建 `compile()` 在記憶體檢查 `watch.py`／`test_watch.py`／`test_v8.py`，全部 PASS；完整測試使用既有 Python 3.12 venv。
- `git diff --check` PASS。

## 未驗證與限制

- UNPROVEN：預設真實 PID `lstart` 來源；只提供 `LogParser(process_start_reader=...)` 注入介面與測試，程式不執行 `ps`。
- NOT_EXECUTED：live 派工、正式 DB、真實 Claude／OpenCode 任務、10 分鐘實機操作、Python 3.9 完整相依套件測試。
- NOT_EXECUTED：§6 第 5、6 點獨立審查／實機人工放行，本次只要求共同驗收 1–4。
- 未改 `~/.local/bin`、`~/Projects/c1-runtime`、`~/.hermes`、gateway、launchd、plist、Keychain、`/Library`、secrets、`.env` 或 live DB；未執行 hermes、sudo。
- 未刪除任何原有未追蹤檔；起始 tracked／untracked 工作樹乾淨，原有 ignored `run.log` 保留。

## 失敗嘗試

- 建立分支第一次遭沙箱拒寫 `.git/index.lock`；使用已授權的 git 升權路徑後成功，未重複盲試。
- 完整測試首輪 95 項中 3 項失敗：兩個舊完成判斷斷言與新 SPEC 不符，一個真實 PID inventory 在受限環境回傳未知。更新語意斷言及固定 inventory fixture 後，第二輪 96/96 PASS。
- `py_compile` 嘗試將 bytecode 寫入沙箱外的 Python 預設快取，被拒絕；改用不寫檔的記憶體 `compile()`，Python 3.9.6 語法檢查 PASS。
- 未發生同一失敗連續兩次；沒有要求使用者補資料或擴大任務授權。

## 接續狀態

- 目前階段：① 已提交，驗證完成；整體 PARTIAL，缺口是預設 PID 啟動時間來源。
- 持續文件：本檔為本包權威交接；`PROGRESS.md` 為階段紀錄，不建立第二份 handoff。
- 保存狀態：PERSISTED／LOCAL_ONLY，程式、fixtures、快照與進度已 commit；本檔將隨最終文件 commit 保存。
- 唯一下一步：後續取得符合程序查詢限制的可信 PID 起點來源，再接上預設 reader；若要使用 `ps lstart`，必須先有明確授權。本次不續做②。
