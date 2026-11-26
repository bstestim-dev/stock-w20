"""日資料快取放進 GitHub Pages，並在 actions/cache 不見時從 Pages 還原（2026-11-26 裁定 M6-Q1＝B）。

背景：D5 以 actions/cache 保留 DayCache；GitHub 會移除超過 7 天未存取的快取，兩次執行相隔超過 7 天即需冷啟動。

發佈（``publish_cache``，僅在選股成功、即將部署 Pages 時執行）：
- 把 DayCache 中「回溯期間內」（日期 ≥ 本次歷史起日）的日檔 ``YYYY-MM-DD.json.gz`` 原樣複製到 ``site/cache/``，
  並寫 ``site/cache/manifest.json``：每個檔的名稱、位元組數與 SHA-256。內容皆為先前自官方端點抓取的公開行情。
- 複製前先檢查每個檔可解壓、為 JSON、含 twse／tpex 兩市且 trade_date 與檔名一致；不合格的檔不發佈。
- 總大小超過 ``MAX_PUBLISH_BYTES``（200 MB，遠低於 Pages 1 GB 上限）即整批不發佈並記錄原因。

還原（``restore_from_pages``，每次執行抓官方資料前）：
- 下載 ``manifest.json``；只下載本機快取「沒有」的日檔（actions/cache 命中時通常只多 1 次請求）。
- 每個檔須通過：檔名格式（防路徑穿越）、SHA-256 與 manifest 相符、上述內容檢查；任一不符即丟棄，
  該日改由官方來源重新抓取。寫入採「暫存檔 → 原子替換」。
- 網站尚未部署、manifest 不存在（404）、網路錯誤或 manifest 格式不符：不還原任何檔、記入 notes，
  全部改由官方來源抓取（等同冷啟動，D5 的抓取預算照常適用）。還原失敗永遠不會中止選股。
- 還原的資料與 actions/cache 同等對待（D5）：都是本系統先前自官方端點取得、逐位元組相同的副本；
  本模組不產生或修改任何行情數值。
"""

from __future__ import annotations

import gzip
import hashlib
import json
import re
import shutil
import urllib.error
import urllib.request
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import date
from pathlib import Path
from typing import Any

MANIFEST_NAME = "manifest.json"
MANIFEST_SCHEMA = 1
NAME_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})\.json\.gz$")
MAX_PUBLISH_BYTES = 200 * 1024 * 1024
FETCH_TIMEOUT_SEC = 30
FETCH_RETRIES = 2
USER_AGENT = "stock-w20/0.6 (+https://github.com/bstestim-dev/stock-w20)"


def valid_day_file(name: str, data: bytes) -> bool:
    """日檔內容檢查：可解壓、為 JSON、兩市皆在且 trade_date 等於檔名日期。"""
    m = NAME_RE.match(name)
    if not m:
        return False
    try:
        raw = json.loads(gzip.decompress(data).decode("utf-8"))
        return all(isinstance(raw.get(k), dict) and raw[k].get("trade_date") == m.group(1)
                   and isinstance(raw[k].get("quotes"), list) for k in ("twse", "tpex"))
    except (OSError, ValueError, AttributeError):
        return False


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


# ---------------------------------------------------------------- 發佈

def publish_cache(cache_dir: str | Path, dest_dir: str | Path, since: date, generated_at: str) -> dict[str, Any]:
    """回傳摘要 {"published", "skipped_invalid", "bytes", "note"}；超過大小上限時不寫出任何檔。"""
    src, dest = Path(cache_dir), Path(dest_dir)
    entries, payloads, invalid = [], [], 0
    for p in sorted(src.glob("*.json.gz")):
        m = NAME_RE.match(p.name)
        if not m or date.fromisoformat(m.group(1)) < since:
            continue
        data = p.read_bytes()
        if not valid_day_file(p.name, data):
            invalid += 1
            continue
        entries.append({"name": p.name, "bytes": len(data), "sha256": sha256(data)})
        payloads.append((p, data))
    total = sum(e["bytes"] for e in entries)
    if total > MAX_PUBLISH_BYTES:
        return {"published": 0, "skipped_invalid": invalid, "bytes": total,
                "note": f"日資料快取 {total / 1e6:.1f} MB 超過發佈上限 {MAX_PUBLISH_BYTES / 1e6:.0f} MB，本次不放進網站"}
    if dest.exists():
        shutil.rmtree(dest)
    dest.mkdir(parents=True)
    for p, _data in payloads:
        shutil.copyfile(p, dest / p.name)
    manifest = {"schema": MANIFEST_SCHEMA, "generated_at_taipei": generated_at, "since": since.isoformat(),
                "files": entries}
    (dest / MANIFEST_NAME).write_text(json.dumps(manifest, ensure_ascii=False, indent=0), encoding="utf-8")
    return {"published": len(entries), "skipped_invalid": invalid, "bytes": total, "note": None}


# ---------------------------------------------------------------- 還原

@dataclass
class RestoreResult:
    restored: int = 0
    already: int = 0
    failed: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)


def http_fetch(url: str) -> bytes:
    """下載原始位元組（不送 Accept-Encoding，避免被轉碼；SHA-256 以原始檔比對）。"""
    last: Exception | None = None
    for _ in range(FETCH_RETRIES + 1):
        try:
            req = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
            with urllib.request.urlopen(req, timeout=FETCH_TIMEOUT_SEC) as resp:   # noqa: S310 — 固定 https Pages 網址
                return resp.read()
        except urllib.error.HTTPError as exc:
            if exc.code == 404:
                raise
            last = exc
        except (urllib.error.URLError, TimeoutError, OSError) as exc:
            last = exc
    raise last if last else RuntimeError("下載失敗")


def restore_from_pages(base_url: str, cache_dir: str | Path,
                       fetch: Callable[[str], bytes] = http_fetch) -> RestoreResult:
    res = RestoreResult()
    base = base_url if base_url.endswith("/") else base_url + "/"
    target = Path(cache_dir)
    target.mkdir(parents=True, exist_ok=True)
    try:
        manifest = json.loads(fetch(base + MANIFEST_NAME).decode("utf-8"))
        files = manifest["files"]
        if manifest.get("schema") != MANIFEST_SCHEMA or not isinstance(files, list):
            raise ValueError("schema")
    except urllib.error.HTTPError as exc:
        res.notes.append(f"網站尚無日資料快取（{base}{MANIFEST_NAME} 回應 {exc.code}），日資料改由官方來源抓取")
        return res
    except Exception as exc:  # noqa: BLE001 — 任何問題都只降級為官方來源抓取
        res.notes.append(f"無法讀取網站日資料快取清單（{type(exc).__name__}），日資料改由官方來源抓取")
        return res

    for e in files:
        name = str(e.get("name", "")) if isinstance(e, dict) else ""
        if not NAME_RE.match(name):
            res.failed.append(name or "?")
            continue
        path = target / name
        if path.exists():
            res.already += 1
            continue
        try:
            data = fetch(base + name)
        except Exception:  # noqa: BLE001
            res.failed.append(name)
            continue
        if sha256(data) != e.get("sha256") or not valid_day_file(name, data):
            res.failed.append(name)
            continue
        tmp = path.with_suffix(path.suffix + ".tmp")
        tmp.write_bytes(data)
        tmp.replace(path)
        res.restored += 1
    if res.restored:
        res.notes.append(f"日資料快取：自網站還原 {res.restored} 日（SHA-256 與清單相符，為先前自官方來源抓取之副本）")
    if res.failed:
        res.notes.append(f"網站日資料快取有 {len(res.failed)} 個檔未通過雜湊或格式檢查，已丟棄並改由官方來源抓取")
    return res
