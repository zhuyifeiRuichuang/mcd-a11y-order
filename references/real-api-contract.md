# 麦当劳 MCP 点餐域真实契约（2026-10-12 live 实测）

> 本文件记录的每一条都来自真实请求与真实响应，凡未经实测确认的一律不写。
> 复现方式：`export MCD_MCP_TOKEN=<你的 Token>` 后使用 `scripts/mcd_mcp_client.py`。

## 0. 三个最容易踩的坑

| 坑 | 现象 | 正确处理 |
|---|---|---|
| `isError` 恒为 `false` | 业务失败时 MCP 层仍返回成功 | 必须校验 `result.structuredContent.success`，失败体为 `{success:false, code, message}` |
| Token 无效 | HTTP **401**，响应体是纯文本 `当前鉴权码不存在或已失效!`（不是 JSON） | 按 401 单独处理并提示换 Token，不要尝试 JSON 解析 |
| `authToken` 头 | 会员/账户域工具缺该头返回 `403 authToken必填` | **点餐域不需要**；实测仅 `Authorization: Bearer` 即可拿到 109 个餐品。本客户端统一双发以便扩展 |

优先解析目标：`result.structuredContent`（服务端已结构化的 `{success, code, message, data}`），
比从 `content[].text` 里抠 JSON 稳得多。

## 1. `now-time-info`

- 入参：无
- 返回 `data`：`{timestamp, datetime, formatted, date, year, month, day, dayOfWeek, timezone, offset, utc}`
- 用途：判定当前处于早餐 / 午餐 / 下午茶 / 晚餐 / 夜宵时段。
- 实测可用（该工具真实存在）。

## 2. `query-nearby-stores`

- **必填**：`beType`（1 到店自提 / 5 得来速）、`searchType`（1 收藏门店 / 2 位置搜索）
- 选填：`city`、`keyword`
- 实测：收藏列表为空时返回 `success:false, code:600050, message:"收藏餐厅列表为空，请您提供城市+关键词进行搜索"`
- `data[]` 字段：`storeCode`、`storeName`、`address`、`distance`、`businessStatus`(bool)、
  `businessStartTime`/`businessEndTime`、`beCode`、`reservation`、`reservationTimeOptions`
- 注意：`beType=1` 时门店**没有** `beCode`，后续调用不要传 `beCode`。

## 3. `query-meals`

- **必填**：`storeCode`、`orderType`（1 到店 / 2 外送）、`beType`
- 返回 `data`：`{categories: [{name, meals: [{code, tags}]}], meals: {<code>: {...}}}`
- `meals[code]` 字段全集（实测）：`name`、`image`、`currentPrice`、`originalPrice`、`canWithOrder`、`discountType`、`withOrder`
- **关键事实**：`meals` 里**没有** `tags` 字段。`tags` 只出现在 `categories[].meals[]` 里，
  且内容是营销标签（`上新` / `爆款` / `麦金卡` / `100%纯牛肉` 等），**不是**辣/儿童/温度分类。
  所以「辣 / 冰 / 好拿 / 软」这类无障碍属性只能本地推导，必须显式标注为派生字段。
- 价格单位是**元**（字符串，如 `"34.5"`）；`originalPrice` 在存在优惠时大于 `currentPrice`。

## 4. `query-meal-detail`

- **必填**：`storeCode`、`orderType`、`beType`、`code`
- 返回 `data`：`{code, name, rounds[], image, supportModify}`
- `rounds[]`：`{id, name, quantity, minQuantity, maxQuantity, choices[]}`
- `choices[]`：`{code, name, quantity, maxQuantity, isDefault, diffPrice, supportModify, modification}`
  - `diffPrice` 形如 `"+ ¥1.5"` / `"- ¥1.5"` / `"+ ¥0"`，是与默认项的差价
  - `modification.items[].values[]`：`{code, price, name, maxQuantity, minQuantity, selectedQuantity, selectedKey, unselectedKey}`
- **这是无障碍场景的关键能力**：实测「选择套餐内饮料」轮次提供 `标准 / 去冰 / 多冰 / 少冰`，
  「板烧鸡腿堡」提供 `烤味酱 / 切块生菜` 的增删，正好对应长辈常要求的「去冰」「不要酱」。
- 真实命名差异：菜单里叫「麦香鱼套餐」，详情返回名是「麦香鱼中套餐」——按 `code` 关联，别按名字。

## 5. `calculate-price`

- **必填**：`storeCode`、`orderType`、`beType`；核心是 `items`
- `items[]`：`{productCode, quantity, couponId?, couponCode?, modification?}`
  - `quantity` 须 ≥ 1 整数
  - `modification`：`{values: [{code, key, quantity}]}`
  - **官方 key 规则**：选中项用 `selectedKey`；同一组内若存在 `unselectedKey`，未选中项也必须用 `unselectedKey` 传入
- **返回金额单位为「分」**：实测 `price: 3450` 即 ¥34.50
- 另返回 `takeWayList: [{code, title, subtitle}]`，实测为 `eat-in`(堂食)、`locker-in`(外带取餐柜)；
  `create-order` 的 `takeWayCode` 必须取自这里。
- 实测：带 `modification`（去冰 `key:"0-0"`、不要酱 `key:"0-0"`）的请求被服务端正常接受并返回 `success:true`，
  但**返回体中不会回显具体特调项**，因此定制是否最终生效需以下单后 `query-order` 为准，本工具不替用户断言。

## 6. 优惠券

- `query-store-coupons`（必填 `storeCode`/`orderType`/`beType`）返回 `data[]`：
  `{title, couponId, couponCode, tradeDateTime, products:[{productCode, productName}], promotionId}`
- **实测反直觉的一点**：券对应的 `productCode` 可能**不在** `query-meals` 的菜单里
  （例：`9900016371 王牌小食拼`），但仍然可以核价并正常打折：
  原价 ¥22.50 → 应付 ¥15.90（`discount: 660` 分）。因此不要以「不在菜单里」为由拒绝用券。

## 7. `list-nutrition-foods`

- 入参：无；返回是**文本表**（非 JSON）：
  ```
  [160]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:
    猪柳麦满分,null,1288,308,16,16,24,781,213
  ```
- 实测全体 160 行均为标准 **9 列**；第二列 `nutritionDescription` 多为 `null`，
  **必须显式剔除**，否则菜名会被污染成 `可乐中杯,null`，导致后续按名匹配全部失败。
- 单位：`energyKj` 千焦、`energyKcal` 千卡、`protein/fat/carbohydrate` 克、`sodium/calcium` 毫克。
- 实测匹配率：菜单品名与营养库品名精确/归一化后命中约一半，套餐类几乎无直接命中。
  因此本项目对**套餐**改用「真实子项（`rounds` 默认 choice）营养求和」的方式，
  并要求全部子项匹配才给出整份合计，否则标注「部分匹配」而不假装是真实总量。

## 8. 麦乐送 / 历史订单（实测边界）

- `delivery-query-addresses`：返回 `{addresses: []}`（本测试账户无配送地址）——必须以空集合优雅降级。
- `delivery-query-stores`：需 `beType` + `addressId`；传入不存在地址返回 `success:false, code:600020, "配送地址不存在"`。
- `order-list`：本测试账户返回空 `{}`；「照着上次再点一遍」依赖历史订单，账户无历史时该功能自动失效。

## 9. 实测错误码

| code | 场景 |
|---|---|
| 400 | 参数类型不对（如 `orderType` 传字符串）提示「缺少参数」 |
| 60002 | `storeCode` 与 `beCode` 不配对 / 未查到门店信息 |
| 600020 | 配送地址不存在 |
| 600050 | 收藏餐厅列表为空 |
| 610403 | 商城商品已下架（商城域） |

## 10. 下单（本项目不执行）

`create-order` 必填 `storeCode`/`orderType`/`beType`，另需：
- `takeWayCode`：取自 `calculate-price` 返回的 `takeWayList[].code`（`orderType=1` 时必填）
- `beCode`：得来速(5)/外送(2)/团餐(6) 必填；到店自提(1) 不传
- `addressId`：`orderType=2` 时必填

**本项目脚本不调用 `create-order`**，测试一律停在付款前。
