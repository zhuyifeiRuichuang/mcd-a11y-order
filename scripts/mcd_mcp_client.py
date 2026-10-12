#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""麦当劳 MCP Streamable HTTP 客户端（仅标准库，零第三方依赖）。

直连 https://mcp.mcd.cn，走 JSON-RPC：
    tools/list  ->  查看真实工具与入参
    tools/call  ->  调用工具

子命令：
    ping                校验 Token 与连通性
    tools-list          列出全部工具（--out 导出含入参 schema）
    call <tool> <json>  调用任意工具
    fetch-context       一次性拉取「无障碍点餐」所需真实上下文（时间/门店/菜单/营养）
    fetch-detail        拉取单个餐品的真实组成（rounds）与特调项
    details-for-plan    对 a11y_engine 输出的候选批量拉取真实组成（闭环第 2 轮）
    price-check         用真实 calculate-price 核价（分 -> 元）

Token 读取优先级：--token > 环境变量 MCD_MCP_TOKEN > .env 文件。
Token 仅用于请求头，绝不写入任何产物文件。

已实测的真实契约要点（2026-10-12 live 验证）：
  * 点餐域工具只需 Authorization: Bearer；会员/账户域工具还需 authToken 头（本客户端双发）。
  * isError 恒为 false，真实失败藏在 result.structuredContent.success=false + code/message 中，
    只看 isError 会把失败当成功，必须校验 success。
  * 优先解析 result.structuredContent（干净的 {success,code,message,data}），失败时回退文本解析。
  * Token 无效返回 HTTP 401 纯文本。
"""

import argparse
import datetime as dt
import json
import os
import re
import sys
import time
import urllib.error
import urllib.request

DEFAULT_URL = "https://mcp.mcd.cn"
PROTOCOL_VERSION = "2025-06-18"
TIMEOUT = 40
USER_AGENT = "mcd-a11y-order/1.0"
RATE_INTERVAL = 0.12  # 温和限速，远低于 600 次/分钟上限


class McpError(RuntimeError):
    pass


# ---------------------------------------------------------------- Token

def load_token(cli_token: str = "") -> str:
    if cli_token:
        return cli_token
    env = os.environ.get("MCD_MCP_TOKEN", "").strip()
    if env:
        return env
    roots = (os.path.dirname(os.path.dirname(os.path.abspath(__file__))), os.getcwd())
    for base in roots:
        env_path = os.path.join(base, ".env")
        if os.path.isfile(env_path):
            with open(env_path, encoding="utf-8") as fh:
                for line in fh:
                    line = line.strip()
                    if line.startswith("MCD_MCP_TOKEN="):
                        return line.split("=", 1)[1].strip().strip('"').strip("'")
    return ""


# ---------------------------------------------------------------- 协议

_LAST_CALL = [0.0]


def rpc(method: str, params, token: str, url: str):
    global _LAST_CALL
    gap = time.time() - _LAST_CALL[0]
    if gap < RATE_INTERVAL:
        time.sleep(RATE_INTERVAL - gap)

    payload = {"jsonrpc": "2.0", "id": 1, "method": method}
    if params is not None:
        payload["params"] = params
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
        "Authorization": "Bearer " + token,
        # 会员/账户类工具要求同时携带 authToken（值同 Token）。点餐工具无需该头，
        # 但携带无害，统一双发可避免后续扩展工具时踩坑。
        "authToken": token,
        "User-Agent": USER_AGENT,
    }
    req = urllib.request.Request(
        url, data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers=headers, method="POST",
    )
    try:
        resp = urllib.request.urlopen(req, timeout=TIMEOUT)
        raw = resp.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", "replace")
        if exc.code == 401:
            raise McpError("401 Token 不存在或已失效，请检查 MCD_MCP_TOKEN。")
        if exc.code == 429:
            raise McpError("429 触发限流（600 次/分钟），请降低调用频率。")
        raise McpError("HTTP %s：%s" % (exc.code, detail[:300]))
    except urllib.error.URLError as exc:
        raise McpError("网络错误：%r（若需代理请检查 HTTPS_PROXY 设置）" % (exc,))
    finally:
        _LAST_CALL[0] = time.time()

    raw = raw.strip()
    if not raw:
        return {}
    if raw.startswith("event:") or raw.startswith("data:") or "\ndata:" in raw:
        raw = "\n".join(ln[5:].strip() for ln in raw.splitlines() if ln.startswith("data:"))
    try:
        msg = json.loads(raw)
    except ValueError:
        raise McpError("响应不是合法 JSON：%s" % raw[:200])
    if "error" in msg:
        err = msg["error"]
        raise McpError("MCP 错误 %s：%s" % (err.get("code"), err.get("message")))
    if "result" not in msg:
        raise McpError("缺少 result 字段：%s" % raw[:200])
    return msg["result"]


def tools_list(token: str, url: str):
    return rpc("tools/list", {}, token, url).get("tools", [])


def call_tool(name: str, arguments: dict, token: str, url: str):
    return rpc("tools/call", {"name": name, "arguments": arguments or {}}, token, url)


# ---------------------------------------------------------------- 解析

def unwrap(result: dict):
    """统一提取业务 data。

    优先用 result.structuredContent（服务端已结构化，契约最稳）；
    校验 success=false 时抛错 —— 这是本 MCP 最容易踩的坑：
    isError 恒为 false，只看它会把业务失败当成功。
    """
    if not isinstance(result, dict):
        return result

    structured = result.get("structuredContent")
    if isinstance(structured, dict) and structured:
        success = structured.get("success")
        if success is False:
            raise McpError("[%s] %s" % (structured.get("code"), structured.get("message")))
        return structured.get("data")

    content = result.get("content") or []
    texts = "\n".join(c.get("text", "") for c in content if c.get("type") == "text").strip()
    parsed = extract_json(texts)
    if isinstance(parsed, dict) and "success" in parsed:
        if not parsed.get("success"):
            raise McpError("[%s] %s" % (parsed.get("code"), parsed.get("message")))
        data = parsed.get("data")
        inner = extract_json(data) if isinstance(data, str) else data
        return inner if inner is not None else data
    if parsed is not None:
        return parsed
    return texts


def extract_json(text):
    if isinstance(text, (dict, list)):
        return text
    if not isinstance(text, str):
        return None
    stripped = text.strip()
    starts = [i for i in (stripped.find('{"'), stripped.find("[{")) if i >= 0]
    if not starts:
        return None
    start = min(starts)
    open_ch, close_ch = stripped[start], ("}" if stripped[start] == "{" else "]")
    depth, in_str, esc = 0, False, False
    for idx in range(start, len(stripped)):
        ch = stripped[idx]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
            continue
        if ch == '"':
            in_str = True
        elif ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                try:
                    return json.loads(stripped[start:idx + 1])
                except ValueError:
                    break
    return None


# ---------------------------------------------------------------- 结构化辅助

def parse_nutrition_table(raw):
    """解析 list-nutrition-foods 返回的文本表（9 列 CSV，容忍缺列与菜名含逗号）。

    真实表头（实测 160 行全部为 9 列）：
        [160]{productName,nutritionDescription,energyKj,energyKcal,protein,
              fat,carbohydrate,sodium,calcium}:
          猪柳麦满分,null,1288,308,16,16,24,781,213

    要点：必须显式剔除第 2 列的 nutritionDescription（多为 "null"），
    否则菜名会污染成 "可乐中杯,null"，后续按名匹配失败。
    """
    rows = []
    if not isinstance(raw, str):
        return rows
    cols = ["productName", "nutritionDescription", "energyKj", "energyKcal",
            "protein", "fat", "carbohydrate", "sodium", "calcium"]
    numeric_cols = [c for c in cols if c not in ("productName", "nutritionDescription")]
    n_num = len(numeric_cols)

    def to_float(v):
        try:
            return float(v)
        except (TypeError, ValueError):
            return None

    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("-"):
            continue
        if line.startswith("[") and "{" in line and "}" in line:
            continue  # 表头行
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < n_num + 1:
            continue

        # 按表头对齐：末尾 n_num 个字段是数值列，倒数第 n_num+1 个是 description
        tail = parts[-n_num:]
        parsed = [to_float(v) if v != "null" else None for v in tail]
        if all(v is None for v in parsed):
            continue  # 噪声行：没有任何可解析数值，丢弃而非猜测

        rest = parts[:-n_num]
        if len(rest) >= 2:
            name = ",".join(rest[:-1]).strip()
            description = rest[-1]
        else:
            name, description = rest[0].strip(), None
        if not name:
            continue

        row = {"productName": name}
        if description not in (None, "", "null"):
            row["nutritionDescription"] = description
        row.update(dict(zip(numeric_cols, parsed)))
        row["complete"] = all(v is not None for v in parsed)
        rows.append(row)
    return rows


def build_category_index(data: dict):
    """categories[].meals[] 里才带 tags（营销标签），据此建 code -> tags 索引。"""
    index = {}
    for cat in (data or {}).get("categories") or []:
        cat_name = (cat.get("name") or "").replace("\n", "")
        for entry in cat.get("meals") or []:
            code = str(entry.get("code") or "")
            if not code:
                continue
            tags = [t for t in (entry.get("tags") or []) if isinstance(t, str)]
            bucket = index.setdefault(code, {"categories": [], "tags": []})
            if cat_name and cat_name not in bucket["categories"]:
                bucket["categories"].append(cat_name)
            for t in tags:
                if t not in bucket["tags"]:
                    bucket["tags"].append(t)
    return index


def structure_menu(data: dict):
    """把 query-meals 的 {categories, meals} 压平成标准餐品列表。"""
    index = build_category_index(data)
    out = []
    for code, detail in ((data or {}).get("meals") or {}).items():
        if not isinstance(detail, dict):
            continue
        name = (detail.get("name") or "").strip()
        if not name:
            continue
        try:
            cur = float(detail.get("currentPrice"))
        except (TypeError, ValueError):
            cur = None
        try:
            org = float(detail.get("originalPrice"))
        except (TypeError, ValueError):
            org = None
        meta = index.get(str(code), {})
        out.append({
            "code": str(code),
            "name": name,
            "currentPrice": cur,
            "originalPrice": org,
            "unit": "元",
            "categories": meta.get("categories", []),
            "shopTags": meta.get("tags", []),
            "canWithOrder": bool(detail.get("canWithOrder")),
            "discountType": detail.get("discountType"),
            "withOrder": detail.get("withOrder"),
        })
    out.sort(key=lambda m: m["code"])
    return out


def structure_stores(raw):
    stores = []
    for s in raw if isinstance(raw, list) else []:
        stores.append({
            "storeCode": str(s.get("storeCode") or ""),
            "storeName": s.get("storeName"),
            "address": s.get("address"),
            "distance": s.get("distance"),
            "businessStatus": bool(s.get("businessStatus")),
            "businessHours": "%s-%s" % (s.get("businessStartTime"), s.get("businessEndTime")),
            "beCode": s.get("beCode"),
            "reservation": bool(s.get("reservation")),
        })
    return stores


def structure_detail(raw):
    """把 query-meal-detail 的 rounds 压平成「人话组成 + 可替换 + 特调」。"""
    if not isinstance(raw, dict):
        return None
    rounds = []
    for rd in raw.get("rounds") or []:
        choices = []
        for c in rd.get("choices") or []:
            mods = []
            for item in ((c.get("modification") or {}).get("items") or []):
                mods.append({
                    "minValues": item.get("minValues"),
                    "maxValues": item.get("maxValues"),
                    "values": [{
                        "code": v.get("code"),
                        "name": v.get("name"),
                        "price": v.get("price"),
                        "selectedQuantity": v.get("selectedQuantity"),
                        "selectedKey": v.get("selectedKey"),
                        "unselectedKey": v.get("unselectedKey"),
                    } for v in (item.get("values") or [])],
                })
            choices.append({
                "code": str(c.get("code") or ""),
                "name": c.get("name"),
                "isDefault": c.get("isDefault") == 1,
                "diffPrice": c.get("diffPrice"),
                "supportModify": bool(c.get("supportModify")),
                "modification": mods,
            })
        rounds.append({
            "id": rd.get("id"),
            "name": rd.get("name"),
            "minQuantity": rd.get("minQuantity"),
            "maxQuantity": rd.get("maxQuantity"),
            "choices": choices,
        })
    return {
        "code": str(raw.get("code") or ""),
        "name": raw.get("name"),
        "supportModify": bool(raw.get("supportModify")),
        "rounds": rounds,
    }


def structure_price(raw):
    """calculate-price 返回金额单位为「分」，统一换算为元便于展示。"""
    def yuan(v):
        try:
            return round(float(v) / 100.0, 2)
        except (TypeError, ValueError):
            return None

    if not isinstance(raw, dict):
        return None
    return {
        "productOriginalPriceYuan": yuan(raw.get("productOriginalPrice")),
        "productPriceYuan": yuan(raw.get("productPrice")),
        "originalPriceYuan": yuan(raw.get("originalPrice")),
        "discountYuan": yuan(raw.get("discount")),
        "payableYuan": yuan(raw.get("price")),
        "productList": [{
            "productCode": str(p.get("productCode") or ""),
            "productName": p.get("productName"),
            "quantity": p.get("quantity"),
            "originalSubtotalYuan": yuan(p.get("originalSubtotal")),
            "subtotalYuan": yuan(p.get("subtotal")),
        } for p in (raw.get("productList") or [])],
        "takeWayList": raw.get("takeWayList") or [],
        "note": "服务端金额单位为分，此处已除以 100 换算为元。",
    }


# ---------------------------------------------------------------- 子命令

def dump(payload, out_path):
    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if out_path:
        with open(out_path, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("已写入：%s" % os.path.abspath(out_path))
    else:
        print(text)


CODE_RE = re.compile(r"^[0-9]{1,20}$")
TEXT_RE = re.compile(r"^[\w一-龥·\-（）()\s]{0,60}$")


def guard_code(value: str, field: str) -> str:
    """门店编码/餐品编码必须是纯数字（官方约束），提前拦截避免脏数据入请求体。"""
    text = str(value or "").strip()
    if not CODE_RE.match(text):
        raise McpError("非法 %s：%r（须为 1-20 位数字）" % (field, text[:40]))
    return text


def guard_text(value: str, field: str, limit: int = 60) -> str:
    """城市名/关键词长度与字符集限制，避免异常长串与注入字符。"""
    text = str(value or "").strip()
    if not text:
        return ""
    if len(text) > limit or not TEXT_RE.match(text):
        raise McpError("非法 %s：%r（长度 ≤%d 且仅允许中英文、数字、常用标点）"
                       % (field, text[:40], limit))
    return text


def cmd_fetch_context(args, token, url):
    """拉取无障碍点餐所需的全部真实上下文，落盘后可离线复用。"""
    now = None
    try:
        now = unwrap(call_tool("now-time-info", {}, token, url))
    except McpError as exc:
        print("! now-time-info 不可用，改用本地时钟：%s" % exc, file=sys.stderr)

    city = guard_text(args.city, "city")
    keyword = guard_text(args.keyword, "keyword")
    if args.store_code:
        guard_code(args.store_code, "storeCode")
    stores_raw = unwrap(call_tool("query-nearby-stores", {
        "beType": args.be_type,
        "searchType": 2 if keyword or city else 1,
        **({"city": city} if city else {}),
        **({"keyword": keyword} if keyword else {}),
    }, token, url))
    stores = structure_stores(stores_raw)
    if not stores:
        raise McpError("未查询到门店：请提供 --city 与 --keyword，或先收藏门店。")
    target = None
    for s in stores:
        if s["storeCode"] == args.store_code:
            target = s
            break
    target = target or stores[0]

    order_type = 2 if args.be_type in (2, 6) else 1
    meal_params = {"storeCode": target["storeCode"], "orderType": order_type, "beType": args.be_type}
    if args.be_code:
        meal_params["beCode"] = args.be_code
    elif target.get("beCode"):
        meal_params["beCode"] = target["beCode"]

    menu_raw = unwrap(call_tool("query-meals", meal_params, token, url))
    menu = structure_menu(menu_raw)

    nutrition = []
    try:
        nutrition = parse_nutrition_table(unwrap(call_tool("list-nutrition-foods", {}, token, url)))
    except McpError as exc:
        print("! list-nutrition-foods 不可用，跳过营养数据：%s" % exc, file=sys.stderr)

    payload = {
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "data_source": "mcp(live)",
        "source_tools": ["now-time-info", "query-nearby-stores", "query-meals", "list-nutrition-foods"],
        "server_time": now,
        "store": target,
        "candidate_stores": stores,
        "order_params": meal_params,
        "meals": menu,
        "nutrition": nutrition,
        "security": {"token_redacted": True, "fields_only_public_business_info": True},
    }
    dump(payload, args.out)
    print("门店：%s（%s）  餐品：%d  营养：%d" % (
        target["storeName"], target["storeCode"], len(menu), len(nutrition)))
    return 0


def cmd_fetch_detail(args, token, url):
    params = {"storeCode": guard_code(args.store_code, "storeCode"),
              "orderType": args.order_type, "beType": args.be_type,
              "code": guard_code(args.code, "code")}
    if args.be_code:
        params["beCode"] = args.be_code
    detail = structure_detail(unwrap(call_tool("query-meal-detail", params, token, url)))
    payload = {
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "data_source": "mcp(live)",
        "source_tool": "query-meal-detail",
        "params": params,
        "detail": detail,
    }
    dump(payload, args.out)
    if detail:
        print("%s：%d 个选配轮次" % (detail["name"], len(detail["rounds"])))
    return 0


def cmd_details_for_plan(args, token, url):
    """给 a11y_engine 输出的候选 TopN 拉取真实套餐组成，闭环两轮工作流。

    第 1 轮：fetch-context -> a11y_engine（无组成说明，仅排序出候选）
    第 2 轮：details-for-plan -> a11y_engine --details（得到「里面有…」与可替换项）
    """
    with open(args.plan, encoding="utf-8") as fh:
        plan = json.load(fh)
    codes = []
    for item in plan.get("candidates") or []:
        code = str(item.get("code") or "")
        if code and code not in codes:
            codes.append(code)
    for p in plan.get("plans") or []:
        code = str(p.get("itemCode") or "")
        if code and code not in codes:
            codes.insert(0, code)
    codes = codes[:max(1, args.limit)]
    if not codes:
        raise McpError("plan 中没有候选餐品 code。")

    details, failures = [], []
    for code in codes:
        params = {"storeCode": guard_code(args.store_code, "storeCode"),
                  "orderType": args.order_type, "beType": args.be_type,
                  "code": guard_code(code, "code")}
        if args.be_code:
            params["beCode"] = args.be_code
        try:
            data = structure_detail(unwrap(call_tool("query-meal-detail", params, token, url)))
        except McpError as exc:
            failures.append({"code": code, "error": str(exc)})
            continue
        if data:
            details.append(data)
    payload = {
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "data_source": "mcp(live)",
        "source_tool": "query-meal-detail",
        "store_code": args.store_code,
        "details": details,
        "failures": failures,
    }
    dump(payload, args.out)
    print("已获取组成：%d 个，失败：%d 个" % (len(details), len(failures)))
    if failures and not details:
        return 1
    return 0


def cmd_price_check(args, token, url):
    """用真实 calculate-price 核价。items 为 JSON 文件、JSON 字符串，或 stdin。"""
    raw = args.items
    if args.items_file:
        with open(args.items_file, encoding="utf-8") as fh:
            raw = fh.read()
    items = json.loads(raw)
    if not isinstance(items, list) or not items:
        raise McpError("items 必须是非空数组。")
    if len(items) > 30:
        raise McpError("单次核价商品数过多：%d（上限 30）" % len(items))
    for it in items:
        qty = it.get("quantity")
        if not isinstance(qty, int) or isinstance(qty, bool) or qty < 1 or qty > 20:
            raise McpError("非法 quantity：%r（须为 1-20 的整数）" % (qty,))
        guard_code(it.get("productCode", ""), "items[].productCode")
        if "modification" in it and not isinstance(it["modification"], dict):
            raise McpError("items[].modification 必须是对象。")
    params = {"storeCode": guard_code(args.store_code, "storeCode"),
              "orderType": args.order_type, "beType": args.be_type, "items": items}
    if args.be_code:
        params["beCode"] = args.be_code
    price = structure_price(unwrap(call_tool("calculate-price", params, token, url)))
    payload = {
        "fetched_at": dt.datetime.now().isoformat(timespec="seconds"),
        "data_source": "mcp(live)",
        "source_tool": "calculate-price",
        "params": {"storeCode": args.store_code, "orderType": args.order_type,
                   "beType": args.be_type, "items": items},
        "price": price,
        "disclaimer": "官方实时核价结果，仅供参考，以麦当劳官方渠道为准。本命令不会下单。",
    }
    dump(payload, args.out)
    if price:
        print("应付 ¥%.2f（优惠 ¥%.2f）" % (
            price["payableYuan"] or 0, price["discountYuan"] or 0))
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description="麦当劳 MCP 客户端（mcd-a11y-order）")
    parser.add_argument("action", choices=["ping", "tools-list", "call", "fetch-context",
                                           "fetch-detail", "details-for-plan", "price-check"])
    parser.add_argument("tool_or_args", nargs="?", default="")
    parser.add_argument("arguments", nargs="?", default="{}")
    parser.add_argument("--token", default="")
    parser.add_argument("--url", default=DEFAULT_URL)
    parser.add_argument("--out", default="")
    parser.add_argument("--store-code", default="")
    parser.add_argument("--be-code", default="")
    parser.add_argument("--be-type", type=int, default=1,
                        help="1-到店自提，2-麦乐送，5-得来速，6-团餐")
    parser.add_argument("--order-type", type=int, default=1, help="1-到店，2-外送")
    parser.add_argument("--city", default="")
    parser.add_argument("--keyword", default="")
    parser.add_argument("--code", default="", help="餐品 code（query-meal-detail）")
    parser.add_argument("--plan", default="", help="details-for-plan：a11y_engine 输出的 plan JSON 路径")
    parser.add_argument("--limit", type=int, default=8, help="details-for-plan：拉取头 N 个候选的组成")
    parser.add_argument("--items", default="[]", help="核价商品 JSON 数组字符串")
    parser.add_argument("--items-file", default="", help="核价商品 JSON 文件路径")
    args = parser.parse_args()

    token = load_token(args.token)
    if not token:
        print("缺少 MCP Token：使用 --token、设置环境变量 MCD_MCP_TOKEN，或写入 .env。", file=sys.stderr)
        return 2

    try:
        if args.action == "ping":
            info = rpc("initialize", {
                "protocolVersion": PROTOCOL_VERSION,
                "capabilities": {},
                "clientInfo": {"name": "mcd-a11y-order", "version": "1.0.0"},
            }, token, args.url)
            print(json.dumps(info, ensure_ascii=False)[:600])
            return 0
        if args.action == "tools-list":
            tools = tools_list(token, args.url)
            print("工具数量：%d" % len(tools))
            for t in tools:
                print("- %s | %s" % (t.get("name"), (t.get("description") or "").split("\n")[0][:60]))
            if args.out:
                with open(args.out, "w", encoding="utf-8") as fh:
                    json.dump(tools, fh, ensure_ascii=False, indent=2)
                print("已写入：%s" % os.path.abspath(args.out))
            return 0
        if args.action == "call":
            params = json.loads(args.arguments or "{}")
            result = unwrap(call_tool(args.tool_or_args, params, token, args.url))
            if args.out:
                dump(result, args.out)
            else:
                print(json.dumps(result, ensure_ascii=False, indent=2)
                      if isinstance(result, (dict, list)) else str(result))
            return 0
        if args.action == "fetch-context":
            return cmd_fetch_context(args, token, args.url)
        if args.action == "fetch-detail":
            for need in (("store_code", "--store-code"), ("code", "--code")):
                if not getattr(args, need[0]):
                    print("fetch-detail 需要 %s。" % need[1], file=sys.stderr)
                    return 2
            return cmd_fetch_detail(args, token, args.url)
        if args.action == "details-for-plan":
            if not args.plan or not args.store_code:
                print("details-for-plan 需要 --plan 与 --store-code。", file=sys.stderr)
                return 2
            return cmd_details_for_plan(args, token, args.url)
        if args.action == "price-check":
            if not args.store_code:
                print("price-check 需要 --store-code。", file=sys.stderr)
                return 2
            return cmd_price_check(args, token, args.url)
    except McpError as exc:
        print("MCP 调用失败：%s" % exc, file=sys.stderr)
        return 1
    except ValueError as exc:
        print("入参不是合法 JSON：%r" % (exc,), file=sys.stderr)
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
