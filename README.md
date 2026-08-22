# HOLDLE 数据助手（holdle-data-skill）

HOLDLE AI 投研助手的**客户端数据组件**：让 AI 助手自动拉取 A股/港股/美股行情数据（月K/周K/日K/财报/实时行情），配合 HOLDLE 方法论做投研分析。

> ⚠️ 本工具仅提供**公开行情数据**，不包含任何投资判断，**不构成投资建议**。

## 这是什么

HOLDLE AI 投研助手由两部分组成：

| 组件 | 位置 | 作用 |
|:--|:--|:--|
| **holdle-ai MCP** | 云端服务（ai.holdle.com） | 知识库检索（HOLDLE 方法论） |
| **本仓库（数据助手）** | 你的电脑 | 拉取真实行情数据 |

两者配合：AI 先拉数据，再检索方法论，然后结合分析回答你。

## 怎么用

1. 让 AI 助手配置 MCP（见安装说明，或直接告诉 AI「安装 HOLDLE AI 投研助手」）
2. 把这个仓库的 `holdle-data-skill` 文件夹放到你的工作目录
3. 安装依赖：
   ```bash
   python3 -m pip install baostock tickflow akshare pandas
   ```
4. 然后就可以问 AI：「帮我分析 600519」「复盘一下 NVDA 这十年」

## 复权口径（重要）

- **当前/近期时点判断**（是否状态A/突破/入场参考价/**近期会不会有机会**/近一两年）→ 前复权（`--adjust forward`，与行情软件一致）
- **历史复盘/回测**（复盘/回测十年/分析XX年）→ 后复权（`--adjust backward`，历史口径固定）
- **拿不准 → 一律前复权**；只有明确「历史/复盘/回测」才用后复权
- AI 回答涉及价格时，会自动注明使用的复权格式

## 手动运行（可选）

```bash
python3 holdle_data.py 600519 ./data --adjust forward   # 前复权（当前判断）
python3 holdle_data.py NVDA ./data                       # 后复权（历史复盘，默认）
```

输出：月K/周K/日K CSV（含 MACD 指标）+ 财务（A股）+ 实时行情。

## 免责声明

- 本工具数据来自公开免费接口（Baostock/TickFlow/AkShare/腾讯/新浪），仅供学习研究
- 不提供投资建议，投资有风险，决策请独立判断
- HOLDLE 方法论相关解释请通过 holdle-ai MCP 获取（需要 HOLDLE 会员 key）

## 仓库

- 数据助手（本仓库）：https://github.com/emilesu/holdle-data-skill
- MCP 服务（私有）：HOLDLE 运营方维护

© 2026 HOLDLE · 本网站/工具不提供投资建议
