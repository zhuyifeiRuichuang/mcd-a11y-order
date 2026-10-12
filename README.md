# 麦麦无障碍点餐 / `mcd-a11y-order`

> 麦当劳程序员创意开发大赛参赛作品 · 版本 v1.0.0 · 全流程已在真实 MCP 上调通

给长辈、（低）视力受限朋友、开车的人点麦当劳，本来就不该是一场"解密游戏"。
这个项目把官方 MCP 里的真实菜单，**压缩成几张大字卡片**：里面有什么用大白话讲清楚，
钠高不高给出实测数值，最后用官方接口核个价——然后停在这里，把付款交给用户本人。

## 一、解决什么问题

| 真实痛点 | 本项目的做法 |
|---|---|
| 菜单字太小、选项层层嵌套 | 一次只给 1–3 个方案，每张卡片 2–3 行字，另有 ≥24px 大字 HTML 页面可直接给长辈看 |
| 看不懂「XX 三件套」到底有什么 | 用官方 `query-meal-detail` 的真实组成，翻译成"里面有板烧鸡腿堡、中薯条和可乐中杯" |
| 长辈怕辣、怕冰、牙口不好、怕噎 | 按人物画像自动过滤 辣 / 冰 / 难嚼 / 带骨，并保留每条排除原因以便追溯 |
| 不知道整份套餐钠高不高 | 用套餐真实子项聚合真实官方营养数据；未全覆盖时明确标注，不用残缺数据冒充总量 |
| 到了付款环节才发现价格不对 | 核价走官方 `calculate-price`，金额由分转元，绝不本地估算 |

## 二、目标用户

- **老年人**及其家属（子女代点是最常见场景）
- 低视力 / 依赖读屏软件的用户
- 驾驶中无法操作手机的司机
- 需要给儿童点餐、关注是否被辣到或噎到的家长

## 三、安装与配置

1. 申请麦当劳中国 MCP Token：<https://open.mcd.cn/mcp>
2. 安装 `mcd-mcp` 连接器（参考 `mcp-config.example.json`，Token 用 `${MCD_MCP_TOKEN}` 占位，**禁止硬编码**）
3. 把 Token 放进项目根目录 `.env`（该文件已被 `.gitignore` 忽略）：

   ```bash
   MCD_MCP_TOKEN=你的真实Token
   ```

环境要求：Python 3.9+，**零第三方依赖**（仅用标准库）。

## 四、使用示例

### 场景：帮家里长辈点一份不辣、好拿、钠不要太高的午餐

```bash
# 1) 拉真实上下文：时段 + 门店 + 菜单 + 营养（一次搞定）
python scripts/mcd_mcp_client.py fetch-context --city 济南 --keyword 麦当劳 --out /tmp/ctx.json

# 2) 首轮排序，得到候选
python scripts/a11y_engine.py --context /tmp/ctx.json --profile elderly --limit 3 --out /tmp/plan.json

# 3) 补齐候选套餐的真实组成（用于"里面有…"与可替换项）
python scripts/mcd_mcp_client.py details-for-plan --plan /tmp/plan.json --store-code 3350332 --out /tmp/dt.json

# 4) 出方案：JSON + Markdown + 大字号 HTML 卡片
python scripts/a11y_engine.py --context /tmp/ctx.json --details /tmp/dt.json \
    --profile elderly --max-sodium 1000 \
    --out /tmp/plan.json --markdown /tmp/plan.md --html-out /tmp/bigmenu.html

# 5) 官方核价（不会下单）
python scripts/mcd_mcp_client.py price-check --store-code 3350332 \
    --items '[{"productCode":"9900005462","quantity":1}]'
```

真实输出片段（`--profile elderly`，数据取自官方 MCP 实时返回）：

```
## 套餐、好拿、软｜板烧鸡腿堡三件套
板烧鸡腿堡三件套（默认搭配）
里面有 板烧鸡腿堡、中薯条 和 可乐中杯。
价格 ¥34.5，合计约 827 千卡 / 钠约 1206 毫克
注意：整份钠已超过成人全天建议上限(2000mg)的一半，建议搭配白水、少蘸酱
可以换：小杯玉米杯（- ¥1.5）；无糖可口可乐中杯（+ ¥0）；雪碧中杯（+ ¥0）

---
本页只到这一步，还没有下单。确认要点，请再说一声。
```

### 无 Token 也能跑

`examples/` 下已内置真实抓取并脱敏的样例数据，直接执行即有完整结果：

```bash
python scripts/a11y_engine.py --profile elderly --markdown /tmp/plan.md --html-out /tmp/bigmenu.html
python tests/test_smoke.py     # 35 项离线自检，含安全护栏
```

## 五、使用的麦当劳 MCP 工具

真实工具总数 35（清单见 `references/mcp-tools.md`，由 `tools/list` 实时响应生成）。本项目实际用到：

| 工具 | 用途 | 实测结论 |
|---|---|---|
| `now-time-info` | 判定早餐 / 午餐 / 下午茶 / 晚餐 / 夜宵时段 | 存在且返回结构化时间，含 `dayOfWeek` |
| `query-nearby-stores` | 定位门店 | `beType` + `searchType` **必填**；收藏为空时返回 `code:600050` |
| `query-meals` | 拉取门店真实菜单与价格 | 价格单位**元**；`meals` **无** `tags` 字段，只有营销标签 |
| `query-meal-detail` | 套餐真实组成（rounds）与可替换项 | 支持"去冰""不要酱"等真实定制选项 |
| `calculate-price` | 官方实时核价 | 返回金额单位**分**；同时给出 `takeWayList` |
| `list-nutrition-foods` | 官方营养数据 | 文本表 9 列，须剔除第 2 列描述列 |

详细真实契约、错误码与踩坑记录见 `references/real-api-contract.md`。
**本项目不调用 `create-order`**——测试与演示一律停在付款前。

## 六、合规说明

- 本项目为非官方工具，**未经用户明确确认不发起任何下单或支付**。
- "辣 / 冰 / 好拿 / 软 / 需餐具"等标签由本工具依据真实菜单名称推导，**并非麦当劳官方分类字段**，输出中已显式说明。
- 营养与钠含量的提示仅供参考（阈值参考《中国居民膳食营养素参考摄入量(2023)》成人全天 PI-NCD 2000mg），
  **不构成医疗或营养治疗建议**；数据缺失或仅部分子项匹配时会如实标注。
- 不输出 Token、手机号、配送地址等个人信息；配置文件仅使用环境变量占位符。
- 不与任何其他品牌进行对比。

## 七、项目结构

```
mcd-a11y-order/
├── README.md                       项目说明
├── SKILL.md                        Skill 工作流定义
├── MCP_INTEGRATION.md              MCP 接入说明
├── CONTEST_DECLARATION.md          参赛声明（官方文件，内容未改动）
├── workbuddy.md                    WorkBuddy 开发上下文
├── mcp-config.example.json         配置示例（仅占位符）
├── scripts/
│   ├── mcd_mcp_client.py           MCP 客户端（零依赖，带入参安全校验）
│   └── a11y_engine.py              无障碍引擎（过滤/排序/解释/渲染）
├── references/
│   ├── mcp-tools.md                由 tools/list 实时生成的工具清单
│   └── real-api-contract.md        点餐域真实契约与实测错误码
├── examples/
│   ├── sample_context.json         真实抓取脱敏：时间/门店/菜单/营养
│   ├── sample_details.json         真实抓取：6 个套餐的真实组成
│   ├── sample_plan.json / .md      样例方案
│   └── demo_bigmenu.html           大字号点餐卡片示例
└── tests/test_smoke.py             35 项离线自检
```

## 免责声明

参赛项目为麦当劳程序员节创意开发大赛参赛作品，由参赛者独立开发，非麦当劳官方产品。
输出仅供参考，餐品信息、价格及供应状态以麦当劳官方渠道实时结果为准。
