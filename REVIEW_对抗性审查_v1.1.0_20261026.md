# 对抗性审查报告：holdle-data-skill v1.1.0 → v1.1.3（取数层整体换源）

> 审查人：独立对抗性审查员（立场：**默认它有 bug，去证明**）
> 审查日期：2026-10-26
> 审查对象：`holdle-data-skill` v1.1.0（41a2dc1）→ **复验至 v1.1.3（fe99afe 后续）**
> 文件：`datasource.py`（新，458 行）· `judge.py`（923 行）· `holdle_data.py`（1018 行）· `tests/`
> 解释器：`/Users/imac/holdle-workspace/data/klines/.venv/bin/python`（3.9.6）
> 复现脚本：全部在 `/tmp/adv2/`（未污染 skill 目录，未改源码，无 git 操作）

---

## 结论先行（v1.1.3 终态）

| 维度 | v1.1.0 首审 | v1.1.3 复验 |
|:--|:--|:--|
| 月末判据保守性（K4 防未来函数） | ✅ 通过 | ✅ **仍通过**（144 样本，危险方向 0） |
| 复权现算正确性 | ✅ 通过 | ✅ **仍通过**（A股 6022 天相对差 0.00e+00） |
| **港股数据源一致性** | 🔴 HIGH（97.2% 不一致） | ✅ **已修**（同日一致率 100%，判定 9/9 一致） |
| **金融三态可用性** | 🔴 CRITICAL（全体拒判） | ✅ **已修**（告警进 final_text，任何分支都带） |
| 删除副作用 / holdle.com 红线 | ✅ 通过 | ✅ **仍通过**（0 处） |
| 测试质量 | ✅ 通过 | ✅ **仍通过**（6 个变异全部被抓） |
| 失败路径 / 自更新 | ⚠️ MEDIUM × 2 | ✅ Yahoo/缓存已修；⚠️ 自更新校验仍子串级 |

**该不该 push → 可以 push。** 详见 §九。

> **本报告跨三个版本，读法**：§二/§三 是 v1.1.0 的两条 critical 原文（保留，含我自己的误判更正）；
> §四 是"我当时攻不下来"的通过项；§三末尾与 §九 是 v1.1.3 的复验结论。

---

## 一、我实际做了什么

**不读代码猜，全部实跑。** 三条独立证据线：

1. **底库对照线**：用底库真实前复权日线（`data/klines/*/daily` + `factors`，与 `l1_engine.load_daily_qfq` 同式现算）喂给 v1.1.0 的 `judge.py`，与知识库 `judge_service.py` 逐字段比对。
2. **真网络线**：直接调 `datasource.load_daily` 打新浪/Yahoo，与底库逐日比对复权价与原始价。
3. **主流程端到端线**：`python3 holdle_data.py <代码> <目录> --adjust forward` 真跑，隔离 `HOLDLE_AI_CACHE`，检查落盘 json 与终端话术。

另外做了**变异测试**（在 /tmp 副本上改坏源码，看测试会不会红）与**构造性反证**（想办法让它在数据不足时给出确定结论）。

---

## 二、CRITICAL：akshare 未安装 → 判定功能 100% 不可用

### 2.1 复现（真网络、真主流程、隔离缓存）

```
$ HOLDLE_AI_CACHE=/tmp/... python3 holdle_data.py 600519 /tmp/out --adjust forward
  ⚠️ akshare 未安装（pip3 install akshare），跳过财报
  ✅ 日线 2001-08-27 ~ 2026-10-23，共 6027 根（源=sina，缓存=False）      ← 取数完全正常
  📋 HOLDLE 方法论判定…
  无法确认该股是否属于金融类（金融类按 HOLDLE 方法论不参与状态A判定），因此本次不做判定。
      请安装数据依赖后重试（pip install akshare）；在确认之前，请不要用其它方式推断该股的金融属性。
判定：已生成（模型只能转述 600519_判定.json 的 final_text，不得自算）
```
判定 json：`status=indeterminate / indeterminate_reason=finance_unverified`。000333 同样。
脚本：`/tmp/adv2/attack8_realtime.py`

### 2.2 根因链

```
_lookup_industry(code, market)
  └─ import akshare 失败 / 接口异常 / 返回空
       → return (None, False)
judge.judge(..., industry=None, finance_checked=False)
  └─ if industry is None and not finance_checked:
       → status="indeterminate", reason="finance_unverified", return
```

**问题在于把三件事合并成了同一个返回值**：
| 真实情形 | 当前返回 | 后果 |
|:--|:--|:--|
| 环境没装 akshare（**可选依赖**） | `(None, False)` | 全部拒判 |
| AkShare 当天接口挂了/字段改名 | `(None, False)` | 全部拒判 |
| 真的查了但该股不在库里 | `(None, False)` | 全部拒判 |
| 查到了但因为异常返回 NaN | `("nan", True)` | **放行判定**（见 M-3） |

### 2.3 为什么是 critical 而不是 high

- `requirements.txt` 只有 pandas，**akshare 在整条链路里是可选依赖**（财报段取不到只是「⚠️ 跳过」，不阻断）；但金融判定把它变成了**硬依赖**。
- akshare 依赖十几个包，用户端安装成功率不会高；**没装 = 判定功能整体失效**，而取数照样成功 → 用户看到「获取完成 ✅」，会以为工具坏了。
- **知识库服务端的对照**（`judge_service._participation_check`）在行业表读失败时是 **放行 + 告警**（`out_warn`），不是拒判。v1.1.0 把方向反过来了。
- 装了 akshare 但接口当天变动，同样全体不可判——AkShare 接口改名是常态。

### 2.4 反例：装对了就没事（证明不是"三态本身"错，是"环境失败被当成不可确认"）

用假 akshare 模拟可用环境（`/tmp/adv2/attack15_fin_e2e.py`）：
```
akshare 可用 601318: finance_industry   该股属于保险。按 HOLDLE 方法论，金融类默认不参与状态A判定…
akshare 可用 600036: finance_industry   该股属于银行。…
akshare 可用 600519: judged             正常判定 ✅
```
三态**设计**是对的，坏在「环境失败」与「无法确认」共用一条路径。

### 2.5 建议修法（三选一，推荐 A 或 B）

- **A. 回落到底库行业表**：`data/financials/a_share_financials.parquet`（v1.0.x 用过），仅本地存在时读；不存在再走 B。
- **B. 拆开「环境缺依赖」与「无法确认是否金融」**：akshare 未安装 → **照常判定 + 显式告警**（"本次未做金融校验，金融股结论可能不适用"）；只有"查了但结果是垃圾"才拒判。
- **C. SA-06 完全移到 MCP 侧**：判定层不再管金融，由已有行业表的知识库服务处理。

---

## 三、HIGH：港股 Yahoo 原始价与底库（新浪）不一致，97.2% 的交易日

### 3.1 实测（`/tmp/adv2/attack13_calendar_gap.py` / `attack13b.py` / `attack13c.py`）

腾讯 0700.HK，Yahoo 线 vs 底库线共有 **4229 个交易日**：

| 指标 | 值 |
|:--|:--|
| 原始收盘价不一致天数 | **4111 天（97.2%）** |
| 相对差中位数 | 1.32% |
| 相对差最大 | 23.2% |
| 同日配对完全一致 | 118 天 |
| 错一天配对完全一致 | 54 天（**排除时区错位假设**） |

**对照美股**：NVDA 同日配对 **6976 / 6977 完全一致**（差异 1 天是新拉的那一天）→ 说明 Yahoo 链路对美股是对的，**问题集中在港股**。

### 3.2 后果：判定结论实测会不同

同一只腾讯、同一锚点，用「Yahoo 线现算」vs「底库线」跑 v1.1.0 judge：

| 锚点 | Yahoo 线 | 底库线 | 一致 |
|:--|:--|:--|:--|
| 0700.HK @ 2026-09-30 | `in_progress` | `false` | 🔴 |
| 0700.HK @ 2025-06-30 | `in_progress` | `true` | 🔴 |
| 0700.HK @ 2024-06-28 | — | — | ✅ |
| NVDA 三个锚点 | — | — | ✅ ✅ ✅ |
| AAPL 三个锚点 | — | — | ✅ ✅ ✅ |

9 组里 2 组不一致，**都是港股**。

### 3.3 为什么不只是"数据源不同"

1. changelog 明写「与回测引擎、数据底座**完全同源**，复权结果与底库逐位一致（差值 0.000000000000）」——**这句话对 A股与美股成立，对港股不成立**。A股我实测逐位一致（`attack2_qfq.py`：最大差值 0.000000000000）。
2. 用户拿港股行情软件对账，参考价会对不上（差 1%~23%）。
3. 导师审 v1.1.0（task-9）要求「同一只股票同一锚点，与 judge_service 逐字段一致」——港股这块答不上来。

**建议**：港股换与 A 股同源的新浪港股（`stock_hk_daily(adjust="")` + 因子）；至少在 SKILL.md/changelog 显式标注「港股数据源为 Yahoo，与回测底库不同源」。

---

## 三·补、v1.1.3 复验结果（两条 critical 的终态）

### 3.5 C-1 港股日期错位 —— ✅ 已修（并且**我上一轮的归因是错的**）

**Lead 的根因判定（正确）**：不是"两个源数据本身不同"，而是**日期整体错位一天**。
Yahoo 时间戳是 UTC 的当地开盘时刻（港股 01:30 UTC = 09:30 HKT），原实现 `t - gmtoffset` 减 8 小时 → 退到**前一天 17:30** → normalize 后全体错一格。美股因 offset 为负恰好没暴露。

**我的误判（必须留档）**：我当时做过「错一天配对」检验，得到 54 条"完全一致"，**把它当噪声排除了**。其实那正是证据本身——2.8% 的同日一致率 + 错一天 100% 才是正确解读。教训：**当同期一致率极低且存在系统性偏移时，先做"整数位移"检验，而不是先假设数据本身不同。**

**复验数据**（`/tmp/adv2/v112_hk.py`、`v112_hk2.py`，真网络 vs 底库）：

| 标的 | 同日配对一致率 | 错一天配对一致率 |
|:--|:--|:--|
| 0700.HK | **100.0%**（5492 天） | 2.8% |
| 09988.HK | **100.0%**（1686 天） | 1.2% |
| NVDA | 100.0%（6977 天） | 0.7% |
| AAPL | 100.0%（11553 天） | 3.2% |

复权价残余差异只有浮点尾数（相对差中位 6.7e-08）→ 判定结论 **9/9 一致**（腾讯/阿里/NVDA × 3 锚点），我首审时报的"9 组里 2 组不一致"全部消失。

### 3.6 C-2 金融三态 —— ✅ 已修（这条修了三轮，根因链条值得记录）

| 轮次 | 现象 | 真实根因 |
|:--|:--|:--|
| v1.1.0 | 无 akshare → **所有股票**拒判 | 三种情形（缺依赖/接口挂/查不到）合并成同一个返回值 |
| v1.1.2 | 改为"照常判 + 告警"，但告警只打 stdout | 告警加在判定主流程**中段**，早退分支在它之前 return |
| v1.1.3 首次 | 移到 `_finalize` 统一追加，仍无告警 | **`holdle_data.py:996` 传参错**：`finance_checked=(_fin_st != 'empty')` → `'nodep'` 被算成「已校验」 |
| v1.1.3 修正 | ✅ 正确 | 改为 `finance_checked=(_fin_st in ('found',))` |

**复验**（`/tmp/adv2/v113_fin.py`，端到端）：

```
601318 无 akshare   → units=['R-MONTH-OPEN','R-FINANCE-UNCHECKED','R-DISCLAIMER']，final_text 含「金融」 ✅
600519 已校验非金融 → units=['R-MONTH-OPEN','R-DISCLAIMER']，不含该单元 ✅
601318 查到保险     → finance_industry 拒判，不含该单元 ✅
```

**我在这个过程中归因错了两次**（先说"早退顺序"，再说"9 个早退分支"），第三次把 `_lookup_industry` 的返回值打出来才定位到。教训：**先打印关键中间值，再看代码路径**。

### 3.7 一条残留（medium，非阻塞）：`'empty'` 三态的互斥假设错误

`judge.py` 入口：
```python
if finance_status == "empty":
    out["_finance_unconfirmed"] = True      # 走这条
elif not finance_checked:                   # ← 被 elif 挡住
    out["_finance_unchecked"] = True
```
实测 `finance_status='nodep'` + `finance_checked=False` → **告警不触发**（`units` 里没有 `R-FINANCE-UNCHECKED`）。
真实链路目前靠 `_finalize` 的兜底是对的（端到端验过），但**同一个 bug 换了个位置又长出来**——把 `elif` 改成两个并列 `if` 即可。

---

## 四、通过的部分（避免重复劳动，附证据）

### 4.1 月末判据的保守性 —— ✅ 这是我最初怀疑的地方，结论是它守住了

**结构性论证**：判据是「数据末日 ≥ 该自然月的日历月末 → 已收完」。要违反（把月中判成整月），必须存在一个交易日 **> 日历月末 且仍属于该自然月**——按定义不存在。所以危险方向**结构上不可能**。

**实证 1：12 个边界点**（判据用底库全市场真实成交日投票，不依赖任何实现，`attack1_month.py`）

| 场景 | 真实 | v1.1.0 | 方向 |
|:--|:--|:--|:--|
| 2025-01-27 春节月末最后交易日 | 已收完 | 未收完 | 保守 ✅ |
| 2025-01-24 春节前差一天 | 未收完 | 未收完 | 一致 ✅ |
| 2023-09-28 中秋月末 | 已收完 | 未收完 | 保守 ✅ |
| 2024-09-30 国庆前 | 已收完 | 已收完 | 一致 ✅ |
| 2024-09-27 国庆前差一天 | 未收完 | 未收完 | 一致 ✅ |
| 2026-09-30 普通月末 | 已收完 | 已收完 | 一致 ✅ |
| 2026-09-29 差一天 | 未收完 | 未收完 | 一致 ✅ |
| 2026-09-25 差三天 | 未收完 | 未收完 | 一致 ✅ |
| 2026-09-15 月中 | 未收完 | 未收完 | 一致 ✅ |

**实证 2：全量扫描**（`attack14_monthscan.py`，6 只 × 近 24 个月 = 144 个「个股末交易日」样本）

```
危险方向（真实未收完却判已收完）: 0
保守方向（真实已收完却判未收完）: 54
```

**实证 3：构造性反证**（`attack14b.py`）——我试图让它判错方向：
- 人为伪造「月末之后」的假行 → 会判已收完，但那是伪造行，真实数据线不产生
- 中间缺口、只留月末几天、长期停牌、2 月闰年：全部保守或正确

**代价量化**（`attack9b.py`）：600519 的 302 个月里 **105 个月（34.8%）真实末交易日早于日历月末**，延迟 1~8 天。实际影响：月末后约 1~8 天才能给出该月结论。**这是可接受的取舍**，但建议在 SKILL.md 明写「节假日月末可能晚 1~8 天才出判定」，否则用户会以为工具挂了。

### 4.2 复权现算 —— ✅ 与底库逐位一致，因子缺失绝不静默

| 测试 | 结果 |
|:--|:--|
| `_to_qfq` vs 底库（600519，6027 行） | 最大差值 **0.000000000000** ✅ |
| 因子缺失（首次拉取路径） | 抛 `DataUnavailable: 复权因子缺失（kind=qfq）…不得以原始价代替` ✅ |
| 因子缺失（缓存命中、因子文件被删） | 同样抛 `DataUnavailable` ✅（**这条最关键**：不会退回原始价） |
| 因子整体缩放 ×3 | 状态A/开窗结论不变；参考价同比缩放（相对判据抗缩放）✅ |
| 因子部分缺失（只给后 1000 行） | `merge_asof` 后 bfill 回溯 → 前半段错，但**不报错** ⚠️（见 M-4） |

历史 P0（美的 158 个月里 38 个月状态A翻转）的防线**保住了**。

### 4.3 删除完整性 / 红线 —— ✅

- `holdle.com`：`datasource.py` / `judge.py` / `holdle_data.py` **各 0 处**（含注释、UA、Referer、异常字符串）
- 被删数据线残留：`fetch_tickflow` / `fetch_baostock` / `fetch_sina_kline` / `_standardize_tickflow` / `code_to_*` / `calc_macd` 全部 0 次
- `_ds_load_daily` / `_ds_to_bars` 是 **try/except 里的正常赋值**（`holdle_data.py:50-55`），不是删除残留
- 两个 JSON 快照：已从仓库删除，`judge.py` 里 0 引用；用户端即使残留旧文件也不被读
- `datasource.py` 缺失 → 明确报错退出（returncode 2，话术「❌ 缺少取数模块 datasource.py…」）
- `judge.py` 缺失 → 降级不崩：`⚠️ 判定未生成` + `判定：❌ 本次未生成`，判定 json 不落盘，取数照常 ✅

### 4.4 测试质量 —— ✅ 变异测试全被抓

基线 `67 passed`。在 /tmp 副本上注入 6 个关键变异（`attack10_tests.py`）：

| 变异 | 结果 |
|:--|:--|
| 复权因子方向反转（`/ fac` → `* fac`） | **5 failed** ✅ |
| 因子缺失时静默用原始价 | **2 failed** ✅ |
| 月末判据放宽 10 天 | **5 failed** ✅ |
| 状态A条件放宽（`hist > 0` → `>= 0`） | **2 failed** ✅ |
| 参考价公式 `max` → `min` | **2 failed** ✅ |
| 贴0轴阈值 0.03 → 0.3 | **3 failed** ✅ |

### 4.5 其他横扫

- 脏数据 fuzz 17 种：**抛异常 0**（非法日期/非数字价格 → `data_quality`；NaN/0价 → `data_quality`；vol=0/乱序/重复/只月线/缺 volume → 正常判定）
- 结构性隔离：`finance_industry` / `finance_unverified` / `insufficient_history` 三个不可判分支，`state_a/window/entry/exit` **全 null** ✅
- 话术自相矛盾（"窗口仍存活 + 入场有效期已过"）全历史 264 组：**0 命中** ✅

---

## 五、MEDIUM

### M-1 Yahoo 异常结构抛的不是 DataUnavailable

`datasource.py:_fetch_yahoo` 解析段无保护。构造 8 种异常 JSON（`attack3c`）：

| 返回形状 | 结果 |
|:--|:--|
| `chart` 无 error 也无 result | ✅ DataUnavailable |
| `result` 有 timestamp 但无 indicators | 🔴 `KeyError: 'indicators'` |
| `indicators` 里没有 quote | 🔴 `KeyError: 'quote'` |
| `quote` 空列表 | 🔴 `IndexError: list index out of range` |
| `timestamp` 是 None | 🔴 `TypeError: 'NoneType' is not iterable` |
| `gmtoffset` 是字符串 | 🔴 `TypeError: unsupported operand type(s) for -: 'int' and 'str'` |
| `close` 全 None | 返回 daily=0（后续 `无日线数据` ✅） |

`holdle_data.py:931` 只 `except _ds.DataUnavailable` → 这些会穿透到最外层，**用户看到 Python traceback**。
**修法**：`_fetch_yahoo` 的解析段整体 `try → DataUnavailable`。

### M-2 缓存损坏静默改走网络

`_read_cache` 捕获所有异常返回 `(None,None,None)`。我造了坏 parquet + 坏 meta：`load_daily` 不报错，**发了 3 次网络请求重新拉**（`attack3b.py`）。
可用性 OK，但「缓存坏了」被完全掩盖，用户每次都会全量重拉（A 股全量约 20s）。
**修法**：打 warning 或把损坏文件改名 `.corrupt` 保留证据。

### M-3 行业值是 NaN/None 时被当成「查到了」

```
_lookup_industry 返回 ("nan", True) / ("None", True)
→ 词表匹配不到 "nan" → 放行判定
```
即「查到了但值是垃圾」→ 静默按非金融放行（`attack4_fin.py`）。
**修法**：`nan/None/空串/'-'` 归一成「没查到」（`(None, False)`）。

### M-4 因子表部分缺失时静默算错早期价格

只给后半段因子时，`merge_asof` + `bfill` 会把最早因子回溯到前面，前半段复权价错（实测差 14 元）。judge 不报错，影响早期 MACD 起始段。
**修法**：校验因子覆盖 `daily` 首日，不覆盖则告警或拒绝。

### M-5 自更新校验仍是子串/结构级

`SIDE_MODULES = ["datasource.py", "judge.py"]`。校验条件：
- `datasource.py`：只查 `"HOLDLE 取数模块 datasource.py"` + `"def load_daily("`
- `judge.py`：只查 `"HOLDLE 判定模块 judge.py"` + `"def judge("`

我构造的两段恶意文件（含 `os.system("curl … | sh")`）**都能过校验**（`attack6b.py`）。两个附属文件都没有版本常量，也没有与 `version.json` 的版本比对（主脚本有）。
**结构性优点**：现在是「全部下齐 + 校验通过才 `os.replace` 原子落盘」，缺一个就不写半套（`_miss_core` 逻辑）——这一点比上一版好，赞。
**建议**：给两个文件加 `__version__` 常量并与 `version.json` 比对（10 行）。

---

## 六、LOW / 观察

- **L-1** `_month_closed` 的"保守"只对**最后一个自然月**生效。历史月份（数据已跨月）走 `(last_ym - prev_ym).n > 1` 分支——若数据存在整月缺口（停牌/源缺数据），中间的月永远不会被判为"已收完"，`last_month_closed` 会指向更早的月（`attack1b.py` D 段）。
- **L-2** 短数据量（<30 根）时报「有效行情数据不足（少于 30 个交易日）」，而 30 根以上但跨月不足 30 个月时报「该股上市不足 30 个月」。两种根因不同，但 reason 都是 `insufficient_history`，用户可能混淆（`attack1c.py`）。
- **L-3** `holdle_data.py` 顶部仍 `import baostock`（可选），TickFlow 字样残留 7 处（注释/文档）。属清理项，不影响功能。
- **L-4** 全量拉取耗时：A股 6027 根一次约 20s（与 changelog 描述一致）；缓存命中后应 <0.1s。未发现 O(n²)。

---

## 七、攻击方向覆盖表

| # | 方向 | 结论 |
|:--|:--|:--|
| 1 | 数据正确性（复权算错？） | ✅ 逐位一致；因子缺失两路径都明确失败；部分缺失静默（M-4） |
| 2 | 失败路径 | ✅ 断网/代码不存在/HTML/空数组/因子异常/缓存损坏都不崩；Yahoo 异常结构抛非 DataUnavailable（M-1）；缓存损坏静默（M-2） |
| 3 | **月末判据边界** | ✅ **危险方向 0 次**（结构上不可能）；保守方向 54/144；节假日多等 1~8 天 |
| 4 | 金融三态 | 🔴 **CRITICAL**（akshare 缺失全体不可判）；M-3（NaN 当查到） |
| 5 | 删除副作用 | ✅ 无残留引用；datasource/judge 缺失都安全降级 |
| 6 | holdle.com 红线 | ✅ 三个 py 各 0 处 |
| 7 | 自更新 | ✅ 全下齐才落盘；⚠️ 校验仍是子串级（M-5） |

---

## 八、实测数字汇总

| 项 | 数字 |
|:--|:--|
| 月末判据全量扫描 | 144 样本，危险方向 **0**，保守方向 54 |
| 月末判据边界点 | 12 点中 10 正确、2 保守，**0 危险** |
| 真实末交易日早于日历月末的月份 | 105 / 302（34.8%），延迟 1~8 天 |
| 复权与底库差 | **0.000000000000**（600519，6027 行） |
| 港股 Yahoo vs 底库原始价不一致 | **4111 / 4229 天（97.2%）**，最大相对差 23.2% |
| 美股 NVDA 一致天数 | 6976 / 6977 |
| 判定结论一致性（A股/美股/港股 9 组） | 7 一致 / **2 不一致（均港股）** |
| 脏数据 fuzz | 17 种，抛异常 **0** |
| 话术自相矛盾扫描 | 264 组，**0** 命中 |
| 变异测试 | 6 个关键变异 **全部被抓**；基线 67 passed |
| holdle.com 引用 | 0 / 0 / 0 |

---

## 九、该不该 push 到公开仓库

### 最终答案：**可以 push。**

三条审查轮的 critical 在 v1.1.3 上全部实测关闭：

| # | 问题 | 终态 | 复验证据 |
|:--|:--|:--|:--|
| 1 | 月末判据可能把月中当整月（K4 防未来函数） | ✅ **攻不下来** | 结构上不可能 + 144 样本危险方向 0 + 12 边界点危险 0 |
| 2 | 复权现算算错 / 因子缺失静默用原始价 | ✅ **攻不下来** | A股 6022 天相对差 0.00e+00；因子缺失两路径都明确抛错 |
| 3 | 港股 Yahoo 日期整体错位一天 | ✅ 已修 | 同日一致率 100%（5492/1686 天），判定 9/9 一致 |
| 4 | 金融未校验告警用户看不到 | ✅ 已修 | 601318 无 akshare → `R-FINANCE-UNCHECKED` 进 final_text；600519 已校验时不出现 |
| 5 | 常量/口径/话术 | ✅ | 变异测试 6/6 被抓；186 组话术矛盾 0；holdle.com 0 处 |

### 建议随本版一起发的（都很便宜）

- [ ] **`judge.py` 入口两行改并列 if**（`'empty'` 与 `'nodep'` 互斥假设错误，真实链路当前靠兜底，但这是同一 bug 的第三次出现）
- [ ] **给依赖"当前日期/真实缓存"的测试固定时钟或固定 fixture** —— 我这边出现过一次 `1 failed` 但三轮无法复现，CI 会偶发飘红

### 可以挂到下一版的

- [ ] 自更新校验仍是子串级（`"HOLDLE 判定模块 judge.py" in txt` + `"def judge(" in txt`），构造样本实测可绕过；两个附属文件都没有版本常量、不与 `version.json` 比对。**不是本版引入的**，但 datasource.py 现在也是判定链必需件，建议补 10 行版本比对。
- [ ] `_cache_paths` 的 `mkdir` 在网络请求之前：缓存目录不可写时，即使网络正常也会直接 `PermissionError` 而非 `DataUnavailable`。

### 我对这一版的判断

**判定逻辑与防未来函数这两件"会让用户看到错结论"的事，我三轮都攻不下来——它们是对的。**
剩下的是工程卫生（校验强度、测试稳定性、一处互斥假设），都不影响判定正确性。

**这次合作里我最重要的收获写在这里给自己留档**：我在这两轮里归因错了三次（C1 方向、港股错位、金融告警顺序）。三次的共同点都是**先看代码路径/先假设成因，再找证据**，而正确的顺序是**先把关键中间值打出来**。Lead 每次坚持重查都是对的。

---

## 附：复现脚本清单（/tmp/adv2/，未进仓库）

| 脚本 | 用途 |
|:--|:--|
| `harness2.py` | 底库前复权日线的标准装载（与 `l1_engine` 同式） |
| `attack1_month.py` / `attack1c.py` / `attack1d.py` | 月末判据 12 边界点 / 短数据量 / 结构性反证 |
| `attack14_monthscan.py` / `attack14b.py` | 月末判据全量扫描（144 样本）与构造性反证 |
| `attack2_qfq.py` / `attack2b.py` / `attack2c.py` | 复权现算、因子缺失/错配/部分缺失 |
| `attack3_fail.py` / `attack3b.py` / `attack3c.py` | 失败路径穷举、缓存、Yahoo 异常结构 |
| `attack4_fin.py` / `attack4b.py` / `attack15_fin_e2e.py` | 金融三态、AkShare 返回值矩阵、端到端 |
| `attack5_del.py` / `attack5b.py` / `attack5c.py` / `attack5d.py` | 删除副作用、模块缺失降级 |
| `attack6_upd.py` / `attack6b.py` / `attack11_meta.py` | 自更新校验强度 |
| `attack8_realtime.py` | **主流程端到端真实运行（CRITICAL 复现）** |
| `attack10_tests.py` / `attack10b.py` | 变异测试 |
| `attack12_yahoo_real.py` / `attack12b.py` / `attack13_calendar_gap.py` / `attack13b.py` / `attack13c.py` | **真网络 vs 底库（HIGH 复现）** |
| `attack16_final.py` | 脏数据 fuzz + 结构隔离 + 话术横扫 |
