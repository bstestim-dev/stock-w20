# 林恩如週 20MA 選股系統（stock-w20）

台股（TWSE 上市／TPEx 上櫃）週線順勢選股的盤後研究工具。每次以 GitHub Actions 手動觸發（`workflow_dispatch`），當場向官方端點抓取歷史行情、計算向後還原價與真正週 K，產出選股名單並發佈至 GitHub Pages。

- 實作依據：`spec_v1`（v1.3）程式規格書；實作期間的裁定見 [`docs/decisions.md`](docs/decisions.md)。
- 所有日期、交易週切分與報告時間一律採 **Asia/Taipei（UTC+8）**。
- 資料來源：臺灣證券交易所（TWSE）、證券櫃檯買賣中心（TPEx）；選用 FinMind。

## 本系統不做的事

- 不設定排程（無 `schedule`），僅手動觸發或本機 CLI 執行。
- 不做任何推播（LINE、Slack、Email 等）。
- 不計算資金配置、投入上限或建議購買張數；只提供均差距與乖離率。
- 不介接券商下單。
- 不引用任何年報酬目標或預期績效；趨勢線、W 底、爆量倍數等屬工程代理參數，結果不代表原作者方法之績效。

## Public repository 與機密

本 repo 為 **Public**（`bstestim-dev/stock-w20`），以 **GitHub Free** 發佈 GitHub Pages（GitHub Free 須從 Public repo 發佈 Pages），不升級付費方案。程式碼、設定檔與 Pages 上的選股結果皆公開。

- `config.yaml`、原始碼、測試、workflow 與 Pages 產物**不得**含任何 token、金鑰或密碼；`src/config.py` 載入時會檢查並拒絕疑似機密的欄位。
- `FINMIND_TOKEN`（選用）**只能**放在 GitHub Secrets，由 workflow 以環境變數注入；本機執行請自行設定環境變數，勿寫入任何版控檔案。未設定時系統全程走免費官方路線。
- 部署前請在 repo 的 Settings → Pages 將來源設為「GitHub Actions」。

## 本機執行

```bash
pip install -r requirements.txt
pytest
```

執行參數可用環境變數覆寫（對應 workflow 輸入）：`SW20_AS_OF_DATE`（`YYYY-MM-DD`）、`SW20_LOOKBACK_WEEKS`、`SW20_VOLUME_SPIKE_RATIO`、`SW20_SKIP_FUNDAMENTALS`（`true`／`false`）。

## API 實測驗證（M2）

`.github/workflows/verify-api.yml` 僅能手動觸發（Actions → verify-api → Run workflow），不使用任何 Secrets。
會對規格第 3 節端點發出真實請求，產出 `verify_report/report.md`（同時顯示於該次執行的 Summary 頁並上傳為 artifact）。

## 進度

| 里程碑 | 內容 | 狀態 |
| :--- | :--- | :--- |
| M1 | 專案骨架、設定檔、時區與民國年工具 | 完成 |
| M2 | TWSE／TPEx／FinMind 抓取器 | 完成（verify-api run1～run3；見 docs/api-verification.md） |
| M3 | 還原價、真正週 K、指標、週結算判定 | 程式完成，待 verify-api run4 與待裁定事項 |
| M4 | 選股引擎 | 未開始 |
| M5 | 輸出與靜態網頁 | 未開始 |
| M6 | GitHub Actions 與 Pages 部署 | 未開始 |
| M7 | 第二階段回測 | 第一階段驗收後才開始 |
