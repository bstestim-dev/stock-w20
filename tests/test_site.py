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


def test_single_line_rows_css():
    """M5b：表格列高度一致——儲存格不換行、徽章欄截斷（white-space: nowrap + text-overflow: ellipsis）。"""
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    assert "white-space: normal" not in css
    assert re.search(r"\.clip \{[^}]*overflow: hidden;[^}]*text-overflow: ellipsis;[^}]*white-space: nowrap;", css)
    assert re.search(r"tbody tr\.row td \{ height: 34px;", css)


def test_exit_status_labels():
    """第 10.1 節第 4 項：出場分頁顯示「跌破週 20MA：建議下週一開盤賣」（紅）與「週中警戒（尚未週結算）」（橘黃）標籤。"""
    js = (SITE / "app.js").read_text(encoding="utf-8")
    css = (SITE / "styles.css").read_text(encoding="utf-8")
    assert "EXIT_CONFIRMED: { text: '跌破週 20MA：建議下週一開盤賣', cls: 'exit-confirmed' }" in js
    assert "MIDWEEK_WARNING: { text: '週中警戒（尚未週結算）', cls: 'exit-midweek' }" in js
    assert re.search(r"\.badge\.exit-confirmed \{[^}]*background: var\(--up\)", css)          # 紅
    assert re.search(r"\.badge\.exit-midweek \{[^}]*background: #fff3cd;[^}]*border-color: #f0b400", css)  # 橘黃
    assert re.search(r"\.clip\.exitnote \{ max-width: 420px; \}", css)


def test_exit_note_text_in_data_unchanged():
    """修法 A 只改網頁：JSON／CSV 的出場說明維持 M4-Q8 定案文字。"""
    from src.screener import EXIT_NOTES
    assert EXIT_NOTES["EXIT_CONFIRMED"] == "跌破週 20MA：建議下週一開盤賣"
    assert EXIT_NOTES["MIDWEEK_WARNING"] == "週中收盤低於暫定週 20MA，待週五收盤確認，第一階段不構成賣訊"
