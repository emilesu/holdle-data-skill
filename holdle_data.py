#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
HOLDLE 行情数据获取脚本（用户版 v0.4）
============================================
这是 HOLDLE 提供给用户的「纯数据抓取器」：
- 只抓取公开行情数据（月K/周K/日K/实时行情/财报）
- 只计算公开标准指标（MACD 12/26/9）
- 🤖 取数 + **确定性判定**：本脚本负责拉行情/财务数据，并在前复权模式下自动产出
  `{code}_判定.json`（状态A/开窗/入场参考价由脚本按方法论确定性计算，模型只能转述 final_text、不得自算）；
  企业质量、行情段资格、出场管理等需人工判断的部分仍由 AI 助手/用户按规则表处理

数据逻辑以 HOLDLE 最新导出脚本 export_data（2026-08 版）为准：
  月K/周K/日K → datasource.py（新浪A股 / Yahoo 港美股，原始价+复权因子现算，全量）
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
  pip3 install pandas numpy requests pyarrow akshare

数据源（全部公开免费，无需任何 token/key）：
  TickFlow Free（三市场 K 线）· Baostock（A股降级）· AkShare（财报）· 腾讯/新浪（实时）
"""
import warnings

warnings.filterwarnings('ignore')

import os
import re
import sys
import urllib.request as urlreq
from datetime import datetime

import pandas as pd

# 🔴 取数统一走 datasource.py（回测试验同源：新浪/Yahoo 原始价 + 复权因子现算）
try:
    import datasource as _ds
    _ds_load_daily = _ds.load_daily
    _ds_to_bars = _ds.to_bars
except Exception as _e:  # noqa: BLE001
    _ds = None
    _ds_load_daily = None
    _ds_to_bars = None
    _DS_IMPORT_ERROR = _e

# 🔴 2026-10-26 换源：baostock / tickflow 已不再使用（K线统一走 datasource），
#    这两个 import 一并移除，避免用户误以为还需要安装。
try:
    import akshare as ak          # 财务数据（可选：未装时跳过财报，不影响K线与判定）
except ImportError:
    ak = None

# ── 版本与更新（2026-08-19 新增自更新机制，2026-08-25 安全加固） ──────
VERSION = "1.2.0"                      # skill 包版本（与 version.json 对齐）

# 🔴 v1.0.8（2026-10-26）：自更新从「只更新自己」扩展为「更新自己 + 附属模块」。
#   背景：judge.py（判定模块）是 v1.0.8 新增的附属文件，只更新 holdle_data.py 会导致
#   老用户升级后 holdle_data.py 里 import judge 找不到文件 → 判定静默失效。
#   原则：附属模块缺失只降级（告警 + 不判定），绝不让整个数据拉取失败。
# 🔴 2026-10-26 审查 C-3 修复：judge.py 从「单文件模块」变成了「模块 + 数据文件」，
#    分发清单必须同步，否则用户端拿到新 judge.py 却没有日历 → 判定整块失效
#    （实测：有日历 state_a=true+开窗+入场；无日历只剩一句"本月尚未收完"）。
#    格式："文件名" = Python 模块（查函数标记）；"文件名:json" = JSON 数据（查结构标记）
SIDE_MODULES = [
    "datasource.py",       # 取数层（新浪/Yahoo 原始价 + 复权因子现算，与回测试验同源）
    "judge.py",            # 判定层（状态A/开窗/入场参考价，只转述 final_text）
]
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
    # 🔴 2026-10-26 换源后只保留 label：取数统一走 datasource（adjust 直接传 forward/backward）
    "backward": {"label": "后复权"},
    "forward":  {"label": "前复权"},
}
DEFAULT_ADJUST = "forward"


# ═══════════════════════════════════════════════════
#  版本自检与更新（v1.2 安全加固：默认只提示，需 --auto-update 才自动覆盖）
# ═══════════════════════════════════════════════════
def _ver_cmp(a, b):
    """版本号字符串比较（1.10 > 1.9）。返回 1/0/-1。"""
    def norm(v):
        parts = [int(x) for x in re.split(r'[._-]', str(v)) if x.isdigit()]
        return parts or [0]
    na, nb = norm(a), norm(b)
    return (na > nb) - (na < nb)


def _check_update(force=False, auto=False):
    """检查是否有新版本。force=True 强制检查（绕过每日限流）；auto=True 自动下载覆盖。

    多进程说明：时间戳文件无文件锁，并发调用时可能出现两个实例同时读到「未检查」
    并同时执行检查。实际危害极低（多查一次远程版本，结果相同），无需加锁。
    自动更新前会校验下载内容（_UPDATE_MARKERS）+ 备份旧版（.bak）；
    环境变量 HOLDLE_DATA_UPDATED=1 表示本次运行已完成一次自动更新，重启后跳过，
    防止版本比较异常导致无限重启。"""
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
        cmp_ = _ver_cmp(remote_ver, VERSION)
        if cmp_ > 0:
            print(f"\n  🔄 检测到新版本 v{remote_ver}（当前 v{VERSION}）")
            print(f"     更新说明: {remote.get('changelog', '')}")
            if auto:
                if os.environ.get("HOLDLE_DATA_UPDATED") == "1":
                    print("     ⚠️ 本次已自动更新过（防循环跳过），继续使用本地版本")
                else:
                    print(f"     正在自动更新，完成后自动重启继续本次任务...")
                    _auto_update(remote_ver, restart=True)
            else:
                print(f"     ⚠️ 检测到新版本，请执行更新（自动覆盖已关闭）：")
                print(f"       python3 holdle_data.py --auto-update")
                print(f"       或: cd <skill目录> && git pull")
        elif cmp_ < 0:
            print(f"  ℹ️ 远程版本 v{remote_ver} 不高于本地 v{VERSION}（远程较旧，忽略）")
        else:
            print(f"  ✅ 已是最新版本 v{VERSION}")


def _auto_update(new_ver, restart=True):
    """自动下载新版脚本覆盖自己。从固定 HTTPS URL 下载。
    restart=True 时更新完成后用 os.execv 以新版代码重启继续本次任务；
    restart=False 时更新完成后退出（供 --auto-update 显式命令使用）。"""
    import base64 as _b64
    import json as _json  # v1.0.4 修复：此前漏 import 导致 GitHub API 段 NameError，静默降级 CDN 缓存旧版
    import urllib.request as _urlreq

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
        # 版本一致性校验（v1.0.4+）：下载内容的 VERSION 必须与期望版本一致。
        # 防 CDN 缓存不同步（如 jsDelivr 缓存滞后于 GitHub API）导致「更新」实际拿到旧代码。
        m = re.search(r'VERSION = "([\d.]+)"', text)
        if m and m.group(1) != new_ver:
            print(f"  ❌ 下载内容版本 v{m.group(1)} ≠ 期望 v{new_ver}（下载源缓存可能未刷新），已中止更新")
            print(f"     请稍后重试（等 CDN 刷新），或手动更新：git pull")
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
        # ── 附属模块分发（v1.0.8）──
        # 任一附属模块下载失败只告警，不阻断主脚本更新（主功能优先）
        _base_dir = os.path.dirname(self_path)
        _side_ok, _side_fail = [], []
        _staged = []          # 先在内存里全部下齐+校验，最后统一落盘
        for _mod in SIDE_MODULES:
            # 🔴 2026-10-26 对抗性审查 critical：_fname 原在"下载失败"分支之后才赋值，
            #    一旦任一源失败就 NameError → 被外层 except 兜住 → 主脚本已写盘、附属模块一个没写，
            #    用户被留在「半装状态」，还被告知"更新失败请手动 git pull"（误导）。
            _fname, _kind = (_mod.split(":", 1) + ["py"])[:2]
            _mcontent = None
            for _u in (f"https://api.github.com/repos/emilesu/holdle-data-skill/contents/{_fname}",
                       f"https://cdn.jsdelivr.net/gh/emilesu/holdle-data-skill@master/{_fname}",
                       f"https://raw.githubusercontent.com/emilesu/holdle-data-skill/master/{_fname}"):
                try:
                    _rq = _urlreq.Request(_u, headers={"User-Agent": "holdle-data/1.0"})
                    with _urlreq.urlopen(_rq, timeout=15) as _rs:
                        _raw = _rs.read()
                    if "api.github.com" in _u:
                        _mcontent = _b64.b64decode(_json.loads(_raw.decode("utf-8")).get("content", "") or "")
                    else:
                        _mcontent = _raw
                    if _mcontent:
                        break
                except Exception:
                    continue
            if not _mcontent:
                _side_fail.append(_fname + "(下载失败)")
                continue
            # 安全检查：按类型校验内容（防止投毒下发任意文件）
            _mtxt = _mcontent.decode("utf-8", errors="ignore")
            if _kind == "json":
                try:
                    _mj = _json.loads(_mtxt)
                except Exception:
                    _side_fail.append(_fname + "(JSON 解析失败)")
                    continue
                if _fname.startswith("trading_calendar"):
                    _valid = ("months" in _mj) and isinstance(_mj.get("months"), dict) and _mj.get("months")
                elif _fname.startswith("finance_symbols"):
                    _valid = ("symbols" in _mj) and isinstance(_mj.get("symbols"), dict)
                else:
                    _valid = False
                if not _valid:
                    _side_fail.append(_fname + "(结构校验失败)")
                    continue
            else:
                if not (("HOLDLE 判定模块 judge.py" in _mtxt and "def judge(" in _mtxt)
                        or ("HOLDLE 取数模块 datasource.py" in _mtxt and "def load_daily(" in _mtxt)):
                    _side_fail.append(_fname + "(内容校验失败)")
                    continue
            _staged.append((_fname, _mcontent))

        # 🔴 要么全写、要么不写：核心判定模块缺一个，判定能力就是残缺的。
        #    （日历缺 → 全部判「未收完」；金融名单缺 → 金融股被放行判定）
        _registered = [m.split(":", 1)[0] for m in SIDE_MODULES]
        _staged_names = {n for n, _ in _staged}
        _miss_core = sorted(set(_registered) - _staged_names)
        if _miss_core:
            print(f"  ⚠️ 附属模块未下齐（缺: {', '.join(_miss_core)}），本次不写入半套文件，判定功能保持旧状态")
            _staged = []
        else:
            for _fname, _mcontent in _staged:
                try:
                    _tmp = os.path.join(_base_dir, _fname + ".new")
                    with open(_tmp, "wb") as _mf:
                        _mf.write(_mcontent)
                    os.replace(_tmp, os.path.join(_base_dir, _fname))   # 原子替换
                    _side_ok.append(_fname)
                except Exception as _me:
                    _side_fail.append(f"{_fname}(写入失败:{_me})")
        if _side_ok:
            print(f"  ✅ 附属模块已更新: {', '.join(_side_ok)}")
        if _side_fail:
            print(f"  ⚠️ 附属模块未能更新: {', '.join(_side_fail)}（数据拉取不受影响；判定可能不可用）")

        print(f"  ✅ 已更新到 v{new_ver}（旧版备份: {os.path.basename(backup)}）")
        if restart:
            # 标记本次已更新，防止重启后再次触发更新导致死循环
            os.environ["HOLDLE_DATA_UPDATED"] = "1"
            print("     自动重启，用新版代码继续本次任务...")
            sys.stdout.flush()  # 防止输出重定向（块缓冲）时 execv 丢失更新过程日志
            try:
                os.execv(sys.executable, [sys.executable] + sys.argv)
            except Exception as e:
                print(f"  ⚠️ 自动重启失败（{e}），请手动重新运行本脚本")
                sys.exit(0)
        else:
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
# 每个字段支持多个备选指标名（按序匹配第一个命中；兼容 akshare/数据源版本差异，
# 例如旧版新浪接口可能返回「经营活动产生的现金流量净额」而非「经营现金流量净额」）
EM_INDICATORS = {
    'roe': ['净资产收益率(ROE)', '净资产收益率'],
    'gross_margin': ['毛利率'],
    # ⚠️ net_margin = 东财「销售净利率」= 含少数股东损益口径（2026-09-27 实测核实：
    #    重庆啤酒 2025 该值为 16.83%，而归母口径仅 8.36%）。
    #    HOLDLE 净利率门槛要求归母口径 → 用下方 revenue + parent_net_profit 自算 parent_net_margin。
    'net_margin': ['销售净利率'],
    'debt_ratio': ['资产负债率'],
    'cash_flow': ['经营现金流量净额', '经营活动产生的现金流量净额'],
    'net_profit': ['净利润', '归母净利润'],
    'roa': ['总资产报酬率(ROA)', '总资产报酬率'],
    # ── 2026-09-27 新增（创始人拍板「方案A」）：支撑归母净利率门槛口径 ──
    'revenue': ['营业总收入', '营业收入'],                    # 单位：元（后续统一 /1e8 转亿）
    'parent_net_profit': ['归母净利润', '归属于母公司所有者的净利润'],
}


def _parent_net_margin(parent_net_profit_yi, revenue_yi):
    """归母净利率（%）= 归母净利润 ÷ 营业总收入，两值均为「亿元」。

    为什么要自算而不直接用数据源自带的「销售净利率 / 净利率」：
      各源口径不一致——东财「销售净利率」与同花顺「销售净利率」是**含少数股东损益**口径；
      港股/美股接口的 NET_PROFIT_RATIO 亦无法与其 HOLDER_PROFIT / OPERATE_INCOME 绝对额对齐。
    统一用两个绝对额相除，才能保证跨市场口径一致、可核验。
    任一值为 nan、或营收为 0 → 返回 nan（不猜）。
    """
    import math
    def bad(v):
        return v is None or (isinstance(v, float) and math.isnan(v))
    if bad(parent_net_profit_yi) or bad(revenue_yi):
        return float('nan')
    try:
        if not float(revenue_yi):
            return float('nan')
        return float(parent_net_profit_yi) / float(revenue_yi) * 100.0
    except Exception:
        return float('nan')

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
                # 港股：HOLDER_PROFIT = 股东应占溢利 = 归母净利润（2026-09-27 核实）
                'net_profit': num('HOLDER_PROFIT') / 1e8 if row.get('HOLDER_PROFIT') else float('nan'),
                'cash_flow': num('PER_NETCASH_OPERATE'),  # 每股经营现金流
                # ── 2026-09-27 新增：归母净利率门槛口径（OPERATE_INCOME=营业总收入）──
                'revenue': num('OPERATE_INCOME') / 1e8 if row.get('OPERATE_INCOME') else float('nan'),
                'parent_net_profit': num('HOLDER_PROFIT') / 1e8 if row.get('HOLDER_PROFIT') else float('nan'),
            }
            entry['parent_net_margin'] = _parent_net_margin(entry.get('parent_net_profit'), entry.get('revenue'))
            result.append(entry)
        return result
    except Exception as e:
        print(f"  ⚠️ 港股财报获取失败: {e}")
        return []


def fetch_financials_us(code):
    """美股财务核心指标（东方财富分析指标 + 现金流量表），近 FIN_YEARS 年"""
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
        # 经营现金流（元）：美股分析指标接口无该字段，改用东财美股现金流量表补充
        ocf_by_year = {}
        try:
            cf_df = ak.stock_financial_us_report_em(stock=code, symbol='现金流量表', indicator='年报')
            ocf = cf_df[cf_df['ITEM_NAME'] == '经营活动产生的现金流量净额']
            for _, r in ocf.iterrows():
                y = str(r.get('REPORT_DATE', ''))[:4]
                v = r.get('AMOUNT')
                if y.isdigit() and v is not None and not (isinstance(v, float) and math.isnan(v)):
                    ocf_by_year[y] = float(v)
        except Exception as e:
            print(f"  ⚠️ 美股现金流量表获取失败，经营现金流留空: {e}")
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
                # 经营现金流：美股现金流量表「经营活动产生的现金流量净额」（元→亿）
                'cash_flow': ocf_by_year[year] / 1e8 if year in ocf_by_year else float('nan'),
                # ── 2026-09-27 新增：归母净利率门槛口径（PARENT_HOLDER_NETPROFIT=归母净利润）──
                'revenue': num('OPERATE_INCOME') / 1e8 if row.get('OPERATE_INCOME') else float('nan'),
                'parent_net_profit': num('PARENT_HOLDER_NETPROFIT') / 1e8 if row.get('PARENT_HOLDER_NETPROFIT') else float('nan'),
            }
            entry['parent_net_margin'] = _parent_net_margin(entry.get('parent_net_profit'), entry.get('revenue'))
            result.append(entry)
        return result
    except Exception as e:
        print(f"  ⚠️ 美股财报获取失败: {e}")
        return []


def _parse_ths_num(s):
    """解析同花顺字符串数值（'5679.18万'/'4.52亿'/'23.24%'/'0.42'）→ float。

    口径对齐主接口：百分比字段（ROE/毛利率/净利率/负债率）返回百分比数值（23.24%→23.24），
    与新浪接口的 ROE(%) 等列一致；绝对字段（净利润/每股经营现金流）返回元。"""
    if s is None:
        return float('nan')
    s = str(s).strip()
    if s in ('', 'nan', 'None', '-', '--'):
        return float('nan')
    if s.endswith('%'):
        s = s[:-1]
        mult = 1.0  # 百分比保留数值本身（23.24%→23.24），与主接口 ROE(%) 列口径一致
    elif s.endswith('万亿'):
        mult = 1e12
        s = s[:-2]
    elif s.endswith('亿'):
        mult = 1e8
        s = s[:-1]
    elif s.endswith('万'):
        mult = 1e4
        s = s[:-1]
    else:
        mult = 1.0
    try:
        return float(s) * mult
    except Exception:
        return float('nan')


def fetch_financials_a_ths(code):
    """A股财务降级（同花顺 stock_financial_abstract_ths，北交所/主接口失败时兜底）。

    输出与 fetch_financials_a 一致的 entry 结构（年份/roe/roa/gross_margin/net_margin/
    net_profit/cash_flow/debt_ratio），避免旧实现直接返回原始 dict 导致
    CSV 字段全部对不上、经营现金流等指标「数据未覆盖」为空。"""
    fin_df = ak.stock_financial_abstract_ths(symbol=code)
    fin_df = fin_df[fin_df['报告期'].str.contains('12-31', na=False)].tail(FIN_YEARS)
    result = []
    for _, row in fin_df.iterrows():
        year = str(row.get('报告期', ''))[:4]
        if not year.isdigit():
            continue
        entry = {
            '年份': year,
            'roe': _parse_ths_num(row.get('净资产收益率')),
            'gross_margin': _parse_ths_num(row.get('销售毛利率')),
            # ⚠️ 同花顺「销售净利率」为**含少数股东损益**口径（2026-09-27 实测 600132 2025H1：
            #    该列 19.55%，归母口径实为 9.79%）；而「净利润」列是**归母**净利润（8.65 亿）。
            #    同源两列口径不一致 → 归母净利率一律自算，不得用该列。
            'net_margin': _parse_ths_num(row.get('销售净利率')),
            'debt_ratio': _parse_ths_num(row.get('资产负债率')),
            # 同花顺关键指标无经营现金流总额，用每股经营现金流（元/股）兜底，语义与港股/美股分支一致
            'cash_flow': _parse_ths_num(row.get('每股经营现金流')),
            'net_profit': _parse_ths_num(row.get('净利润')) / 1e8,  # 元 → 亿（同花顺该列为归母）
            'roa': float('nan'),  # 同花顺关键指标无 ROA，留空（主接口正常时不受影响）
            # ── 2026-09-27 新增：归母净利率门槛口径 ──
            'revenue': _parse_ths_num(row.get('营业总收入')) / 1e8,        # 元 → 亿
            'parent_net_profit': _parse_ths_num(row.get('净利润')) / 1e8,  # 元 → 亿（归母）
        }
        entry['parent_net_margin'] = _parent_net_margin(entry.get('parent_net_profit'), entry.get('revenue'))
        result.append(entry)
    return result


def _is_bj(code):
    """北交所代码：4/8 开头，以及 920 新段。

    🔴 2026-10-26 代码审查 C-1：换源删旧数据线时，本函数被连带删除，
       但 `_a_em_prefix` 仍在调用 → NameError → 被 fetch_financials_a 的
       `except Exception: pass` 吞掉 → 所有非 6 开头 A 股（占一半以上）
       的「现金占总资产(%)」字段永久为空，而这是企业质量门槛要用的字段。
    """
    return str(code).startswith(('4', '8', '92'))


def _a_em_prefix(code):
    """东方财富代码前缀：6→SH(沪)、0/3→SZ(深)、北交所(4/8/92)→BJ"""
    if code.startswith('6'):
        return 'SH'
    if _is_bj(code):
        return 'BJ'
    return 'SZ'


def fetch_cash_ratio_a(code):
    """A股/北交所「现金占总资产」= 货币资金/总资产（东方财富资产负债表，年报）。
    返回 {年份: 百分比}。HOLDLE 企业质量门槛：≥10%合格，≥20%优秀。"""
    if ak is None:
        return {}
    try:
        import math
        symbol = f"{_a_em_prefix(code)}{code}"
        df = ak.stock_balance_sheet_by_report_em(symbol=symbol)
        if df is None or df.empty:
            return {}
        if not ({"MONETARYFUNDS", "TOTAL_ASSETS"} <= set(df.columns)):
            return {}
        df = df.copy()
        df['REPORT_DATE'] = df['REPORT_DATE'].astype(str)
        df['年'] = df['REPORT_DATE'].str[:4]
        # 只取年报（12-31），每年保留最新一条
        df = df[df['REPORT_DATE'].str.contains('12-31', na=False)]
        df = df.sort_values('REPORT_DATE').drop_duplicates(subset=['年'], keep='last')
        result = {}
        for _, row in df.iterrows():
            money, total = row.get('MONETARYFUNDS'), row.get('TOTAL_ASSETS')
            try:
                money_f, total_f = float(money), float(total)
            except Exception:
                continue
            if math.isnan(money_f) or math.isnan(total_f) or total_f == 0:
                continue
            result[row['年']] = round(money_f / total_f * 100, 2)
        return result
    except Exception as e:
        print(f"  ⚠️ 现金占总资产获取失败: {e}")
        return {}


def fetch_financials_a(code):
    """A股财务核心指标（新浪 stock_financial_abstract，近 FIN_YEARS 年；失败降级同花顺）
    额外从东方财富资产负债表补充「现金占总资产」（cash_ratio，货币资金/总资产）。"""
    if ak is None:
        print("  ⚠️ akshare 未安装（pip3 install akshare），跳过财报")
        return []
    try:
        df = ak.stock_financial_abstract(symbol=code)
        date_cols = [c for c in df.columns if c not in ['选项', '指标']]
        year_cols = [c for c in date_cols if c.endswith('1231') and len(c) == 8 and c[:4].isdigit()]
        year_cols.sort()
        indicators = {}
        for key, labels in EM_INDICATORS.items():
            for label in labels:
                row = df[df['指标'] == label]
                if not row.empty:
                    indicators[key] = row.iloc[0]
                    break
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
                if key in ('net_profit', 'cash_flow', 'revenue', 'parent_net_profit') and isinstance(val, (int, float)) and not (isinstance(val, float) and math.isnan(val)):
                    val = val / 1e8
                entry[key] = val
            # 归母净利率（%）：两个绝对额自算（HOLDLE 门槛口径，见 _parent_net_margin 说明）
            entry['parent_net_margin'] = _parent_net_margin(entry.get('parent_net_profit'), entry.get('revenue'))
            result.append(entry)
        # 补充现金占总资产（东方财富资产负债表，货币资金/总资产）
        try:
            cr = fetch_cash_ratio_a(code)
            for e in result:
                e['cash_ratio'] = cr.get(e['年份'], float('nan'))
        except Exception:
            pass
        return result
    except Exception as e:
        print(f"  ⚠️ 新浪财报获取失败，降级同花顺: {e}")
        try:
            fin_ths = fetch_financials_a_ths(code)
            cr = fetch_cash_ratio_a(code)
            for e in fin_ths:
                e['cash_ratio'] = cr.get(e['年份'], float('nan'))
            return fin_ths
        except Exception:
            return []



# ═══════════════════════════════════════════════════
#  行业查询（SA-06 金融类判定用，在线获取，不背静态名单）
# ═══════════════════════════════════════════════════
def _clean_industry(v):
    """行业字段清洗：空值/垃圾值一律视为**没查到**。

    🔴 2026-10-26 导师终审 C-2 + 对抗性审查同报：
       原实现 A 股分支直接 `return str(...).strip(), True`，没有清洗 →
       AkShare 返回 'nan'/'None'/''/'—' 时，会变成「查到了，行业叫 nan」，
       词表匹配不到 → **静默按非金融放行** → 平安被判成普通股给出状态A结论。
       这正是 v1.0.8 被大回的 P0 同型复发。
    """
    if v is None:
        return None
    s2 = str(v).strip()
    if not s2 or s2.lower() in ('nan', 'none', 'null', '-', '--', 'n/a'):
        return None
    return s2


def _lookup_industry(code, market='a'):
    """查该股所属行业 → (industry 或 None, 状态)。

    🔴 2026-10-26 修正（导师终审 C-2 + 对抗性审查）：返回值改成三态字符串，
       因为原先的布尔「是否查到」把三件不同的事混成了一件：

       ('银行',  'found')     查到且是金融
       ('软件',  'found')     查到且非金融 → 正常判定
       (None,    'empty')     **查了，但接口正常返回、只是这只股行业字段是空的**
                              → 无法确认 → 拒判（风险方向必须保守）
       (None,    'nodep')     **环境缺依赖**（akshare 未安装）→ 不是"无法确认"，
                              是"这次没做校验" → 照常判定 + 显式告警
                              （知识库 judge_service 也是这么处理：读表失败=放行+告警）
       (None,    'error')     查的过程中抛异常（网络/接口改版）→ 照常判定 + 告警

       ⚠️ 'nodep' / 'error' 为什么不再拒判：
          akshare 是**可选依赖**，用户没装是常态（要装十几个包）。
          若因此对所有股票拒判，等于「判定功能 100% 不可用、取数却照常成功」，
          用户会以为工具坏了。而金融股被误判的风险，由显式告警 + SKILL.md 说明来兜。
    """
    try:
        import akshare as ak
    except Exception:
        return None, 'nodep'          # 缺依赖 ≠ 无法确认
    try:
        if market == 'a':
            df = ak.stock_individual_info_em(symbol=code)
            if df is not None and not df.empty and 'item' in df.columns:
                row = df[df['item'].astype(str).str.contains('行业', na=False)]
                if not row.empty:
                    v = _clean_industry(row.iloc[0]['value'])
                    if v:
                        return v, 'found'
                    return None, 'empty'      # 字段在，但值是空/垃圾
            return None, 'empty'
        # 港股/美股
        if market == 'hk':
            df = ak.stock_hk_company_profile_em(symbol=code)
        else:
            df = ak.stock_us_company_profile_em(symbol=code)
        if df is not None and not df.empty:
            for col in ('所属行业', '行业', 'industry'):
                if col in df.columns:
                    v = _clean_industry(df.iloc[0][col])
                    if v:
                        return v, 'found'
        return None, 'empty'
    except Exception:
        return None, 'error'


# ═══════════════════════════════════════════════════
#  主流程（datasource 统一取数 → judge 判定）
# ═══════════════════════════════════════════════════
def main():
    if len(sys.argv) < 2:
        print("用法: python3 holdle_data.py <股票代码> [输出目录] [--adjust forward|backward]")
        print("  A股示例:  600519  603259  000858")
        print("  美股示例:  NVDA    AAPL    MSFT")
        print("  港股示例:  hk_00700  hk_09988")
        print("  --adjust: forward=前复权(默认，HOLDLE 判定统一口径) backward=后复权(仅作历史数据对照，不参与判定)")
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
    # datasource 需要的代码形态：A股 600519.SH / 港股 00700.HK / 美股 NVDA
    if market == 'hk':
        _sym = '%05d.HK' % int(re.sub(r'\D', '', code) or 0) if re.sub(r'\D', '', code) else code
    elif market == 'a':
        _pre = 'SH' if code.startswith(('6', '5', '9')) else ('BJ' if code.startswith(('8', '4')) else 'SZ')
        _sym = '%s.%s' % (code, _pre)
    else:
        _sym = code.upper()
    if _ds is None:
        print('  ❌ 缺少取数模块 datasource.py（或其依赖 requests/pyarrow）')
        print('     %s' % globals().get('_DS_IMPORT_ERROR', ''))
        print('     请重新运行 --auto-update，或 git pull 后重试。')
        sys.exit(2)
    out_dir = args[1].strip() if len(args) > 1 else os.path.join(os.getcwd(), 'holdle_data')
    os.makedirs(out_dir, exist_ok=True)

    adj = ADJUST_MAP[adjust]
    adj_label = adj["label"]

    now = datetime.now()
    market_label = {'a': 'A股', 'us': '美股', 'hk': '港股'}.get(market, '未知')

    print("=" * 70)
    print(f"HOLDLE 行情数据获取（用户版 v0.4）")
    print(f"标的: {code}（{market_label}）  |  {now.strftime('%Y-%m-%d %H:%M')}")
    print(f"复权口径: {adj_label}（{adjust}）")
    print("数据源: 新浪（A股）/ Yahoo（港美股）原始价 + 复权因子现算 · AkShare 财报 · 腾讯/新浪 实时")
    print(f"输出目录: {out_dir}")
    print("=" * 70)

    # 版本自检（v1.0.4 起：每次运行强制检查，非最新则先自动更新+重启，再继续拉数据；
    # 安全校验（身份标记+备份）保留；失败静默继续不阻塞拉取）
    _check_update(force=True, auto=True)

    name = code

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
        want = ['年份', 'roe', 'roa', 'gross_margin', 'parent_net_margin', 'net_margin',
                'parent_net_profit', 'net_profit', 'revenue', 'cash_flow', 'cash_ratio', 'debt_ratio']
        showf = fdf[[c for c in want if c in fdf.columns]].copy()
        showf = showf.rename(columns={
            'roe': 'ROE(%)', 'roa': 'ROA(%)', 'gross_margin': '毛利率(%)',
            # ⭐ 归母净利率 = HOLDLE 净利率门槛口径；「净利率(含少数)」仅作对照，不得用于门槛判定
            'parent_net_margin': '归母净利率(%)⭐',
            'net_margin': '净利率(含少数)(%)',
            'parent_net_profit': '归母净利润(亿)', 'net_profit': '净利润(含少数)(亿)',
            'revenue': '营业总收入(亿)',
            'cash_flow': '经营现金流(亿)',
            'cash_ratio': '现金占总资产(%)', 'debt_ratio': '负债率(%)'
        })
        showf.to_csv(f"{out_dir}/{code}_财务_近20年.csv", index=False)
        print(f"  ✅ 已存 {code}_财务_近20年.csv")
        print("\n" + showf.to_string(index=False))
    else:
        print("  ❌ 财报获取失败")

    # ── 2/3/4. K线（月/周/日）—— 统一走 datasource（回测试验同源取数方式） ─
    # 🔴 2026-10-26 换源（老板确认：不需要两套）：原先 TickFlow 主用 → Baostock → 新浪兜底，
    #    现统一为「新浪(A股) / Yahoo(港美股) 原始价 + 复权因子现算」，与回测试验、底库完全同源。
    #    收益：复权可复算、口径与回测逐位一致、无 key 依赖；代价：不设二级源，
    #    失败即明确报错（不再用另一个源的数据顶替——那会让口径悄悄变化）。
    print(f"\n[2/4] K线（{adj_label}）...")
    try:
        daily_full, meta = _ds_load_daily(_sym, adjust=adjust)
    except _ds.DataUnavailable as e:
        print(f"  ❌ 行情数据获取失败：{e}")
        print("     按 HOLDLE 口径，此时不做判定、也不用旧数据顶替。")
        print("     请检查网络后重试；若持续失败请反馈给 HOLDLE。")
        daily_full = None
        meta = None

    if daily_full is not None:
        print(f"  ✅ 日线 {daily_full['date'].min().date()} ~ {daily_full['date'].max().date()}"
              f"，共 {len(daily_full)} 根（源={meta.source}，缓存={meta.from_cache}）")
        if meta.is_stale:
            print(f"  ⚠️ 数据截至 {meta.trade_date}，距今天已 {meta.stale_days} 天")
        if meta.is_partial and meta.partial_note:
            print(f"  ⚠️ {meta.partial_note}")

        for _freq, _label, _fname in (("M", "月K", "月K"), ("W", "周K", "周K"), ("D", "日K", "日K")):
            _bars = _ds_to_bars(daily_full, _freq)
            _bars.to_csv(f"{out_dir}/{code}_{_fname}_{adj_label}.csv", index=False)
            print(f"  💾 已存 {code}_{_fname}_{adj_label}.csv（{len(_bars)} 根，含 DIF/DEA/MACD柱）")
        # 终端展示最近 12 根月K
        _show = _ds_to_bars(daily_full, "M").tail(12)[["date", "close", "DIF", "DEA", "MACD柱"]].copy()
        _show.columns = ["日期", "收盘", "DIF", "DEA", "柱"]
        print("\n=== 月K最近12根（含MACD指标）===")
        print(_show.to_string(index=False))

    # ── 判定（2026-10-26 新增）：状态A/开窗/入场参考价由脚本算，模型只转述 ──
    # 🔴 只在"现状判断"（前复权）时做判定：判定口径是前复权（裁决#1），
    #    后复权序列只用于历史复盘，不能拿来判状态A。
    # 🔴 必须喂**未取整**的日线原始价，不能读回 CSV（CSV 里 MACD 已 round(2)，
    #    会让贴0轴 |柱|/|DEA|<0.03 的临界月翻转，实测 167 个月里 4 个月落在临界带）。
    _judge_ok = False
    _has_daily = daily_full is not None and not daily_full.empty
    if adjust == "forward" and _has_daily:
        try:
            import judge as _judge
            # 金融类(SA-06)行业：在线查询（查不到传 None → 按非金融正常判定）
            _industry, _fin_st = _lookup_industry(code, market)
            # found → 已确认（金融则拒判）；empty → 无法确认（拒判）；
            # nodep/error → 未做校验（照常判定 + 告警，不因缺依赖废掉判定功能）
            if _fin_st in ('nodep', 'error'):
                print(f"  ⚠️ 本次未做金融类（SA-06）校验："
                      f"{'akshare 未安装' if _fin_st == 'nodep' else '行业接口异常'}；"
                      f"判定结果对金融股可能不适用（金融类按方法论不参与判定）。")
            _j = _judge.judge(daily_full, code=code, market=market,
                              stock_name=None, industry=_industry,
                              finance_checked=(_fin_st in ('found',)),
                              finance_status=_fin_st)
            _jpath = f"{out_dir}/{code}_判定.json"
            _judge.dump(_j, _jpath)
            print("\n" + "=" * 70)
            print("📋 HOLDLE 方法论判定（脚本确定性计算，非模型推理）")
            print("=" * 70)
            print(_j["required_statement"]["final_text"])
            if _j.get("state_a"):
                _sa = _j["state_a"]
                print(f"\n  [结构化] 状态={_sa['status']}  判定月={_sa['month']}  "
                      f"本月已收完={_sa['month_closed']}")
                _w = _j.get("window") or {}
                if _w.get("event") and _w["event"] != "none":
                    print(f"  [结构化] 开窗={_w['event']}@{_w['event_month']}  "
                          f"存活={_w['alive']['is_alive']}")
                _e = _j.get("entry") or {}
                if _e.get("ref_price"):
                    print(f"  [结构化] 入场参考价={_e['ref_price']}（{_e['status']}）")
            print(f"  💾 已存 {code}_判定.json（模型只能转述 final_text，不得自算）")
            print("=" * 70)
            _judge_ok = True
        except Exception as _e:
            print(f"\n  ⚠️ 判定未生成（{_e}）；行情数据不受影响，仍可正常使用。")
            print("     若持续失败请 --auto-update 更新脚本或反馈给 HOLDLE。")
    elif adjust != "forward":
        print("\n  ℹ️ 本次为后复权——仅作历史数据对照，不参与 HOLDLE 判定。")
        print("     HOLDLE 判定与价格对账一律用前复权，请改用默认参数或显式传 --adjust forward")

    print("\n" + "=" * 70)
    print(f"获取完成 ✅ 数据已保存到: {out_dir}")
    print(f"复权口径：{adj_label}。")
    # 🔴 判定状态必须显式说清，不能含糊成「如有」——否则模型会以为有判定而自行发挥
    if adjust == "forward" and _has_daily:
        if _judge_ok:
            print(f"判定：已生成（模型只能转述 {code}_判定.json 的 final_text，不得自算）")
        else:
            print("判定：❌ 本次未生成（原因见上方告警）。请勿用你的推理替代判定结果，")
            print("      应告知用户「本次未能生成方法论判定」，并按 fallback_unknown 话术回应。")
    elif adjust != "forward":
        print("判定：本次未做（后复权模式不做判定，判定口径固定前复权）。")
    print("=" * 70)


if __name__ == "__main__":
    main()
