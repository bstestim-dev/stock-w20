# API 實測紀錄

依 IR-1，本專案只使用經實測的端點、欄位與單位。本檔記錄每次 verify-api 的結論與據以修改的程式。

## run1：2026-11-25 13:39（Asia/Taipei）

- GitHub Actions run 45554661549，commit `9011345`，63 個探測全部 HTTP 200，耗時 3 分 48 秒。
- 樣本交易日 2026-11-20；短窗 2026-09-21～2026-10-09。

### 已確認（與規格一致）

| 項目 | 實測結果 |
| :--- | :--- |
| MI_INDEX 筆數 | 20260115＝1,334、20261120＝1,393，與規格第 3.2B 節相同 |
| MI_INDEX 表格定位 | 以標題關鍵字可找到「價格指數(臺灣證券交易所)」「每日收盤行情」；tables 末尾另有一個 `fields: null` 的空表 |
| 成交股數單位 | TWSE：MI_INDEX 與 STOCK_DAY 之 2330 成交股數相同（88,179,393）。TPEx：dailyQuotes 成交股數 ÷ tradingStock 成交張數×1000 ＝ 0.99997（張數為四捨五入值） |
| TAIEX | MI_INDEX 收盤指數與 MI_5MINS_HIST 一致（36,033.29） |
| 非交易日 | TWSE 回 `stat = "很抱歉，沒有符合條件的資料!"`；TPEx 回 `stat = "ok"` 且 `tables[0].data = []` |
| 週末 | FinMind TaiwanStockTradingDate 2024-01-01 起 698 個交易日，無週末 |
| 短窗休市 | 2026-09-25（五）、09-28（一）、10-09（五）兩市皆判為非交易日，無缺漏 |
| FinMind 外層結構 | `{"msg": "success", "status": 200, "data": [...]}` |

### 規格未載、由實測補齊

| 項目 | 實測結果 | 程式處理 |
| :--- | :--- | :--- |
| 回傳日期鍵 | MI_INDEX、dailyQuotes、T86、insti/dailyTrade 頂層有 `date`（YYYYMMDD），與查詢日相同；TPEx 非交易日亦回傳 | MI_INDEX、dailyQuotes、insti 解析時比對，不一致或缺鍵即停 |
| 日期格式 | MI_5MINS_HIST `日期`、tradingStock `日 期`、exDailyQ `除權息日期`：`115/11/02`；holidaySchedule `Date`：`1160101`；revivt：`1141124`；TWT49U：`114年11月21日`；inx `日期`：`2026/11/02` | 皆為已支援格式；新增測試 |
| TPEx 無成交價字串 | dailyQuotes 開高低收出現 `---`（收盤為 ` ---`，前有空白） | 列入無成交價佔位字串 |
| TPEx 三大法人 | `insti/dailyTrade` 24 欄，「買進股數／賣出股數／買賣超股數」各重複 7 次，最後一欄「三大法人買賣超股數合計」唯一；3 列樣本驗算：第 3＋4＋7 組買賣超＝合計 | 只依唯一欄名取合計（股）；重複欄名不放入對應、列為必要欄位時直接報錯 |
| 櫃買指數 | `indexInfo/inx` fields：日期、開市、最高、最低、收市、漲/跌 | 新增 `fetch_otc_index_month`（第二階段用） |
| TPEx dailyQuotes 第二張表 | 標題「管理股票」，兩樣本日皆 0 列 | 目前不讀，待裁定 |
| TWSE 休市表 | 回傳 29 筆，樣本為民國 116 年（2027） | 程式尚未使用，run2 傾印年度分佈 |
| FinMind TaiwanStockInfo | 4,397 列，部分列 `date` 為 null | 暫行規則（待裁定）：同代號有具日期列時不採 null 列；只有 null 列時採用並標 `_date_unknown` |
| TWTB8U | 一年內 2 筆（7780、6949），規格例 4763 不在此期間；回應無 strDate/endDate 回顯，但各分段結果不同，顯示有依區間查詢 | run2 回溯至 2023-01-01 查 4763 |

### 耗時

短窗 30 次請求平均 2.30 秒（含 0.7 秒間隔），60 週約 600 次 ≈ 23 分鐘，未含事件表與參考欄，逼近第 10.1 節「25 分鐘內」。run2 另量測分主機並行模式。
