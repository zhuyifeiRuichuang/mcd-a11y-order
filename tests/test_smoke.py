#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""离线冒烟测试（无需 Token / 无需联网）。

覆盖：真实样例数据解析、标签推导、画像过滤、营养聚合的诚实性、
     大字号渲染、安全护栏（不下单 / 不落凭证 / 核价载荷合法）。
运行： python tests/test_smoke.py
"""

import json
import os
import re
import subprocess
import sys

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, os.path.join(ROOT, "scripts"))

os.environ.setdefault("MCD_MCP_TOKEN", "dummy-token-for-tests")

import mcd_mcp_client as client  # noqa: E402
import a11y_engine as engine  # noqa: E402

PASSED, FAILED = [], []


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print("  PASS  %s" % name)
    else:
        FAILED.append(name)
        print("  FAIL  %s %s" % (name, detail))


def load(name):
    with open(os.path.join(ROOT, "examples", name), encoding="utf-8") as fh:
        return json.load(fh)


print("== 1. 真实样例数据 ==")
context = load("sample_context.json")
details = load("sample_details.json")
check("context 含餐品", len(context.get("meals", [])) > 0, len(context.get("meals", [])))
check("context 含营养", len(context.get("nutrition", [])) > 0, len(context.get("nutrition", [])))
check("context 门店字段完整", all(k in (context.get("store") or {})
                                 for k in ("storeCode", "storeName", "businessHours")))
check("样例不含任何 Token", "$ENV" not in json.dumps(context) and
      not re.search(r"[A-Za-z0-9]{20,}", json.dumps(context.get("server_time") or "")))

print("== 2. 营养表解析（基于真实 160 行）==")
rows = client.parse_nutrition_table(
    "[2]{productName,nutritionDescription,energyKj,energyKcal,protein,fat,carbohydrate,sodium,calcium}:\n"
    "  猪柳麦满分,null,1288,308,16,16,24,781,213\n"
    "  可乐中杯,null,0,0,0,0,0,0,0\n"
    "  双层某某,null,1486,355,23,17,27,984,130\n"
    "  噪声行,null,null,null,null\n")
check("解析出 3 行有效数据（噪声行被丢弃）", len(rows) == 3, len(rows))
check("菜名未被 description 污染", rows[0]["productName"] == "猪柳麦满分", rows[0]["productName"])
check("全零行不被丢弃", rows[1]["productName"] == "可乐中杯", rows[1]["productName"])
check("数值按表头对齐", rows[0]["sodium"] == 781 and rows[0]["calcium"] == 213,
      (rows[0].get("sodium"), rows[0].get("calcium")))
check("样例全表数值完整", all(r.get("complete") for r in context["nutrition"]))

print("== 3. 标签推导 ==")
spicy = [m for m in context["meals"] if "麦辣" in m["name"] and "堡" in m["name"]]
if spicy:
    tags = engine.derive_tags(spicy[0])
    check("辣味品被识别为辣", "辣" in tags, tags)
else:
    check("辣味样本存在", False, "样例中未找到辣味汉堡")
ice = [m for m in context["meals"] if "可乐" in m["name"]]
if ice:
    check("含冰品名识别为冰/冷", "冰/冷" in engine.derive_tags(ice[0]),
          engine.derive_tags(ice[0]))
else:
    check("冰品样本存在", False)

print("== 4. 画像过滤与安全护栏 ==")
details_index = {d["code"]: d for d in details.get("details") or []}
items = engine.build_items(context, details_index)
profile = engine.PROFILES["elderly"]
avoid = profile["avoid_default"]
kept, excluded = engine.apply_filters(items, avoid)
check("长辈画像确实排除了部分品项", len(excluded) > 0, len(excluded))
bad_in_kept = [i["name"] for i in kept if set(i["derivedTags"]) & set(avoid)]
check("保留集合里不含被禁标签", not bad_in_kept, bad_in_kept[:3])
check("排除原因可读", all(e.get("reasons") for e in excluded))
kids = [i for i in items if "汉堡开心乐园餐" in i["name"]]
if kids:
    check("单品营养标注 available 标志存在", "available" in kids[0]["nutrition"])
check("组成说明来自真实 rounds",
      any(i.get("composition") for i in items if i.get("hasDetail")))

print("== 5. 营养聚合的诚实性 ==")
combos = [i for i in items if i.get("comboNutritionAggregate")]
check("存在套餐聚合结果", len(combos) > 0, len(combos))
check("聚合结果带 matched_all 标志",
      all("matched_all" in i["comboNutritionAggregate"] for i in combos))
partial = [i for i in combos if not i["comboNutritionAggregate"]["matched_all"]]
check("部分匹配会标注 coverage 而非静默合计",
      all(i["comboNutritionAggregate"].get("coverage") for i in partial), len(partial))

print("== 6. 核价载荷合法性（安全输入校验）==")
plans_payload = [{"productCode": i["code"], "quantity": 1} for i in kept[:2]]
check("载荷 productCode 全非空", all(str(p.get("productCode", "")).strip() for p in plans_payload))
check("载荷 quantity 均为 >=1 的整数",
      all(isinstance(p["quantity"], int) and p["quantity"] >= 1 for p in plans_payload))
rc_worked = subprocess.run(
    [sys.executable, os.path.join(ROOT, "scripts", "mcd_mcp_client.py"), "price-check",
     "--store-code", "x", "--items", json.dumps([{"productCode": "1", "quantity": 0}])],
    capture_output=True, text=True, encoding="utf-8",
    env={**os.environ, "MCD_MCP_TOKEN": "dummy"})
check("非法 quantity 被本地拦截（未发起网络请求）", rc_worked.returncode == 1,
      (rc_worked.returncode, rc_worked.stderr.strip()[:80]))
for bad_args, label in (
        (["--store-code", "3350332; DROP"], "非法 storeCode"),
        (["--store-code", "3350332", "--code", "abc' OR 1=1"], "非法 code")):
    action = "price-check" if len(bad_args) == 2 else "fetch-detail"
    proc = subprocess.run(
        [sys.executable, os.path.join(ROOT, "scripts", "mcd_mcp_client.py"), action]
        + bad_args + (["--items", '[{"productCode":"9900005462","quantity":1}]']
                      if action == "price-check" else []),
        capture_output=True, text=True, encoding="utf-8",
        env={**os.environ, "MCD_MCP_TOKEN": "dummy"})
    check("%s 被本地拦截" % label, proc.returncode == 1 and "非法" in proc.stderr,
          proc.stderr.strip()[:80])

print("== 7. 渲染输出 ==")
plan_payload = {
    "store": context["store"], "window": engine.resolve_window(context.get("server_time")),
    "plans": engine.make_plans(kept, profile, None, 3,
                               engine.resolve_window(context.get("server_time"))),
}
md = engine.render_markdown(plan_payload)
html_text = engine.render_html(plan_payload)
check("Markdown 含门店名", context["store"]["storeName"] in md)
check("Markdown 含「未下单」声明", "还没有下单" in md)
check("HTML 无未替换占位符", "__" not in html_text.replace("__", "__", 0) or
      not re.search(r"__(STORE|HOURS|STATUS|WINDOW|CARDS)__", html_text))
check("HTML 含 24px 以上大字号", re.search(r"font-size:2[0-9]px", html_text) is not None)
check("HTML 含 aria 无障碍属性", 'role="main"' in html_text and 'aria-label' in html_text)
check("HTML 含非官方免责声明", "非官方工具" in html_text)
check("HTML 对用户输入做了转义", "<script" not in html_text.lower())

print("== 8. 不下单护栏 ==")
src = open(os.path.join(ROOT, "scripts", "a11y_engine.py"), encoding="utf-8").read()
# 检测真实调用（call_tool/call 里出现 create-order），而不是免责声明里提到的字样
invokes_create = re.search(r'(call_tool|call)\s*\(\s*[\'"]create-order[\'"]', src)
check("引擎未发生 create-order 调用", invokes_create is None)
check("引擎明确标注禁止下单", 'orderAllowed": False' in src)
client_src = open(os.path.join(ROOT, "scripts", "mcd_mcp_client.py"), encoding="utf-8").read()
check("客户端未发生 create-order 调用",
      re.search(r'(call_tool|rpc)\s*\(\s*[\'"]create-order[\'"]', client_src) is None)
check("客户端不提供 create-order 子命令", '"create-order"' not in client_src)

print("\n通过 %d 项，失败 %d 项" % (len(PASSED), len(FAILED)))
if FAILED:
    print("失败项：%s" % ", ".join(FAILED))
sys.exit(1 if FAILED else 0)
