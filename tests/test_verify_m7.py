"""M7a：tools/verify_m7 以假回應走完全流程（不連網），並測試只讀原始回應的輔助函式。"""

from datetime import date

from src.fetchers.http import RequestLog
from tests import fixtures as F
from tests.test_verify_api import RouteClient
from tools import verify_m7 as m7
from tools.verify_api import Verifier, render

DELIST = {"msg": "success", "status": 200, "data": [
    {"date": "2021-05-03", "stock_id": "1234", "stock_name": "某甲"},
    {"date": "2023-08-01", "stock_id": "5678", "stock_name": "某乙"},
    {"date": "2019-01-02", "stock_id": "030000", "stock_name": "權證"}]}


class M7Client(RouteClient):
    def _route(self, url, params):
        if "finmindtrade" in url and params.get("dataset") == "TaiwanStockDelisting":
            self.logs.append(RequestLog(url, params, 200, 0.01, 1))
            return DELIST
        return super()._route(url, params)


def test_tr_index_close_and_fields():
    p = F.mi_index_payload()
    p = {**p, "tables": p["tables"] + [{"title": "報酬指數(臺灣證券交易所)", "fields": ["報酬指數", "收盤指數"],
                                         "data": [["寶島股價報酬指數", "61,501.93"], ["發行量加權股價報酬指數", "83,329.36"]]}]}
    assert m7.tr_index_close(p) == (83329.36, "報酬指數(臺灣證券交易所)")
    assert m7.tr_index_close({"stat": "OK", "tables": []}) == (None, None)
    assert m7.tr_index_close(None) == (None, None)


def test_fields_check():
    assert m7.fields_check(list(m7.MI_REQUIRED) + ["本益比"], m7.MI_REQUIRED)[0] is True
    ok, detail = m7.fields_check(["證券代號"], m7.MI_REQUIRED)
    assert not ok and detail.startswith("缺 [")
    assert m7.fields_check(None, m7.DQ_REQUIRED)[0] is False
    assert m7.quote_fields(F.daily_quotes_payload(), None) == F.DAILY_QUOTES_FIELDS


def test_delisting_summary():
    ok, detail = m7.delisting_summary(DELIST)
    assert ok and "3 筆" in detail and "{'2021': 1, '2023': 1}" in detail and "4 碼數字代號 2 筆" in detail
    ok, detail = m7.delisting_summary({"msg": "Token is required", "status": 402})
    assert not ok and "非預期格式" in detail
    assert m7.delisting_summary({"data": []})[0] is False


def test_run_m7_offline(tmp_path):
    v = Verifier(M7Client(), tmp_path)
    m7.run_m7(v)
    items = {i: (r, d) for i, r, d in v.checks}
    assert not [i for i in items if "未預期錯誤" in i], [i for i in items if "未預期錯誤" in i]
    for d in m7.SAMPLE_DATES:
        assert any(i.startswith(f"解析器：M7a：MI_INDEX {d:%Y%m%d}") for i in items), d
    assert any("dailyQuotes" in i for i in items) and any("櫃買指數月表" in i for i in items)
    assert "M7a：FinMind 下市清單無 token 可讀" in items and items["M7a：FinMind 下市清單無 token 可讀"][0] == "通過"
    assert any(i.startswith("M7a：0050 事件") for i in items)
    assert any("TWSE 除權息事件逐年筆數" in i for i in items)
    # 請求量維持在抽查等級（約 100 次），不抓多年日資料
    assert len(v.results) < 160
    report = render(v, m7.SAMPLE_DATES[0], m7.SAMPLE_DATES[-1], title="M7a 歷史資料可用性抽查報告", scope="x")
    assert report.startswith("# M7a 歷史資料可用性抽查報告") and "- x" in report


def test_finmind_probe_sends_no_token(monkeypatch):
    seen = {}

    class C(M7Client):
        def get_json(self, url, params=None, headers=None):
            if "finmindtrade" in url:
                seen["headers"], seen["params"] = headers, dict(params or {})
            return super().get_json(url, params, headers)
    monkeypatch.delenv("FINMIND_TOKEN", raising=False)
    v = Verifier(C(), __import__("pathlib").Path(__import__("tempfile").mkdtemp()))
    m7.probe_finmind(v)
    assert not seen["headers"] and "token" not in seen["params"] and seen["params"]["dataset"] == "TaiwanStockDelisting"
