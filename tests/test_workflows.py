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
