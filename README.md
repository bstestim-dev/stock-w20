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

## 選股引擎全量試跑（M4）

同一支 `verify-api` workflow 選 `mode = m4_dry_run`（可填 `as_of_date`），以真實 60 週官方資料試跑選股引擎，
產出 `dry_run_report/report.md` 與 `result.json`（artifact `m4-dry-run-report`），**不**部署 Pages、**不**寫入 `site/`。
報告內含第 10.1 節第 3、4 項核對、跳空異常與事件對帳結果，以及族群、ETF 子類型、處置／注意的實測統計。本機可執行 `python -m tools.dry_run_m4 --out dry_run_report`。

## 輸出與網頁（M5）

- 產出：`python -m src.screener --out site/data`（讀 `config.yaml` 與 `SW20_*` 環境變數），寫出
  `latest.json`、`latest.js`（同內容，供 file:// 直接開啟）與 4 份 UTF-8 BOM CSV（`new_cross`、`above_and_spike`、`etf`、`exit_alerts`）。
  歷史資料缺漏超過第 9 節門檻時結束碼為 2，不寫出任何檔案。
- 網頁：`site/index.html`＋`app.js`＋`styles.css`，純靜態、無 build；週 K 圖使用 TradingView Lightweight Charts 4.1.1（CDN，需連網）。
- CSV 欄名為中文（M5-Q3），JSON 鍵名為英文（第 6.3 節）。
- 預覽（不部署）：Actions → `verify-api` → `mode = m5_preview`，下載 artifact `site-preview` 後直接開啟 `index.html`。
- 本機：`python -m src.screener --out site/data` 後，`cd site && python -m http.server 8000`，瀏覽 http://localhost:8000/ ；或直接開啟 `site/index.html`。

## 執行與部署（M6）

本 repo 為 **Public**，以 GitHub Free 發佈 GitHub Pages（第 8.3 節）。網址：**https://bstestim-dev.github.io/stock-w20/**

一次性設定（repo 擁有者）：

1. Settings → Pages → Build and deployment → Source 選 **GitHub Actions**。
2. Settings → Actions → General → Actions permissions 允許執行 GitHub 官方 actions（`actions/*`）；Workflow permissions 維持預設即可（權限由 workflow 檔自行宣告）。
3. （選配）Settings → Secrets and variables → Actions → New repository secret：`FINMIND_TOKEN`。未設定時走免費官方路線，本系統第一階段不需要。

每次執行：Actions → **run-screen** → Run workflow（branch `main`）。網頁頂部的「更新資料 ↗」連結會在新分頁開啟
https://github.com/bstestim-dev/stock-w20/actions/workflows/run-screen.yml ，登入有權限的帳號後按 Run workflow 即可
（一般超連結，網頁本身不呼叫 GitHub API、不觸發 workflow、不含 token）。四個輸入皆可留預設：
`as_of_date`（空白＝最新可用交易日）、`lookback_weeks`（空白＝config 的 60 週）、`volume_spike_ratio`（1.5）、`skip_fundamentals`（不勾）。
build 成功後 deploy 自動更新 Pages。

日資料快取（D5）：每次執行先還原上次的 `actions/cache`，日資料每抓完一日即寫入，成功或失敗都會儲存。
**第一次（冷啟動）**約需 30 分鐘以上的抓取，超過 16 分鐘的抓取預算即以結束碼 3 結束（紅叉，訊息「請再執行一次以補齊」），
Pages 不更新；再按一次 Run workflow 接續抓取，通常 1～3 次補齊。補齊後的執行只抓新增的交易日。
成功的執行另會把日資料快取（公開官方行情）連同 SHA-256 清單放進網站 `cache/`（約 30 MB）。
GitHub 會移除超過 7 天未存取的 actions/cache；此時程式改從 https://bstestim-dev.github.io/stock-w20/cache/ 下載回來，
不需冷啟動（M6-Q1＝B）。網站上的檔若未通過雜湊或格式檢查即丟棄、改由官方來源重抓。

結束碼：0 完成；2 歷史缺漏超過第 9 節門檻（不發佈）；3 冷啟動抓取預算用盡（快取已保存，請再執行）。

## 進度

| 里程碑 | 內容 | 狀態 |
| :--- | :--- | :--- |
| M1 | 專案骨架、設定檔、時區與民國年工具 | 完成 |
| M2 | TWSE／TPEx／FinMind 抓取器 | 完成（verify-api run1～run3；見 docs/api-verification.md） |
| M3 | 還原價、真正週 K、指標、週結算判定 | 完成（verify-api run4；裁定 M3-Q1～Q7 見 docs/decisions.md） |
| M4 | 選股引擎 | 完成（m4_dry_run run 45597664068；裁定 M4-Q1～Q12、M4b-Q1） |
| M5 | 輸出與靜態網頁 | 完成（m5_preview run 45621969504；裁定 M5-Q1～Q3；M5b 修正列高） |
| M6 | GitHub Actions 與 Pages 部署 | 完成（run 45742193270、45757811557）；第一階段 10.1 驗收 17／17 通過（docs/acceptance-phase1.md） |
| M7 | 第二階段回測 | 第一階段驗收後才開始 |
