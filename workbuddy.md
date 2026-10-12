# workbuddy.md

本文件记录该 Skill 在 WorkBuddy 中的开发上下文，用于核验 WorkBuddy 联动活动奖励条件。

- 工具：WorkBuddy（本次活动官方合作伙伴）
- 工作区：`C:/Users/zhuyi/Downloads/麦当劳挑战赛/mcd-a11y-order`
- 项目：麦当劳无障碍 / 适老化点餐 Skill（`mcd-a11y-order`）
- MCP 连接器：`mcd-mcp`（`https://mcp.mcd.cn`，Streamable HTTP）

## 需求

分析已有项目 `mcd-a11y-order`（无障碍 / 适老化点餐），并完成三项：

1. 使用麦当劳 MCP 真实调测，禁止猜测，确认功能真实可用且无故障；
2. 完整检查功能、模拟测试确认可用、排查并解决功能安全隐患，把旧配置更新到最新适配版本；
3. 完成后推送发布到 GitHub 新建仓库，带版本号发布正式 Release，
   并按 <https://github.com/M-China/mcd-developer-innovation-challenge#开发-skill> 的要求全量提交并报名。

## 过程

1. **真实调测**：用真实 Token 直连 `https://mcp.mcd.cn`，先取 `tools/list`（35 个工具）确认真实工具名与入参，
   再逐项 live 验证 `now-time-info`、`query-nearby-stores`、`query-meals`、`query-meal-detail`、
   `query-store-coupons`、`calculate-price`、`list-nutrition-foods`、`delivery-query-addresses`、`order-list`。
2. **纠正旧文档的错误假设**：
   - 旧 `references/mcp-tools.md` 手写清单漏了 `query-promotions`、`query-survey-coupon`，
     且派对工具名写成 `query-partystore-date`（真实为 `query-party-store-date`）；已改为由 `tools/list` 自动生成。
   - 旧 SKILL/README 声称「菜单带辣/儿童/新品标签」——实测 `query-meals` 的 `meals` **没有** `tags` 字段，
     `tags` 只在 `categories[].meals[]` 且是营销标签。标签改为本地推导并明确标注派生。
   - 旧文档未写 `query-nearby-stores` 的 `beType`/`searchType` 必填，照抄会直接失败。
3. **修复真实缺陷**：
   - 营养表第 2 列 `nutritionDescription` 未剔除，导致菜名污染成「可乐中杯,null」，匹配全部失败；已按 9 列表头对齐修复。
   - 套餐营养仅在部分子项匹配时仍展示整份合计（严重误导）；改为标注「已知 X/Y 项，非整份真实合计」，
     且未全覆盖时不参与钠相关排序扣分。
   - `isError` 恒为 `false` 的陷阱：改为校验 `structuredContent.success`。
   - 补充入参安全校验（storeCode/code/item quantity 的类型与范围，限 30 件），在发起网络请求前拦截。
4. **离线自检**：`tests/test_smoke.py` 共 35 项断言全通过，覆盖解析、过滤、聚合诚实性、渲染与不下单护栏。

## 结论

- 本 Skill 的读链路已在真实 MCP 上逐项验证通过，核价链路端到端跑通
  （示例：板烧鸡腿堡三件套 + 麦香鸡套餐 + 汉堡开心乐园餐 → 官方核价 ¥85.50）。
- **未发起任何真实下单**，测试一律停在付款前（`create-order` 未被调用）。
- 未在代码或产物中留存任何 Token / 个人信息。
