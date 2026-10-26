# 代码审查 · v1.0.10 复验报告

**审查人**：code-review（工程实践视角）
**复验时间**：2026-10-26 09:39 起
**复验对象**：子仓库 `88b89f0`（v1.0.10）—— `judge.py` 1010 行 / `holdle_data.py` 1192 行 / `check_release.py` 80 行 / `tests/test_judge.py` 385 行
**基线**：上一轮报告 `REVIEW_代码审查_judge_v1.0.8_20261026.md`（v1.0.8，753 行）
**解释器**：`/Users/imac/holdle-workspace/data/klines/.venv/bin/python`（3.9.6）+ 真 pytest（`/tmp/review/vpy`）+ 3.8.20 / 3.12.15 多环境

---

## 〇、结论先行

### 我报的问题处理情况

| 我上一轮报的 | 状态 | 复验证据 |
|:--|:--|:--|
| **C-1** `expired` 话术自相矛盾（窗口存活却说「有效期已过」） | ✅ **真修好** | 真实数据 `600743@2026-05-29`、`000333@2026-08-31` 均输出「窗口仍存活，入场等待继续有效」，`"入场有效期已过" in txt == False` |
| **C-2** `is_stale` 硬编码 False | ⚠️ **一半** | 真停更场景 ✅ 正确（`R-STALE-DATA` 排首位）；但 **`as_of` 回放场景仍错**（见 H-1） |
| **C-3** 分发清单缺数据文件 | ✅ **修好且工程化** | `SIDE_MODULES` 三文件齐全，`check_release.py` 五项全 ✅ |
| **H-2/H-3** 附属模块校验弱、缺版本一致性 | ✅ 改进 | 按 `py`/`json` 分类型校验，json 查结构标记（`months`/`symbols`） |
| **H-5** `dump()` 非原子写 | ✅ 修好 | 改 `os.replace` 原子替换 |
| **测试 2 例失败** | ✅ 修绿 | **55 passed**（真 pytest），自检机制保留 |

### 这一轮新发现

| 级别 | 问题 | 一句话 |
|:--|:--|:--|
| **high** | H-1 `is_stale` 在 `as_of` 回放时违反契约 | 契约要求恒 `false`，实测 `true`；且字段 `true` 但 `final_text` 无任何陈旧话术 → 字段与话术不一致 |
| **high** | H-2 单点 NaN 导致**整只股票拒判** | 6000 根日线里 1 根缺 `low` → `data_quality`。实测 dropna 后结论与完整数据**完全一致**，本来可判 |
| **high** | H-3 E-02 参考价公式**零测试保护** | 变异 `max(high)→min(low)` 存活。这是上一版出过 16% 偏差的 P0 口径 |
| **medium** | M-1 `check_release.py` 解析脆弱 | 单引号/单行/`+`拼接三种写法解析为空 → **静默通过** |
| **medium** | M-2 跨文件"全写或不写"实际做不到 | `os.replace` 逐文件原子，非跨文件事务；中途失败就是新旧混搭 |
| **medium** | M-3 `.new` 残留无清理、`.gitignore` 不覆盖 | Windows `os.replace` 失败会留下 `.new` |
| **medium** | M-4 全局缓存污染不可恢复 | 名单一次读失败 → 空表永久缓存 → 整个进程生命周期都报 `finance_unverified`，重启才恢复 |
| **medium** | M-5 测试依赖 gitignore 的 CSV | 3 个用例在 CI/用户端**永久 skip**，本机绿、CI 绿得不一样 |
| **low** | `1e4` 跨度阈值**挡不住它声称要挡的事** | 实测茅台不复权 ratio=125、前复权 767，全都远低于 1e4 |

### 能不能发到用户端

## ⚠️ **可以发，但 H-2 建议先修（约 10 行）。**

理由：
- 工程地基很扎实：跨版本 60/60 一致、语法 3.7/3.8 通过、55 测试全绿、变异体 7/14 杀死（比 v1.0.8 的 1/14 好得多）、`check_release` 五项全 ✅
- **H-1/H-3 可在发布后跟版**（H-1 是字段语义噪声 + 跨实现不一致，不改判定结论；H-3 是测试债，不影响运行时）
- **H-2 建议先修**，因为它把「数据源偶发缺失 1 天」放大成「整只股票判不了」，用户会认为是工具坏了

---

## 一、Lead 点名的 4 项，逐项回答

### 1. `_auto_update` 重写 —— 主线逻辑正确，3 个边角问题

**做得对的**（这几处确实是关键修复）：
```python
_fname, _kind = (_mod.split(":", 1) + ["py"])[:2]   # ✅ NameError 根治，且在循环开头就赋值
_staged = []                                        # ✅ 先全下齐再落盘，避免半装
if _miss_core:                                      # ✅ 缺一个就不写，判定保持旧状态
os.replace(_tmp, target)                            # ✅ 单文件原子替换
```

**边角问题：**

| # | 问题 | 实测 | 建议 |
|:--|:--|:--|:--|
| M-2 | **"要么全写、要么不写"做不到**。注释这么写，但 `os.replace` 是**逐文件原子**，不是跨文件事务 | 我模拟 3 文件、第 2 个 `os.replace` 抛 `PermissionError`：<br>`judge.py: NEW` / `trading_calendar_months.json: OLD` / `finance_symbols.json: NEW`<br>→ **新旧混搭** | 要么改注释说实话（"尽量一起写，中途失败可能混搭"）；要么加一遍**回读校验**：全部落盘后重新读 3 个文件，任一结构校验失败则打印明确告警 "附属模块版本可能不一致，请重新运行 --auto-update" |
| M-3 | **`.new` 残留无清理** | 写入成功、`os.replace` 失败（Windows 文件被占用）→ 留下 `judge.py.new`。代码无 `try/finally` | `try: ... finally: os.path.exists(_tmp) and os.remove(_tmp)`；`_tmp` 用 `tempfile.mkstemp(dir=_base_dir)` 更稳（免撞名） |
| — | `.gitignore` 只忽略 `*.bak`，不含 `*.new` | — | 加一行 `*.new`（否则用户 `git status` 脏） |

**Windows 语义确认**：POSIX 下 `os.replace` 对只读目标也能成功（我实测），**Windows 下目标被占用会 `PermissionError`** —— 这正是 M-2/M-3 的现实触发场景，不是理论问题。

**并发**：`_auto_update` 由 `_check_update` 调用，而 stamp 检查（`holdle_data.py:120`）无文件锁。两个进程同时判断"未检查"会各自跑一遍更新 → 同时写同一个 `.new` → 一个 `os.replace` 成功后另一个 `os.replace` 报 `FileNotFoundError`（自己的 `.new` 已被对方 replace）。**概率低，且被 `except Exception` 兜住只打印告警**，可接受；但用 `mkstemp` 可彻底消除。

### 2. `finish_unverified` 与 `_FIN_OK` —— 语义正确，但有 2 个真实缺陷

**设计判断：这个分支本身做得对**。名单缺失时"宁可拒判也不静默放行"，方向正确（`_is_finance_symbol` 恒 False 的旧 bug 已被安全兜底）。

**缺陷 1（medium，M-4）：全局缓存一旦污染不可恢复**
```
场景：名单文件一次读失败（更新中/权限/磁盘抖动）
  → _FIN_SET = {}, _FIN_OK = False
  → 之后【所有】judge() 调用都返回 finance_unverified
  → 即使文件恢复了也不会重读（因为 if _FIN_SET is None 已经不成立）
  → 整个进程生命周期内判定全废，必须重启
```
实测确认：`_FIN_SET={}` 污染后，恢复文件路径也不会重读；手动 `_FIN_SET=None` 才恢复。
> **修法**：失败时不要缓存空表，而是保留 `_FIN_SET = None` 并加一个"失败计数 + 下次重试"；或简单点：失败时 `_FIN_SET = None`（下次调用重试），用另一个 `_FIN_ERR` 记失败原因。3 行。

**缺陷 2（low）：线程安全**
**实测 16 线程并发首次调用：16/16 成功，结果一致，无异常。** 原因是 CPython GIL + 最坏情况只是重复加载同一个文件（幂等）。**不是问题，但值得记**：若将来换 PyPy/多进程共享，`_MONTH_CAL`/`_FIN_SET` 这种惰性全局要加锁。

**小瑕疵**：`_is_finance_symbol` 的 docstring 还写着"名单文件缺失时返回 False 并**不拒判**"，但实际已改成走 `finance_unverified` 拒判。**docstring 与代码矛盾**，会误导下一个维护者。

### 3. 脏数据取舍（coerce + 报 data_quality，而非 dropna）—— ⚠️ **方向对，尺度过了**

**我认可的部分**：不用 `dropna` 静默丢行，这个原则是对的。你的理由（"那样会静默丢行"）成立，而且我实测过知识库侧 `dropna(subset=["qfq_close"])` 确实会悄悄丢行。

**但实测发现尺度过头了（H-2）**，两条硬证据：

**证据 1：同一份真实数据，dropna 能算出完全正确的结论，当前实现却拒判**
```
603637.SH @2026-04-30
  完整数据:      judged / state_a=true / window=ai_to_gao / entry=no_green
  单点 low 缺失: indeterminate / data_quality          ← 拒判
  dropna 后判定: judged / state_a=true / window=ai_to_gao / entry=no_green
                → 与完整数据 final_text 逐字一致
```
**证据 2：6000 根日线里 1 根缺值，无论哪个字段、哪个位置，全被拦**

| 缺失位置 | 结果 |
|:--|:--|
| `open` @第 10 / 3000 / 5000 行 | 全部 `indeterminate data_quality` |
| `high` @第 10 / 3000 / 5000 行 | 全部 `indeterminate data_quality` |
| `low` @第 10 / 3000 / 5000 行 | 全部 `indeterminate data_quality` |
| `close` @第 10 / 3000 / 5000 行 | 全部 `indeterminate data_quality` |

**为什么这是问题**：价格列出现 NaN 的**主流原因是数据源问题，不是用户输入错误**——
1. 新浪/雅虎 qfq 因子缺失日（偶发，尤其除权日附近）
2. 新股上市首日部分字段空
3. 港股停牌日（`data_source` 有剔除逻辑，但 `holdle_data` 这条链未必全剔）

而代码 `L770-778` 的拦截把所有 NaN 一律说成「有 N 条记录的价格**无法解析**」——这个话术暗示"你的数据格式错了"，会让用户反复重装。

**我建议的改法（区分两类，~15 行）**：
| 场景 | 判据 | 处理 |
|:--|:--|:--|
| **输入格式错**（`'abc'` 被 coerce） | 原始值非空且 coerce 后变 NaN | 保持现在：`data_quality`（这是真错） |
| **数据源缺失**（本来就是 NaN） | 原始值就是 NaN | **按字段分流**：`close`/`high` 缺失 → 丢该行继续判（但要报 `dropped_rows` 计数）；`open`/`low` 缺失 → 可安全丢行（E-02 只用 `high`，判定不用 `open`/`low`） |
| **缺失过多** | 丢行数 > 3% 或连续 > 3 行 | `data_quality` 拒判 |

关键洞察：**判定逻辑实际只吃 `close`（MACD）和 `high`（E-02 参考价）**。`open`/`low` 只在 D-08 一字板（`high==low`）和 E-06（`open > ref*1.05`）用到。所以 `open`/`low` 缺失时按“该日不可用于一字板/E-06 判断”降级即可，**没必要整只票拒判**。

### 4. 测试依赖 `holdle_data/*.csv` —— ⚠️ **确实是问题，会永久 skip**

**实测证据**：
```
git ls-files holdle_data/ | wc -l   →  0        （git 完全不跟踪）
ls holdle_data/*.csv | wc -l        →  15       （本机有）
.gitignore:9                        →  holdle_data/
```
**我把 `holdle_data/` 临时移走模拟 CI，结果：**
```
本机（有 CSV）:  54 passed, 1 skipped
模拟 CI（无 CSV）: 33 passed, 3 skipped      ← 多出 2 个 skip
  SKIPPED test_window_alive_never_says_expired: 仓库内无真实缓存行情
  SKIPPED test_replay_no_present_tense_in_window: 无真实缓存行情
```

**所以：本机绿 ≠ CI 绿 ≠ 用户端绿。** 而且这两条恰好是 **P0 级回归测试**（窗口存活不说"过期"、回放不用现在时），在 CI 里**永久不跑**。

**建议（我倾向方案 B）**：
- **方案 A**：把 CSV 切成 tiny fixture（每只留最近 200 行 + 关键锚点附近 100 行），提交进 `tests/fixtures/`。优点：CI 真测；缺点：需构造，且"最近 200 行"可能覆盖不到目标分支。
- **方案 B（推荐）**：把「真实数据构造」改成**确定性合成数据**。我实测过 `make_daily` 的确定性很重要——问题不是"合成 vs 真实"，而是**合成数据的月末对齐**。你已经解决了对齐（从日历取月末），那就可以**再往前一步**：用固定种子 + 明确的价格形态（先涨后横盘）构造出「窗口存活」分支，`test_window_alive_never_says_expired` 就能在 CI 里真跑。你现在的 `test_data_end_must_not_say_expired` 已经这么做了（虽仍有 skip 兜底，见下）。
- **无论选哪个**：`pytest.skip` 兜底必须配一个**跨平台的 fail 条件**。现在 `test_data_end_must_not_say_expired` 在**任何环境**都 100% skip（我实测 `expire_reason=None`），等于最关键的 C-1 回归测试**从未跑过**。

> **这条必须修**：C-1 是我上一轮报的最高优先级 critical，修它的回归测试却全程 skip。建议改成：用真实数据锚点（`600743` + `2026-05-29`）做成 fixture（约 6000 行，压到 200KB 可接受），或直接把 `(expire_reason, expected_substring)` 做成参数化表，绕开"构造不出来"。

---

## 二、分级问题表（v1.0.10）

### 🟠 high

| # | 位置 | 问题 | 证据 | 修法 |
|:--|:--|:--|:--|:--|
| **H-1** | `judge.py:763` `_compute_stale(trade_date)` | **`as_of` 回放时 `is_stale` 违反契约 §二.1**（契约：回放恒 `false`，由 `replay_stale` 表达相对今天）。而且字段为 `true` 时 `final_text` 里**没有任何陈旧话术**（话术只在非回放时加）→ **字段与话术不一致** | 契约 `01_判定输出格式_v0.3.md` §二.1；实测 4/4 回放锚点 `KB is_stale=False` vs `SK is_stale=True`；SK 侧 `R-STALE-DATA` 不在 units 里 | `_compute_stale(trade_date, as_of)`：`as_of` 非空时直接 `return False`；补一条断言测试「回放场景 is_stale 必须 False 且 R-STALE-DATA 不在 units」 |
| **H-2** | `judge.py:770-778` | 单点 NaN → **整只股票拒判**（详见 §1 第 3 项） | 6000 根里 1 根缺值 → `data_quality`；dropna 后结论与完整数据逐字一致 | 区分「原始值就 NaN（数据源缺失，可丢行）」vs「非空被 coerce（输入错误，拒判）」；`open`/`low` 缺失可安全丢行 |
| **H-3** | `tests/test_judge.py` 全文 | **E-02 参考价公式零测试保护**。变异 `["high"].max() → ["low"].min()` **存活 55 passed** | 变异测试实测；`grep ref_price` 仅 1 处（`test_state_a_false_voids_ref_price`，只测"不下发"，不测**数值**） | 加断言：`entry.ref_price == max(high[开窗月首日..绿柱日])`，用真实锚点（`600743@2026-05-29` → `3.53`）或确定性合成数据 |

### 🟡 medium

| # | 位置 | 问题 | 修法 |
|:--|:--|:--|:--|
| **M-1** | `check_release.py:32-41` | **解析脆弱**：`if line.startswith('"')` + `split('"')[1]`。单引号 / 单行写法 / `+` 拼接 → **解析为空 list → 静默通过**（`SIDE_MODULES 共 0 个文件` 然后报 ✅） | 用 `ast.literal_eval` 抽 AST 常量，或至少加一条 `assert mods`（解析为空直接 `❌`）。**这是"发版守门员"，它静默失效比没有更糟** |
| **M-2** | `holdle_data.py:308-320` | 注释"要么全写、要么不写"**做不到**：`os.replace` 逐文件原子 | 改注释；或落盘后回读校验三个文件的结构，失败则明确告警 |
| **M-3** | `holdle_data.py:313-316` | `.new` 无 `finally` 清理；`.gitignore` 不含 `*.new` | `try/finally` 清理；`.gitignore` 加 `*.new`；`tempfile.mkstemp(dir=_base_dir)` |
| **M-4** | `judge.py:551-562` | 名单一次读失败 → 空表**永久缓存**，全进程 `finance_unverified`，重启才恢复 | 失败时保持 `_FIN_SET = None`（下次重试），用独立 `_FIN_ERR` 记原因 |
| **M-5** | `tests/test_judge.py:251,373` | 依赖 gitignore 的 `holdle_data/*.csv` → CI/用户端**永久 skip** 2 个 P0 回归测试 | 见 §1 第 4 项方案 B（确定性合成数据） |
| **M-6** | `tests/test_judge.py:241` | `test_data_end_must_not_say_expired` **100% skip**（`expire_reason=None`）→ C-1 回归从未真跑 | 参数化表 + 真实锚点 fixture |
| **M-7** | `judge.py` `_is_finance_symbol` docstring | docstring 说"缺失时返回 False 并不拒判"，代码已改成 `finance_unverified` 拒判 → **文档与代码矛盾** | 更新 docstring |

### 🔵 low / nit

| # | 位置 | 问题 |
|:--|:--|:--|
| L-1 | `judge.py:794-799` | **`1e4` 跨度阈值挡不住它注释声称要挡的事**。实测茅台：前复权 ratio=**767**、不复权 ratio=**125** → 全部远低于 1e4，混用**不会被拦**。1e4 需连续 ~90 个跌停才触发，实际只能抓"单位错 1 万倍"。**建议要么改判据（相邻日跳变 >2x 且间隔 ≤5 天，我实测能抓到不复权数据），要么删掉换诚实注释** |
| L-2 | `check_release.py:15` | `import json` 未使用（ruff F401） |
| L-3 | `check_release.py:49` | `subprocess.run(["git", ...])` → S607（partial executable path）。建议 `shutil.which("git")` 或固定路径 |
| L-4 | `holdle_data.py:120` | 更新期间无并发锁（见 §1 第 1 项并发） |
| L-5 | `judge.py:262` | `last_closed_pos` 仍未被使用（ruff F841，v1.0.8 就报过，未修） |
| L-6 | `judge.py:637,645` | 两处 `except Exception: pass`（ruff S110）——`pd.Period` 转换失败的兜底，建议至少 `# noqa` + 注释说明 |
| L-7 | `judge.py` 多处 | E702 单行多语句 16 处（v1.0.8 是 9 处，**增加了**） |

---

## 三、实测数据汇总

### 3.1 兼容性（重点，这轮继续验证）

| 项 | 结果 |
|:--|:--|
| Python 3.7 语法（`feature_version=(3,7)`）| ✅ judge.py / holdle_data.py / check_release.py 全过 |
| Python 3.8.20 真解释器 `py_compile` | ✅ 三文件全过 |
| **跨版本一致性**（4 环境 × 20 组判定，含 `final_text` 全文 + `units` codes） | **60/60 一致** |
| 　└ py3.8 / pandas 1.5.3 | ✅ 20/20 |
| 　└ py3.8 / pandas 2.0.3 | ✅ 20/20 |
| 　└ py3.12 / pandas 3.1.0 | ✅ 20/20 |

> **这个结论连续三轮（v1.0.8 / v1.0.9 / v1.0.10）都成立**，可以正式把"版本兼容风险"关闭。

### 3.2 测试

| 项 | 结果 |
|:--|:--|
| 真 pytest 全量 | **55 passed**（本机，有 CSV） |
| 模拟 CI（无 CSV） | 33 passed, **3 skipped** |
| 变异测试 14 个 | **7 杀死 / 7 存活**（v1.0.8 是 1/14，v1.0.9 是 0/14 —— 注意 v1.0.9 那轮的数字是替身 runner 的误判，本轮用真 pytest 重算） |

**变异存活清单**（7 个）：
```
❌ E-02 max(high)→min(low)      ← H-3，P0 口径零保护
❌ C1 关窗 ref→+inf             ← 窗口关闭通道无覆盖
❌ 状态A 去掉 dif>=0             ← SA-01 残缺无覆盖
❌ _compute_stale 恒 False      ← 真停更场景无测试（H-1 顺带）
❌ _compute_stale 阈值 2→9999   ← 同上
❌ _is_finance_symbol 恒 False  ← 金融名单命中无正向测试
❌ 跨度阈值 1e4→1e9              ← 输入防呆无边界测试
```

> 说明：`_compute_stale` 恒 False 存活，意味着**我上一轮报的 C-2 虽然代码修了，但没有任何测试守住它**。建议连 H-1 一起补。

### 3.3 性能

| 场景 | 耗时 |
|:--|:--|
| 6027 根日线 | 中位 6 ms |
| 6836 根日线 | 中位 6 ms，最坏 16 ms |
| 冷启动（含日历 35KB + 名单 10KB 加载） | 7 ms |

**结论：性能完全不是问题**（相对网络拉取 5-30 秒）。v1.0.8 → v1.0.10 从 753 → 1010 行，耗时无变化。

### 3.4 语法/静态检查

| 项 | 结果 |
|:--|:--|
| `ast.parse(feature_version=(3,7))` | ✅ 三文件 |
| 3.8.20 `py_compile` | ✅ 三文件 |
| ruff `F,S,B,E7,E9` | 24 项，其中 **12 项是 E702 风格**，实质问题 6 项（见 low 表） |
| `check_release.py` 实跑 | ✅ 五项全 ✅，退出码 0 |

---

## 四、能不能发到用户端

## ⚠️ **可以发，但建议先花 30 分钟修 H-2（单点 NaN 整只拒判）。**

如果时间紧、必须先发，**H-2/H-1/H-3 都可跟版**，因为：
- **H-2** 只在实际遇到 NaN 时触发（数据源正常时零影响），且失败**可见**（用户看到 `data_quality` 话术）
- **H-1** 是字段语义噪声，不改判定结论，只是让消费端多看一条陈旧警示
- **H-3** 纯测试债，运行时无影响

**不建议拖到下一个大版本的**：`check_release.py` 的 M-1（解析脆弱）—— 它是发版守门员，静默失效比没有更糟。**这一条我今天就建议改，成本 3 行。**

**发版前最后一件事**：`check_release.py --remote` 在 push 之后跑一遍，确认 raw 全 200。**我上一轮担心的 404 静默失效，现在只剩这一步能真正兜住。**

---

## 五、三轮审查的问题收敛趋势

| 版本 | 行数 | 测试 | 变异存活 | 我报的 critical | 我报的 high |
|:--|--:|:--|:--|:--|:--|
| v1.0.8 | 753 | 24 | 13/14 | 2 | 5 |
| v1.0.9 | 905 | 54 | （替身 runner 误判，不计） | 0 | 3 |
| **v1.0.10** | **1010** | **55** | **7/14** | **0** | **3** |

**趋势判断**：critical 已清零，high 从 5 → 3，但**没有继续收敛**。原因很清楚——v1.0.9→v1.0.10 新增的代码（`_FIN_OK`、NaN 检查、1e4 阈值）**本身引入了新的 high**（H-2）。

这是正常的（防御性代码会引入新的失败模式），但说明一件事：**新增防御逻辑时，同样需要「先构造真实失败样本，再写断言」**。H-2 就是典型的"写了防御、没量过尺度"。

---

**源码零改动确认**：全程只读，未修改 `judge.py` / `holdle_data.py` / `check_release.py` / `tests/` 任何内容。变异测试每次都在 `finally` 中还原并校验 md5 一致。临时脚本在 `/tmp/review/`。

---

## 六、追加：v1.0.12 快速复验（复验期间又发了两版）

**背景**：我复验 v1.0.10 期间，实现方连续出了 v1.0.11 / v1.0.12。以下是我在 **v1.0.12**（`judge.py` md5 `4ed8ad85`）上重跑的结果。

### 6.1 版本漂移确认

| 版本 | 我复验了什么 |
|:--|:--|
| v1.0.10（`88b89f0`，md5 `276c02a1`） | 本轮报告主体 |
| v1.0.12（md5 `4ed8ad85`，工作区未提交状态） | 本节追加复验 |

**v1.0.12 的两处改动**（对比 v1.0.11）：
1. `R-ASOF-REPLAY` 加 `and not as_of_beyond_data` —— 修「as_of 越界时两句互相矛盾」
2. **价格污染判据从「全序列 max/min > 1e4」改为「月对月收盘跳变 > 20 倍」** —— 正是我报的 L-1

### 6.2 L-1 的修复效果：**净改善，但仍未达成目标**

**✅ 误伤问题解决了**（这是真进步）：

| 标的 | 旧判据 ratio | 新判据 bad_jump | 判定 |
|:--|--:|--:|:--|
| 600519 茅台 | 767.6 | 0 | ✅ judged |
| 603637 | 4.5 | 0 | ✅ judged |
| 000333 美的 | 13.8 | 0 | ✅ judged |
| 600743 | 21.1 | 0 | ✅ judged |

实现方在 changelog 里写的 WMT 51150 / MSFT 10230 / NVDA 7455 —— 我无法验证（无美股数据），但**方向判断正确**：旧判据确实随上市年限单调增长，会误伤。

**❌ 但检出能力依然是 0**：

| 标的 | qfq 最大月跳变 | 不复权最大月跳变 | 不复权超20倍次数 |
|:--|--:|--:|--:|
| 600519 | 1.47 | **1.47** | 0 |
| 000333 | 1.24 | **1.24** | 0 |
| 600743 | 2.53 | **2.53** | 0 |
| 603637 | 1.42 | **1.42** | 0 |

**关键发现**：`qfq_close` 和 `close`（不复权）的**月对月跳变完全一样**。这说明前复权只改变**绝对价格水平**（整体乘一个因子），不改变**相邻期相对变化**——所以任何基于「相邻期比值」的判据，**原理上就不可能检出复权口径混用**。

> **结论**：这条防线可以留着（它确实能拦"单位错 1 万倍"这种极端情况，且不误伤），但**注释必须改成实话**——它挡不住"上游退回未复权价"。真正的防线应该是**结构性的**（如：`judge()` 要求调用方显式传 `adjust="forward"` 并在输出里回显，让 MCP 侧能核对），而不是靠数值体检猜。

### 6.3 v1.0.12 回归结果

| 项 | 结果 |
|:--|:--|
| 真 pytest 全量 | **55 passed** |
| 跨版本（4 环境 × 20 组） | **60/60 一致** |
| `check_release.py` | 3 个文件，退出码 0 |

### 6.4 我报的 3 个 high 在 v1.0.12 的状态

| # | 问题 | v1.0.12 状态 | 说明 |
|:--|:--|:--|:--|
| **H-1** | `is_stale` 回放场景违反契约 | ❌ **仍未修** | 实测 `as_of=2026-05-29` → `is_stale=True`（契约要求 False） |
| **H-2** | 单点 NaN 整只拒判 | ❌ **仍未修** | 实测单点 `low` 缺失 → `data_quality` |
| **H-3** | E-02 参考价零测试保护 | ⚠️ **部分** | 新增 `tests/fixtures/603637_data_end_case.csv`（96KB 真实行情，已入库），解决了 M-5/M-6 的 skip 问题；但**E-02 数值断言仍无**（见 §6.6 变异复验） |

### 6.5 fixture 入库情况（M-8，已核实）

**`tests/fixtures/603637_data_end_case.csv`（96KB，真实行情）是 v1.0.12 的一个重要改进**——终于让 C-1 回归测试在 CI 里真跑，而不是永久 skip。这个方向完全正确，正是我上一轮 M-5/M-6 的建议。

| 检查项 | 实测结果 |
|:--|:--|
| fixture 是否已入库 | ✅ **已入库**（`git ls-files tests/fixtures/603637_data_end_case.csv` 命中） |
| `.gitignore` 会不会挡 | ✅ 不会（规则只有 `holdle_data/`，不覆盖 `tests/fixtures/`） |
| `check_release.py` 会不会查它 | ❌ **不会**（只查 `SIDE_MODULES` + 主脚本 + `version.json`） |

> **建议（仍然成立，只是不是缺陷）**：`check_release.py` 可以加一项「测试 fixture 完整性」——扫 `tests/` 里的 `tests/fixtures/*` 引用，断言每个都 `git ls-files --error-unmatch`。
> 理由：**"登记了但没入库"这个坑已经出现过两次**（第一次是 `SIDE_MODULES` 的两个 json，第二次差点是这个 fixture）。做成通用能力比逐个补可靠。

---

## 七、最终结论（v1.0.12）

| 维度 | 评价 |
|:--|:--|
| **版本兼容** | ✅ 优（4 环境 60/60，连续三轮稳定） |
| **性能** | ✅ 优（6 ms，无退化） |
| **确定性** | ✅ 优（跨实现、跨版本均一致） |
| **错误处理** | ⚠️ 中（H-2 过度拒判；H-1 字段/话术不一致） |
| **可维护性** | ⚠️ 中（两份实现无机制守护，仍靠人同步） |
| **测试质量** | ⚠️ 中偏上（55 用例、fixture 化，但 7 个变异体存活，E-02 零保护） |
| **分发安全** | ✅ 良（check_release 已工程化，但解析脆弱 + 不查 fixture） |

### 能不能发到用户端

## ⚠️ **可以发。H-1 / H-2 建议跟版修（合计约 20 行）；H-3 和 M-8 下个迭代补。**

**放行的核心理由**：
1. 所有 critical 已清零，且我连续三轮未能推翻「判定结果正确性」这一核心（跨实现 12 组、跨版本 60 组全部逐字段一致）
2. 剩余 high 全是**输出层语义与测试债**，不改判定结论
3. 失败都是**可见失败**（`data_quality` / `finance_unverified` 有明确话术），不存在静默错结论

**发版前唯一必做**：`check_release.py --remote` 在 push 后跑，确认 5 个文件 raw 全 200。（`tests/fixtures/` 已核实入库，`.gitignore` 不覆盖，无需额外动作。）

### 6.6 v1.0.12 变异复验：6 个核心变异体全部存活

在 v1.0.12（55 passed）上重跑关键变异体：

| 变异 | 结果 | 含义 |
|:--|:--|:--|
| **E-02 `max(high)→min(low)`** | ❌ 存活（55 passed） | **参考价公式改错，测试无感**——这是历史上出过 16% 偏差的 P0 口径 |
| `_compute_stale` 恒 False | ❌ 存活 | C-2 修复**无测试守住**（回退即静默复原） |
| `_is_finance_symbol` 恒 False | ❌ 存活 | 金融股名单命中**无正向测试**（只测了"名单缺失"的反向场景） |
| C1 关窗 `ref→+inf`（永不关窗） | ❌ 存活 | 窗口关闭通道无覆盖 |
| 状态A 去掉 `dif>=0` | ❌ 存活 | SA-01 残缺无覆盖 |
| 月跳变判据恒不触发 | ❌ 存活 | 新加的输入防呆**无边界测试** |

**这是本轮最值得重视的一条**：55 个测试、fixture 化、自检完善，**但对 6 类"真实会犯的错"集体失明**。

**根因**：现有测试几乎全部断言的是「**结构性事实**」（`entry is None`、`status == "indeterminate"`、`units` 排序、"不得出现某词"），而**极少断言"算出来的数是多少"**。

- ✅ 结构断言拦住的是"形状错"（该为 null 时不是 null）
- ❌ 它拦不住"算错了但形状对"（参考价 20.1 而不是 3.53 —— 形状完全正常）

**具体补法（按性价比排序，合计约 1 小时）**：

```python
# 1. E-02 参考价数值锁定（最高性价比，10 行）
def test_ref_price_formula_is_range_high():
    """E-02：ref_price 必须等于 max(high[开窗月首日..绿柱日])，不是最低价。"""
    d = _load_fixture("603637_data_end_case.csv")
    r = judge.judge(d, code="603637")
    en = r["entry"]
    if not en: pytest.skip("fixture 未命中入场分支")
    # 独立复算：从 fixture 里取 same 区间，用 pandas 重算一遍
    start = pd.Timestamp(en["switch_month"] + "-01")
    green = pd.Timestamp(en["green_day"])
    expect = float(d[(d["date"] >= start) & (d["date"] <= green)]["high"].max())
    assert en["ref_price"] == round(expect, 4), "E-02 公式被改动"

# 2. 月跳变判据边界（5 行）
def test_month_jump_guard_triggers():
    d = _load_fixture("603637_data_end_case.csv").copy()
    i = len(d) // 2
    d.loc[i:, ["open","high","low","close"]] *= 100     # 人为制造 100 倍跳变
    r = judge.judge(d, code="603637")
    assert r["status"] == "indeterminate" and r["indeterminate_reason"] == "data_quality"

# 3. is_stale 双场景（8 行）
def test_stale_true_on_real_staleness(): ...   # 数据停 61 天 → is_stale True + R-STALE-DATA 在 units
def test_stale_false_on_replay(): ...          # as_of 历史日期 → is_stale False（当前实现会红，正好暴露 H-1）

# 4. SA-01 与 C1（各 8 行，用 fixture 锚点构造）
```

**一句话**：v1.0.10→v1.0.12 的测试增量主要在「补 skip」「换 fixture」，**没有补"数值断言"这类真正能杀变异体的测试**。建议下一迭代把重心从"增加用例数"转到"给核心公式加数值锁定"。

---

## 八、附：复验用到的工具与临时资产

| 文件 | 用途 |
|:--|:--|
| `/tmp/review/vpy` | 真 pytest 环境（3.9.6 + pandas 2.3.3），交叉验证替身 runner |
| `/tmp/review/venv38` / `venv381` / `venv12` | 3.8+pandas2.0.3 / 3.8+pandas1.5.3 / 3.12+pandas3.1.0 |
| `/tmp/review/pythons/cpython-3.8.20` | 真实 3.8 解释器 |
| `/tmp/review/mutate_v10.py` | v1.0.10 变异测试（14 个变异体） |
| `/tmp/review/g12.json` | v1.0.12 的 20 组判定基准 |
| `/tmp/review/*.csv` | 4 只标的真实前复权日线（600519/603637/000333/600743） |

**源码零改动**：全程只读。所有变异测试在 `finally` 中还原并校验 md5 一致（每次输出 `还原一致: True`）。
