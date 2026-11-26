"""M6-Q1＝B：日資料快取放進 Pages、actions/cache 不見時自網站還原（SHA-256 與內容檢查，失敗即改由官方來源抓取）。"""

import gzip
import json
import urllib.error
from datetime import date

import pytest

from src.cache_publish import (MANIFEST_NAME, publish_cache, restore_from_pages, sha256, valid_day_file)
from src.fetchers import history
from src.fetchers.parsing import DailyMarket, DailyQuote

BASE = "https://bstestim-dev.github.io/stock-w20/cache/"
TODAY = date(2026, 11, 26)


def day(d, trading=True):
    q = [DailyQuote("twse", "2330", "台積電", 1.0, 2.0, 0.5, 1.5, 1000.0, 1500.0)] if trading else []
    return history.DayResult(d, DailyMarket("twse", d, trading, q, 23000.0 if trading else None),
                             DailyMarket("tpex", d, trading, [], None))


def fill(cache_dir, dates):
    c = history.DayCache(cache_dir)
    for d in dates:
        assert c.put(day(d), TODAY)
    return c


DATES = [date(2026, 11, 16), date(2026, 11, 17), date(2026, 11, 18)]


def site_fetch(site_dir):
    def fetch(url):
        name = url[len(BASE):]
        p = site_dir / name
        if not p.exists():
            raise urllib.error.HTTPError(url, 404, "Not Found", None, None)
        return p.read_bytes()
    return fetch


def test_publish_writes_files_and_manifest(tmp_path):
    fill(tmp_path / "c", DATES)
    s = publish_cache(tmp_path / "c", tmp_path / "site", since=date(2026, 11, 17), generated_at="2026-11-26T18:00:00+08:00")
    assert s["published"] == 2 and s["note"] is None
    m = json.loads((tmp_path / "site" / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert m["schema"] == 1 and m["since"] == "2026-11-17"
    assert [f["name"] for f in m["files"]] == ["2026-11-17.json.gz", "2026-11-18.json.gz"]   # 起日之前不發佈
    for f in m["files"]:
        data = (tmp_path / "site" / f["name"]).read_bytes()
        assert f["sha256"] == sha256(data) and f["bytes"] == len(data)


def test_publish_skips_invalid_and_respects_size_limit(tmp_path, monkeypatch):
    fill(tmp_path / "c", DATES)
    (tmp_path / "c" / "2026-11-19.json.gz").write_bytes(b"broken")
    s = publish_cache(tmp_path / "c", tmp_path / "site", date(2026, 11, 1), "t")
    assert s["published"] == 3 and s["skipped_invalid"] == 1
    import src.cache_publish as cp
    monkeypatch.setattr(cp, "MAX_PUBLISH_BYTES", 10)
    s = publish_cache(tmp_path / "c", tmp_path / "site2", date(2026, 11, 1), "t")
    assert s["published"] == 0 and "超過發佈上限" in s["note"] and not (tmp_path / "site2").exists()


def test_roundtrip_restore_only_missing(tmp_path):
    fill(tmp_path / "old", DATES)
    publish_cache(tmp_path / "old", tmp_path / "site", date(2026, 11, 1), "t")
    fill(tmp_path / "new", DATES[:1])                         # actions/cache 只剩 1 日
    r = restore_from_pages(BASE, tmp_path / "new", fetch=site_fetch(tmp_path / "site"))
    assert (r.restored, r.already, r.failed) == (2, 1, [])
    assert any("自網站還原 2 日" in n for n in r.notes)
    c = history.DayCache(tmp_path / "new")
    got = c.get(DATES[2])
    assert got is not None and got.twse.quotes[0].close == 1.5 and got.from_cache
    assert (tmp_path / "new" / "2026-11-18.json.gz").read_bytes() == (tmp_path / "old" / "2026-11-18.json.gz").read_bytes()


def test_site_not_deployed_yet(tmp_path):
    r = restore_from_pages(BASE, tmp_path / "c", fetch=site_fetch(tmp_path / "nothing"))
    assert r.restored == 0 and r.failed == [] and "網站尚無日資料快取" in r.notes[0] and "404" in r.notes[0]


@pytest.mark.parametrize("payload", [b"not json", json.dumps({"schema": 99, "files": []}).encode(),
                                     json.dumps({"schema": 1}).encode()])
def test_bad_manifest_degrades(tmp_path, payload):
    r = restore_from_pages(BASE, tmp_path / "c", fetch=lambda url: payload)
    assert r.restored == 0 and "無法讀取網站日資料快取清單" in r.notes[0]


def test_network_error_degrades(tmp_path):
    def boom(url):
        raise urllib.error.URLError("down")
    r = restore_from_pages(BASE, tmp_path / "c", fetch=boom)
    assert r.restored == 0 and "改由官方來源抓取" in r.notes[0]


def test_hash_mismatch_corrupt_and_traversal_rejected(tmp_path):
    fill(tmp_path / "old", DATES)
    publish_cache(tmp_path / "old", tmp_path / "site", date(2026, 11, 1), "t")
    site = tmp_path / "site"
    (site / "2026-11-16.json.gz").write_bytes(b"tampered")                       # 雜湊不符
    m = json.loads((site / MANIFEST_NAME).read_text(encoding="utf-8"))
    bad = gzip.compress(b"{}")
    (site / "2026-11-20.json.gz").write_bytes(bad)
    m["files"] += [{"name": "2026-11-20.json.gz", "bytes": len(bad), "sha256": sha256(bad)},   # 雜湊相符但內容不合格
                   {"name": "../evil.json.gz", "bytes": 1, "sha256": "x"}]                      # 路徑穿越
    (site / MANIFEST_NAME).write_text(json.dumps(m), encoding="utf-8")
    r = restore_from_pages(BASE, tmp_path / "new", fetch=site_fetch(site))
    assert r.restored == 2 and sorted(r.failed) == ["../evil.json.gz", "2026-11-16.json.gz", "2026-11-20.json.gz"]
    assert not (tmp_path / "new" / "2026-11-16.json.gz").exists() and not (tmp_path / "evil.json.gz").exists()
    assert not list((tmp_path / "new").glob("*.tmp"))
    assert any("3 個檔未通過" in n for n in r.notes)


def test_valid_day_file_checks_date_matches_name():
    d = day(date(2026, 11, 16))
    raw = gzip.compress(json.dumps({"twse": history._market_to_dict(d.twse), "tpex": history._market_to_dict(d.tpex)}).encode())
    assert valid_day_file("2026-11-16.json.gz", raw)
    assert not valid_day_file("2026-11-17.json.gz", raw)
    assert not valid_day_file("x.json.gz", raw)


def test_run_restores_then_publishes(tmp_path, monkeypatch):
    """run()：抓官方資料前先自網站還原；成功後把回溯期間內的日檔放進 site/cache，並把還原情形寫進 notes。"""
    from src import screener as sc
    from tests.test_output import FakeClient
    from tests.test_screener_rules import make_inputs

    fill(tmp_path / "old", DATES)
    publish_cache(tmp_path / "old", tmp_path / "deployed", date(2026, 11, 1), "t")
    inp = make_inputs()
    inp.history_start = date(2026, 11, 17)

    def fake_collect(client, cfg, end, today, cache=None, **k):
        assert cache is not None and cache.get(DATES[2]) is not None          # 還原發生在抓官方資料之前
        return inp
    monkeypatch.setattr(sc, "collect_inputs", fake_collect)
    out = tmp_path / "site" / "data"
    rc = sc.run(str(out), env={"SW20_SKIP_FUNDAMENTALS": "true"}, client=FakeClient(), today=date(2026, 7, 31),
                cache_dir=str(tmp_path / "cache"), restore_from=BASE, publish_cache_dir=str(tmp_path / "site" / "cache"),
                pages_fetch=site_fetch(tmp_path / "deployed"))
    assert rc == 0
    latest = json.loads((out / "latest.json").read_text(encoding="utf-8"))
    assert any("自網站還原 3 日" in n for n in latest["notes"])
    m = json.loads((tmp_path / "site" / "cache" / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert [f["name"] for f in m["files"]] == ["2026-11-17.json.gz", "2026-11-18.json.gz"]
