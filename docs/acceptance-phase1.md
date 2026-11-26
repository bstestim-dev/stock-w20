# 第一階段驗收對照表（spec_v1 第 10.1 節）

更新：2026-11-26（Asia/Taipei）。線上證據以 run-screen run 45742193270（M6b，e449034）部署的版本為準；
Hector 線上確認步驟 1～8 於 2026-11-26 完成。第 4 項修正（AC-4）以 run-screen run 45757811557（commit dd47424）部署版本線上確認。

**整體結果：17／17 通過——第一階段 10.1 驗收通過。**

| 條文 | 驗收條件 | 結果 | 原因與證據 |
| :--- | :--- | :--- | :--- |
| 1 | workflow 僅 `workflow_dispatch`、無 `schedule:`；無 LINE、資金輸入欄、張數計算 | 通過 | `tests/test_workflows.py::test_only_workflow_dispatch`；每次交付掃描無 `schedule:`；全 repo 無 LINE API 字樣；`test_site.py::test_no_forbidden_ui`、`tests/js/app_test.js` 欄名檢查（不得含張數／資金／停損）。 |
| 2-1 | 免費模式（不設 `FINMIND_TOKEN`）手動觸發 25 分鐘內完成並更新 Pages | 通過 | run 45742193270：build 13 分 47 秒＋deploy 12 秒，Pages HTTP 200；Hector 確認 Repository secrets 無 `FINMIND_TOKEN`（免費模式）。依 D5，以已有快取之執行為準；冷啟動 run 45737967148、45738001231 各約 16 分 30 秒以結束碼 3 分次補齊（預期行為）；快取全部命中時 run 45757811557 build 2 分 7 秒、deploy 13 秒。 |
| 2-2 | 抽樣 3 檔近一年除息／減資／面額變更：週 K 連續、`close_raw` 與公開收盤價一致 | 通過 | verify-api run4：00939、2330、7780、6949 還原連續，`close_raw` 與 MI_INDEX、STOCK_DAY 一致；run 45616245478 事件對帳 4 檔差額 0；m4_dry_run run 45597664068 對帳 1,815／1,816 相符（不符 00775B 原因見 M4b-Q1）；Hector 線上確認 00939 於 2026-11-03 除息週前後 K 線連續。規格例 4763 依決策改用 7780／6949。 |
| 2-3 | 國定假日短週以最後交易日結算為 1 根週 K；`W20MA`、`D100MA` 不相等且分欄 | 通過 | verify-api run4：2330、0050 短週結算於 2026-09-24、10-02、10-08（各 4 日）；2330 W20MA 2,373.49、D100MA 2,387.39；線上 latest.json 391 列無兩值相等者；網頁「週20MA（還原）」「日100MA（對照）」分欄。 |
| 3-1 | 普通股名單 100% 股本 > 20 億，不含權證／ETN／特別股／DR | 通過 | 線上頁 1＋頁 2 共 12 檔皆 4 碼普通股，股本最小 27.12 億；m4_dry_run 核對不含 36 個 DR；`test_capital_threshold_is_strict_and_all_listed_pass`、`test_excludes_dr_preferred_and_non_common_codes`。 |
| 3-2 | 創新板股本 > 20 億即進普通股池 | 通過 | m4_dry_run：池中創新板 2258；`test_tib_included_when_capital_passes`。 |
| 3-3 | 頁 1、頁 2 互斥；頁 2 `vol_ratio_20w >= 1.5` | 通過 | 線上頁 1 9 檔、頁 2 3 檔，交集空；頁 2 倍數 2.985、1.763、2.288；`test_pages_are_mutually_exclusive_and_expected`。 |
| 3-4 | 頁 1 含未爆量新站上；勾選「僅看同時爆量」後只剩爆量者 | 通過 | 線上頁 1 未爆量 5 檔（1216、5521、2474、9904、3715）；Hector 線上勾選後剩 8213、2603、1802、2323，取消後回 9 檔；`test_new_cross_does_not_require_spike`、`tests/js/app_test.js`。 |
| 3-5 | 週均量門檻預設 1000，設 0 關閉 | 通過 | 線上 `params.min_avg_daily_lots_20w = 1000`；m4_dry_run 設 0：通過 U2 355 → 534、頁 1 增 5、頁 2 增 3；`test_liquidity_threshold_and_switch_off`。 |
| 3-6 | ETF 專頁獨立、含分類標記；預設不顯示債券與槓反，一鍵展開 | 通過 | Hector 線上：預設 6 檔皆原型；「顯示全部子類型」後 30 檔；「全部 ETF」後 370 檔；`test_site.py::test_default_controls`。 |
| 3-7 | 綠燈時頂部顯示 🟢 綠燈與 3 成水位，三個選股分頁照常產出 | 通過 | 線上實際為綠燈：頂部「🟢 綠燈（偏空）」與「總水位降至約 3 成」，名單 9／3／30；`test_green_light_does_not_filter`；m4_dry_run 強制綠燈名單不變。 |
| 4 | 無 7～10% 停損；週五跌破標「下週一開盤賣」；週中跌破標「週中警戒」 | 通過 | 無停損：`test_no_hard_stop_loss_logic`。週五跌破：紅色標籤「跌破週 20MA：建議下週一開盤賣」（`tests/js/app_test.js`、`test_site.py::test_exit_status_labels`、`test_exit_confirmed_when_settled_and_midweek_when_not`）。週中跌破：AC-4 修法 A 部署後線上確認——run-screen run 45757811557（commit dd47424，build 2 分 7 秒、deploy 13 秒，資料日 2026-11-25、W48 未結算），「跌破警戒／出場檢視」9 筆（2886、2880、3705、5880、00870B、5324、1614、2923、5522）皆顯示橘色「週中警戒（尚未週結算）」，滑鼠停留全文為「週中警戒（尚未週結算）：週中收盤低於暫定週 20MA，待週五收盤確認，第一階段不構成賣訊」，9 列列高皆 34.2px 單行；exit_alerts.csv 有 BOM，9 列「出場說明」與修正前下載檔逐位元組相同（M4-Q8 文字未變，`test_exit_note_text_in_data_unchanged`）。 |
| 5-1 | `latest.json` 與 4 份 CSV 可下載、符合第 6 節 Schema、CSV 含 UTF-8 BOM | 通過 | Hector 線上下載：4 份 CSV 皆有 BOM（位元組檢查）、中文欄名正確，行數 new_cross 10、above_and_spike 4、etf 371、exit_alerts 10（含標題列，與 9／3／370／9 筆相符）；latest.json 2,919,474 bytes，頂層 12 欄與名單元素欄位齊全；`test_output.py`。 |
| 5-2 | 參考欄預設收合可展開；點列 1 秒內展開週 K 圖（含 W20MA 線與成交量均線） | 通過 | Hector 線上：參考欄 3 欄，8213 顯示 115Q3 2.13、11510 55.17、−2,196；點 8213 約 0.97 秒出現週 K 圖，紅漲綠跌、藍色週 20MA、成交量柱與「前20週均量」虛線皆有。 |
| 5-3 | 週中執行時頂部與個股列標「週中未結算（已交易 X 天）」；倍數用實際累計量並另顯示推算倍數 | 通過 | 線上頂部「⚠️ 週中未結算（已交易 3 天）」；Hector 確認頁 1 每列警示欄皆有「週中未結算（已交易 3 天）」；「推算滿週倍數」欄顯示（如 8213 2.85x）；`test_midweek_uses_actual_volume_and_projects_with_five_over_x`。 |
| 6 | pytest 涵蓋還原比例、短週聚合、分頁邏輯、頁 1 不強制爆量、週中累計量與推算倍數、創新板納入、空資料防呆，全數通過 | 通過 | 333 項全過（run-screen build 亦先跑 pytest）。對應：`test_factor_is_ref_over_before`、`test_backward_adjustment_par_value_change_is_continuous`；`test_short_week_settles_on_last_trading_day`；`test_pages_are_mutually_exclusive_and_expected`、`test_page_of_equality_and_missing`；`test_new_cross_does_not_require_spike`；`test_midweek_uses_actual_volume_and_projects_with_five_over_x`；`test_tib_included_when_capital_passes`；`test_empty_data_does_not_crash`。 |
| 7 | Public repo、GitHub Free Pages、README 載明；config／原始碼無 token；`FINMIND_TOKEN` 僅在 Secrets | 通過 | GitHub API 回傳 `private=false`、`visibility=public`；Pages HTTP 200；README「Public repository 與機密」；每次交付密鑰掃描無命中、config 防呆；workflow 僅以 `${{ secrets.FINMIND_TOKEN }}` 注入（`test_run_screen_spec`）；Hector 確認 Secrets 無 `FINMIND_TOKEN`。 |
