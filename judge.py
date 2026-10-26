#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""HOLDLE 判定模块 judge.py（data-skill 内置，2026-10-26）

把「状态A / 开窗 / 入场参考价 / 突破判定」从「模型看 K 线自己算」改成**脚本确定性计算**。
模型拿到本模块产出的 judgment.json 后**只能转述**，不得自算任何数字。

════════════════════════════════════════════════════════════════
🔴 本文件的核心函数是**从知识库已验证实现机械移植**的，不是重写的。

   detect_windows   ← 知识库 13_回测试验/scripts/l1_engine.py（回测同源引擎）
   _build_window    ← 知识库 13_回测试验/AI脚本化/judge_service.py（导师三轮裁决 + 独立验收）
   _build_entry     ← 同上

   移植时只做两处**机械替换**，未改动任何判定逻辑：
     ① qfq_close/qfq_high/qfq_low/qfq_open → close/high/low/open
        （data-skill 传入的就是 --adjust forward 前复权后的价格，与底库 qfq_* 列等价）
     ② from l1_engine import macd → 本模块同式 macd()

   ⚠️ 教训（2026-10-26 踩过）：第一版照着文档自己重写，结果
      茅台的开窗事件判错、600743 参考价 2.08 vs 正确 2.47（差 16%）。
      根因：漏了三条隐藏逻辑（「矮的三选一前提」、hard_gate 硬闸、12 个月久远过滤），
      且把 E-02「区间最高价」误解成「绿柱日最低价」。
      **判定这种东西必须移植，不许重写。**
════════════════════════════════════════════════════════════════

🔴 另两条铁律：
  2. **必须在未取整的原始 DIF/DEA/柱 上计算**。脚本写 CSV 时 round(2)，
     而贴0轴判据是 |柱|/|DEA| < 0.03 —— 实测某标的 167 个月里有 4 个月落在临界带，
     读回 CSV 再算会翻转判定。所以本模块吃内存里的原始序列，不吃 CSV。
  3. **不可判就明说**：本月未收完 / 双线贴轴 / 次新股 / 金融类 → 不判，不猜。

数据来源：用户本机 holdle_data.py 拉好的 DataFrame（不由服务端拉行情）。
"""
from __future__ import annotations

import json
from datetime import datetime

import numpy as np
import pandas as pd

SCHEMA_VERSION = "0.4"
CALIBER_VERSION = "v1.10.1"      # 01_口径映射表 v1.10.1

# ── 判定常量（出处：03_投资体系/02_状态A判断体系 v2.18 + 映射表 v1.10.1）──
FAST, SLOW, SIG = 12, 26, 9      # SA-02 不可调参
REL_STICKY_RATIO = 0.03          # SA-03 贴0轴主口径：|柱|/|DEA| < 0.03（2026-10-18 拍板）
STICKY_BAND = 0.5                # abs 口径档（敏感性对照，不用于实际判定）
DEA_NEAR_ZERO_FRAC = 1e-6        # 双线贴轴：|DEA| < 1e-6 × 收盘价 → 不可判（N2 终审）
DEA_NEAR_ZERO_ABS_FLOOR = 1e-12
NEW_MONTHS = 30                  # SA-04 次新股前 30 个月不参与
MISS5_PCT = 0.05                 # E-06：开盘价高于参考价 5% 放弃
RECENT_MONTHS = 12               # 太久远的已关闭窗口不作为当前事件

FINANCE_KEYWORDS = (
    "银行", "保险", "证券", "多元金融", "其他金融", "金融服务", "消费金融",
    "资本市场", "资产管理", "金融数据与交易所", "金融综合集团", "信用服务",
    "抵押金融", "抵押REIT", "专业保险", "保险经纪", "再保险", "地区银行",
    "多元化银行", "寿险", "综合保险", "财险",
)

HUMAN_JUDGEMENT_NOTES = [
    "综合判断原则（企业质量门槛不是逐项闸门，单项稍差+整体无碍→通过）",
    "ROE 失真三步（派现率≥100%/净资产不累积/一次性减权益 → 高分红调制值降权）",
    "异常高三步（ROE≥35% 先查成因）",
    "ROE 10~15% 强理由出口（仅限研究/复盘层，≠ 可以参与）",
    "行情段自查（AI 无用户账户数据，须以自查问句向用户提问，不可假设持仓）",
    "方式 a/b 标签（歧义#7 待裁，可输出但仅作注记，非判定结论）",
    "月K上升趋势判据（低点+高点均逐月抬高；非参与硬门槛，仅关注名单方向审查）",
    "三阶止损 / 周K低点出场（锚入场价，须依赖用户真实成本与持仓状态，脚本不判）",
]


# ══════════════════════════════════════════════════
#  底层指标（与 l1_engine 同式）
# ══════════════════════════════════════════════════

def macd(close: pd.Series, fast=FAST, slow=SLOW, sig=SIG):
    """SA-02 标准公开公式：EMA 差分。返回 (dif, dea, hist)，hist 为国内惯例 ×2。"""
    ema_f = close.ewm(span=fast, adjust=False).mean()
    ema_s = close.ewm(span=slow, adjust=False).mean()
    dif = ema_f - ema_s
    dea = dif.ewm(span=sig, adjust=False).mean()
    hist = (dif - dea) * 2
    return dif, dea, hist


def _dea_is_near_zero(dea, close) -> bool:
    """双线贴轴判定（相对判据，2026-10-25 N2 终审）。"""
    if dea is None or pd.isna(dea):
        return True
    d = abs(float(dea))
    if d == 0.0:
        return True
    base = DEA_NEAR_ZERO_ABS_FLOOR
    if close is not None and not pd.isna(close):
        base = max(abs(float(close)), DEA_NEAR_ZERO_ABS_FLOOR)
    return d < DEA_NEAR_ZERO_FRAC * base


def _is_sticky(mode, hist, dea, close, rel_ratio=REL_STICKY_RATIO):
    """贴0轴三态：True=贴0轴(归绿) / False=不贴 / None=不可判(双线贴轴)。"""
    if hist is None or pd.isna(hist):
        return False
    if mode == "rel":
        if _dea_is_near_zero(dea, close):
            return None
        return abs(float(hist)) / abs(float(dea)) < rel_ratio
    thr = STICKY_BAND
    if mode == "d06" and close is not None and not pd.isna(close):
        thr = STICKY_BAND * min(1.0, float(close))
    return abs(float(hist)) < thr


def _r(v, n=4):
    try:
        if v is None or pd.isna(v):
            return None
        return round(float(v), n)
    except Exception:
        return None


def _unit(seq, code, text):
    return {"seq": seq, "code": code, "text": text}


def judge_state_a(monthly):
    """SA-01：双线≥0 且 柱>0（与 l1_engine.judge_state_a 同式）。"""
    dif, dea, hist = macd(monthly["close"])
    monthly = monthly.copy()
    monthly["dif"], monthly["dea"], monthly["hist"] = dif, dea, hist
    monthly["state_a"] = (dif >= 0) & (dea >= 0) & (hist > 0)
    monthly["dir_up"] = hist.diff() > 0
    return monthly


def _resample_monthly(daily):
    """D-02：月K重采样，**保留原始精度**（不 round）。只保留月末已收盘行；补齐日历月。

    与 l1_engine.resample_monthly 口径一致：停牌月 close 前值填充保证 MACD 在自然月序连续；
    traded=False 的月不产生开窗事件。
    """
    m = daily.copy()
    m["date"] = pd.to_datetime(m["date"])
    m["ym"] = m["date"].dt.to_period("M")
    g = m.groupby("ym")
    monthly = pd.DataFrame({
        "month_end": g["date"].max(),
        "open": g["close"].first(),      # D-05 现行口径：首日**收盘**
        "high": g["high"].max(),
        "low": g["low"].min(),
        "close": g["close"].last(),
        "n_days": g["date"].count(),
    })
    monthly["traded"] = True
    full = pd.period_range(monthly.index.min(), monthly.index.max(), freq="M")
    # 🔴 重排索引时直接给定 fill_value，避免 reindex 产生 object 列后再 fillna
    #    （pandas 3.x 会对此告警并改变行为）
    monthly = monthly.reindex(full, fill_value=float("nan"))
    monthly.index.name = "ym"
    monthly["traded"] = monthly["traded"].notna()          # 无交易月 = NaN → False
    monthly["n_days"] = monthly["n_days"].fillna(0).round().astype("int64")
    monthly["close"] = monthly["close"].ffill()            # 停牌月前值填充保 MACD 连续
    return monthly.reset_index()


# ══ 以下三段：从知识库已验证实现机械移植（勿手改）══
def detect_windows(monthly: pd.DataFrame, sticky_mode: str = "rel",
                   rel_ratio: float = REL_STICKY_RATIO,
                   close_rule: str = "base") -> list[dict]:
    """W 族开窗判定 v2（锚点修正后的正确语义，映射表 v1.2）。

    核心语义（源自 导师判断_2016续命窗口复核.md 逐句对齐）：
    - 状态A 是背景态（SA-01）。开窗事件发生在状态A内。
    - **开窗（矮转高）= 前月柱 → 本月柱转高**（局部比较），且不贴0轴（rel：|柱|/|DEA|≥0.03，W-03）、柱≤DEA（W-01 硬闸）。
      ⚠️ 2026-10-18 红队实测：|柱|<0.5 绝对阈值与复权尺度强耦合（见 reviews/发现_贴0轴阈值尺度不一致_20261018.md）。
      绿转红（柱<0→>0）是矮转高的特例（前月柱≤0）。
      注：sticky_mode='rel' 是 2026-10-18 老板拍板的主口径，2026-10-25 起为**函数默认值**；
          'abs' 自 2026-10-25 降为敏感性对照档（与复权尺度耦合，不用于实际判定）。
      注：双线贴轴（|DEA|≈0）时 _is_sticky 返回 None（不可判）→ 不开窗（状态A v2.18）。
    - **一个窗口 = 一次开窗事件**，基准柱 = 开窗月柱。**同一时刻只允许一个存活窗**：
      新开窗前提 = 前窗已关闭（原文 §4.1 2026-09-14 三条件之一）。前窗未关时的矮转高
      记为 `window_suppressed`（诊断用），不产生事件。
    - 关闭通道（任一命中）：
      C1 月K高转矮：默认 close_rule="base" → 本月柱 < 本窗基准柱（v2.11 基准月=开窗月，封版口径）；
         close_rule="adjacent"        → 本月柱 < 上月柱（含停牌月的前值填充柱）——**口径变更对照档，非拍板口径**
         close_rule="adjacent_traded" → 本月柱 < 上一个"有交易月"的柱（停牌月不参与比较）——同对照档
      C2 状态A失效：`not state_a`（双线任一<0 或 柱≤0 —— SA-01 三条件任一不满足）
      （注：原文 §7.2「50-60 天不突破→入场失效」按老板 2026-10-18 拍板视为上述两条的通俗表述，不单独实现）
      C3 日K突破超期（§7.2 50-60天）：日K层判定，月K层接口预留（本函数只出月K事件）
    - 方式a/b：状态A形成方式（双线先上 vs 柱先上0轴），见映射表歧义#7（语义待导师澄清，
      当前按"启动段内柱先上0轴=b"注记，不作为事件流判据）。
    """
    events = []
    windows = []            # 存活窗口（单窗语义：[{open_month, base_hist}]）
    hist_above0_seen = False
    dif_above0_seen = False
    prev_h = None           # 前月柱（矮转高的局部比较基准）
    last_closed_pos = None  # 上次关窗的月序（判"续命"标签）
    traded_ok = monthly["traded"].values if "traded" in monthly.columns else np.ones(len(monthly), dtype=bool)
    if close_rule not in ("base", "adjacent", "adjacent_traded"):
        raise ValueError(f"未知 close_rule={close_rule}（可选 base / adjacent / adjacent_traded）")
    prev_traded_h = None    # 上一个"有交易月"的柱（close_rule=adjacent_traded 用）

    for i in range(len(monthly)):
        row = monthly.iloc[i]
        h, dif, dea = row["hist"], row["dif"], row["dea"]
        month = str(row["ym"])
        if pd.isna(h):
            continue

        # --- 停牌月：整月跳过判定（无交易 → 不产生事件、不关窗；MACD 用前值填充保持连续）---
        if not bool(traded_ok[i]):
            prev_h = h
            continue

        # --- C2 状态A失效：SA-01 三条件任一不满足 → 全部存活窗关闭 ---
        if not bool(row["state_a"]):
            for w_ in windows:
                events.append({"month": month, "type": "window_closed",
                               "reason": "状态A失效", "hist": float(h),
                               "open_month": w_["open_month"]})
            windows = []

        # --- C1 逐窗检查高转矮：比较基准随 close_rule 变化（默认 base=开窗月柱）---
        if close_rule == "base":
            c1_ref = None                      # 每窗各用自己的 base_hist
        elif close_rule == "adjacent":
            c1_ref = prev_h                    # 上月柱（含停牌月的前值填充值）
        else:
            c1_ref = prev_traded_h             # 上一个有交易月的柱
        c1_on = (c1_ref is not None) if close_rule != "base" else True
        still = []
        for w_ in windows:
            ref = w_["base_hist"] if close_rule == "base" else c1_ref
            if c1_on and ref is not None and h < ref:
                events.append({"month": month, "type": "window_closed",
                               "reason": "月K高转矮", "hist": float(h),
                               "open_month": w_["open_month"]})
            else:
                still.append(w_)
        if len(still) < len(windows):
            last_closed_pos = i
        windows = still

        prev_traded_h = h     # 本月已判定完，作为下一个交易月的"上月柱"

        # --- 开窗判定（W-01 矮转高，v2 语义）---
        # "矮"前提三选一：①前月绿柱（≤0）②前月贴0轴归绿（rel：|柱|/|DEA|<0.03）③前月红柱内回落（prev<prev_prev）
        prev_prev = monthly.iloc[i - 2]["hist"] if i >= 2 else None
        prev_dea = monthly.iloc[i - 1]["dea"] if i >= 1 else None
        prev_close = monthly.iloc[i - 1]["close"] if i >= 1 else None
        prev_is_low = (
            (prev_h is not None and prev_h <= 0)
            or (prev_h is not None and _is_sticky(sticky_mode, prev_h, prev_dea, prev_close, rel_ratio) is True)
            or (prev_prev is not None and not pd.isna(prev_prev) and prev_h < prev_prev)
        )
        # 2026-10-25：本月柱贴0轴判定改为三态，None（双线贴轴）＝不可判 → 不开窗
        cur_sticky = _is_sticky(sticky_mode, h, dea, row["close"], rel_ratio)
        if (row["state_a"] and prev_h is not None and prev_is_low
                and h > prev_h and h > 0
                and cur_sticky is False):
            hard_gate = h <= dea
            mode = "b" if hist_above0_seen and not dif_above0_seen else "a"
            if windows:
                # 原文 §4.1（2026-09-14）：前窗未关闭 → 不产生新窗口
                events.append({"month": month, "type": "window_suppressed",
                               "reason": "前窗未关闭", "hist": h,
                               "prev_hist": prev_h, "dif": dif, "dea": dea,
                               "open_month": windows[0]["open_month"]})
            elif hard_gate:
                events.append({"month": month, "type": "window_opened",
                               "mode": mode, "hist": float(h),
                               "prev_hist": float(prev_h),
                               "dif": float(dif), "dea": float(dea),
                               "reason": None})
                windows.append({"open_month": month, "base_hist": h})
            else:
                events.append({"month": month, "type": "gate_blocked",
                               "mode": mode, "hist": float(h),
                               "prev_hist": float(prev_h),
                               "dif": float(dif), "dea": float(dea),
                               "reason": "柱>DEA不开窗"})

        # 方式追踪 + 状态A失效段重置
        if h > 0:
            hist_above0_seen = True
        if dif > 0 and dea > 0:
            dif_above0_seen = True
        if not row["state_a"]:
            hist_above0_seen = False
            dif_above0_seen = False

        prev_h = h

    return events


def _build_window(events: list, monthly_all: pd.DataFrame, judge_from: pd.Timestamp) -> dict:
    judge_lo = pd.Period(judge_from, "M")
    opened = [e for e in events if e["type"] == "window_opened"
              and pd.Period(e["month"], "M") >= judge_lo]
    closed = [e for e in events if e["type"] == "window_closed"
              and pd.Period(e["month"], "M") >= judge_lo]
    suppressed = [e for e in events if e["type"] == "window_suppressed"
                  and pd.Period(e["month"], "M") >= judge_lo]
    if not opened:
        return {"event": "none", "event_month": None, "reasons": [], "gate": None,
                "alive": {"is_alive": False, "alive_until": None,
                          "close_month": None, "close_reason": None},
                "suppressed": []}
    # 🔴 只认「近期有效」事件：最新开窗若早已关闭且距今超过 R, 对用户无意义，
    #    不应作为"当前事件"呈现（例：海天最新开窗 2020-12 已关闭，距今 5 年+）。
    #    规则：最新开窗未关闭 → 有效；已关闭但关闭月距最新月 ≤ RECENT_MONTHS → 仍报出并标已关闭。
    RECENT_MONTHS = 12
    last = opened[-1]
    close_of_last = next((c for c in closed
                          if pd.Period(c["month"], "M") > pd.Period(last["month"], "M")), None)
    is_alive = close_of_last is None
    now_m = pd.Period(str(monthly_all.iloc[-1]["ym"]), "M")
    if not is_alive:
        closed_at = pd.Period(close_of_last["month"], "M")
        if (now_m - closed_at).n > RECENT_MONTHS:
            # 太久远的历史事件，不作为当前事件输出
            return {"event": "none", "event_month": None, "reasons": [], "gate": None,
                    "alive": {"is_alive": False, "alive_until": None,
                              "close_month": None, "close_reason": None},
                    "suppressed": [],
                    "stale_history": {"event_month": last["month"],
                                      "closed_at": close_of_last["month"],
                                      "note": f"最近一次开窗为 {last['month']}，已于 {close_of_last['month']} 关闭，"
                                              f"距今超过 {RECENT_MONTHS} 个月，不作为当前事件"}}
    return {
        "event": "lv_zhuan_hong" if (last.get("prev_hist") is not None and last["prev_hist"] <= 0)
                 else "ai_to_gao",
        "event_month": last["month"],
        "reasons": ["prev_hist_lt_0"] if (last.get("prev_hist") is not None and last["prev_hist"] <= 0)
                   else ["hist_turn_up"],
        # 🔴 2026-10-25：只下发布尔结论，**不下发 hist/dea 绝对值**
        #    （v2.18 刚把绝对量思维消灭，不能在输出层又放回来）
        # 只给布尔 + 原因（不给绝对值；原因用于让模型能解释而非编造）
        "gate": {"pass": bool(last.get("hist") is not None and last.get("dea") is not None
                              and last["hist"] <= last["dea"]),
                 "blocked_reason": None if (last.get("hist") is not None and last.get("dea") is not None
                                            and last["hist"] <= last["dea"]) else "hist>dea"},
        "alive": {"is_alive": is_alive,
                  "alive_until": None if is_alive else close_of_last["month"],
                  "close_month": None if is_alive else close_of_last["month"],
                  "close_reason": None if is_alive else close_of_last.get("reason")},
        "suppressed": [{"month": s["month"], "reason": s.get("reason")} for s in suppressed[-5:]],
    }



def _build_entry(events: list, daily: pd.DataFrame, monthly_all: pd.DataFrame,
                 judge_from: pd.Timestamp) -> dict | None:
    """按 l2_trade_sim.find_entry 的口径实现（复用同一套方法论条款）。

    E-01 切日K月=开窗次月首交易日 → 首个日K绿柱日（顺延）
    E-02 参考价 = max(high[开窗月首日 .. 绿柱日])（闭区间，不看柱色）
    E-03 突破 = 绿柱日后首个 盘中 high > 参考价
    E-06 开盘价 > 参考价×1.05 → 不追，放弃
    D-08 一字板（high==low）不可成交，顺延

    🔴 突破结论由本函数算好，**模型不得自己比较 high 与 ref_price**（判定格式 v0.3 §5）。
    """
    judge_lo = pd.Period(judge_from, "M")
    opened = [e for e in events if e["type"] == "window_opened"
              and pd.Period(e["month"], "M") >= judge_lo]
    if not opened:
        return None
    ev = opened[-1]
    ym = ev["month"]

    # 🔴 2026-10-25 修复：入场判定必须与开窗判定用**同一套事件过滤**，
    # 否则会出现「开窗=none 但入场=entered」的矛盾（海天实测：入场用的是 5 年前的老事件）。
    # 规则与 _build_window 一致：已关闭且超过 RECENT_MONTHS 的历史事件不作为当前事件。
    RECENT_MONTHS = 12
    closed_after = [e for e in events if e["type"] == "window_closed"
                    and pd.Period(e["month"], "M") > pd.Period(ym, "M")]
    now_m = pd.Period(str(monthly_all.iloc[-1]["ym"]), "M")
    if closed_after:
        closed_at = pd.Period(closed_after[0]["month"], "M")
        if (now_m - closed_at).n > RECENT_MONTHS:
            return None          # 老事件，与 _build_window 保持一致 → 无当前入场

    # 日K柱（12/26/9，与月K同参）
    # macd 用本模块同名函数（data-skill 不依赖 l1_engine）
    d = daily.dropna(subset=["close"]).sort_values("date").reset_index(drop=True).copy()
    if d.empty:
        return None
    _, _, hist_d = macd(d["close"])
    d["hist_d"] = hist_d

    start_day = d[d["date"].dt.to_period("M") == pd.Period(ym, "M")]["date"].min()
    if pd.isna(start_day):
        return {"status": "no_green", "reason": f"开窗月 {ym} 无日K数据"}
    month_end = pd.Period(ym, "M").end_time.normalize()

    # 窗口存活上限：该开窗之后的关窗月
    closed = [e for e in events if e["type"] == "window_closed"
              and pd.Period(e["month"], "M") > pd.Period(ym, "M")]
    if closed:
        close_month = closed[0]["month"]
        alive_until = min(pd.Period(close_month, "M").end_time.normalize(), d["date"].max())
    else:
        close_month = None
        alive_until = d["date"].max()

    after = d[d["date"] > month_end]
    if after.empty:
        return {"status": "no_green", "reason": "数据末端在开窗月内，无切日K月"}
    scan_from = after["date"].iloc[0]

    g = d[(d["date"] >= scan_from) & (d["date"] <= alive_until) & (d["hist_d"] < 0)]
    if g.empty:
        return {"status": "no_green", "reason": "窗口存活期内无日K绿柱",
                "switch_month": scan_from.strftime("%Y-%m"), "ref_price": None,
                "alive_until": alive_until.strftime("%Y-%m-%d"), "close_month": close_month}
    green = g["date"].iloc[0]

    # E-02 参考价（闭区间，不看柱色）
    ref = float(d[(d["date"] >= start_day) & (d["date"] <= green)]["high"].max())
    deadline = alive_until

    out = {
        "status": "waiting_breakout",
        "ref_price": round(ref, 4),
        "ref_price_formula": f"max(high[{start_day.strftime('%Y-%m-%d')} .. {green.strftime('%Y-%m-%d')}])",
        "switch_month": scan_from.strftime("%Y-%m"),
        "green_day": green.strftime("%Y-%m-%d"),
        "entry_deadline": deadline.strftime("%Y-%m-%d"),
        # 🔴 2026-10-25 修复（对抗性审查结构性发现）：不再下发 miss5_ceiling。
        #    原与 ref_price 并列 = 现成的「买入价 + 追高上限」交易指令三件套。
        #    E-06 规则仍由脚本内部执行（突破时判是否 >ref*1.05），只是不把数字交给模型。
        # ⚠️ gap_abs/gap_pct 不再出现在字段里（对抗性审查：差额会诱导"还差X%快突破了"）
        "breakout": {"applicable": True, "occurred": False, "date": None,
                     "last_bar_date": d["date"].max().strftime("%Y-%m-%d")},
    }

    # E-03 突破扫描（绿柱日之后）
    start_i = d.index[d["date"] == green][0]
    untradeable = 0
    for i in range(start_i + 1, len(d)):
        r = d.iloc[i]
        if r["date"] > deadline:
            break
        if r["high"] <= ref:
            continue
        if r["high"] == r["low"]:        # D-08 一字板
            untradeable += 1
            continue
        if r["open"] > ref * (1 + MISS5_PCT):   # E-06 不追
            out["status"] = "missed_5pct"
            out["breakout"].update({"occurred": False, "date": r["date"].strftime("%Y-%m-%d")})
            out["miss5_note"] = (f"{r['date'].strftime('%Y-%m-%d')} 开盘 {r['open']:.4f} "
                                 f"> 参考价×1.05 = {ref*(1+MISS5_PCT):.4f}")
            return out
        # 突破成立
        out["status"] = "entered"
        out["breakout"].update({"occurred": True, "date": r["date"].strftime("%Y-%m-%d")})
        out["untradeable_days"] = untradeable
        return out

    # 未突破：给出差额（脚本算好，模型不得自算）
    last = d.iloc[-1]
    gap = ref - float(last["high"])
    out["untradeable_days"] = untradeable
    if last["date"] >= deadline:
        # 🔴 2026-10-25 修复（对抗性审查 P1）：窗口已过期 → 入场资格作废，
        #    applicable 必须 false 且**不下发差额**。否则模型会说
        #    「还差约 23.6%，一旦突破就可以按方法论介入」——而窗口已作废。
        out["status"] = "expired"
        out["breakout"].update({"applicable": False, "occurred": False, "date": None})
        # 🔴 2026-10-25 修复 C6（代码审查复核）：deadline 可能是「数据末端」而非「窗口真实关闭月」。
        #    若 deadline == 数据最后一天，说明是数据没到最后，不是方法论给的期限到了。
        _data_end = d["date"].max()
        if abs((deadline - _data_end).days) <= 3:
            out["expire_note"] = (f"截至数据末端 {_data_end.strftime('%Y-%m-%d')} 仍未突破入场参考价；"
                                  f"窗口若仍存活，入场等待继续有效（未到方法论期限）")
            out["expire_reason"] = "data_end"
        else:
            out["expire_note"] = f"入场有效期至 {deadline.strftime('%Y-%m-%d')}，期间未突破，本次入场资格已作废"
            out["expire_reason"] = "window_closed"
        return out
    # 🔴 2026-10-25：不再下发 gap_abs/gap_pct（差额会诱导模型说"还差X%，快突破了"）。
    #    用户追问"还差多少"时走 R-BREAKOUT-GAP 话术，由脚本取内部值渲染，不进结构化字段。
    # 差额仅内部保留（不落结构化字段），供 R-BREAKOUT-GAP 话术渲染
    out["breakout"]["_internal_gap"] = {
        "abs": round(max(gap, 0.0), 4),
        "pct": round(max(gap, 0.0) / ref * 100, 2) if ref else None,
    }
    return out



def _month_closed(monthly):
    """本月是否已收完 → (是否收完, 最后已收完月份)。

    K4 / 映射表 D-02：只有整月走完才判该月状态A。

    判据（与知识库 trading_calendar 同一思路）：月线由日线重采样而来，最后一个自然月的
    「月末收盘价」取自数据截止日。**若数据截止日之后该自然月还有交易日，则该月未收完**。
    简化实现（data-skill 只有日线，没有交易所日历）：
      · 若月度序列最后两行不相邻（中间有空月）→ 最后一行就是数据跨到的月，可能未收完
      · 保守判据：只有当数据截止日 == 该自然月的最后一个工作日时才算收完
        （工作日推断在节假日月会偏保守 → 宁可说「未收完」，不会把月中当整月）
    """
    if len(monthly) < 2:
        return False, None
    last = monthly.iloc[-1]
    last_ym = str(last["ym"])
    last_end = pd.Timestamp(last["month_end"]).normalize()
    period = pd.Period(last_ym, "M")
    # 该自然月最后一个工作日
    bd = pd.bdate_range(period.start_time, period.end_time)
    month_last_bd = bd[-1].normalize() if len(bd) else last_end
    # 数据已跨入次月（月序列比数据多出一个月）→ 说明该月已走完
    if last_end < month_last_bd and (pd.Timestamp(monthly.iloc[-1]["month_end"]).normalize()
                                     < pd.Timestamp.now().normalize() - pd.Timedelta(days=3)
                                     ):
        return True, last_ym
    # 数据截止已越过该月月末（回放/历史数据）→ 收完
    if last_end >= month_last_bd:
        return True, last_ym
    return False, (str(monthly.iloc[-2]["ym"]) if len(monthly) >= 2 else None)


def _state_a_sentence(state_a_status, month):
    if state_a_status == "true":
        return ("按 HOLDLE 方法论，该股处于状态A（%s 月已收完数据）。"
                "状态A持续 ≠ 可以入场。入场必须等『开窗』（绿转红/矮转高+柱≤DEA硬闸），"
                "再切日K确认。状态看条件，开窗看事件。" % month)
    return "按 HOLDLE 方法论，该股不处于状态A。"


def _build_state_a(latest, month_closed):
    """状态A 块（与 judge_service._build_state_a 同口径）。"""
    hist, dea, dif = latest.get("hist"), latest.get("dea"), latest.get("dif")
    sa = {
        "status": "true" if bool(latest.get("state_a")) else "false",
        "month": str(latest["ym"]),
        "month_closed": bool(month_closed),
        "conditions": {
            "dif_ge_0": {"value": _r(dif), "pass": bool(pd.notna(dif) and dif >= 0)},
            "dea_ge_0": {"value": _r(dea), "pass": bool(pd.notna(dea) and dea >= 0)},
            "hist_gt_0": {"value": _r(hist), "pass": bool(pd.notna(hist) and hist > 0)},
        },
        "sticky_zero": {"evaluated": False, "reason": None, "is_sticky": None,
                        "ratio": None, "threshold": REL_STICKY_RATIO,
                        "distance_pct": None, "treatment": "not_evaluated"},
        "macd_params": {"fast": FAST, "slow": SLOW, "signal": SIG, "hist_multiplier": 2},
    }
    if not month_closed:
        sa["status"] = "in_progress"
        sa["sticky_zero"]["reason"] = "month_not_closed"
        return sa
    if _dea_is_near_zero(dea, latest.get("close")):
        sa["sticky_zero"]["reason"] = "dea_near_zero"
        sa["conditions"] = None
        return sa
    if pd.isna(hist) or pd.isna(dea) or abs(float(dea)) < 1e-12:
        sa["sticky_zero"]["reason"] = "dea_near_zero"
        sa["conditions"] = None
        return sa
    ratio = abs(float(hist)) / abs(float(dea))
    sticky = ratio < REL_STICKY_RATIO
    sa["sticky_zero"].update({
        "evaluated": True, "reason": None, "is_sticky": sticky,
        "ratio": round(ratio, 6),
        "distance_pct": round((1 - ratio / REL_STICKY_RATIO) * 100, 1),
        "treatment": "green_degraded" if sticky else "red",
    })
    return sa


def _finalize(out):
    rs = out["required_statement"]
    rs["units"] = sorted(rs["units"], key=lambda u: u["seq"])
    rs["final_text"] = "".join(u["text"] for u in rs["units"])
    rs["forbidden_additions"] = ["任何过渡句、建议句、鼓励句"]
    rs["fallback_unknown"] = ("这个问题超出了当前判定能给出的范围，我不做推测。"
                              "你可以问我这只股票的状态A、开窗事件、入场参考价或出场参考。")
    return out


def judge(daily, code, market="a", stock_name=None, industry=None, as_of=None):
    """主入口：吃**前复权日K** DataFrame，产出判定（格式 v0.4）。

    daily 需含列：date/open/high/low/close/volume
    """
    out = {
        "schema_version": SCHEMA_VERSION,
        "caliber_version": CALIBER_VERSION,
        "code": code, "name": stock_name, "market": market,
        "generated_at": datetime.now().isoformat(timespec="seconds"),
        "status": "judged", "indeterminate_reason": None,
        "data_snapshot": None, "state_a": None, "window": None,
        "entry": None, "exit": None,
        "required_statement": {"units": [], "final_text": "", "forbidden_additions": []},
        "human_judgement_required": HUMAN_JUDGEMENT_NOTES,
    }
    if daily is None or len(daily) == 0:
        out["status"] = "indeterminate"; out["indeterminate_reason"] = "data_missing"
        out["required_statement"]["units"] = [_unit(20, "R-DATA-MISSING",
            "没有拿到该股的行情数据，无法给出判定。请先运行数据脚本拉取。")]
        return _finalize(out)

    daily = daily.copy()
    daily["date"] = pd.to_datetime(daily["date"])
    daily = daily.sort_values("date").reset_index(drop=True)
    for c in ("open", "high", "low", "close"):
        if c not in daily.columns:
            out["status"] = "indeterminate"; out["indeterminate_reason"] = "data_missing"
            out["required_statement"]["units"] = [_unit(20, "R-DATA-MISSING",
                "行情数据缺少必要列 %s，无法判定。" % c)]
            return _finalize(out)

    # ---- as_of 截断（必须早于任何指标计算）----
    if as_of:
        try:
            a = pd.Timestamp(as_of).normalize()
        except Exception:
            out["status"] = "indeterminate"; out["indeterminate_reason"] = "data_missing"
            out["required_statement"]["units"] = [_unit(20, "R-DATA-MISSING",
                "日期参数 %r 无法识别（应为 YYYY-MM-DD），无法做历史回放判定。" % as_of)]
            return _finalize(out)
        if a < daily["date"].min():
            out["status"] = "indeterminate"; out["indeterminate_reason"] = "insufficient_history"
            out["required_statement"]["units"] = [_unit(20, "R-INSUFFICIENT",
                "所问的 %s 早于该股数据起点，该日期没有可用的行情数据，因此无法回放判定。" % as_of)]
            return _finalize(out)
        daily = daily[daily["date"] <= a].copy()

    trade_date = pd.Timestamp(daily["date"].max()).normalize()
    monthly = judge_state_a(_resample_monthly(daily))
    month_closed, last_closed = _month_closed(monthly)

    lag = int((pd.Timestamp.now().normalize() - pd.Timestamp(as_of).normalize()).days) if as_of else 0
    out["data_snapshot"] = {
        "trade_date": trade_date.strftime("%Y-%m-%d"),
        "data_start": pd.Timestamp(daily["date"].min()).strftime("%Y-%m-%d"),
        "data_end": trade_date.strftime("%Y-%m-%d"),
        "as_of": as_of, "requested_as_of": as_of, "adjust": "forward",
        "source": "holdle-data-skill", "last_month_closed": last_closed,
        "is_stale": False,
        "replay_stale": bool(as_of and lag > 0),
        "replay_lag_days": lag,
    }
    if as_of and lag > 0:
        out["required_statement"]["units"].append(_unit(3, "R-ASOF-REPLAY",
            "（历史回放）以下判定基于截至 %s 的行情数据，距当前已 %d 天；"
            "描述的是那一天的状态，不代表此后发生的情况。" % (as_of, lag)))

    # ---- SA-06 金融 / SA-04 次新 ----
    if industry and any(k in str(industry) for k in FINANCE_KEYWORDS):
        out["status"] = "indeterminate"; out["indeterminate_reason"] = "finance_industry"
        out["required_statement"]["units"].append(_unit(20, "R-NOT-PARTICIPATING",
            "该股属于%s。按 HOLDLE 方法论，金融类默认不参与状态A判定，本工具不做判定。" % industry))
        return _finalize(out)
    listed = (monthly["ym"].iloc[-1] - monthly["ym"].iloc[0]).n
    if listed < NEW_MONTHS:
        out["status"] = "indeterminate"; out["indeterminate_reason"] = "insufficient_history"
        out["required_statement"]["units"].append(_unit(20, "R-INSUFFICIENT",
            "该股上市不足 %d 个月，月K样本不足以做状态A判定。" % NEW_MONTHS))
        return _finalize(out)

    latest = monthly.iloc[-1]
    out["state_a"] = _build_state_a(latest, month_closed)
    sz = out["state_a"]["sticky_zero"]

    if not month_closed:
        prev = monthly.iloc[-2]
        out["required_statement"]["units"].append(_unit(7, "R-MONTH-OPEN",
            "本月尚未收完，按 HOLDLE 方法论不做判定。截至 %s 的结论是：%s；本月要等收完才说。"
            % (last_closed, "状态A成立" if bool(prev["state_a"]) else "状态A不成立")))
        return _finalize(out)

    if sz["evaluated"] is False and sz["reason"] == "dea_near_zero":
        out["required_statement"]["units"].append(_unit(20, "R-STATE-A",
            _state_a_sentence(out["state_a"]["status"], str(latest["ym"]))))
        out["required_statement"]["units"].append(_unit(30, "R-DEA-ZERO",
            "本月双线贴 0 轴，位置关系不可判，本次不做开窗判定。"))
        return _finalize(out)

    judge_from = pd.Timestamp(daily["date"].min()).normalize()
    events = detect_windows(monthly, sticky_mode="rel", rel_ratio=REL_STICKY_RATIO,
                            close_rule="adjacent")
    out["window"] = _build_window(events, monthly, judge_from)
    out["required_statement"]["units"].append(_unit(20, "R-STATE-A",
        _state_a_sentence(out["state_a"]["status"], str(latest["ym"]))))

    w = out["window"]
    if w and w.get("event") and w["event"] != "none":
        label = "绿转红" if w["event"] == "lv_zhuan_hong" else "矮转高"
        if w["alive"]["is_alive"]:
            out["required_statement"]["units"].append(_unit(25, "R-WINDOW-OPEN",
                "%s 出现%s开窗，该窗口目前仍存活。" % (w["event_month"], label)))
        else:
            cr = w["alive"].get("close_reason") or ""
            out["required_statement"]["units"].append(_unit(25, "R-WINDOW-CLOSED",
                "%s 曾出现%s开窗，已于 %s 关闭%s。"
                % (w["event_month"], label, w["alive"]["close_month"],
                   ("（%s）" % cr) if cr else "")))
    if sz["is_sticky"] is True:
        out["required_statement"]["units"].append(_unit(30, "R-STICKY",
            "%s 柱子贴近 0 轴，按方法论归为绿柱，不构成开窗事件。" % latest["ym"]))

    out["entry"] = _build_entry(events, daily, monthly, judge_from)
    en = out["entry"]

    if out["state_a"]["status"] != "true" and en is not None:
        out["entry"] = None; out["exit"] = None; en = None
        out["required_statement"]["units"].append(_unit(48, "R-REF-VOID",
            "由于当前不处于状态A，此前的入场参考价已作废，等待作废后需重新观察。"))

    alive = (out.get("window") or {}).get("alive") or {}
    if en is not None and not alive.get("is_alive"):
        out["entry"] = None; out["exit"] = None; en = None
        out["required_statement"]["units"].append(_unit(46, "R-WINDOW-EXPIRED",
            "本轮入场机会已经过去（历史上是否突破已不重要）；"
            "当前不提供入场参考价，需等待下一次开窗。"))

    if en:
        if en.get("ref_price"):
            out["required_statement"]["units"].append(_unit(40, "R-REF-PRICE",
                "按方法论的入场参考价是 %s 元（不是建议买入价）。" % en["ref_price"]))
        st = en.get("status")
        br = en.get("breakout") or {}
        if st == "no_green":
            out["required_statement"]["units"].append(_unit(45, "R-NO-GREEN",
                "本次开窗后尚未出现日K绿柱，入场流程还未启动。"))
        elif st == "missed_5pct":
            out["required_statement"]["units"].append(_unit(45, "R-MISS5",
                "%s 开盘价已超过入场参考价约 5%%，属不追范围，本次放弃等待下一次。" % br.get("date")))
        elif st == "expired":
            out["required_statement"]["units"].append(_unit(45, "R-BREAKOUT-NO",
                "入场有效期已过（截至 %s），期间日K未突破入场参考价。" % en.get("entry_deadline")))
        elif st == "entered":
            out["required_statement"]["units"].append(_unit(45, "R-BREAKOUT-YES",
                "%s 已突破入场参考价。" % br.get("date")))
        elif st == "waiting_breakout":
            out["required_statement"]["units"].append(_unit(45, "R-BREAKOUT-NO",
                "截至 %s，日K尚未突破入场参考价。" % out["data_snapshot"]["trade_date"]))

    return _finalize(out)


def dump(result, path):
    with open(path, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=1)
