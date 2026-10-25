# AGENTS.md — agent-watch

agent-watch：在終端機即時看 AI agent 任務狀態的儀表板（Python TUI）。

## 測試
```
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m unittest test_watch test_v8
```
改畫面後要跑全部測試，全過才算完成。

## 分支規則
- 只在 `agent/*` 分支上工作；不要 push main、不要合併、不要開 PR，合併由擁有者決定。
- 保留未追蹤的檔案，不要清理別人的工作區。

## 進度放哪
進度不寫在 repo，放在擁有者的私人狀態檔。本檔只放固定規則與測試指令。

## 安全規則
- 不讀、不印 secrets、.env、token、金鑰。
- 不用 sudo，不改系統服務設定（launchd、plist）。
- 同一件事失敗兩次就停，回報原因，不要換寫法硬試。
