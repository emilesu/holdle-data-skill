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
    """合成日K（随机游走，工作日）。只用于结构测试，不用于口径验证。

    🔴 2026-10-26 修正：数据必须**截到真实的月末交易日**。
       原实现任意截止（如 2020-10-02），而日历判据正确地把月中截止当"未收完"
       → 12 个种子全部停在「本月尚未收完」分支，状态A/开窗/入场整条主路径走不到，
       测试虽"通过"却是空转（被 assert checked > 0 自检抓到）。
       现从 trading_calendar_months.json 里取真实末交易日作为截止日。
    """
    rng = np.random.default_rng(seed)
    dates = pd.bdate_range(start, periods=n)
    # 截到「日历里真实存在的月末交易日」，保证 month_closed 能成立
    try:
        import json as _json, os as _os
        _cal = _json.load(open(_os.path.join(_os.path.dirname(_os.path.abspath(judge.__file__)),
                                             "trading_calendar_months.json"), encoding="utf-8"))
        _cn = _cal["months"]["cn"]
        # 🔴 必须找「不晚于数据末日」的月末交易日；否则会截到未来日期（等于没截）
        _base = dates[-1].to_period("M")
        for _back in range(0, 8):
            _ym = str(_base - _back)
            _last = _cn.get(_ym)
            if _last and pd.Timestamp(_last) <= dates[-1]:
                dates = dates[dates <= pd.Timestamp(_last)]
                break
    except Exception:
        pass
    n = len(dates)
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
        a = str((d["date"] - pd.offsets.MonthEnd(0)).iloc[-200].date())
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


# ── 7. P0 修复的回归保护（2026-10-26 对抗性审查 H-A 要求）──────────
# 🔴 由来：审查员把 `_build_entry` 的 expire_reason 分支改回旧写法（强制「入场有效期已过」），
#    本测试文件当时**依然 24 全绿** —— 说明这条 P0 修复零回归保护，下次机械移植会原样复活。
#    这里的用例必须做到：把修复改回去，测试要红。
class TestP0RegressionGuards:
    def test_data_end_must_not_say_expired(self):
        """expire_reason == 'data_end'（数据到头、窗口仍存活）时，final_text 不得说「有效期已过」。"""
        from datetime import date
        # 构造一个价格先涨后横的序列，保证能开出窗口
        rng = np.random.default_rng(11)
        dates = pd.bdate_range("2023-01-02", "2025-12-31")
        n = len(dates)
        close = 10 * np.exp(np.cumsum(rng.normal(0.0006, 0.015, n)))
        # 末段横盘：制造"窗口还开着但没突破"的场景
        close[-40:] = close[-41]
        d = pd.DataFrame({"date": dates, "open": close, "high": close * 1.002,
                          "low": close * 0.998, "close": close, "volume": 1e5})
        r = judge.judge(d, code="TEST")
        en = r.get("entry") or {}
        if en.get("expire_reason") == "data_end":
            txt = r["required_statement"]["final_text"]
            assert "入场有效期已过" not in txt, "data_end 分支不得说「有效期已过」"
            assert ("入场等待继续有效" in txt) or ("窗口仍存活" in txt), "应说明窗口仍存活"
        else:
            pytest.skip("本次构造未命中 data_end 分支（expire_reason=%r）" % en.get("expire_reason"))

    def test_window_alive_never_says_expired(self):
        """窗口 is_alive=True 时，final_text 绝不能出现「入场有效期已过」。

        用仓库内的真实缓存行情（随机游走造不出稳定的「窗口存活」场景，
        synthetic 版本实测 20 个种子 0 命中 → 空转）。"""
        import os
        cand = []
        for f in ("300308", "NVDA"):
            p = os.path.join(ROOT, "holdle_data", "%s_日K_前复权.csv" % f)
            if os.path.exists(p):
                cand.append((f, p))
        if not cand:
            pytest.skip("仓库内无真实缓存行情，跳过")
        hit = 0
        for name, path in cand:
            d = pd.read_csv(path, parse_dates=["date"])[["date", "open", "high", "low", "close", "volume"]]
            for a in ("2026-04-30", "2026-05-29", "2025-12-31", None):
                r = judge.judge(d, code=name, market=("us" if name == "NVDA" else "a"),
                                **({"as_of": a} if a else {}))
                alive = ((r.get("window") or {}).get("alive") or {}).get("is_alive")
                txt = r["required_statement"]["final_text"]
                if alive:
                    hit += 1
                    assert "入场有效期已过" not in txt, \
                        "%s@%s 窗口存活却说有效期已过（自相矛盾）" % (name, a)
        assert hit > 0, "真实数据里也没有窗口存活的情形，本测试空转"

    def test_month_closed_calendar_spring_festival(self):
        """春节月末必须按真实交易日判定：2025-01-27 是 1 月最后一个交易日 → 已收完；
        2025-01-24（差一天）→ 未收完。这对用例正是日历驱动与工作日推断的分水岭。"""
        rng = np.random.default_rng(3)
        dates = pd.bdate_range("2015-01-05", "2025-01-31")
        close = 10 * np.exp(np.cumsum(rng.normal(0.0004, 0.018, len(dates))))
        base = pd.DataFrame({"date": dates, "open": close, "high": close * 1.01,
                             "low": close * 0.99, "close": close, "volume": 1e5})
        for anchor, expect in [("2025-01-27", True), ("2025-01-24", False)]:
            sub = base[base["date"] <= pd.Timestamp(anchor)]
            if len(sub) < 40:
                pytest.skip("构造数据未覆盖 %s" % anchor)
            m = judge._resample_monthly(sub)
            got = judge._month_closed(m, "a")[0]
            assert got is expect, "%s 判定期望 %s 实际 %s" % (anchor, expect, got)

    def test_month_closed_other_holiday_months(self):
        """国庆/清明等节假日月也要按真实交易日判定（工作日推断会判反）。"""
        rng = np.random.default_rng(5)
        dates = pd.bdate_range("2015-01-05", "2026-10-31")
        close = 10 * np.exp(np.cumsum(rng.normal(0.0004, 0.018, len(dates))))
        base = pd.DataFrame({"date": dates, "open": close, "high": close * 1.01,
                             "low": close * 0.99, "close": close, "volume": 1e5})
        for anchor, expect in [("2023-09-28", True), ("2023-09-27", False),
                               ("2020-01-23", True), ("2020-01-22", False)]:
            sub = base[base["date"] <= pd.Timestamp(anchor)]
            m = judge._resample_monthly(sub)
            got = judge._month_closed(m, "a")[0]
            assert got is expect, "%s 判定期望 %s 实际 %s" % (anchor, expect, got)

    def test_disclaimer_in_final_text(self):
        """免责段必须进 final_text（模型只转述 final_text）。"""
        r = judge.judge(make_daily(), code="TEST")
        assert "不构成任何投资建议" in r["required_statement"]["final_text"]

    def test_dirty_date_returns_structured(self):
        """非法日期 → 结构化 data_quality，不得抛异常。"""
        d = make_daily()
        d = d.copy(); d["date"] = d["date"].astype(object); d.loc[5, "date"] = "2019-13-45"
        r = judge.judge(d, code="TEST")
        assert r["status"] == "indeterminate"
        assert r["indeterminate_reason"] == "data_quality"

    def test_dirty_price_returns_structured(self):
        """非数字价格 → 结构化 data_quality，不得静默丢行后照常判。"""
        d = make_daily().copy()
        d["high"] = d["high"].astype(object); d.loc[10, "high"] = "abc"
        r = judge.judge(d, code="TEST")
        assert r["status"] == "indeterminate"
        assert r["indeterminate_reason"] == "data_quality"


# ── 8. 导师复签建议补的测试（2026-10-26）────────────────────────
class TestMentorRecheckSuggestions:
    def test_spring_festival_both_sides(self):
        """春节月边界两侧：2020-01-22（差一天）vs 2020-01-23（真实末日）。"""
        rng = np.random.default_rng(9)
        dates = pd.bdate_range("2015-01-05", "2021-12-31")
        close = 10 * np.exp(np.cumsum(rng.normal(0.0004, 0.018, len(dates))))
        base = pd.DataFrame({"date": dates, "open": close, "high": close * 1.01,
                             "low": close * 0.99, "close": close, "volume": 1e5})
        for anchor, expect in [("2020-01-23", True), ("2020-01-22", False)]:
            sub = base[base["date"] <= pd.Timestamp(anchor)]
            m = judge._resample_monthly(sub)
            got = judge._month_closed(m, "a")[0]
            assert got is expect, "%s 期望 %s 实际 %s" % (anchor, expect, got)

    def test_calendar_expired_reports_unavailable(self):
        """模拟日历过期：数据跨到日历覆盖范围之后 → 必须 calendar_unavailable。"""
        rng = np.random.default_rng(13)
        dates = pd.bdate_range("2024-01-02", "2027-06-30")   # 超出日历（到 2026-10）
        close = 10 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, len(dates))))
        d = pd.DataFrame({"date": dates, "open": close, "high": close * 1.01,
                          "low": close * 0.99, "close": close, "volume": 1e5})
        r = judge.judge(d, code="600519")
        assert r["status"] == "indeterminate"
        assert r["indeterminate_reason"] == "calendar_unavailable", \
            "日历过期必须明确报不可用，不得伪装成「本月未收完」，实际=%r" % r["indeterminate_reason"]
        assert r["state_a"] is None and r["window"] is None

    def test_finance_list_missing_not_silent(self, monkeypatch):
        """🔴 金融名单缺失时不得静默放行（F-1，与 C2 P0 同型）。"""
        monkeypatch.setattr(judge, "_FIN_PATH", "/nonexistent/finance_symbols.json")
        monkeypatch.setattr(judge, "_FIN_SET", None)
        monkeypatch.setattr(judge, "_FIN_OK", False)
        r = judge.judge(make_daily(), code="601318")
        assert r["status"] == "indeterminate"
        assert r["indeterminate_reason"] == "finance_unverified", \
            "名单缺失应报 finance_unverified（拒绝静默放行），实际=%r" % r["indeterminate_reason"]

    def test_no_awkward_phrase(self):
        """H-1 病句：「数据截至的 X 这个月」多余「的」字。"""
        rng = np.random.default_rng(17)
        dates = pd.bdate_range("2025-01-02", "2026-10-16")   # 末日月中
        close = 10 * np.exp(np.cumsum(rng.normal(0.0004, 0.015, len(dates))))
        d = pd.DataFrame({"date": dates, "open": close, "high": close * 1.01,
                          "low": close * 0.99, "close": close, "volume": 1e5})
        txt = judge.judge(d, code="600519")["required_statement"]["final_text"]
        assert "数据截至的 " not in txt, "病句仍在：%r" % txt[:60]

    def test_replay_no_present_tense_in_window(self):
        """H-2 回放时「该窗口目前仍存活」必须改成「当时」。"""
        import os
        p = os.path.join(ROOT, "holdle_data", "300308_日K_前复权.csv")
        if not os.path.exists(p):
            pytest.skip("无真实缓存行情")
        d = pd.read_csv(p, parse_dates=["date"])[["date", "open", "high", "low", "close", "volume"]]
        hit = 0
        for a in ("2026-04-30", "2026-05-29", "2025-12-31"):
            r = judge.judge(d, code="300308", as_of=a)
            alive = ((r.get("window") or {}).get("alive") or {}).get("is_alive")
            if alive:
                hit += 1
                txt = r["required_statement"]["final_text"]
                assert "目前仍存活" not in txt, "%s 回放仍用「目前」，应改「当时」" % a
        assert hit > 0, "无窗口存活场景，本测试空转"
