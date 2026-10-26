#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""judge.py 离线单测（不访问网络，用合成K线）。

只测「改坏了用户会看到错结论」的事：
  1. 不可判分支不能猜（未收完/次新/金融/空数据）
  2. 结构性隔离：不可判时 state_a/window/entry 必须为 null
  3. 话术拼装：final_text 必须等于 units 按 seq 拼接
  4. 关键口径常量不得被误改（0.03 贴0轴 / 5% 不追 / 12-26-9）
  5. 窗口关闭时不得下发参考价（P0 防"可以买"误读）

跑法：cd holdle-data-skill && python -m pytest tests/ -q
"""
import importlib.util
import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
judge = pytest.importorskip("judge")


def make_daily(n=1500, seed=7, start="2015-01-05"):
    """合成日K（随机游走，工作日）。只用于结构测试，不用于口径验证。"""
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n)
    ret = rng.normal(0.0004, 0.018, n)
    close = 10 * np.exp(np.cumsum(ret))
    high = close * (1 + np.abs(rng.normal(0, 0.008, n)))
    low = close * (1 - np.abs(rng.normal(0, 0.008, n)))
    open_ = close * (1 + rng.normal(0, 0.004, n))
    return pd.DataFrame({
        "date": dates, "open": open_, "high": high, "low": low,
        "close": close, "volume": rng.integers(1e5, 1e6, n),
    })


# ── 1. 不可判分支不猜 ─────────────────────────────
class TestIndeterminateNeverGuesses:
    def test_empty_data(self):
        r = judge.judge(pd.DataFrame(), code="600519")
        assert r["status"] == "indeterminate"
        assert r["indeterminate_reason"] == "data_missing"
        assert r["state_a"] is None

    def test_none_data(self):
        r = judge.judge(None, code="600519")
        assert r["status"] == "indeterminate"

    def test_finance_industry(self):
        r = judge.judge(make_daily(), code="000001", industry="银行")
        assert r["status"] == "indeterminate"
        assert r["indeterminate_reason"] == "finance_industry"
        assert "金融类默认不参与" in r["required_statement"]["final_text"]

    def test_new_stock(self):
        r = judge.judge(make_daily(n=300), code="301999")   # ~14 个月
        assert r["status"] == "indeterminate"
        assert r["indeterminate_reason"] == "insufficient_history"

    def test_missing_column(self):
        d = make_daily().drop(columns=["high"])
        r = judge.judge(d, code="600519")
        assert r["status"] == "indeterminate"

    def test_as_of_before_listing(self):
        r = judge.judge(make_daily(), code="600519", as_of="1990-01-02")
        assert r["status"] == "indeterminate"
        assert r["indeterminate_reason"] == "insufficient_history"

    def test_bad_as_of(self):
        r = judge.judge(make_daily(), code="600519", as_of="不是日期")
        assert r["status"] == "indeterminate"


# ── 2. 结构性隔离 ─────────────────────────────────
class TestStructuralIsolation:
    def test_indeterminate_blocks_are_null(self):
        r = judge.judge(make_daily(), code="000001", industry="保险")
        assert r["state_a"] is None
        assert r["window"] is None
        assert r["entry"] is None
        assert r["exit"] is None

    def test_no_price_in_indeterminate_text(self):
        r = judge.judge(make_daily(), code="000001", industry="证券")
        txt = r["required_statement"]["final_text"]
        assert "元" not in txt
        assert "参考价" not in txt


# ── 3. 话术拼装 ───────────────────────────────────
class TestStatementAssembly:
    def test_final_text_equals_units_joined(self):
        for md in (make_daily(), make_daily(seed=99)):
            r = judge.judge(md, code="600519")
            units = sorted(r["required_statement"]["units"], key=lambda u: u["seq"])
            assert r["required_statement"]["final_text"] == "".join(u["text"] for u in units)

    def test_units_sorted_by_seq(self):
        r = judge.judge(make_daily(), code="600519")
        seqs = [u["seq"] for u in r["required_statement"]["units"]]
        assert seqs == sorted(seqs)

    def test_fallback_present(self):
        r = judge.judge(make_daily(), code="600519")
        assert r["required_statement"]["fallback_unknown"]

    def test_human_judgement_notes_present(self):
        r = judge.judge(make_daily(), code="600519")
        assert len(r["human_judgement_required"]) >= 8


# ── 4. 口径常量栅栏 ───────────────────────────────
class TestCaliberConstants:
    def test_macd_params(self):
        assert (judge.FAST, judge.SLOW, judge.SIG) == (12, 26, 9)

    def test_sticky_ratio(self):
        assert judge.REL_STICKY_RATIO == 0.03

    def test_miss5(self):
        assert judge.MISS5_PCT == 0.05

    def test_new_months(self):
        assert judge.NEW_MONTHS == 30

    def test_dea_near_zero_frac(self):
        assert judge.DEA_NEAR_ZERO_FRAC == 1e-6


# ── 5. 窗口关闭不下发参考价（P0）───────────────────
class TestWindowClosedNoRefPrice:
    def test_closed_window_has_no_entry(self):
        """扫多个随机序列，凡窗口已关闭的输出，都不得带参考价。"""
        checked = 0
        for seed in range(12):
            r = judge.judge(make_daily(seed=seed), code="600519")
            w = r.get("window") or {}
            alive = (w.get("alive") or {}).get("is_alive")
            if w.get("event") not in (None, "none") and alive is False:
                checked += 1
                assert r["entry"] is None
                assert "参考价是" not in r["required_statement"]["final_text"]
        assert checked > 0, "样本里没有出现「窗口已关闭」的情形，本测试空转"

    def test_state_a_false_voids_ref_price(self):
        """状态A不成立时，不得下发参考价。"""
        checked = 0
        for seed in range(20):
            r = judge.judge(make_daily(seed=seed), code="600519")
            sa = r.get("state_a") or {}
            if sa.get("status") == "false":
                checked += 1
                assert r["entry"] is None
        assert checked > 0, "样本里没有状态A不成立的情形，本测试空转"


# ── 6. as_of 回放 ─────────────────────────────────
class TestAsOfReplay:
    def test_replay_declaration_first(self):
        d = make_daily()
        a = str(d["date"].iloc[-200].date())
        r = judge.judge(d, code="600519", as_of=a)
        units = r["required_statement"]["units"]
        if units and units[0]["code"] == "R-ASOF-REPLAY":
            assert units[0]["seq"] <= 3
        assert r["data_snapshot"]["as_of"] == a

    def test_truncation_no_future_rows(self):
        d = make_daily()
        a = d["date"].iloc[-500]
        r = judge.judge(d, code="600519", as_of=str(a.date()))
        assert r["data_snapshot"]["trade_date"] <= str(a.date())
        assert r["data_snapshot"]["data_start"] == str(d["date"].iloc[0].date())

    def test_replay_stale_flag(self):
        d = make_daily()
        r = judge.judge(d, code="600519", as_of="2020-06-30")
        assert r["data_snapshot"]["replay_lag_days"] > 0
        assert r["data_snapshot"]["replay_stale"] is True

    def test_no_as_of_means_not_replay(self):
        r = judge.judge(make_daily(), code="600519")
        assert r["data_snapshot"]["as_of"] is None
        assert r["data_snapshot"]["replay_stale"] is False
