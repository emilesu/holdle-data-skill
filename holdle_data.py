#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HOLDLE 行情数据获取脚本（用户版 v0.4）
============================================
这是 HOLDLE 提供给用户的「纯数据抓取器」：
- 只抓取公开行情数据（月K/周K/日K/实时行情/财报）
- 只计算公开标准指标（MACD 12/26/9）
- ⚠️ 不含任何 HOLDLE 方法论判断（状态A/开窗/优先级等判断由你的 AI 助手完成）

数据逻辑以 HOLDLE 最新导出脚本 export_data（2026-08 版）为准：
  月K/周K/日K → TickFlow 主用（后复权，A/港/美三市场）→ 失败自动降级 Baostock（A股）
  实时行情 → 腾讯（A股/港股）/ 新浪（美股）
  财报 → AkShare 东方财富（A股，近20年）

用法：
  python3 holdle_data.py <股票代码> [输出目录]
示例：
  python3 holdle_data.py 600519          # 茅台（A股），输出到 holdle_data/ 目录
  python3 holdle_data.py NVDA            # 英伟达（美股）
  python3 holdle_data.py hk_00700        # 腾讯（港股）
  python3 holdle_data.py 600519 /tmp/out # 指定输出目录

代码格式：
  A股：6位数字（600519）
  美股：英文代码（NVDA / AAPL）
  港股：hk_ 前缀（hk_00700）

依赖安装：
  pip3 install baostock tickflow akshare pandas

数据源（全部公开免费，无需任何 token/key）：
  TickFlow Free（三市场 K 线）· Baostock（A股降级）· AkShare（财报）· 腾讯/新浪（实时）
"""
import warnings
warnings.filterwarnings('ignore')

import sys
import os
import re
import time
import urllib.request as urlreq
from datetime import datetime

import pandas as pd

try:
    import baostock as bs
except ImportError:
    bs = None
try:
    from tickflow import TickFlow
except ImportError:
    TickFlow = None
try:
    import akshare as ak
except ImportError:
    ak = None

# ── 版本与更新（2026-08-19 新增自更新机制，2026-08-25 安全加固） ──────
VERSION = "1.0.2"                      # skill 包版本（与 version.json 对齐）
VERSION_URL_API = "https://api.github.com/repos/emilesu/holdle-data-skill/contents/version.json"
VERSION_URL_RAW = "https://raw.githubusercontent.com/emilesu/holdle-data-skill/master/version.json"
# 国内镜像源（仅 jsDelivr CDN；ghproxy.net 第三方代理已移除，MITM 风险）
VERSION_URL_CDN = "https://cdn.jsdelivr.net/gh/emilesu/holdle-data-skill@master/version.json"
SCRIPT_URL_CDN = "https://cdn.jsdelivr.net/gh/emilesu/holdle-data-skill@master/holdle_data.py"
VERSION_CHECK_INTERVAL = 86400         # 每天最多检查一次更新（秒）
# 安全校验标记（必须同时存在才视为合法脚本）
_UPDATE_MARKERS = (
    "HOLDLE_DATA_SKILL",               # 自更新安全标记
    "HOLDLE 行情数据获取",              # 脚本身份标识
    'VERSION = "1.',                    # 版本声明格式
)

# ── 配置 ──────────────────────────────────────────
MAX_RETRIES = 3
RETRY_DELAY = 2
BAOSTOCK_START_DATE = "1990-01-01"
BAOSTOCK_END_DATE = datetime.now().strftime('%Y-%m-%d')
FIN_YEARS = 20  # 财报取近20年（与内部生产脚本 holdle_stock_data.py 一致）

# 复权模式映射（2026-08-19 老板定稿）
ADJUST_MAP = {
    "backward": {"tickflow": "backward", "baostock": "1", "label": "后复权"},
    "forward":  {"tickflow": "forward",  "baostock": "2", "label": "前复权"},
}
DEFAULT_ADJUST = "backward"


# ═══════════════════════════════════════════════════
#  版本自检与更新（v1.2 安全加固：默认只提示，需 --auto-update 才自动覆盖）
# ═══════════════════════════════════════════════════
def _check_update(force=False, auto=False):
    """检查是否有新版本。每天最多一次（本地时间戳）。force=True 强制检查。
    auto=True 才自动下载覆盖；默认只提示用户手动更新。

    多进程说明：时间戳文件无文件锁，并发调用时可能出现两个实例同时读到「未检查」
    并同时执行检查。实际危害极低（多查一次远程版本，结果相同），无需加锁。
    如果未来改为自动覆盖（auto=True），建议用 fcntl.flock 或 rename 原子操作防竞争。"""
    import json as _json
    import time as _time

    stamp_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".version_check_stamp")
    if not force and os.path.exists(stamp_file):
        try:
            last = float(open(stamp_file).read().strip())
            if _time.time() - last < VERSION_CHECK_INTERVAL:
                return  # 距离上次检查不足一天，跳过
        except Exception:
            pass

    import urllib.request as _urlreq
    remote = None
    # 优先 GitHub API（实时、无 CDN 缓存），失败降级 jsDelivr CDN
    try:
        req = _urlreq.Request(VERSION_URL_API, headers={"User-Agent": "holdle-data/1.0"})
        with _urlreq.urlopen(req, timeout=8) as resp:
            api_data = _json.loads(resp.read().decode("utf-8"))
            content = _json.loads(__import__("base64").b64decode(api_data.get("content", "")).decode("utf-8"))
            remote = content
    except Exception:
        # 国内镜像 fallback：仅 jsDelivr CDN（ghproxy.net 已移除，防 MITM）
        for url in (VERSION_URL_CDN, VERSION_URL_RAW):
            try:
                req = _urlreq.Request(url, headers={"User-Agent": "holdle-data/1.0"})
                with _urlreq.urlopen(req, timeout=8) as resp:
                    remote = _json.loads(resp.read().decode("utf-8"))
                    break
            except Exception:
                continue
        if remote is None:
            print("  ⚠️ 版本检查失败（所有源不可达），继续使用本地版本")
            return
    if remote:
        remote_ver = remote.get("version", "")
        # 写时间戳
        try:
            open(stamp_file, "w").write(str(_time.time()))
        except Exception:
            pass
        if remote_ver != VERSION:
            print(f"\n  🔄 检测到新版本 v{remote_ver}（当前 v{VERSION}）")
            print(f"     更新说明: {remote.get('changelog', '')}")
            if auto:
                print(f"     正在自动更新...")
                _auto_update(remote_ver)
            else:
                print(f"     ⚠️ 请手动更新（自动覆盖已关闭）：")
                print(f"       python3 holdle_data.py --update")
                print(f"       或: cd <skill目录> && git pull")
        else:
            print(f"  ✅ 已是最新版本 v{VERSION}")


def _auto_update(new_ver):
    """自动下载新版脚本覆盖自己。从固定 HTTPS URL 下载。"""
    import urllib.request as _urlreq
    import base64 as _b64

    script_url_api = "https://api.github.com/repos/emilesu/holdle-data-skill/contents/holdle_data.py"
    script_url_raw = "https://raw.githubusercontent.com/emilesu/holdle-data-skill/master/holdle_data.py"
    content = None
    # 优先 GitHub API（实时无缓存），失败降级 jsDelivr CDN
    try:
        req = _urlreq.Request(script_url_api, headers={"User-Agent": "holdle-data/1.0"})
        with _urlreq.urlopen(req, timeout=15) as resp:
            api_data = _json.loads(resp.read().decode("utf-8"))
            content = _b64.b64decode(api_data.get("content", "") or "")
    except Exception:
        # 国内镜像 fallback：仅 jsDelivr CDN（ghproxy.net 已移除，防 MITM）
        for url in (SCRIPT_URL_CDN, script_url_raw):
            try:
                req = _urlreq.Request(url, headers={"User-Agent": "holdle-data/1.0"})
                with _urlreq.urlopen(req, timeout=15) as resp:
                    content = resp.read()
                    break
            except Exception:
                continue
        if content is None:
            print(f"  ❌ 自动更新失败（所有源不可达），请手动更新：git pull 或重新下载")
            return
    try:
        # 安全校验：必须同时包含所有身份标记（缺任一则拒绝）
        text = content.decode("utf-8", errors="ignore")
        for marker in _UPDATE_MARKERS:
            if marker not in text:
                print(f"  ❌ 下载内容校验失败（缺少标记: {marker}），已中止更新")
                return
        # 备份当前 + 写入新版 + 清理旧备份
        self_path = os.path.abspath(__file__)
        backup = self_path + ".bak"
        try:
            import shutil
            shutil.copy2(self_path, backup)
        except Exception:
            pass
        with open(self_path, "wb") as f:
            f.write(content)
        # 清理旧备份（只保留最新一个）
        old_backups = [f for f in os.listdir(os.path.dirname(self_path))
                       if f.startswith(os.path.basename(self_path)) and f.endswith(".bak")]
        for old in old_backups:
            old_path = os.path.join(os.path.dirname(self_path), old)
            if old_path != backup:
                try:
                    os.remove(old_path)
                except Exception:
                    pass
        print(f"  ✅ 已更新到 v{new_ver}（旧版备份: {os.path.basename(backup)}）")
        print(f"     请重新运行本脚本。")
        sys.exit(0)
    except Exception as e:
        print(f"  ❌ 自动更新失败（{e}），请手动更新：git pull 或重新下载")


# ═══════════════════════════════════════════════════
#  市场识别
# ═══════════════════════════════════════════════════
def parse_code(code):
    """识别市场类型。返回 (market, raw_code)"""
    code = code.strip().upper()
    if code.startswith("HK_"):
        return "hk", code[3:]
    if code.startswith("US_"):
        return "us", code[3:]
    if re.match(r'^\d{6}$', code):
        return "a", code
    if re.match(r'^[A-Z]{1,5}$', code):
        return "us", code
    if re.match(r'^[A-Z]{1,5}\.', code):
        return "us", code.split('.')[0]
    return "a", code.zfill(6)


# ═══════════════════════════════════════════════════
#  代码转换
# ═══════════════════════════════════════════════════
def code_to_baostock(code, market='a'):
    """A股代码转 Baostock 格式"""
    if market == 'a':
        prefix = 'sh' if code.startswith('6') or code.startswith('9') else 'sz'
        return f"{prefix}.{code}"
    return None


def code_to_tickflow(code, market='a'):
    """A股/美股/港股代码转 TickFlow 格式（2026-08-19 实测：600519.SH / NVDA.US / 00700.HK）"""
    if market == 'a':
        return f"{code}.SH" if code.startswith('6') else f"{code}.SZ"
    if market == 'us':
        return f"{code}.US"
    if market == 'hk':
        return f"{code}.HK"
    return code


# ═══════════════════════════════════════════════════
#  TickFlow K线（主用，后复权，A/港/美三市场）
# ═══════════════════════════════════════════════════
def _standardize_tickflow(df):
    """TickFlow 返回统一为标准列 date/open/high/low/close/volume"""
    if df is None or df.empty:
        return pd.DataFrame()
    df = df.rename(columns={'trade_date': 'date'})
    df['date'] = pd.to_datetime(df['date'])
    df = df.sort_values('date').reset_index(drop=True)
    keep = [c for c in ['date', 'open', 'high', 'low', 'close', 'volume'] if c in df.columns]
    return df[keep]


def _drop_partial_last(df, now, freq):
    """剔除「进行中」的当月/当周 partial bar（月末/周五结算后不误删完整K线）"""
    if df is None or df.empty:
        return df
    last = df.iloc[-1]['date']
    if freq == 'M':
        same_month = (last.year == now.year and last.month == now.month)
        if same_month:
            import calendar
            last_day_of_month = calendar.monthrange(now.year, now.month)[1]
            is_month_end = now.day >= last_day_of_month - 1
            if not is_month_end:
                return df.iloc[:-1].reset_index(drop=True)
        return df
    else:  # 'w'
        last_week = last.isocalendar()[:2]
        now_week = now.isocalendar()[:2]
        if last_week == now_week and now.weekday() < 4:
            return df.iloc[:-1].reset_index(drop=True)
        return df


def fetch_tickflow_monthly(symbol_tf, drop_partial=True, adjust="backward"):
    """TickFlow 月K（period='1M'），默认剔除进行中的当月。adjust: backward(后复权)/forward(前复权)"""
    if TickFlow is None:
        print("  ⚠️ tickflow 未安装（pip3 install tickflow）")
        return pd.DataFrame()
    try:
        tf = TickFlow.free()
        df = tf.klines.get(symbol_tf, period='1M', count=2000, adjust=adjust, as_dataframe=True)
        df = _standardize_tickflow(df)
        if df.empty:
            return df
        if drop_partial:
            df = _drop_partial_last(df, datetime.now(), 'M')
        return df
    except Exception as e:
        print(f"  ⚠️ TickFlow 月K异常: {e}")
        return pd.DataFrame()


def fetch_tickflow_weekly(symbol_tf, drop_partial=True, adjust="backward"):
    """TickFlow 周K（period='1w'），默认剔除进行中的当周。adjust: backward/forward"""
    if TickFlow is None:
        print("  ⚠️ tickflow 未安装（pip3 install tickflow）")
        return pd.DataFrame()
    try:
        tf = TickFlow.free()
        df = tf.klines.get(symbol_tf, period='1w', count=2000, adjust=adjust, as_dataframe=True)
        df = _standardize_tickflow(df)
        if df.empty:
            return df
        if drop_partial:
            df = _drop_partial_last(df, datetime.now(), 'w')
        return df
    except Exception as e:
        print(f"  ⚠️ TickFlow 周K异常: {e}")
        return pd.DataFrame()


def fetch_tickflow_daily(symbol_tf, adjust="backward"):
    """TickFlow 日K（count=10000）。adjust: backward/forward"""
    if TickFlow is None:
        print("  ⚠️ tickflow 未安装（pip3 install tickflow）")
        return pd.DataFrame()
    try:
        tf = TickFlow.free()
        df = tf.klines.get(symbol_tf, count=10000, adjust=adjust, as_dataframe=True)
        df = _standardize_tickflow(df)
        return df
    except Exception as e:
        print(f"  ⚠️ TickFlow 日K异常: {e}")
        return pd.DataFrame()


# ═══════════════════════════════════════════════════
#  Baostock K线（降级兜底，仅A股）
# ═══════════════════════════════════════════════════
def fetch_baostock_kline(symbol_bs, frequency, adjustflag="1"):
    """Baostock K线通用（frequency: 'm'/'w'）。adjustflag: 1=后复权 2=前复权"""
    if bs is None:
        return pd.DataFrame()
    for attempt in range(MAX_RETRIES):
        try:
            lg = bs.login()
            if lg.error_code != '0':
                print(f"  ⚠️ Baostock 登录失败: {lg.error_msg}")
                return pd.DataFrame()
            rs = bs.query_history_k_data_plus(
                symbol_bs,
                fields='date,open,high,low,close,volume,amount',
                start_date=BAOSTOCK_START_DATE,
                end_date=BAOSTOCK_END_DATE,
                frequency=frequency,
                adjustflag=adjustflag
            )
            data_list = []
            while rs.next():
                row = rs.get_row_data()
                if row[0] is not None:
                    data_list.append(row)
            bs.logout()
            if not data_list:
                print(f"  ⚠️ Baostock {frequency}K无数据: {symbol_bs}")
                return pd.DataFrame()
            df = pd.DataFrame(data_list, columns=rs.fields)
            for col in ['open', 'high', 'low', 'close', 'volume', 'amount']:
                df[col] = pd.to_numeric(df[col], errors='coerce')
            df['date'] = pd.to_datetime(df['date'])
            df = df.sort_values('date').reset_index(drop=True)
            return df[['date', 'open', 'high', 'low', 'close', 'volume', 'amount']]
        except Exception as e:
            print(f"  ⚠️ Baostock {frequency}K异常（第{attempt+1}次）: {e}")
            time.sleep(RETRY_DELAY)
    return pd.DataFrame()


def fetch_baostock_monthly(symbol_bs, adjustflag="1"):
    return fetch_baostock_kline(symbol_bs, 'm', adjustflag)


def fetch_baostock_weekly(symbol_bs, adjustflag="1"):
    return fetch_baostock_kline(symbol_bs, 'w', adjustflag)


# ═══════════════════════════════════════════════════
#  数据源：实时行情（腾讯 A股/港股 · 新浪 美股）
# ═══════════════════════════════════════════════════
def fetch_tencent_realtime(symbol):
    """腾讯实时行情（A股/港股）"""
    if re.match(r'^\d{4,5}$', symbol):
        prefix = 'hk'
    else:
        prefix = 'sh' if symbol.startswith('6') or symbol.startswith('9') else 'sz'
    url = f"https://qt.gtimg.cn/q={prefix}{symbol}"
    try:
        req = urlreq.Request(url, headers={'User-Agent': 'Mozilla/5.0'})
        with urlreq.urlopen(req, timeout=8) as resp:
            text = resp.read().decode('gbk')
        if '~' not in text:
            return {}
        parts = text.split('~')
        return {
            '名称': parts[1], '代码': parts[2],
            '当前': parts[3] if len(parts) > 3 else '',
            '昨收': parts[4] if len(parts) > 4 else '',
            '今开': parts[5] if len(parts) > 5 else '',
            '最高': parts[33] if len(parts) > 33 else '',
            '最低': parts[34] if len(parts) > 34 else '',
            '换手率': parts[38] if len(parts) > 38 else '',
            '市盈率': parts[39] if len(parts) > 39 else '',
            '市净率': parts[46] if len(parts) > 46 else '',
            '总市值': parts[45] if len(parts) > 45 else '',
        }
    except Exception as e:
        print(f"  ⚠️ 腾讯行情异常: {e}")
        return {}


def fetch_sina_us_realtime(symbol):
    """新浪美股实时行情"""
    url = f"https://hq.sinajs.cn/list=gb_{symbol}"
    try:
        req = urlreq.Request(url, headers={
            'User-Agent': 'Mozilla/5.0',
            'Referer': 'https://finance.sina.com.cn/stock/usstock/'
        })
        with urlreq.urlopen(req, timeout=8) as resp:
            text = resp.read().decode('gbk')
        if not text or '=' not in text:
            return {}
        parts = text.split('"')[1].split(',')
        if len(parts) < 6:
            return {}
        return {'名称': parts[0], '当前': parts[1], '昨收': parts[2], '今开': parts[3],
                '最高': parts[4], '最低': parts[5]}
    except Exception as e:
        print(f"  ⚠️ 新浪行情异常: {e}")
        return {}


# ═══════════════════════════════════════════════════
#  数据源：财报（A股，东方财富，近20年）
# ═══════════════════════════════════════════════════
EM_INDICATORS = {
    'roe': '净资产收益率(ROE)',
    'gross_margin': '毛利率',
    'net_margin': '销售净利率',
    'debt_ratio': '资产负债率',
    'cash_flow': '经营现金流量净额',
    'net_profit': '净利润',
    'roa': '总资产报酬率(ROA)',
}

def fetch_financials_hk(code):
    """港股财务核心指标（东方财富 stock_financial_hk_analysis_indicator_em），近 FIN_YEARS 年"""
    if ak is None:
        print("  ⚠️ akshare 未安装（pip3 install akshare），跳过财报")
        return []
    try:
        df = ak.stock_financial_hk_analysis_indicator_em(symbol=code, indicator='年度')
        if df is None or df.empty:
            return []
        # 按 REPORT_DATE 排序（新→旧），取最近 FIN_YEARS 年
        df = df.sort_values('REPORT_DATE', ascending=False).head(FIN_YEARS)
        import math
        result = []
        for _, row in df.iterrows():
            year = str(row.get('REPORT_DATE', ''))[:4]
            if not year.isdigit():
                continue
            def num(key):
                v = row.get(key)
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    return float('nan')
                return float(v)
            entry = {
                '年份': year,
                'roe': num('ROE_YEARLY'),
                'gross_margin': num('GROSS_PROFIT_RATIO'),
                'net_margin': num('NET_PROFIT_RATIO'),
                'debt_ratio': num('DEBT_ASSET_RATIO'),
                'net_profit': num('HOLDER_PROFIT') / 1e8 if row.get('HOLDER_PROFIT') else float('nan'),
                'cash_flow': num('PER_NETCASH_OPERATE'),  # 每股经营现金流
            }
            result.append(entry)
        return result
    except Exception as e:
        print(f"  ⚠️ 港股财报获取失败: {e}")
        return []


def fetch_financials_us(code):
    """美股财务核心指标（东方财富 stock_financial_us_analysis_indicator_em），近 FIN_YEARS 年"""
    if ak is None:
        print("  ⚠️ akshare 未安装（pip3 install akshare），跳过财报")
        return []
    try:
        df = ak.stock_financial_us_analysis_indicator_em(symbol=code, indicator='年报')
        if df is None or df.empty:
            return []
        # 按 REPORT_DATE 排序（新→旧），取最近 FIN_YEARS 年
        df = df.sort_values('REPORT_DATE', ascending=False).head(FIN_YEARS)
        import math
        result = []
        for _, row in df.iterrows():
            year = str(row.get('STD_REPORT_DATE', ''))[:4]
            if not year.isdigit():
                year = str(row.get('REPORT_DATE', ''))[:4]
            if not year.isdigit():
                continue
            def num(key):
                v = row.get(key)
                if v is None or (isinstance(v, float) and math.isnan(v)):
                    return float('nan')
                return float(v)
            entry = {
                '年份': year,
                'roe': num('ROE_AVG'),
                'gross_margin': num('GROSS_PROFIT_RATIO'),
                'net_margin': num('NET_PROFIT_RATIO'),
                'debt_ratio': num('DEBT_ASSET_RATIO'),
                'net_profit': num('PARENT_HOLDER_NETPROFIT') / 1e8 if row.get('PARENT_HOLDER_NETPROFIT') else float('nan'),
                'cash_flow': float('nan'),  # 美股接口无直接现金流，后续补充
            }
            result.append(entry)
        return result
    except Exception as e:
        print(f"  ⚠️ 美股财报获取失败: {e}")
        return []


def fetch_financials_a(code):
    """A股财务核心指标（东方财富 stock_financial_abstract），近 FIN_YEARS 年"""
    if ak is None:
        print("  ⚠️ akshare 未安装（pip3 install akshare），跳过财报")
        return []
    try:
        df = ak.stock_financial_abstract(symbol=code)
        date_cols = [c for c in df.columns if c not in ['选项', '指标']]
        year_cols = [c for c in date_cols if c.endswith('1231') and len(c) == 8 and c[:4].isdigit()]
        year_cols.sort()
        indicators = {}
        for key, label in EM_INDICATORS.items():
            row = df[df['指标'] == label]
            if not row.empty:
                indicators[key] = row.iloc[0]
        import math
        result = []
        for c in year_cols[-FIN_YEARS:]:
            year = c[:4]
            entry = {'年份': year}
            for key in indicators:
                val = indicators[key][c]
                if isinstance(val, str) and val.lower() in ('nan', 'none', '', '-'):
                    val = float('nan')
                elif isinstance(val, str):
                    try:
                        val = float(val)
                    except Exception:
                        val = float('nan')
                if key in ('net_profit', 'cash_flow') and isinstance(val, (int, float)) and not (isinstance(val, float) and math.isnan(val)):
                    val = val / 1e8
                entry[key] = val
            result.append(entry)
        return result
    except Exception as e:
        print(f"  ⚠️ 东方财富财报获取失败: {e}")
        try:
            fin_df = ak.stock_financial_abstract_ths(symbol=code)
            fin_df = fin_df[fin_df['报告期'].str.contains('12-31', na=False)].tail(FIN_YEARS)
            return fin_df.to_dict('records')
        except Exception:
            return []


# ═══════════════════════════════════════════════════
#  MACD 计算（标准公开公式 12/26/9）
# ═══════════════════════════════════════════════════
def calc_macd(df, fast=12, slow=26, signal=9):
    """输入含 close 列的 DataFrame，返回 (DIF, DEA, MACD柱)。标准公开公式，无方法论含量。"""
    ema_fast = df['close'].ewm(span=fast, adjust=False).mean()
    ema_slow = df['close'].ewm(span=slow, adjust=False).mean()
    dif = ema_fast - ema_slow
    dea = dif.ewm(span=signal, adjust=False).mean()
    macd = (dif - dea) * 2
    return dif, dea, macd


# ═══════════════════════════════════════════════════
#  主流程（TickFlow 主用 → Baostock 降级）
# ═══════════════════════════════════════════════════
def main():
    if len(sys.argv) < 2:
        print("用法: python3 holdle_data.py <股票代码> [输出目录] [--adjust forward|backward]")
        print("  A股示例:  600519  603259  000858")
        print("  美股示例:  NVDA    AAPL    MSFT")
        print("  港股示例:  hk_00700  hk_09988")
        print("  --adjust: backward=后复权(默认,历史复盘/回测) forward=前复权(当前时点判断)")
        print("  --update: 检查更新（仅提示）")
        print("  --auto-update: 检查并自动下载覆盖（⚠️ 覆盖脚本文件，请确认来源可信）")
        print("  --version: 显示当前版本")
        sys.exit(1)

    # 解析 --adjust / --update / --auto-update / --check-update
    adjust = DEFAULT_ADJUST
    args = sys.argv[1:]
    if "--auto-update" in args:
        _check_update(force=True, auto=True)
        return
    if "--update" in args or "--check-update" in args:
        _check_update(force=True, auto=False)
        return
    if "--version" in args:
        print(f"holdle-data skill v{VERSION}")
        return
    if "--adjust" in args:
        i = args.index("--adjust")
        if i + 1 < len(args) and args[i + 1] in ADJUST_MAP:
            adjust = args[i + 1]
            args = args[:i] + args[i + 2:]
        else:
            print(f"  ⚠️ --adjust 参数无效，使用默认 {DEFAULT_ADJUST}")

    if len(args) < 1:
        print("用法: python3 holdle_data.py <股票代码> [输出目录] [--adjust forward|backward]")
        sys.exit(1)
    raw_code = args[0].strip()
    market, code = parse_code(raw_code)
    out_dir = args[1].strip() if len(args) > 1 else os.path.join(os.getcwd(), 'holdle_data')
    os.makedirs(out_dir, exist_ok=True)

    adj = ADJUST_MAP[adjust]
    adj_label = adj["label"]
    adj_bs = adj["baostock"]
    adj_tf = adj["tickflow"]

    now = datetime.now()
    market_label = {'a': 'A股', 'us': '美股', 'hk': '港股'}.get(market, '未知')

    print("=" * 70)
    print(f"HOLDLE 行情数据获取（用户版 v0.4）")
    print(f"标的: {code}（{market_label}）  |  {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"复权口径: {adj_label}（{adjust}）")
    print(f"数据源: TickFlow 主用（月/周/日）→ Baostock 降级 · AkShare 财报 · 腾讯/新浪 实时")
    print(f"输出目录: {out_dir}")
    print("=" * 70)

    # 版本自检（非阻塞，每天最多一次；默认只提示，不自动覆盖）
    _check_update(auto=False)

    name = code
    sym_tf = code_to_tickflow(code, market)
    bs_code = code_to_baostock(code, market) if market == 'a' else None

    # ── 0. 实时行情 ─────────────────────────────
    print("\n[0/5] 实时行情...")
    if market == 'us':
        rt = fetch_sina_us_realtime(code)
        if rt.get('名称'):
            name = rt['名称']
            print(f"  {name}（美股）  当前: ${rt['当前']}  昨收: ${rt['昨收']}  今开: ${rt['今开']}")
            print(f"  最高: ${rt['最高']}  最低: ${rt['最低']}")
        else:
            print("  ⚠️ 实时行情获取失败")
    else:
        rt = fetch_tencent_realtime(code)
        if rt.get('名称'):
            name = rt['名称']
            print(f"  {name}  最新: ¥{rt['当前']}  昨收: ¥{rt['昨收']}  今开: ¥{rt['今开']}")
            print(f"  最高: ¥{rt['最高']}  最低: ¥{rt['最低']}  换手: {rt['换手率']}%  PE: {rt['市盈率']}  PB: {rt['市净率']}")
        else:
            print("  ⚠️ 实时行情获取失败")

    # ── 1. 财务核心指标（A股/港股/美股，近20年） ─
    print("\n[1/5] 财务核心指标（近20年）...")
    fin = []
    if market == 'a':
        fin = fetch_financials_a(code)
    elif market == 'hk':
        fin = fetch_financials_hk(code)
    elif market == 'us':
        fin = fetch_financials_us(code)
    if fin:
        fdf = pd.DataFrame(fin)
        want = ['年份', 'roe', 'roa', 'gross_margin', 'net_margin', 'net_profit', 'cash_flow', 'debt_ratio']
        showf = fdf[[c for c in want if c in fdf.columns]].copy()
        showf = showf.rename(columns={
            'roe': 'ROE(%)', 'roa': 'ROA(%)', 'gross_margin': '毛利率(%)',
            'net_margin': '净利率(%)', 'net_profit': '净利润(亿)', 'cash_flow': '经营现金流(亿)',
            'debt_ratio': '负债率(%)'
        })
        showf.to_csv(f"{out_dir}/{code}_财务_近20年.csv", index=False)
        print(f"  ✅ 已存 {code}_财务_近20年.csv")
        print("\n" + showf.to_string(index=False))
    else:
        print("  ❌ 财报获取失败")

    # ── 2. 月K线（TickFlow 主用 → Baostock 降级） ─
    print(f"\n[2/5] 月K线（{adj_label}）+ MACD...")
    monthly = fetch_tickflow_monthly(sym_tf, adjust=adj_tf)
    monthly_src = 'TickFlow'
    if monthly.empty and bs_code:
        print("  ⚠️ TickFlow 月K不可用，降级到 Baostock...")
        monthly = fetch_baostock_monthly(bs_code)
        monthly_src = 'Baostock(降级)'
    if not monthly.empty:
        dif, dea, mbar = calc_macd(monthly)
        monthly['DIF'] = dif.round(2)
        monthly['DEA'] = dea.round(2)
        monthly['MACD柱'] = mbar.round(2)
        monthly.to_csv(f"{out_dir}/{code}_月K_{adj_label}.csv", index=False)
        print(f"  ✅ 月K {monthly['date'].min().date()} ~ {monthly['date'].max().date()}，共 {len(monthly)} 条（来源: {monthly_src}，{adj_label}）")
        print(f"  💾 已存 {code}_月K_{adj_label}.csv（含 DIF/DEA/MACD柱）")
        show = monthly.tail(24)[['date', 'close', 'DIF', 'DEA', 'MACD柱']].copy()
        show.columns = ['日期', '收盘', 'DIF', 'DEA', '柱']
        print("\n=== 月K最近24条（含MACD指标）===")
        print(show.to_string(index=False))
    else:
        print("  ❌ 月K获取失败")

    # ── 3. 周K线（TickFlow 主用 → Baostock 降级） ─
    print(f"\n[3/5] 周K线（{adj_label}）+ MACD...")
    weekly = fetch_tickflow_weekly(sym_tf, adjust=adj_tf)
    if weekly.empty and bs_code:
        print("  ⚠️ TickFlow 周K不可用，降级到 Baostock...")
        weekly = fetch_baostock_weekly(bs_code, adjustflag=adj_bs)
    if not weekly.empty:
        dif, dea, mbar = calc_macd(weekly)
        weekly['DIF'] = dif.round(2)
        weekly['DEA'] = dea.round(2)
        weekly['MACD柱'] = mbar.round(2)
        weekly.to_csv(f"{out_dir}/{code}_周K_{adj_label}.csv", index=False)
        print(f"  ✅ 周K {weekly['date'].min().date()} ~ {weekly['date'].max().date()}，共 {len(weekly)} 条（{adj_label}）")
        print(f"  💾 已存 {code}_周K_{adj_label}.csv")
    else:
        print("  ⚠️ 周K获取失败")

    # ── 4. 日K线（TickFlow） ────────────────────
    print(f"\n[4/5] 日K线（TickFlow Free，{adj_label}）+ MACD...")
    daily = fetch_tickflow_daily(sym_tf, adjust=adj_tf)
    if not daily.empty:
        dif, dea, mbar = calc_macd(daily)
        daily['DIF'] = dif.round(2)
        daily['DEA'] = dea.round(2)
        daily['MACD柱'] = mbar.round(2)
        daily.to_csv(f"{out_dir}/{code}_日K_{adj_label}.csv", index=False)
        print(f"  ✅ 日K {daily['date'].min().date()} ~ {daily['date'].max().date()}，共 {len(daily)} 条（{adj_label}）")
        print(f"  💾 已存 {code}_日K_{adj_label}.csv")
    else:
        print("  ⚠️ 日K获取失败")

    print("\n" + "=" * 70)
    print(f"获取完成 ✅ 数据已保存到: {out_dir}")
    print(f"提示：以上为原始数据 + 公开指标（MACD），复权口径为 {adj_label}，不含任何投资判断。")
    print("方法判断（状态A等）请交给你的 HOLDLE AI 助手完成。")
    print("=" * 70)


if __name__ == "__main__":
    main()
