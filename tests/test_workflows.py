"""第 10.1 節第 1 項：workflow 僅 workflow_dispatch、無 schedule、不含機密明文。"""

from pathlib import Path

import yaml

WF = sorted((Path(__file__).resolve().parents[1] / ".github" / "workflows").glob("*.yml"))


def test_workflows_exist():
    assert WF


def test_only_workflow_dispatch():
    for f in WF:
        doc = yaml.safe_load(f.read_text(encoding="utf-8"))
        triggers = doc.get("on", doc.get(True))           # PyYAML 將 on 解析為 True
        assert set(triggers) == {"workflow_dispatch"}, f.name
        assert "schedule:" not in f.read_text(encoding="utf-8"), f.name


def test_no_secret_literals():
    for f in WF:
        text = f.read_text(encoding="utf-8")
        assert "ghp_" not in text and "github_pat_" not in text and "eyJ" not in text, f.name


def _load(name):
    return yaml.safe_load((Path(__file__).resolve().parents[1] / ".github" / "workflows" / name).read_text(encoding="utf-8"))


def test_run_screen_spec():
    """第 8.2 節：四個手動輸入、權限、Pages 官方部署、timeout 30、Python 3.12、TZ；D5 快取失敗也儲存。"""
    doc = _load("run-screen.yml")
    inputs = doc.get("on", doc.get(True))["workflow_dispatch"]["inputs"]
    assert list(inputs) == ["as_of_date", "lookback_weeks", "volume_spike_ratio", "skip_fundamentals"]
    assert inputs["skip_fundamentals"]["type"] == "boolean" and inputs["skip_fundamentals"]["default"] is False
    assert inputs["volume_spike_ratio"]["default"] == "1.5" and inputs["as_of_date"]["default"] == ""
    assert doc["permissions"] == {"contents": "read", "pages": "write", "id-token": "write"}
    build, deploy = doc["jobs"]["build"], doc["jobs"]["deploy"]
    assert build["timeout-minutes"] == 30 and build["env"]["TZ"] == "Asia/Taipei"
    env = build["env"]
    assert env["SW20_AS_OF_DATE"] == "${{ inputs.as_of_date }}"
    assert env["SW20_LOOKBACK_WEEKS"] == "${{ inputs.lookback_weeks }}"
    assert env["SW20_VOLUME_SPIKE_RATIO"] == "${{ inputs.volume_spike_ratio }}"
    assert env["SW20_SKIP_FUNDAMENTALS"] == "${{ inputs.skip_fundamentals }}"
    assert env["FINMIND_TOKEN"] == "${{ secrets.FINMIND_TOKEN }}"           # 只由 Secrets 注入
    uses = {s.get("uses", "").split("@")[0]: s for s in build["steps"]}
    assert uses["actions/setup-python"]["with"]["python-version"] == "3.12"
    assert uses["actions/cache/save"]["if"] == "always()"
    assert uses["actions/upload-pages-artifact"]["with"]["path"] == "site/"
    run = next(s for s in build["steps"] if "src.screener" in s.get("run", ""))
    assert "--cache-dir" in run["run"] and "--fetch-budget-min" in run["run"] and run["timeout-minutes"] < 30
    assert '--restore-from "$PAGES_CACHE_URL"' in run["run"] and "--publish-cache-dir site/cache" in run["run"]
    assert env["PAGES_CACHE_URL"] == ("https://${{ github.repository_owner }}.github.io/"
                                      "${{ github.event.repository.name }}/cache/")
    assert deploy["needs"] == "build" and deploy["environment"]["name"] == "github-pages"
    assert deploy["steps"][0]["uses"].startswith("actions/deploy-pages@")


def test_actions_on_node24_versions():
    """Node 20 runtime 已淘汰：一律使用支援 Node 24 的主版本。"""
    allowed = {"actions/checkout": "v6", "actions/setup-python": "v6", "actions/upload-artifact": "v6",
               "actions/cache/restore": "v5", "actions/cache/save": "v5", "actions/upload-pages-artifact": "v5",
               "actions/deploy-pages": "v5"}
    for f in WF:
        for job in yaml.safe_load(f.read_text(encoding="utf-8"))["jobs"].values():
            for step in job["steps"]:
                if "uses" in step:
                    name, ver = step["uses"].split("@")
                    assert allowed.get(name) == ver, (f.name, step["uses"])


def test_verify_api_m7_probe_job():
    """M7a：verify-api 新增 mode=m7_probe（仍只能手動觸發）；不設定 token、不讀 Secrets，報告上傳 artifact。"""
    doc = _load("verify-api.yml")
    assert "m7_probe" in doc.get("on", doc.get(True))["workflow_dispatch"]["inputs"]["mode"]["options"]
    job = doc["jobs"]["m7-probe"]
    assert job["if"] == "${{ inputs.mode == 'm7_probe' }}"
    text = yaml.safe_dump(job, allow_unicode=True)
    assert "secrets" not in text and "FINMIND_TOKEN" not in text
    runs = [s.get("run", "") for s in job["steps"]]
    assert "pytest -q" in runs and "python -m tools.verify_m7 --out verify_m7_report" in runs
    up = next(s for s in job["steps"] if str(s.get("uses", "")).startswith("actions/upload-artifact"))
    assert up["with"]["name"] == "m7-probe-report" and up["if"] == "always()"
