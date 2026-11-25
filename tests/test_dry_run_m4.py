"""試跑工具以假資料走完整流程（不連網）。"""

from tests.test_screener_rules import make_inputs
from tools import dry_run_m4


def test_dry_run_report(tmp_path, monkeypatch):
    monkeypatch.setattr(dry_run_m4, "collect_inputs", lambda *a, **k: make_inputs())
    assert dry_run_m4.main(["--out", str(tmp_path), "--as-of", "2026-07-31"]) == 0
    text = (tmp_path / "report.md").read_text(encoding="utf-8")
    for sec in ("驗收核對", "各分頁前 20 檔", "族群（S6）", "ETF 子類型", "處置／注意", "跳空異常", "事件對帳",
                "跳空異常警示（M4-Q1"):
        assert sec in text
    assert "| 頁 1 與頁 2 互斥 | 通過 |" in text
    assert "| 模擬綠燈時名單照常產出 | 通過 |" in text
    assert "不符 |" not in text.split("## 二、")[0]
    assert (tmp_path / "result.json").exists()
