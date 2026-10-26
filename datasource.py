#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HOLDLE 取数模块 datasource.py（data-skill 内置，2026-10-26）

════════════════════════════════════════════════════════════════
🔴 本文件的取数逻辑是**从知识库已验证实现机械移植**的，不是新写的。

   来源：knowledge-base/13_回测试验/AI脚本化/data_source.py
        （该文件由老板 2026-10-25 拍板、经过三步验证：
          K线三市场实时性 ✅ / 复权计算逐位一致（差 0.00000000）✅ / 财务口径 8/8 一致 ✅）

   移植改动清单（**如实 5 条**，2026-10-26 代码审查指出原先只声明了 2 条、与事实不符）：
     ① 缓存根：工作区固定路径 → 用户目录（`~/.holdle_ai_cache`，可用 HOLDLE_AI_CACHE 覆盖）
        · 验证：取数后检查缓存文件是否落在该目录
     ② 调试 print 全部移除（用户端不该看到内部日志）
        · 验证：跑一次取数，终端除主流程输出外无额外行
     ③ ⚠️ **结构调整（非机械替换）**：复权因子 factors 的读写
        原：load_daily 内单独调用 pd.read_parquet / to_parquet
        现：_read_cache / _write_cache 一次性返回/接收三元组 (daily, meta, factors)
        · 理由：避免 load_daily 里对同一目录反复拼路径
        · 验证：三市场取数后，qfq_* 列与知识库逐位一致（实测差 0.000000000000）
     ④ ⚠️ **新增**：写缓存包 try/except（缓存写失败不影响本次取数结果）
        · 验证：缓存目录不可写时仍能取到数据
     ⑤ ⚠️ **新增**：缓存目录建不出来时降级为「不缓存、照常联网」
        （原实现会在任何网络请求前抛 PermissionError，直接崩溃）
        · 验证：HOLDLE_AI_CACHE 指向不可写路径，仍能取数

   ⚠️ 其中 ③④⑤ 不是"机械替换"。下次维护者改这里时，**别以为是安全的一字不改**，
      改完必须重跑「与知识库 data_source.py 的复权逐位比对」+ 三市场取数。

════════════════════════════════════════════════════════════════

取数方式（老板 2026-10-25 拍板，见 data_source.py 头部与日记）：
  A股   ：新浪 getKLineData 原始价 + 新浪 qfq/hfq 复权因子 → 复权价**用时现算**
  港/美股：Yahoo v8 chart 原始价 + adjclose 因子 → 复权价**用时现算**
  全量   ：LIVE_DAILY_BARS=0，拉满历史。
           🔴 任何截短都会改判定（MACD 的 EMA 是累积型，起点不同则整条序列不同）：
           实测跳过头 36 月 → 相对误差 13.6%；96 月才收敛到 0.01%。
           曾为提速截到 1500 根（22s→0.6s），结果**判定全错**。
           老板拍板：用户经 AI 工具异步调用，对 10+ 秒不敏感，**一律全量**。
  重试   ：RETRIES=2（含首发共 3 次），失败间隔递增；Yahoo 429 退避 60s
  降级   ：不设二级数据源；失败抛 DataUnavailable，由上层给出明确失败话术
           （**绝不返回陈旧数据顶替**——这正是原 TickFlow 那套的隐患）
  缓存   ：~/.holdle_ai_cache/daily/，TTL 6 小时；命中后 0.02s

用法：
    from datasource import load_daily, DataUnavailable
    daily, meta = load_daily("600519.SH", adjust="forward")
    # daily 列：date/open/high/low/close/volume/qfq_open/qfq_high/qfq_low/qfq_close
"""
from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ---------- 配置 ----------

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")

# 缓存目录：用户主目录（跨平台可写），可用 HOLDLE_AI_CACHE 覆盖
CACHE_ROOT = Path(os.environ.get("HOLDLE_AI_CACHE") or (Path.home() / ".holdle_ai_cache"))
CACHE_TTL_HOURS = 6
STALE_TRADING_DAYS = 2

# 🔴 全量（0 = 不截断）。见文件头「全量」一节，改这个值会改判定结果。
LIVE_DAILY_BARS = int(os.environ.get("HOLDLE_AI_BARS", "0"))

SINA_KLINE = "https://money.finance.sina.com.cn/quotes_service/api/json_v2.php/CN_MarketData.getKLineData"
SINA_FACTOR = "https://finance.sina.com.cn/realstock/company/{code}/{kind}.js"
YAHOO_CHART = "https://query1.finance.yahoo.com/v8/finance/chart/{symbol}"

TIMEOUT = 20
RETRIES = 2
RETRY_INTERVAL = 1.5


class DataUnavailable(Exception):
    """数据不可得。**调用方必须明确告知用户，不得用旧数据顶替。**"""


@dataclass
class FetchMeta:
    """数据快照元信息（映射到判定格式的 data_snapshot）"""
    symbol: str
    source: str
    fetched_at: str
    trade_date: str | None = None
    is_stale: bool = False
    stale_days: int = 0
    stale_trading_days: int = 0
    adjust: str = "forward"
    from_cache: bool = False
    is_partial: bool = False
    partial_note: str | None = None
    warnings: list = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "trade_date": self.trade_date,
            "fetched_at": self.fetched_at,
            "adjust": self.adjust,
            "source": self.source,
            "is_stale": self.is_stale,
            "stale_days": self.stale_days,
            "stale_trading_days": self.stale_trading_days,
            "from_cache": self.from_cache,
            "is_partial": self.is_partial,
            "partial_note": self.partial_note,
        }


# ---------- 缓存 ----------

# 缓存目录是否可用（建不出来 → 走纯网络路径，不崩）
_CACHE_USABLE = None


def _cache_paths(symbol: str):
    """→ 三个缓存文件路径；缓存目录不可用时返回 (None, None, None)。

    🔴 2026-10-26 代码审查 high：原实现在这里直接 `d.mkdir(parents=True)`，
       而它发生在**任何网络请求之前** → 家目录只读/磁盘满/被杀软锁/企业受限环境时，
       load_daily 直接抛 PermissionError（不是 DataUnavailable），整个取数崩溃。
       用户端环境参差（Windows 杀软、企业域控、容器只读挂载都很常见），
       正确行为是「缓存不可用 → 照常联网拉，只是不缓存」。
    """
    global _CACHE_USABLE
    if _CACHE_USABLE is False:
        return None, None, None
    safe = symbol.replace(".", "_")
    try:
        d = CACHE_ROOT / "daily"
        d.mkdir(parents=True, exist_ok=True)
        # 真正写一次探针，确认可写（mkdir 成功不等于可写）
        _probe = d / ".write_probe"
        _probe.write_text("ok", encoding="utf-8")
        _probe.unlink(missing_ok=True)
        _CACHE_USABLE = True
    except Exception:
        _CACHE_USABLE = False
        return None, None, None
    return d / f"{safe}.parquet", d / f"{safe}.meta.json", d / f"{safe}.factors.parquet"


def _read_cache(symbol: str):
    dp, mp, fp = _cache_paths(symbol)
    if dp is None or mp is None:
        return None, None, None          # 缓存不可用 → 走纯网络
    if not dp.exists() or not mp.exists():
        return None, None, None
    try:
        factors = pd.read_parquet(fp) if fp.exists() else None
        return pd.read_parquet(dp), json.loads(mp.read_text()), factors
    except Exception as e:      # noqa: BLE001
        # 🔴 2026-10-26 对抗性审查 M-2：缓存损坏原先完全静默（用户每次全量重拉，
        #    且"缓存坏了"这个事实被掩盖）。现在留一条告警到 stderr，不影响取数。
        import sys as _sys
        _sys.stderr.write("[holdle] 缓存损坏，已改用网络重新拉取：%s（%s）\n" % (symbol, str(e)[:80]))
        return None, None, None


def _write_cache(symbol: str, df, meta: dict, factors=None):
    dp, mp, fp = _cache_paths(symbol)
    if dp is None:
        return                            # 缓存不可用 → 静默跳过（不影响本次结果）
    df.to_parquet(dp, index=False)
    mp.write_text(json.dumps(meta, ensure_ascii=False, indent=1))
    if factors is not None and not factors.empty:
        factors.to_parquet(fp, index=False)


def _cache_fresh(meta) -> bool:
    if not meta:
        return False
    try:
        t = datetime.fromisoformat(meta["fetched_at"])
    except Exception:
        return False
    return datetime.now() - t < timedelta(hours=CACHE_TTL_HOURS)


# ══════════════════════════════════════════════════
#  以下：从 data_source.py 机械移植（勿手改逻辑）
# ══════════════════════════════════════════════════

def _req(url: str, params: dict | None = None) -> requests.Response:
    last = None
    for attempt in range(RETRIES + 1):
        try:
            r = requests.get(url, params=params, headers={"User-Agent": UA,
                             "Referer": "https://finance.sina.com.cn/"}, timeout=TIMEOUT)
            if r.status_code == 429:
                time.sleep(60)       # Yahoo 限流退避（沿用 fetch_yahoo.py 规则）
                continue
            r.raise_for_status()
            return r
        except Exception as e:
            last = e
            if attempt < RETRIES:
                time.sleep(RETRY_INTERVAL * (attempt + 1))
    raise DataUnavailable(f"请求失败: {url}｜{last}")


def _symbol_kind(symbol: str) -> tuple[str, str]:
    """→ (market, vendor_symbol)。symbol 形如 603288.SH / 000333.SZ / 920xxx.BJ / 0700.HK / NVDA"""
    if symbol.endswith(".HK"):
        code = symbol[:-3]
        # Yahoo 要求 4 位（00700→0700.HK）；底库文件名为 5 位（00700.HK）
        return "hk", f"{(code.lstrip('0') or '0').zfill(4)}.HK"
    if "." in symbol:
        code, suf = symbol.rsplit(".", 1)
        _p = {"SH": "sh", "SZ": "sz", "BJ": "bj"}.get(suf.upper())
        if _p:
            return "a", f"{_p}{code}"
        # 🔴 2026-10-25 修复：带点美股（BRK.B / BF.B）原被当成 A 股 → KeyError 'B' 崩溃。
        #    末段不是交易所后缀 → 按美股处理（Yahoo 用 BRK-B）。
        return "us", symbol.replace(".", "-")
    return "us", symbol.replace(".", "-")


def _fetch_a_share(symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """→ (原始日线, 因子表[hfq/qfq])。因子表结构对齐底库：columns=[kind,date,factor]"""
    _, code = _symbol_kind(symbol)
    # 日线：一次取满全历史（scale=240 日线，10000 根覆盖 1990 年上市）
    # datalen: 0(全量) → 10000（覆盖 1990 年上市最老股，老股约 8600 根）
    _bars = 10000 if LIVE_DAILY_BARS <= 0 else LIVE_DAILY_BARS
    r = _req(SINA_KLINE, {"symbol": code, "scale": 240, "ma": "no", "datalen": _bars})
    txt = r.text.strip()
    if not txt.startswith("["):
        raise DataUnavailable(f"新浪K线返回异常: {txt[:120]}")
    rows = json.loads(txt)
    if not rows:
        raise DataUnavailable(f"新浪无数据: {code}")
    daily = pd.DataFrame(rows)
    daily = daily.rename(columns={"day": "date"})
    for c in ("open", "high", "low", "close", "volume"):
        daily[c] = pd.to_numeric(daily[c], errors="coerce")
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.sort_values("date").reset_index(drop=True)

    # 因子：qfq / hfq 两个 js 文件，形如 var qfq={"data":[{"d":"2020-01-01","f":"1.23"},...]}
    recs = []
    for kind in ("qfq", "hfq"):
        try:
            rr = _req(SINA_FACTOR.format(code=code, kind=kind))
            body = rr.text
            i, j = body.find("{"), body.rfind("}")
            payload = json.loads(body[i:j + 1])
            for item in payload.get("data", []):
                d = item.get("d") or item.get("date")
                f = item.get("f") or item.get("factor")
                # 🔴 C-2 同源修复：字符串 "0" 是 truthy，会通过 `if d and f` → float("0")=0.0
                #    因此必须显式校验因子 > 0 且有限，非法值直接丢弃并计数。
                if d and f:
                    try:
                        _fv = float(f)
                    except (TypeError, ValueError):
                        continue
                    if _fv > 0 and np.isfinite(_fv):
                        recs.append({"kind": kind, "date": pd.to_datetime(d), "factor": _fv})
        except DataUnavailable:
            raise
        except Exception as e:
            raise DataUnavailable(f"新浪因子解析失败({kind}): {e}")
    factors = pd.DataFrame(recs) if recs else pd.DataFrame(columns=["kind", "date", "factor"])
    if factors.empty:
        raise DataUnavailable(f"新浪无复权因子: {code}")
    return daily, factors


def _fetch_yahoo(symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    """港/美股 Yahoo v8 chart。**任何解析异常统一转 DataUnavailable**。

    🔴 2026-10-26 对抗性审查 M-1：原实现遇到异常 JSON 形状（缺 indicators / quote 空 /
       gmtoffset 是字符串）会抛 KeyError / IndexError / TypeError 并穿透到最外层，
       用户看到的是 Python traceback 而不是「行情数据获取失败」。
    """
    market, vsym = _symbol_kind(symbol)
    # period1=0 表示取全历史（Yahoo range=max 会静默降采样，必须显式窗口）
    p1 = 0 if LIVE_DAILY_BARS <= 0 else int(
        (datetime.utcnow() - timedelta(days=int(LIVE_DAILY_BARS * 1.6))).timestamp())
    try:
        r = _req(YAHOO_CHART.format(symbol=vsym), {"period1": p1, "period2": 9999999999, "interval": "1d"})
        j = r.json()
        chart = j.get("chart", {})
        if chart.get("error"):
            raise DataUnavailable("Yahoo无此标的: %s｜%s" % (vsym, chart["error"]))
        res = (chart.get("result") or [None])[0]
        if not res or "timestamp" not in res:
            raise DataUnavailable("Yahoo无数据: %s" % vsym)
        ts = res["timestamp"]
        quote = (res.get("indicators") or {}).get("quote") or [{}]
        q = quote[0] or {}
        if not q.get("close"):
            raise DataUnavailable("Yahoo返回结构异常（无 quote/close）: %s" % vsym)
        # 🔴 2026-10-26 修复（自查 + 对抗性审查同时命中）：
        #    Yahoo 时间戳是 UTC 的「当地开盘时刻」：
        #      港股 01:30 UTC = 09:30 HKT（当天）→ UTC 日期即交易日 ✅
        #      美股 13:30 UTC = 09:30 ET（当天）→ 同样 ✅
        #    原实现 `t - off` 减 8 小时（港股）→ 退到前一天 17:30 → normalize 后**整体错位一天**。
        #    实测港股同日一致率仅 2.8%，「错一天配对」100%。美股因 offset 为负恰好没暴露。
        dates = pd.to_datetime(list(ts), unit="s").normalize()
        daily = pd.DataFrame({
            "date": dates, "open": q.get("open"), "high": q.get("high"),
            "low": q.get("low"), "close": q.get("close"), "volume": q.get("volume"),
        })
        adj = ((res.get("indicators") or {}).get("adjclose") or [{}])[0].get("adjclose")
        if adj is not None:
            daily["adjclose"] = adj
        daily = daily.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)

        # 因子：用 adjclose/close 反推逐日前复权因子
        recs = []
        if "adjclose" in daily.columns:
            ratio = (daily["adjclose"] / daily["close"]).replace([float("inf"), -float("inf")], pd.NA)
            for d, v in zip(daily["date"], ratio):
                if pd.isna(v) or v <= 0:
                    continue        # 负/零 adjclose 不采信（沿用 fetch_yahoo.py 规则）
                recs.append({"kind": "qfq", "date": d, "factor": 1.0 / float(v)})
        factors = pd.DataFrame(recs)
        return daily, factors
    except DataUnavailable:
        raise
    except Exception as e:      # noqa: BLE001 —— 任何解析异常都收敛成明确失败
        raise DataUnavailable("Yahoo 数据解析失败（%s）: %s" % (vsym, str(e)[:120]))


def _apply_guards(symbol: str, daily: pd.DataFrame) -> tuple[pd.DataFrame, list[str]]:
    """复刻 l1_engine.load_daily_qfq 的脏数据防护（2026-10-20/21 实跑踩出来的）。"""
    warns = []
    _is_us = "." not in symbol and symbol.isalpha() and symbol == symbol.upper()
    if symbol.endswith(".HK") or _is_us:
        before = len(daily)
        daily = daily[daily["volume"] > 0]
        dropped = before - len(daily)
        if dropped:
            warns.append(f"剔除 volume=0 行 {dropped} 条（港股占位价/停牌重述防护）")
    if _is_us and not daily.empty:
        mc = daily.groupby(daily["date"].dt.to_period("M"))["close"].last()
        ratio = mc / mc.shift(1)
        if (ratio > 20).any():
            warns.append("美股价格体系断裂（月对月跳变>20倍）→ 整票丢弃")
            return daily.iloc[0:0], warns
    return daily, warns


def _to_qfq(daily: pd.DataFrame, factors: pd.DataFrame, adjust: str = "forward") -> pd.DataFrame:
    """复权现算：前复权 qfq = close / qfq_factor（因子最新日归一=1）"""
    if daily.empty:
        return daily
    kind = "hfq" if adjust == "backward" else "qfq"
    if factors is None or factors.empty or not factors["kind"].eq(kind).any():
        # 🔴 2026-10-25 修复 C3（代码审查 critical）：
        # 原实现静默把原始价当复权价（不抛错、无 warning）——实测美的 158 个月里
        # 38 个月状态A翻转。复权因子缺失时必须**明确失败**，绝不能用原始价顶替。
        raise DataUnavailable(
            f"复权因子缺失（kind={kind}），无法计算前复权价；"
            f"不得以原始价代替（会导致状态A误判）")
    if daily is None or daily.empty:
        raise DataUnavailable("无日线数据")
    q = factors[factors["kind"] == kind][["date", "factor"]].copy()
    q["date"] = pd.to_datetime(q["date"])
    q = q.drop_duplicates("date").sort_values("date").reset_index(drop=True)
    dd = pd.DataFrame({"date": pd.to_datetime(daily["date"])})
    fac = pd.merge_asof(dd, q, on="date", direction="backward")["factor"]
    fac = pd.Series(fac.values, index=daily.index).ffill().bfill()
    out = daily.copy()
    for c in ("open", "high", "low", "close"):
        out[f"qfq_{c}"] = out[c] / fac
    # 🔴 2026-10-26 代码审查 C-2：因子本身可能是脏值（factor=0 → qfq=inf；factor=NaN → qfq=nan）。
    #    原实现只查 `qfq_close <= 0`，inf/nan 都能漏过 → 脏数据一路喂进判定，
    #    judge 仍给出「该股不处于状态A」这种**确定的错结论**（实测 98% 的行是 inf）。
    #    现在：出现非有限值/非正值一律**拒绝判定**，绝不带着脏数据往下走。
    # 🔴 2026-10-26 代码审查：原实现把两种完全不同的情况混成一类，导致误拒真实数据。
    #    · 原始价 <= 0（新浪停牌日占位价 0）→ **正常现象**，置 NaN 继续
    #      （与知识库 l1_engine 一致；实测 430047.BJ 有 5 行 close=0 的停牌日，
    #        原实现直接抛"疑似复权因子异常"，把一只正常股票拒了）
    #    · 因子本身 <= 0 或非有限 → **真脏**，必须拒绝判定
    _px = out["qfq_close"].to_numpy(dtype="float64")
    _raw = pd.to_numeric(daily["close"], errors="coerce").to_numpy(dtype="float64")
    _raw_bad = ~np.isfinite(_raw) | (_raw <= 0)          # 停牌/占位
    _fac_bad = (~np.isfinite(_px) | (_px <= 0)) & ~_raw_bad   # 原始价正常但复权后异常 → 因子问题
    if _fac_bad.any():
        raise DataUnavailable(
            "复权计算异常：%d/%d 行原始价正常但复权价非正或非有限（疑似复权因子异常），"
            "拒绝以脏数据判定。请重试；若持续出现请反馈给 HOLDLE。" % (int(_fac_bad.sum()), len(_px)))
    if _raw_bad.any():
        out.loc[_raw_bad, ["qfq_close", "qfq_open", "qfq_high", "qfq_low"]] = float("nan")
    return out


def _market_close_utc(market: str) -> tuple[int, int]:
    """各市场收盘时刻（UTC 小时, 分钟），用于判断最新一日是否已收盘。"""
    return {
        "a":  (7, 0),    # A股 15:00 CST = 07:00 UTC
        "hk": (8, 0),    # 港股 16:00 HKT(收盘竞价后) = 08:00 UTC
        "us": (20, 0),   # 美股 16:00 ET；冬令时 21:00 UTC、夏令时 20:00 UTC，取较保守的 20:00
    }.get(market, (23, 59))


def _is_partial_bar(market: str, trade_date: pd.Timestamp,
                   now_utc: datetime | None = None) -> tuple[bool, str | None]:
    """最新一根日K是否可能是盘中数据（老板 2026-10-25 拍板方案B：保留但标记）。

    判据：trade_date 是 UTC 今天或昨天，且 now_utc 早于该市场当日收盘时刻 → 盘中。
    A股/港股在本地收盘后抓取安全；美股因时差常在亚洲时段被抓到盘中快照
    （实测：底库 2026-10-16 23:19 抓 NVDA → 成交量 4,095 万，收盘 216.41；
     实际收盘后为 1.21 亿 / 214.13，确认是半日数据）。

    参数 now_utc 仅用于单测注入。
    """
    now = now_utc or datetime.utcnow()
    td = pd.Timestamp(trade_date).normalize()
    gap = (now.date() - td.date()).days
    if gap < 0 or gap > 1:
        return False, None                      # 数据是两天前或更早 → 早已收盘
    ch, cm = _market_close_utc(market)
    close_utc = now.replace(hour=ch, minute=cm, second=0, microsecond=0)
    if now < close_utc:
        return True, (f"最新一日 {td.strftime('%Y-%m-%d')} 可能为盘中数据"
                      f"（该市场尚未收盘），成交量和收盘价可能不完整")
    return False, None


def _stale_info(trade_date: pd.Timestamp, today: pd.Timestamp | None = None) -> tuple[bool, int, int]:
    """→ (is_stale, 自然日, 交易日)。

    - 交易日按工作日估算（未剔除节假日，偏保守：会把节假日算成交易日 → 更容易判 stale，
      这是安全方向：宁可提示"数据可能陈旧"，也不要漏提示）。
    - 数据日期晚于今天（时钟偏差/时区问题）时，一律按 0 天处理，不产生负值。
    """
    now = pd.Timestamp(today).normalize() if today is not None else pd.Timestamp.now().normalize()
    td = pd.Timestamp(trade_date).normalize()
    if td >= now:
        return False, 0, 0                      # 同一天或未来日期 → 不算陈旧
    days = int((now - td).days)
    tdays = max(int(pd.bdate_range(td, now).size) - 1, 0)
    return (tdays > STALE_TRADING_DAYS), days, tdays


# ══════════════════════════════════════════════════
#  🔴 验收口径（2026-10-26 导师终审 C-1，务必照此理解，别再误用）
#
#  「与底库逐位一致」的准确含义，以及它**不能**用来做什么：
#   · 成立的部分：同一交易日、同一原始价语料下，本模块的复权算法（原始价 ÷ 因子）
#     与知识库 l1_engine 数值完全一致 —— 这证明的是**算法移植保真**。
#   · 不成立的部分：底库是**某一时刻的静态快照**（止于某个交易日，且末日常为盘中数据）。
#     当数据商后来修订了历史收盘价时，现拉值会与底库不同（实测 NVDA 2026-10-16：
#     Yahoo 现拉 214.13 vs 底库 216.415，差 2.28）。这是数据修订，不是算法错。
#   · 因此：**不得再用「与底库逐位一致」当发布验收标准** ——
#     那等于把底库某个时点的快照当成金标准，与"用静态快照当依赖"是同一个毛病的另一面。
#     正确做法：判定以**用户此刻能拉到的数据**为准；差异只做记录，不做"谁对谁错"的判断。
#
#  已知的真实差异（记录，非待修）：
#   · 美股个别交易日数据商修订（如 NVDA 2026-10-16 差 2.28）→ 临界月份可能翻转结论，
#     但这类月份贴0轴比值往往已接近阈值，方法论上本就应谨慎（贴0轴 → 不可判）。
#
# ══════════════════════════════════════════════════
#  对外主入口（移植自 data_source.load_daily，缓存改为用户目录）
# ══════════════════════════════════════════════════

def load_daily(symbol: str, adjust: str = "forward", use_cache: bool = True):
    """拉取（或读缓存）日线 + 复权价。

    ⚠️ 失败时抛 DataUnavailable，**调用方必须据此明确告知用户，禁止用旧数据顶替**。
    返回 (daily_df, FetchMeta)；daily 列含 date/open/high/low/close/volume/qfq_*。
    """
    if adjust not in ("forward", "backward"):
        raise ValueError("adjust 只允许 forward/backward，收到 %s" % adjust)

    daily, meta_raw, factors = _read_cache(symbol) if use_cache else (None, None, None)
    from_cache = False
    if daily is not None and _cache_fresh(meta_raw):
        from_cache = True
        daily = daily.copy()
        source = meta_raw.get("source", "cache")
    else:
        market, _ = _symbol_kind(symbol)
        if market == "a":
            daily, factors = _fetch_a_share(symbol)
            source = "sina"
        else:
            daily, factors = _fetch_yahoo(symbol)
            source = "yahoo"
        daily, warns = _apply_guards(symbol, daily)
        if daily.empty:
            raise DataUnavailable("%s: 数据被防护规则整票丢弃（价格体系断裂）" % symbol)
        meta_tmp = {"fetched_at": datetime.now().isoformat(timespec="seconds"),
                    "source": source, "symbol": symbol}
        try:
            _write_cache(symbol, daily, meta_tmp, factors)
        except Exception:
            pass          # 缓存写失败不影响本次取数

    if daily is None or daily.empty:
        raise DataUnavailable("%s: 无日线数据" % symbol)

    out = _to_qfq(daily, factors, adjust)
    last = pd.Timestamp(out["date"].max())
    is_stale, days, tdays = _stale_info(last)
    market, _ = _symbol_kind(symbol)
    is_partial, partial_note = _is_partial_bar(market, last)

    meta = FetchMeta(
        symbol=symbol, source=source,
        fetched_at=datetime.now().isoformat(timespec="seconds"),
        trade_date=last.strftime("%Y-%m-%d"),
        is_stale=is_stale, stale_days=days, stale_trading_days=tdays,
        adjust=adjust, from_cache=from_cache,
        is_partial=is_partial, partial_note=partial_note,
    )
    return out, meta


def to_bars(daily: pd.DataFrame, freq: str = "M") -> pd.DataFrame:
    """把前复权日线重采样成月K/周K，并算 MACD（供展示 CSV 用）。

    freq: 'M' 月 / 'W' 周 / 'D' 日（原样）
    使用 qfq_* 列（前复权）——用户看到的 CSV 与判定用的是**同一份复权价**，
    这是「只有一套取数」的关键：不再出现"展示用一套、判定用另一套"。
    """
    d = daily.copy()
    d["date"] = pd.to_datetime(d["date"])
    for c in ("open", "high", "low", "close"):
        d[c] = d["qfq_" + c]
    d = d.dropna(subset=["close"]).sort_values("date")

    if freq == "D":
        bars = d[["date", "open", "high", "low", "close", "volume"]].reset_index(drop=True)
    else:
        rule = "ME" if freq == "M" else "W"
        d = d.set_index("date")
        bars = d.resample(rule).agg({"open": "first", "high": "max", "low": "min",
                                     "close": "last", "volume": "sum"}).dropna(subset=["close"])
        bars = bars.reset_index()

    dif, dea, hist = macd(bars["close"])
    bars["DIF"] = dif.round(2)
    bars["DEA"] = dea.round(2)
    bars["MACD柱"] = hist.round(2)
    bars["date"] = pd.to_datetime(bars["date"]).dt.strftime("%Y-%m-%d")
    return bars


def macd(close: pd.Series, fast: int = 12, slow: int = 26, sig: int = 9):
    """SA-02 标准公开公式（12/26/9 EMA 差分），hist 为国内惯例 ×2。"""
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    dif = ema_f - ema_s
    dea = dif.ewm(span=sig, adjust=False).mean()
    hist = (dif - dea) * 2
    return dif, dea, hist
