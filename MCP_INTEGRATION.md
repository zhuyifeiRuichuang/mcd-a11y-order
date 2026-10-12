# MCP 接入说明 / `mcd-a11y-order`

## MCP Server

- 名称：`mcd-mcp`
- 接入地址：`https://mcp.mcd.cn`
- 协议：Streamable HTTP
- 鉴权：`Authorization: Bearer ${MCD_MCP_TOKEN}`（Token 通过环境变量注入，禁止硬编码）

Token 读取优先级：`--token` > 环境变量 `MCD_MCP_TOKEN` > 项目根目录 `.env`。
Token 仅用于请求头，**不会写入任何产物文件**（已在 `tests/test_smoke.py` 中断言）。

真实工具总数：**35**（由 `tools/list` 实时返回，清单见 `references/mcp-tools.md`）。

## 本 Skill 实际使用的工具

| 工具 | 在项目中的角色 |
|---|---|
| `now-time-info` | 用真实服务器时间判定用餐时段，驱动时段化建议 |
| `query-nearby-stores` | 定位门店，取 `storeCode` / 营业时间 / 营业状态 / `beCode` |
| `query-meals` | 拉取门店真实菜单：名称、code、现价、原价、所属分类 |
| `query-meal-detail` | 拉取套餐真实组成（rounds）、可替换项与差价、特调项（去冰/不要酱） |
| `calculate-price` | 官方实时核价：商品金额 / 优惠 / 应付，并返回 `takeWayList` |
| `list-nutrition-foods` | 官方营养数据（能量 / 蛋白质 / 脂肪 / 碳水 / 钠 / 钙） |

> 本项目**不使用** `create-order`。核价完成即停止，下单必须由用户明确确认后另行发起。

## 调用流程

```
now-time-info            → 判定时段（早餐/午餐/下午茶/晚餐/夜宵）
        ↓
query-nearby-stores      → 选门店 → storeCode（+ beCode）
        ↓
query-meals              → 真实菜单与价格 → 过滤 / 排序 / 打推导标签
        ↓                        ↘
query-meal-detail        → 套餐真实组成         list-nutrition-foods → 真实营养
        ↓                                                  ↓
    「里面有…」人话解释 + 可替换项与差价          单品钠/热量 + 套餐真实子项聚合
        ↓                                                  ↓
        └────────────────→ 1~3 个极简方案 ←────────────────┘
                                 ↓
                        calculate-price（官方核价，分→元）
                                 ↓
                        停在付款前，等用户确认
```

## 业务价值

麦当劳 MCP 提供的是"能力"（门店、菜单、组成、营养、核价），而真实用户要的是"决定"。
尤其对长辈、低视力、驾驶中的人来说，**选项本身就是负担**。

本 Skill 把这些分散的接口收敛成一次可得的决策结果：

1. **把选择负担降到 1–3 项**：按人物画像过滤掉不适合的品项，并保留排除原因。
2. **把"看不懂"变成"听得懂"**：套餐组成翻译成一句话，可替换项附上官方差价。
3. **把"大概健康"变成"有据可依"**：整份钠/热量由真实子项聚合，数据不全时如实标注而非猜测。
4. **把"到了才知道价格"提前**：核价与方案一并给出（金额由官方返回的分换算为元），然后停在付款前。

技术上的一个额外价值：官方文档未覆盖的点餐域真实契约（必填参数、金额单位、`isError` 恒为 false 的陷阱、
券商品不在菜单却可核价等）已在 `references/real-api-contract.md` 中逐条记录并经 live 验证，可直接复用。

## 速率与合规

- 单 Token 每分钟上限 600 次请求；客户端内置温和限速（默认 ≥120ms 间隔）。
- 不输出真实 Token / 个人信息；配置文件仅用环境变量占位符。
- 餐品、价格、供应状态一律以官方实时结果为准。
- **未经用户明确确认，绝不调用 `create-order` / `party-order-create` / `mall-create-order`。**

## 实测错误码（节选）

| code | 场景 | 处理 |
|---|---|---|
| 401 | Token 无效（响应体为纯文本） | 提示更换 Token，不做 JSON 解析 |
| 400 | 参数类型不合法 | 本地已做严格入参校验，提前拦截 |
| 60002 | `storeCode` 与 `beCode` 不配对 | 提示重新选门店 |
| 600020 | 配送地址不存在 | 引导先创建配送地址 |
| 600050 | 收藏门店为空 | 自动改用 city + keyword 搜索 |

完整记录见 `references/real-api-contract.md`。

## 参考链接

- 麦当劳 MCP Server 使用指南：<https://github.com/M-China/mcd-mcp-server>
- 官方 MCP 接入指南：<https://open.mcd.cn/mcp/doc>
