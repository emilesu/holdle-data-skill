#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""datasource.py 离线单测（不访问网络）。

只测「改坏了用户看不到、但结论是错的」那几类：
  1. 复权因子脏值必须拒绝判定（C-2，历史高危：实测 98% 行变 inf 仍给出确定结论）
  2. 复权因子缺失必须明确失败，绝不用原始价顶替（历史 P0：美的 158 个月里 38 个月翻转）
  3. 缓存目录不可用必须降级，不得崩溃
  4. 复权计算与知识库口径一致

跑法：cd holdle-data-skill && python -m pytest tests/ -q
"""
import pathlib
import sys

import numpy as np
import pandas as pd
import pytest

ROOT = pathlib.Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
ds = pytest.importorskip("datasource")


def _daily(n=100, start="2020-01-01"):
    dates = pd.bdate_range(start, periods=n)
    return pd.DataFrame({"date": dates, "open": 10.0, "high": 11.0,
                         "low": 9.0, "close": 10.0, "volume": 1e5})


def _factors(d, value=1.0):
    return pd.DataFrame({"kind": "qfq", "date": d["date"], "factor": value})


class TestFactorDirtyValues:
    """C-2：因子脏值必须拒绝，不得带着脏数据给出确定结论。"""

    def test_zero_factor_rejected(self):
        d = _daily()
        f = _factors(d)
        f.loc[50:, "factor"] = 0.0          # factor=0 → qfq = inf
        with pytest.raises(ds.DataUnavailable):
            ds._to_qfq(d, f, "forward")

    def test_nan_factor_falls_back_to_last_valid(self):
        """NaN 因子 = 该日无因子数据 → merge_asof 沿用前一有效因子（不是脏值，不该拒判）。

        ⚠️ 与 factor=0 区别：0 是**明确的错误值**（qfq=inf，必须拒判）；
           NaN 是**缺测**（沿用前值，是正确行为）。两者不能混为一谈。
        """
        d = _daily()
        f = _factors(d, 2.0)
        f.loc[30:, "factor"] = np.nan
        out = ds._to_qfq(d, f, "forward")
        assert np.isfinite(out["qfq_close"]).all(), "NaN 因子不该产生 nan 价格"
        assert np.allclose(out["qfq_close"], d["close"] / 2.0), "应沿用前一有效因子"

    def test_negative_factor_rejected(self):
        d = _daily()
        f = _factors(d)
        f.loc[10:, "factor"] = -1.0
        with pytest.raises(ds.DataUnavailable):
            ds._to_qfq(d, f, "forward")

    def test_normal_factor_passes(self):
        """反向断言：正常因子不得被误拦（防止"为了通过测试无条件报错"）。"""
        d = _daily()
        out = ds._to_qfq(d, _factors(d, 2.0), "forward")
        assert len(out) == len(d)
        assert np.isfinite(out["qfq_close"]).all()


class TestFactorMissing:
    """历史 P0：因子缺失时静默用原始价 → 状态A 大面积翻转。"""

    def test_empty_factors_raises(self):
        d = _daily()
        with pytest.raises(ds.DataUnavailable):
            ds._to_qfq(d, pd.DataFrame(columns=["kind", "date", "factor"]), "forward")

    def test_wrong_kind_raises(self):
        d = _daily()
        f = _factors(d)
        f["kind"] = "hfq"                    # 只有后复权因子，却要前复权
        with pytest.raises(ds.DataUnavailable):
            ds._to_qfq(d, f, "forward")


class TestCacheDegrade:
    """缓存不可用必须降级，不得崩溃（用户家目录可能只读/被杀软锁）。"""

    def test_cache_paths_returns_none_when_unwritable(self, monkeypatch, tmp_path):
        bad = tmp_path / "readonly"
        bad.mkdir()
        bad.chmod(0o500)                     # 只读
        monkeypatch.setattr(ds, "CACHE_ROOT", bad)
        monkeypatch.setattr(ds, "_CACHE_USABLE", None)
        paths = ds._cache_paths("600519.SH")
        assert paths == (None, None, None), "不可写时应返回全 None 而非抛异常"
        bad.chmod(0o700)

    def test_read_write_cache_handle_none_paths(self, monkeypatch, tmp_path):
        bad = tmp_path / "ro2"
        bad.mkdir()
        bad.chmod(0o500)
        monkeypatch.setattr(ds, "CACHE_ROOT", bad)
        monkeypatch.setattr(ds, "_CACHE_USABLE", False)
        assert ds._read_cache("600519.SH") == (None, None, None)
        ds._write_cache("600519.SH", _daily(), {"fetched_at": "x"})   # 不得抛异常
        bad.chmod(0o700)


class TestQfqCorrectness:
    """复权计算：前复权 = 原始价 / 因子，且因子缺失日沿用前一有效值。"""

    def test_simple_division(self):
        d = _daily(5)
        f = _factors(d, 2.0)
        out = ds._to_qfq(d, f, "forward")
        assert np.allclose(out["qfq_close"], d["close"] / 2.0)

    def test_forward_fill_between_factor_dates(self):
        """因子只在月初给出，中间日期应沿用前一因子（merge_asof backward）。"""
        d = _daily(60)
        f = pd.DataFrame({"kind": "qfq",
                          "date": [d["date"].iloc[0], d["date"].iloc[30]],
                          "factor": [1.0, 2.0]})
        out = ds._to_qfq(d, f, "forward")
        # 前半段因子 1.0，后半段 2.0
        assert abs(out["qfq_close"].iloc[10] - 10.0) < 1e-9
        assert abs(out["qfq_close"].iloc[40] - 5.0) < 1e-9
