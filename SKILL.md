---
name: mcd-a11y-order
description: 麦当劳无障碍 / 适老化点餐 Skill。面向老人、低视力、驾驶中人群，基于官方 MCP 真实菜单与真实套餐组成，生成大字 / 少字 / 口语化的极简点餐方案，标注辣·冰·需餐具·带骨等本地推导标签，给出官方实时核价，然后停在付款前。适用于“帮老人点餐”“看不清菜单”“开车没法操作 app”“给爸妈点一份不辣好拿的”“这份钠会不会太高”等需求。
---

# 麦麦无障碍点餐 / mcd-a11y-order

App 上密密麻麻的小字菜单、层层嵌套的套餐选择、还要比券——对长辈、低视力用户和正在开车的人都不友好。
本 Skill 把这件事压成三步：**给几个能看懂的选项 → 用人话讲清楚里面有什么 → 官方核价**，
最后把决定权交回本人或家属，绝不代下单。

## 何时使用

- 用户提到：帮老人点、给爸妈点、字太小看不清、开车不方便点、不要辣、要好拿的、要软一点的
- 需要知道「这个套餐里到底有什么」
- 关心整份饭菜的钠 / 热量是否偏高（尤其长辈或需要控钠的人群）

## 前置条件

- 麦当劳中国 MCP Token：<https://open.mcd.cn/mcp>
- MCP Client 配置 `mcd-mcp`（见 `mcp-config.example.json`，Token 用环境变量 `MCD_MCP_TOKEN` 占位）
- 无 Token 时可用 `examples/` 下的真实样例跑通全流程，但真实使用必须走真实 MCP

## 核心原则

1. **真实数据优先**：菜单、价格、组成、核价、营养全部来自官方 MCP；缺数据就写「未获取」，绝不估算。
2. **标签必须标注来源**：官方 `query-meals` **没有**辣/冰/温度这类字段（`tags` 只是 `上新`/`爆款` 营销标签），
   所以「辣 / 冰 / 好拿 / 软 / 需餐具」是本 Skill **基于真实名称推导**的，输出里必须说明这一点。
3. **少即是好**：默认只给 1–3 个方案，每个方案 2–3 行字。选项太多等于没有选项。
4. **营养只作参考**：不构成医疗或营养治疗建议；部分子项未匹配时明确标注「非整份真实合计」。
5. **停在付款前**：未经用户明确确认，绝不调用 `create-order`。

## 标准工作流（速查）

```
第 1 步  收集最少入参（一次问完）
        · 怎么吃：堂食 eat-in / 外带 locker-in / 麦乐送（默认堂食或外带，最好拿）
        · 服务对象画像：长辈 elderly / 低视力 lowvision / 驾驶中 driver / 儿童 child / 通用 general
        · 忌口与约束：不要辣、不要冰、控钠（可用 --max-sodium 数值化）、预算上限

第 2 步  拉取真实上下文
        python scripts/mcd_mcp_client.py fetch-context --city 济南 --keyword 麦当劳 --out /tmp/ctx.json
        → now-time-info（时段）+ query-nearby-stores（门店）+ query-meals（菜单）+ list-nutrition-foods（营养）

第 3 步  首轮排序（只需候选列表，暂无套餐组成）
        python scripts/a11y_engine.py --context /tmp/ctx.json --profile elderly --limit 3 --out /tmp/plan.json

第 4 步  补齐真实组成（可选但推荐，用于「里面有…」与可替换项）
        python scripts/mcd_mcp_client.py details-for-plan --plan /tmp/plan.json --store-code 3350332 --out /tmp/dt.json

第 5 步  出方案（JSON / Markdown / 大字 HTML 卡片）
        python scripts/a11y_engine.py --context /tmp/ctx.json --details /tmp/dt.json --profile elderly \
            --out /tmp/plan.json --markdown /tmp/plan.md --html-out /tmp/bigmenu.html

第 6 步  官方核价 -> 停在付款前
        python scripts/mcd_mcp_client.py price-check --store-code 3350332 --items-file <items.json>
        输出末尾必须包含：“以上是建议与官方核价，还没下单。确认要点请告诉我。”
```

## 第 1 步：最少入参

一次问完，最多 3 个问题：

- 在哪吃 / 怎么取：`eat-in` 堂食、`locker-in` 外带、麦乐送（需配送地址）
- 谁吃：`elderly` / `lowvision` / `driver` / `child` / `general`
- 硬约束：不要辣、不要冰、要热饮、控钠上限（如 1000mg）、预算

## 第 2–5 步：引擎做了什么

1. **时段适配**：用 `now-time-info` 的真实服务器时间判定时段，不同时段给不同提示
   （例如夜宵时段明确提醒长辈不宜夜间进食）。
2. **过滤**：按画像默认排除。「长辈」默认排除 辣 / 冰、冷 / 难嚼、脆硬 / 带骨、带刺。
   排除原因逐条保留，便于向用户解释为什么某个常见品没出现。
3. **控制钠**：`--max-sodium` 可按整份真实子项聚合值硬过滤；超标者被排除而非隐藏。
4. **讲人话**：用 `query-meal-detail` 的 `rounds` 把套餐翻译成「里面有板烧鸡腿堡、中薯条和可乐中杯」，
   并列出真实可替换项及官方差价（如「小杯玉米杯 -¥1.5」）。
5. **聚合营养**：套餐整体钠/热量由**真实子项**求和得到；只要不是全部子项都匹配到官方营养数据，
   就标注「已知 X/Y 项合计……非整份真实合计」，不用残缺数据冒充总量。

## 第 6 步：官方核价与停手

`plan.json` 的 `priceCheck.items` 可直接回灌 `calculate-price`；金额由「分」换算成「元」后展示。
核价完成即止，**下单必须由用户明确确认后再调用 `create-order`**，
且 `create-order` 还需从核价返回的 `takeWayList[].code` 取 `takeWayCode`（如 `eat-in`）。

## 输出要求

- 默认大字号、短句、分点；关键标签（辣 / 冰 / 热 / 儿童 / 好拿 / 需餐具）显式写出。
- 套餐必须用一句人话说明包含什么。
- 每次输出都要包含：非麦当劳官方产品、输出仅供参考、餐品与价格以官方实时为准、
  营养数据仅供参考不构成医疗建议、标签为本地推导而非官方分类。

## 硬约束

- 不得输出真实 Token、手机号、配送地址等个人信息。
- 不得将麦当劳与其他品牌对比。
- 不得代替用户下单或支付。
- 不得给出医疗 / 营养治疗建议；仅做参考性提示。
- 单 Token 每分钟不超过 600 次请求；本仓库脚本已内置温和限速。

## 文件说明

| 文件 | 作用 |
|---|---|
| `SKILL.md` | 本工作流定义 |
| `MCP_INTEGRATION.md` | MCP 接入说明（真实工具、调用流程、业务价值） |
| `CONTEST_DECLARATION.md` | 参赛声明（官方文件，内容不可改） |
| `README.md` | 项目介绍、安装、使用示例、目标用户 |
| `workbuddy.md` | 使用 WorkBuddy 开发的对话上下文 |
| `mcp-config.example.json` | 配置示例（仅环境变量占位符） |
| `scripts/mcd_mcp_client.py` | MCP 客户端：连通性、上下文抓取、组成抓取、核价 |
| `scripts/a11y_engine.py` | 无障碍引擎：过滤 / 排序 / 组成解释 / 渲染 |
| `tests/test_smoke.py` | 离线冒烟测试（35 项，含安全护栏） |
| `references/mcp-tools.md` | 由 `tools/list` 实时响应生成的工具清单 |
| `references/real-api-contract.md` | 点餐域真实契约与实测错误码 |
| `examples/` | 真实抓取的样例数据、样例方案、大字号 HTML 示例 |
