#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""holdle-data-skill 离线单测（不访问网络、不调行情接口）。

只覆盖三件「改坏了要等用户端出事才发现」的事：

  1. **归母净利率口径与边界** —— 投研门槛用的是**归母口径**净利率（归母净利润 ÷ 营业总收入）。
     各数据源自带的「销售净利率」是**含少数股东损益**口径，不得用于门槛判定
     （实测 600132 2025：含少数 16.83% vs 归母 8.36%）。
  2. **VERSION 与 version.json 一致** —— 脚本自更新按版本号比较，
     **改了脚本不升 VERSION，已装旧版的客户端永远不会自动更新**。
  3. **自更新安全标记齐全** —— 标记缺失会让**所有**客户端拒绝下载新脚本，比不更新更糟。

跑法（需 pytest；务必从本 skill 目录执行）：

    cd <holdle-data-skill>
    python -m pytest tests/ -q
"""
import importlib.util
import inspect
import json
import math
import pathlib
import re

import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
SCRIPT_PATH = ROOT / "holdle_data.py"
VERSION_JSON_PATH = ROOT / "version.json"
SCRIPT_TEXT = SCRIPT_PATH.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def mod():
    """按路径加载 holdle_data.py（顶层 import pandas，需已安装；不会执行 main）。"""
    spec = importlib.util.spec_from_file_location("holdle_data_under_test", SCRIPT_PATH)
    m = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(m)
    return m


# ─────────────────────────────────────────────────────────────
# 1. 归母净利率（HOLDLE 门槛口径）
# ─────────────────────────────────────────────────────────────

def test_parent_net_margin_chongqing_beer_2025(mod):
    """600132 2025：归母净利 12.31 亿 / 营业总收入 147.22 亿 = 8.36%（门槛口径实测值）。"""
    assert mod._parent_net_margin(12.31, 147.22) == pytest.approx(8.361, abs=0.01)


def test_parent_net_margin_is_not_minority_inclusive(mod):
    """口径防串：若误用含少数净利（24.78 亿），结果会变成 16.8% —— 必须能区分开。"""
    parent_scope = mod._parent_net_margin(12.31, 147.22)
    minority_inclusive = mod._parent_net_margin(24.78, 147.22)
    assert minority_inclusive > parent_scope * 1.9  # 差异接近一倍，不是舍入级别
    assert parent_scope < 10.0 <= minority_inclusive  # 两种口径会得出相反的门槛结论


def test_parent_net_margin_loss_making_is_negative_not_nan(mod):
    """亏损公司：归母净利为负 → 返回负净利率（是有效数据，不能吞成 nan）。"""
    v = mod._parent_net_margin(-5.0, 100.0)
    assert not math.isnan(v) and v == pytest.approx(-5.0)


@pytest.mark.parametrize("profit,revenue", [
    (float("nan"), 100.0),     # 归母净利缺失
    (10.0, float("nan")),      # 营业总收入缺失
    (float("nan"), float("nan")),
    (None, 100.0),
    (10.0, None),
    (10.0, 0.0),               # 营收为 0：不得 ZeroDivisionError
    (10.0, 0),                 # 同上（int）
])
def test_parent_net_margin_returns_nan_when_not_computable(mod, profit, revenue):
    """任一值缺失或营收为 0 → nan（不猜、不抛异常）。"""
    assert math.isnan(mod._parent_net_margin(profit, revenue))


def test_parent_net_margin_all_four_data_lines_emit_it(mod):
    """四条数据线（东财A股 / 港股 / 美股 / 同花顺）都必须产出 parent_net_margin。

    某条线漏了，该市场就用不上归母口径净利率。
    """
    for fn_name in ("fetch_financials_a", "fetch_financials_hk",
                    "fetch_financials_us", "fetch_financials_a_ths"):
        fn = getattr(mod, fn_name, None)
        assert fn is not None, f"{fn_name} 不存在（被改名或删除？）"
        src = inspect.getsource(fn)
        assert "parent_net_margin" in src, f"{fn_name} 未产出 parent_net_margin"


def test_net_margin_field_still_means_minority_inclusive(mod):
    """net_margin 语义未变（仍是源自带的含少数口径），防止有人"顺手"把它改成归母。"""
    src = inspect.getsource(mod.fetch_financials_a_ths)
    assert "'net_margin': _parse_ths_num(row.get('销售净利率'))" in src


# ─────────────────────────────────────────────────────────────
# 2. 版本号纪律（VERSION == version.json）
# ─────────────────────────────────────────────────────────────

def test_version_in_script_matches_version_json():
    """脚本 VERSION 与 version.json 必须一致 —— 否则客户端自更新的版本比较会失真。"""
    m = re.search(r'^VERSION = "([\d.]+)"', SCRIPT_TEXT, re.MULTILINE)
    assert m, "脚本里找不到 VERSION 声明"
    script_ver = m.group(1)
    data_ver = json.loads(VERSION_JSON_PATH.read_text(encoding="utf-8"))["version"]
    assert script_ver == data_ver, (
        f"脚本 VERSION={script_ver} 与 version.json={data_ver} 不一致。"
        f" 发布前必须同时升版：不升版本号，已装旧版的客户端永远不会自动更新。"
    )


def test_version_is_semver():
    """版本号必须是 x.y.z（自更新用 _ver_cmp 做语义化比较）。"""
    data_ver = json.loads(VERSION_JSON_PATH.read_text(encoding="utf-8"))["version"]
    assert re.fullmatch(r"\d+\.\d+\.\d+", data_ver), f"版本号格式非法: {data_ver}"


def test_version_json_changelog_leads_with_its_own_version():
    """changelog 必须以「vX.Y.Z：」开头且与 version 字段同号 —— 防升了号忘写说明。"""
    d = json.loads(VERSION_JSON_PATH.read_text(encoding="utf-8"))
    assert d["changelog"].startswith(f"v{d['version']}："), (
        f"changelog 开头与 version={d['version']} 不匹配：{d['changelog'][:40]!r}"
    )


def test_version_json_has_updated_date():
    """version.json 必须带 updated（发布留痕）。"""
    d = json.loads(VERSION_JSON_PATH.read_text(encoding="utf-8"))
    assert re.fullmatch(r"\d{4}-\d{2}-\d{2}", d.get("updated", "")), d.get("updated")


# ─────────────────────────────────────────────────────────────
# 3. 自更新安全标记（缺失 = 所有客户端拒绝更新）
# ─────────────────────────────────────────────────────────────

def test_update_markers_present_in_script(mod):
    """脚本必须同时包含全部身份标记，否则客户端校验失败、拒绝下载。"""
    assert mod._UPDATE_MARKERS, "_UPDATE_MARKERS 不应为空"
    for marker in mod._UPDATE_MARKERS:
        assert marker in SCRIPT_TEXT, f"缺少自更新安全标记: {marker!r}"


def test_update_markers_are_specific_enough():
    """标记不能退化成空串或单字符（否则校验形同虚设）。"""
    script_ver = re.search(r'^VERSION = "([\d.]+)"', SCRIPT_TEXT, re.MULTILINE).group(1)
    for marker in ("HOLDLE_DATA_SKILL", "HOLDLE 行情数据获取", 'VERSION = "1.'):
        assert marker in SCRIPT_TEXT, f"标记丢失: {marker!r}"
    assert f'VERSION = "{script_ver}"' in SCRIPT_TEXT


def test_auto_update_version_consistency_guard_exists():
    """版本一致性校验必须保留：防 CDN 缓存滞后时把旧代码当新版装上。"""
    assert "≠ 期望" in SCRIPT_TEXT or "版本一致性校验" in SCRIPT_TEXT
