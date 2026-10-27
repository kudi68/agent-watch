# needs-attention-v2：agent-watch「需處理」改善、tok/s、「其他」分類

來源：博元 2026-10-28 01:27～02:02（經安西教練轉達），14 題全部照推薦。
基準版本：agent-watch main `ea3fcd9`（#16 merge）。

## 要解決什麼
1. 修正卡接手後，舊的 PARTIAL／FAIL 卡還留在需處理（例：`claude-perm-p2-fix` → `claude-perm-p2-fix2`）。
2. 需處理沒有統計，逢澤美優化師無法追蹤。
3. 需處理約 10 項清不掉。
4. 「等博元回答」可以被 `x` 清掉，安全閘門有漏洞。
5. 看不到每張卡的 tok/s，也沒有各模型／tier 的累計。
6. 「其他」分類越來越多，靠人工整理。

## 現況（讀 main 程式碼得知）
- 已有「已被取代」：認 note 裡的 `SUPERSEDED(by X)`、同名重跑、名稱去後綴。
- 名稱規則只去一層後綴，所以 fix → fix2 對不上；照名字比對時，修正卡要完成才算。
- 被取代的舊卡直接進歷史，沒有自己的顏色。
- 已有 `x`（清除需處理）和 `Shift+X`（清除過期／取代）；`x` 也能清題目。
- 沒有任何 token／usage 欄位。
- 派工帶的 project 不在 `arch.json` 就直接歸「其他」，不再看路徑或關鍵字；`arch.json` 只列 11 個專案。

## 功能需求

### A. 已接手
- 連結來源，依優先序：
  1. 派工 `--supersedes <舊卡名>`（night-dispatch 寫進 note，沿用 `SUPERSEDED(by X)` 格式）。
  2. decisions 的 parent。
  3. 名稱規則，改成可去多層後綴。
- 修正卡進行中就算接手：舊卡標「已接手 → 新卡名」，青綠色 `#73daca`，移出需處理，放在需處理下方自己一區，圖例加上。
- 修正卡失敗：舊卡回到需處理。
- 自動清：有審查的卡要 DONE＋審查 PASS，沒審查的 DONE 就清。清完進歷史，不刪檔。

### B. 安全閘門
- 「等博元回答」不能被接手、不能手動清、不能自動清；只有寫了 `ANSWER.md` 才離開需處理。
- 已轉介的照現狀。

### C. 手動處理
- 不加新功能，沿用 `x`，只用在失敗／卡住／無報告類。
- 清除時可選填一句原因，並算進統計。
- 先確認 Mac 上跑的是最新版。

### D. 需處理統計
- 每天寫一行 TSV，分類計數：等回答、逾時未答、已轉介、卡住、失敗、無報告、已接手、手動清除，再加當天總卡數。
- 比率＝需處理 ÷ 總卡數。逢澤美優化師的週報讀這個檔。

### E. tok/s（等資料來源查完才開工，不擋 A～D、F）
- 公式：output tokens ÷ 實際產生時間。
- 進行中的卡每秒更新一次。
- 累計按模型和 tier 各算平均、中位數、P10／P90，寫進每日 TSV。
- 來源沒有的欄位顯示「—」，不硬估。
- 候選來源：magpie usage --csv、run.log 的 usage、Claude stream json，參考 nananko/syrtis 的做法（安西教練的研究報告）。

### F. 「其他」分類
- night-dispatch 派工時必填 `--project`，不在專案表就拒絕派工並提示。
- agent-watch 遇到不認得的 project，繼續看路徑、git、關鍵字，用 repo 名自動歸類；都對不到才算「其他」。
- `arch.json` 補上博元現有的 repo。
- 畫面顯示「其他 N（x%）」；每日 TSV 記比率和歸類依據的分布。目標低於 5%。

## 驗收條件
- [ ] 重現 `claude-perm-p2-fix` → `claude-perm-p2-fix2`：fix2 進行中時，fix 顯示「已接手」、不在需處理。
- [ ] fix2 失敗：fix 回到需處理。
- [ ] fix2 DONE（有審查的加 PASS）：fix 自動進歷史。
- [ ] 帶 `--supersedes` 的派工，名字完全不像也能接手。
- [ ] 等回答的題目按 `x` 清不掉，自動規則也動不到；寫了 `ANSWER.md` 才離開。
- [ ] 每日統計 TSV 有全部分類欄和比率。
- [ ] 不在專案表的 project 派工會被拒絕；agent-watch 用 repo 名歸類成功。
- [ ] 畫面顯示「其他」數量和比率。
- [ ] tok/s：有來源對照表；有資料的模型顯示 tok/s，沒有的顯示「—」。
- [ ] 舊測試全過；新測試涵蓋以上；附一張畫面截圖。

## 不做的事
- 不改 `~/.hermes`、gateway、launchd，不改任何 Mac 設定。
- 不刪任何 QUESTION.md／ANSWER.md 或 log。
- 不新增「標完成」按鍵（沿用 `x`）。
- tok/s 不從字數估算 token。
- 合併由博元決定。

## 假設
- night-dispatch 不在 agent-watch repo，它的改動是另一張卡。
- 「審查 PASS」可以從現有 decisions 或報告讀到；讀不到就退回 DONE 即清，並寫明。
