"""第 7 節前端：靜態檔結構與預設狀態；app.js 純函式以 Node 測試（無 Node 時略過）。"""

import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SITE = ROOT / "site"


def html():
    return (SITE / "index.html").read_text(encoding="utf-8")


def test_static_files_exist():
    for f in ("index.html", "app.js", "styles.css"):
        assert (SITE / f).exists()


def test_four_tabs_and_title():
    h = html()
    assert "<title>林恩如週 20MA 選股系統</title>" in h
    assert re.findall(r'data-tab="([a-z_]+)"', h) == ["new_cross", "above_and_spike", "etf", "exit_alerts"]


def test_default_controls():
    h = html()
    assert re.search(r'<input type="checkbox" id="spikeOnly">', h)            # 預設不勾選
    assert re.search(r'<input type="checkbox" id="showFund">', h)             # 參考欄預設收合
    assert re.search(r'value="bond">', h) and re.search(r'value="leveraged_inverse">', h)   # 債券、槓反預設未勾
    assert re.search(r'value="equity_regular" checked', h) and re.search(r'value="active" checked', h)
    assert 'id="etfAll"' in h


def test_chart_library_pinned_and_attributed():
    h = html()
    assert "lightweight-charts@4.1.1/dist/lightweight-charts.standalone.production.js" in h
    assert "tradingview.com" in h


def test_no_forbidden_ui():
    text = html() + (SITE / "app.js").read_text(encoding="utf-8")
    for bad in ("建議買進張數", "投入資金", "LINE", "line.me", "localStorage"):
        assert bad not in text


@pytest.mark.skipif(shutil.which("node") is None, reason="未安裝 Node.js")
def test_app_js_unit():
    r = subprocess.run(["node", str(ROOT / "tests" / "js" / "app_test.js")], capture_output=True, text=True, timeout=60)
    assert r.returncode == 0, r.stdout + r.stderr
