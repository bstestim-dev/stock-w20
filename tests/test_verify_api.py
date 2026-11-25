"""以假回應走完 tools/verify_api 全流程，確保驗證腳本本身不會在 runner 上崩潰。"""

from datetime import date

from src.fetchers.http import FetchError, RequestLog
from tests import fixtures as F
from tools import verify_api

NON_TRADING = {"20261121", "2026/11/21"}


class RouteClient:
    def __init__(self):
        self.logs = []

    def get_json(self, url, params=None, headers=None):
        params = params or {}
        self.logs.append(RequestLog(url, params, 200, 0.01, 1))
        d = params.get("date", "")
        if "MI_INDEX" in url:
            return F.TWSE_NO_DATA if d in NON_TRADING or d.endswith(("1009",)) else F.mi_index_payload()
        if "dailyQuotes" in url:
            return F.TPEX_NO_DATA if d in NON_TRADING or d.endswith("10/09") else F.daily_quotes_payload()
        if "STOCK_DAY" in url and "ALL" not in url:
            return {"stat": "OK", "fields": ["日期", "成交股數", "成交金額", "開盤價", "最高價", "最低價", "收盤價", "漲跌價差", "成交筆數", "註記"],
                    "data": [["115/11/20", "30,123,456", "1", "1", "1", "1", "1", "0", "1", ""]]}
        if "MI_5MINS_HIST" in url:
            return {"stat": "OK", "fields": ["日期", "開盤指數", "最高指數", "最低指數", "收盤指數"],
                    "data": [["115/11/20", "1", "1", "1", "23,100.50"]]}
        if "TWT49U" in url:
            return F.TWT49U_PAYLOAD
        if "TWTAUU" in url:
            return F.TWTAUU_PAYLOAD
        if "T86" in url:
            return F.T86_PAYLOAD
        if "tradingStock" in url:
            return F.TRADING_STOCK_PAYLOAD
        if "revivt" in url:
            return F.REVIVT_PAYLOAD
        if "insti/dailyTrade" in url:
            return {"stat": "ok", "tables": [{"title": "三大法人買賣明細資訊", "fields": ["代號", "名稱"], "data": [["6488", "環球晶"]]}]}
        if "tpex.org.tw/www" in url:
            return {"stat": "ok", "tables": [{"title": "", "fields": [], "data": []}]}
        if "twse.com.tw/rwd" in url:
            return F.TWSE_NO_DATA
        if "finmindtrade" in url:
            if params.get("dataset") == "TaiwanStockTradingDate":
                return {"data": [{"date": "2026-11-20"}]}
            return {"data": [{"industry_category": "ETF", "stock_id": "0050", "stock_name": "元大台灣50", "type": "twse", "date": "2024-01-01"}]}
        if "t187ap03_L" in url:
            return [{"公司代號": "2330", "公司簡稱": "台積電", "產業別": "24", "普通股每股面額": "新台幣 10.0000元",
                     "實收資本額": "259323700670", "特別股": "0", "已發行普通股數或TDR原股發行股數": "25932370067"}]
        if "notice" in url:
            return [{"Date": "", "Code": "", "Name": ""}]
        if "openapi" in url:
            raise FetchError("模擬失敗")
        raise AssertionError(url)


def test_verify_run_and_render(tmp_path):
    v = verify_api.Verifier(RouteClient(), tmp_path)
    verify_api.run(v, date(2026, 11, 20), date(2026, 10, 9))
    report = verify_api.render(v, date(2026, 11, 20), date(2026, 10, 9))
    assert "# M2 API 實測報告" in report
    items = {item: (res, detail) for item, res, detail in v.checks}
    assert items["單位：TWSE MI_INDEX 與 STOCK_DAY 之 2330 成交股數相同"][0] == "通過"
    assert items["單位：dailyQuotes 成交股數 ≈ tradingStock 成交張數×1000（6488）"][0] == "通過"
    assert items["TAIEX：MI_INDEX 收盤指數與 MI_5MINS_HIST 一致"][0] == "通過"
    assert items["短窗歷史流程（fetch_daily_history）"][0] == "通過"
    assert "2026-10-09(五)=non_trading" in items["短窗歷史流程（fetch_daily_history）"][1]
    assert "Q4：TPEx 三大法人完整欄位" in items
    assert "驗證腳本未預期錯誤" not in items
    assert any(r["ok"] is False for r in v.results)          # 失敗探測也被記錄而不中斷
    assert (tmp_path / "raw").is_dir()
