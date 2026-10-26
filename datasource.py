#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HOLDLE 取数模块 datasource.py（data-skill 内置，2026-10-26）

════════════════════════════════════════════════════════════════
🔴 本文件的取数逻辑是**从知识库已验证实现机械移植**的，不是新写的。

   来源：knowledge-base/13_回测试验/AI脚本化/data_source.py
        （该文件由老板 2026-10-25 拍板、经过三步验证：
          K线三市场实时性 ✅ / 复权计算逐位一致（差 0.00000000）✅ / 财务口径 8/8 一致 ✅）

   移植原则（同 judge.py）：**只做机械替换，不改判定与取数逻辑**。
     ① 缓存根：工作区固定路径 → 用户目录（可用 HOLDLE_AI_CACHE 覆盖）
     ② print 调试输出 → 静默（用户端不该看到内部日志）

   本模块取代了 holdle_data.py 原先的 TickFlow / Baostock / 同花顺 / 东财 四条数据线，
   全产品**只保留这一套取数方式**（老板 2026-10-26 确认：不需要两套）。
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

def _cache_paths(symbol: str):
    safe = symbol.replace(".", "_")
    d = CACHE_ROOT / "daily"
    d.mkdir(parents=True, exist_ok=True)
    return d / f"{safe}.parquet", d / f"{safe}.meta.json", d / f"{safe}.factors.parquet"


def _read_cache(symbol: str):
    dp, mp, fp = _cache_paths(symbol)
    if not dp.exists() or not mp.exists():
        return None, None, None
    try:
        factors = pd.read_parquet(fp) if fp.exists() else None
        return pd.read_parquet(dp), json.loads(mp.read_text()), factors
    except Exception:
        return None, None, None


def _write_cache(symbol: str, df, meta: dict, factors=None):
    dp, mp, fp = _cache_paths(symbol)
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
                if d and f:
                    recs.append({"kind": kind, "date": pd.to_datetime(d), "factor": float(f)})
        except DataUnavailable:
            raise
        except Exception as e:
            raise DataUnavailable(f"新浪因子解析失败({kind}): {e}")
    factors = pd.DataFrame(recs) if recs else pd.DataFrame(columns=["kind", "date", "factor"])
    if factors.empty:
        raise DataUnavailable(f"新浪无复权因子: {code}")
    return daily, factors


def _fetch_yahoo(symbol: str) -> tuple[pd.DataFrame, pd.DataFrame]:
    market, vsym = _symbol_kind(symbol)
    # period1=0 表示取全历史（Yahoo range=max 会静默降采样，必须显式窗口）
    p1 = 0 if LIVE_DAILY_BARS <= 0 else int(
        (datetime.utcnow() - timedelta(days=int(LIVE_DAILY_BARS * 1.6))).timestamp())
    r = _req(YAHOO_CHART.format(symbol=vsym), {"period1": p1, "period2": 9999999999, "interval": "1d"})
    j = r.json()
    chart = j.get("chart", {})
    if chart.get("error"):
        raise DataUnavailable(f"Yahoo无此标的: {vsym}｜{chart['error']}")
    res = (chart.get("result") or [None])[0]
    if not res or "timestamp" not in res:
        raise DataUnavailable(f"Yahoo无数据: {vsym}")
    ts = res["timestamp"]
    q = res["indicators"]["quote"][0]
    off = res.get("meta", {}).get("gmtoffset", 0)
    # ⚠️ 2026-10-25：Yahoo 返回的时间戳含日内时分（如 17:30），
    # 直接用会导致与底库（纯日期）对齐失败 → 必须归一化为交易日零点
    dates = pd.to_datetime([t - off for t in ts], unit="s").normalize()
    daily = pd.DataFrame({
        "date": dates, "open": q.get("open"), "high": q.get("high"),
        "low": q.get("low"), "close": q.get("close"), "volume": q.get("volume"),
    })
    adj = (res["indicators"].get("adjclose") or [{}])[0].get("adjclose")
    if adj is not None:
        daily["adjclose"] = adj
    daily = daily.dropna(subset=["close"]).sort_values("date").reset_index(drop=True)

    # 因子：用 adjclose/close 反推逐日前复权因子（与底库 factors 结构一致）
    recs = []
    if "adjclose" in daily.columns:
        ratio = (daily["adjclose"] / daily["close"]).replace([float("inf"), -float("inf")], pd.NA)
        for d, v in zip(daily["date"], ratio):
            if pd.isna(v) or v <= 0:
                continue        # 负/零 adjclose 不采信（沿用 fetch_yahoo.py 规则）
            recs.append({"kind": "qfq", "date": d, "factor": 1.0 / float(v)})
    factors = pd.DataFrame(recs)
    return daily, factors


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
    bad = out["qfq_close"] <= 0
    if bad.any():
        out.loc[bad, ["qfq_close", "qfq_open", "qfq_high", "qfq_low"]] = float("nan")
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
