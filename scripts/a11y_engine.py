#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""无障碍 / 适老化点餐引擎（mcd-a11y-order）。

输入：mcd_mcp_client.py fetch-context / fetch-detail 落盘的真实数据（或内置样例）。
输出：大字号、少字的极简点餐方案 JSON / Markdown / HTML 大字卡片，以及可直接回灌
      calculate-price 的核价载荷。

设计底线（与之一致，别改）：
  1. 菜单、价格、组成全部来自真实 MCP 返回，绝不臆造；营养数据缺失就写"未获取"，不估。
  2. 辣/冰/需餐具等标签是本引擎**基于真实名称与官方分类做的本地推导**，
     不是官方字段——因为实测 query-meals 的 meals 里根本没有 tags 字段
     （tags 只存在于 categories[].meals[]，且是"上新/爆款"这类营销标签）。
     所以每个标签都带 derived=true，输出里显式说明推导来源。
  3. 停在付款前：本脚本只产生建议与核价载荷，绝不调用 create-order。
  4. 营养/钠含量仅作参考，不构成医疗或营养治疗建议。
"""

import argparse
import datetime as dt
import html
import json
import os
import re
import sys

# ---------------------------------------------------------------- 推导规则（本地启发式，非官方）

TAG_RULES = [
    ("辣", ("辣", "麻辣", "香辣", "川香", "藤椒", "龙焰", "灰焰", "韩式辣椒", "炝")),
    ("冰/冷", ("冰", "冷饮", "新地", "旋风", "圆筒", "冰淇淋", "雪碧", "可乐", "麦炫酷")),
    ("需餐具", ("沙拉", "玉米杯", "冰淇淋", "新地", "旋风", "粥", "汤", "薯泥")),
    ("难嚼/脆硬", ("脆", "酥", "薯饼", "熏鸡", "辣翅")),
    ("带骨/带刺", ("鸡翅", "翅", "骨", "鱼排", "鱼")),
    ("儿童", ("开心乐园", "儿童", "乐园")),
    ("新品", ("上新", "全新", "新版")),
    ("套餐", ("三件套", "件套", "套餐", "组合", "分享餐", "双人")),
]

HANDHELD_HINTS = ("堡", "卷", "派", "薯条", "鸡块", "汉堡")
WARM_HINTS = ("热", "咖啡", "红茶", "豆浆", "牛奶", "玉米", "粥", "汤")

# 场景画像：默认过滤项与偏好权重
PROFILES = {
    "elderly": {
        "label": "长辈/适老化",
        "avoid_default": ["辣", "冰/冷", "难嚼/脆硬", "带骨/带刺"],
        "prefer_tags": ["好拿", "软"],
        "dislike_tags": ["儿童"],
        "reasons": "长辈普遍怕辣、怕冰、牙口较弱，且带骨带刺食物容易噎住；套餐整体钠摄入需留意。",
    },
    "lowvision": {
        "label": "低视力/读屏",
        "avoid_default": [],
        "prefer_tags": ["好拿"],
        "dislike_tags": [],
        "reasons": "重点是把菜单压到最少条目、减少逐级选择，配合读屏器与超大字号。",
    },
    "driver": {
        "label": "驾驶中",
        "avoid_default": ["需餐具"],
        "prefer_tags": ["好拿"],
        "dislike_tags": ["儿童"],
        "reasons": "单手取食、不掉渣不外洒，避免汤水与需要餐具的品项。",
    },
    "child": {
        "label": "儿童",
        "avoid_default": ["辣", "冰/冷", "带骨/带刺"],
        "prefer_tags": ["儿童"],
        "dislike_tags": [],
        "reasons": "儿童不宜辣/冰，带骨带刺食物存在卡喉风险。",
    },
    "general": {"label": "通用", "avoid_default": [], "prefer_tags": [],
                "dislike_tags": [], "reasons": ""},
}

# 套餐级钠参考阈值（mg）：按整份占成人全天 PI-NCD(2000mg) 的比例提示，仅供参考
COMBO_SODIUM_THRESHOLDS = [(1600, "整份钠占成人全天建议上限(2000mg)的 80% 以上，长辈/需控钠人群请谨慎"),
                           (1000, "整份钠已超过成人全天建议上限(2000mg)的一半，建议搭配白水、少蘸酱")]

# 钠参考阈值（mg/份），依据《中国居民膳食营养素参考摄入量(2023)》：成人钠 AI 1500mg/d、
# 预防慢病建议摄入量 PI-NCD 2000mg/d。单份占全天 PI-NCD 的比例作为提示，仅供参考。
SODIUM_LEVELS = [(1200, "高钠", "单份钠已达成人全天建议上限(2000mg)的 60% 以上"),
                 (800, "钠偏高", "单份钠占成人全天建议上限(2000mg)的 40% 以上")]

MEAL_WINDOWS = [
    (5, 10, "早餐", "长辈宜吃温热、软的食物，避免空腹喝冰饮。"),
    (10, 14, "正餐/午餐", "适合安排一份主食+热饮的完整套餐。"),
    (14, 17, "下午茶", "可安排小份甜品或热饮，量宜小。"),
    (17, 21, "正餐/晚餐", "宜清淡、控制钠摄入，不宜过饱。"),
    (21, 24, "夜宵", "长辈不建议夜间进食；如必须，请选小份、热、好消化的品项。"),
]


# ---------------------------------------------------------------- 工具

def normalize(text: str) -> str:
    text = str(text or "")
    text = text.replace("（", "(").replace("）", ")").replace("\u3000", "")
    text = re.sub(r"[\s【】「」“”‘’™®*·]", "", text)
    return text.strip()


def load_json(path: str):
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def resolve_window(server_time):
    """优先用服务器时间(now-time-info)，失败回退本地时钟。"""
    source = "local-clock"
    hour = dt.datetime.now().hour
    date_txt = dt.datetime.now().strftime("%Y-%m-%d")
    if isinstance(server_time, dict):
        formatted = server_time.get("formatted") or server_time.get("datetime")
        if isinstance(formatted, str) and len(formatted) >= 13:
            try:
                parsed = dt.datetime.strptime(formatted[:16], "%Y-%m-%d %H:%M")
                hour, date_txt = parsed.hour, parsed.strftime("%Y-%m-%d")
                source = "now-time-info"
            except ValueError:
                pass
    for start, end, name, tip in MEAL_WINDOWS:
        if start <= hour < end:
            return {"hour": hour, "date": date_txt, "window": name, "tip": tip, "source": source}
    return {"hour": hour, "date": date_txt, "window": "非营业时段",
            "tip": "当前可能不在常规营业时间内，请先确认门店营业状态。", "source": source}


def derive_tags(meal: dict) -> list:
    """基于真实名称/分类/官方营销标签推导无障碍属性（derived=true）。"""
    name = meal.get("name") or ""
    haystack = normalize(name + " " + "".join(meal.get("categories") or []) + " " +
                         "".join(meal.get("shopTags") or []))
    tags = []
    for tag, keys in TAG_RULES[-2:]:  # 儿童/新品/套餐：官方 shopTags 也算依据
        for key in keys:
            if normalize(key) in haystack:
                tags.append(tag)
                break
    for tag, keys in TAG_RULES[:-2]:
        for key in keys:
            if normalize(key) in normalize(name) or normalize(key) in haystack:
                tags.append(tag)
                break
    if any(h in normalize(name) for h in HANDHELD_HINTS) and "需餐具" not in tags:
        tags.append("好拿")
    if "软" not in tags and any(h in normalize(name) for h in ("堡", "粥", "鸡蛋", "玉米")):
        tags.append("软")
    if "冰/冷" not in tags and any(h in normalize(name) for h in WARM_HINTS):
        tags.append("热")
    # 去重保序
    seen, out = set(), []
    for t in tags:
        if t not in seen:
            seen.add(t)
            out.append(t)
    return out


def match_nutrition(name: str, nutrition_rows: list):
    """三级匹配：原名 -> 归一化 -> 归一化包含(取最长)。返回 (row, match_type)。"""
    by_name = {}
    for row in nutrition_rows:
        by_name.setdefault(row.get("productName"), row)
    if name in by_name:
        return by_name[name], "exact"
    norm_map = {}
    for row in nutrition_rows:
        key = normalize(row.get("productName"))
        if key and key not in norm_map:
            norm_map[key] = row
    target = normalize(name)
    if target in norm_map:
        return norm_map[target], "normalized"
    best, best_len = None, 0
    for key, row in norm_map.items():
        if len(key) < 3:
            continue
        if key in target or target in key:
            if len(key) > best_len:
                best, best_len = row, len(key)
    if best:
        return best, "fuzzy"
    return None, "none"


def nutrition_view(row, match_type: str):
    if not row:
        return {"available": False,
                "note": "官方营养库未匹配到该品项，本引擎不做估算。" if match_type == "none"
                        else "营养数据不可用。"}
    sodium = row.get("sodium")
    view = {
        "available": True,
        "match_type": match_type,
        "kcal": row.get("energyKcal"),
        "protein_g": row.get("protein"),
        "fat_g": row.get("fat"),
        "carbohydrate_g": row.get("carbohydrate"),
        "sodium_mg": sodium,
        "calcium_mg": row.get("calcium"),
    }
    if sodium is not None:
        for threshold, level, why in SODIUM_LEVELS:
            if sodium >= threshold:
                view["sodium_level"] = level
                view["sodium_reason"] = why
                break
        else:
            view["sodium_level"] = "钠含量一般"
    return view


def combo_nutrition(detail: dict, nutrition_rows: list):
    """用套餐真实子项(query-meal-detail rounds)聚合营养——比猜套餐名靠谱得多。"""
    if not detail:
        return None
    total = {"kcal": 0.0, "sodium_mg": 0.0, "protein_g": 0.0, "fat_g": 0.0,
             "carbohydrate_g": 0.0, "calcium_mg": 0.0}
    matched, components = 0, 0
    for rd in detail.get("rounds") or []:
        for choice in rd.get("choices") or []:
            if not choice.get("isDefault"):
                continue
            components += 1
            row, mtype = match_nutrition(choice.get("name") or "", nutrition_rows)
            if row and mtype != "none":
                matched += 1
                for key, field in (("kcal", "energyKcal"), ("sodium_mg", "sodium"),
                                   ("protein_g", "protein"), ("fat_g", "fat"),
                                   ("carbohydrate_g", "carbohydrate"), ("calcium_mg", "calcium")):
                    val = row.get(field)
                    if isinstance(val, (int, float)):
                        total[key] += float(val)
    if not components:
        return None
    result = {k: (round(v, 1) if v else round(v, 1)) for k, v in total.items()}
    result["components_total"] = components
    result["components_matched"] = matched
    result["coverage"] = "全部匹配" if matched == components else "部分匹配(%d/%d)" % (matched, components)
    result["matched_all"] = matched == components
    result["matched_count"] = matched
    return result


def explain_combo(detail: dict) -> str:
    """把 rounds 翻成一句人话：「里面有 X、Y 和 Z」"""
    if not (detail or {}).get("rounds"):
        return ""
    parts = []
    for rd in detail.get("rounds"):
        for choice in rd.get("choices") or []:
            if choice.get("isDefault"):
                parts.append(choice.get("name"))
    if not parts:
        return ""
    if len(parts) == 1:
        return "里面有 %s。" % parts[0]
    return "里面有 " + "、".join(parts[:-1]) + " 和 " + parts[-1] + "。"


def swap_options(detail: dict, max_show: int = 3):
    """列出套餐内可替换项及其真实差价(diffPrice)，只收录不加价的适老替换。"""
    swaps = []
    for rd in detail.get("rounds") or []:
        for choice in rd.get("choices") or []:
            if choice.get("isDefault"):
                continue
            diff = choice.get("diffPrice") or ""
            swaps.append({"round": rd.get("name"), "name": choice.get("name"),
                          "diffPrice": diff, "code": choice.get("code")})
    swaps.sort(key=lambda s: parse_diff(s["diffPrice"]))
    return swaps[:max_show]


def parse_diff(diff: str) -> float:
    """'+ ¥1.5' / '- ¥1.5' / '+ ¥0' -> float"""
    if not diff:
        return 0.0
    sign = -1.0 if diff.strip().startswith("-") else 1.0
    m = re.search(r"([0-9]+(?:\.[0-9]+)?)", diff)
    return sign * float(m.group(1)) if m else 0.0


# ---------------------------------------------------------------- 核心

def build_items(context: dict, details_index: dict):
    """给每个真实餐品打推导标签 + 真实营养 + 真实组成说明。"""
    nutrition_rows = context.get("nutrition") or []
    items = []
    for meal in context.get("meals") or []:
        tags = derive_tags(meal)
        detail = details_index.get(meal["code"])
        row, mtype = match_nutrition(meal["name"], nutrition_rows)
        combo_nut = combo_nutrition(detail, nutrition_rows) if detail else None
        price = meal.get("currentPrice")
        items.append({
            "code": meal["code"],
            "name": meal["name"],
            "priceYuan": price,
            "originalPriceYuan": meal.get("originalPrice"),
            "categories": meal.get("categories") or [],
            "shopTags": meal.get("shopTags") or [],
            "derivedTags": tags,
            "tags_are_derived": True,
            "nutrition": nutrition_view(row, mtype),
            "comboNutritionAggregate": combo_nut,
            "composition": explain_combo(detail) if detail else "",
            "swapOptions": swap_options(detail) if detail else [],
            "hasDetail": bool(detail),
        })
    return items


def apply_filters(items: list, avoid: list, max_sodium=None):
    """按画像排除项过滤，并给出可读的排除理由。

    max_sodium：整份钠上限（mg）。套餐优先用真实子项聚合值，单品用官方营养值；
    无法获取营养数据的品项不剔除（因为没有数据就不该武断排除）。
    """
    kept, excluded = [], []
    for item in items:
        tags = item.get("derivedTags") or []
        hit = [a for a in avoid if a in tags]
        if hit:
            excluded.append({"code": item["code"], "name": item["name"],
                             "reasons": ["含" + h for h in hit]})
            continue
        if max_sodium is not None:
            agg = item.get("comboNutritionAggregate") or {}
            sodium = agg.get("sodium_mg")
            if sodium is None:
                sodium = (item.get("nutrition") or {}).get("sodium_mg")
            if isinstance(sodium, (int, float)) and sodium > max_sodium:
                excluded.append({"code": item["code"], "name": item["name"],
                                 "reasons": ["整份钠约 %.0f 毫克，超过上限 %d 毫克" % (sodium, max_sodium)]})
                continue
        kept.append(item)
    return kept, excluded


def score(item: dict, profile: dict) -> float:
    """排序分：画像偏好 + 有真实组成 + 钠摄入可控 + 价格适中。"""
    score = 0.0
    tags = item.get("derivedTags") or []
    for pref in profile.get("prefer_tags") or []:
        if pref in tags:
            score += 2
    for bad in profile.get("dislike_tags") or []:
        if bad in tags:
            score -= 6
    if item.get("hasDetail"):
        score += 2  # 套餐有真实组成，能解释"里面有什么"
    nut = item.get("nutrition") or {}
    sodium = nut.get("sodium_mg")
    if isinstance(sodium, (int, float)):
        score -= 2 if sodium >= 1200 else (1 if sodium >= 800 else 0)
        score += 0.5 if sodium < 400 else 0
    # 套餐整体钠（真实子项聚合）比单品更能反映实际摄入，权重更高
    agg = item.get("comboNutritionAggregate") or {}
    combo_sodium = agg.get("sodium_mg")
    if isinstance(combo_sodium, (int, float)) and agg.get("matched_all"):
        score -= 3 if combo_sodium >= 1600 else (2 if combo_sodium >= 1000 else 0)
        score += 0.5 if combo_sodium < 600 else 0
    price = item.get("priceYuan")
    if isinstance(price, (int, float)) and 20 <= price <= 40:
        score += 1
    return score


def make_plans(kept: list, profile: dict, budget, limit: int, window: dict):
    """
    生成 1-3 个极简方案。
    优先给「有真实组成的套餐」（能讲清楚里面有什么），其次单品。
    """
    ranked = sorted(kept, key=lambda i: -score(i, profile))
    combos = [i for i in ranked if i.get("hasDetail")][:limit]
    plans = []
    for idx, item in enumerate(combos[:limit]):
        price = item.get("priceYuan")
        lines = [item["name"] + "（默认搭配）"]
        if item.get("composition"):
            lines.append(item["composition"])
        tail = "价格 ¥%s" % price if price is not None else "价格以门店实时为准"
        agg = item.get("comboNutritionAggregate")
        full = agg and agg.get("matched_all")
        if agg and agg.get("kcal") is not None and (full or agg.get("matched_count")):
            if full:
                tail += "，合计约 %.0f 千卡" % agg["kcal"]
                if agg.get("sodium_mg") is not None:
                    tail += " / 钠约 %.0f 毫克" % agg["sodium_mg"]
            else:
                # 只有部分子项匹配到营养库，不能假装这是整份的真实合计
                tail += "，已知 %d/%d 项合计约 %.0f 千卡" % (
                    agg.get("matched_count") or 0, agg.get("components_total") or 0, agg["kcal"])
                tail += "（部分子项未匹配到官方营养数据，非整份真实合计）"
        lines.append(tail)
        # 只有完整覆盖才给钠建议，避免用残缺数据误导
        if full and agg.get("sodium_mg") is not None:
            for threshold, advice in COMBO_SODIUM_THRESHOLDS:
                if agg["sodium_mg"] >= threshold:
                    lines.append("注意：%s" % advice)
                    break
        if budget is not None and isinstance(price, (int, float)) and price > budget:
            lines.append("超过预算 ¥%.0f，已标注但实际不符，可忽略本项。" % budget)
        plans.append({
            "id": "方案" + "ABC"[idx],
            "headline": "、".join(item.get("derivedTags") or ["无特殊标签"]) + "｜" + item["name"],
            "itemCode": item["code"],
            "lines": lines,
            "swapOptions": item.get("swapOptions") or [],
            "priceYuan": price,
            "withinBudget": None if budget is None or not isinstance(price, (int, float))
                           else price <= budget,
        })
    # 若套餐不足，用单品补齐到 1 个方案
    if not plans:
        for item in ranked[:1]:
            plans.append({
                "id": "方案A",
                "headline": item["name"],
                "itemCode": item["code"],
                "lines": [item["name"], "价格 ¥%s" % item.get("priceYuan")],
                "swapOptions": [],
                "priceYuan": item.get("priceYuan"),
                "withinBudget": True,
            })
    for p in plans:
        p["windowTip"] = window["tip"]
    return plans


def render_markdown(payload: dict) -> str:
    store = payload["store"]
    w = payload["window"]
    lines = [
        "# 麦当劳点餐建议（大字号版）",
        "",
        "门店：%s" % store.get("storeName"),
        "营业时间：%s　状态：%s" % (store.get("businessHours"),
                                   "营业中" if store.get("businessStatus") else "休息中"),
        "当前时段：%s（来源：%s）" % (w.get("window"), w.get("source")),
        "",
    ]
    for plan in payload["plans"]:
        lines.append("## %s" % plan["headline"])
        lines.extend(plan["lines"])
        if plan.get("swapOptions"):
            lines.append("可以换：" + "；".join(
                "%s（%s）" % (s["name"], s["diffPrice"] or "不加价") for s in plan["swapOptions"]))
        lines.append("")
    lines.extend([
        "---",
        "本页只到这一步，还没有下单。确认要点，请再说一声。",
        "餐品、价格、供应状态以麦当劳官方渠道实时结果为准；本工具非麦当劳官方产品。",
        "标签为本工具基于真实菜单名称推导，非官方分类；营养数据仅供参考，不构成医疗建议。",
    ])
    return "\n".join(lines)


HTML_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>麦当劳点餐建议（大字号）</title>
<style>
  :root { --ink:#111; --bg:#fff; --line:#111; --hi:#b3001b; }
  * { box-sizing: border-box; }
  body { margin:0 auto; max-width:760px; padding:24px 20px 64px;
         background:var(--bg); color:var(--ink);
         font-family:"PingFang SC","Microsoft YaHei",system-ui,sans-serif;
         font-size:24px; line-height:1.7; -webkit-text-size-adjust:100%; }
  h1 { font-size:40px; line-height:1.3; margin:0 0 16px; }
  .meta { border:3px solid var(--line); border-radius:14px; padding:14px 18px;
          margin-bottom:24px; font-size:24px; }
  .meta div { margin:6px 0; }
  .card { border:3px solid var(--line); border-radius:14px; padding:18px;
          margin-bottom:20px; break-inside:avoid; }
  .card h2 { font-size:34px; margin:0 0 12px; line-height:1.35; }
  ul.lines { margin:0; padding-left:1.2em; }
  ul.lines li { margin:6px 0; }
  .swaps { margin-top:14px; border-top:2px dashed var(--line); padding-top:10px; }
  .swaps p { margin:0 0 6px; font-weight:700; }
  .swaps ul { margin:0; padding-left:1.2em; }
  .diff { color:var(--hi); font-weight:700; }
  footer { margin-top:32px; border-top:3px solid var(--line); padding-top:16px; font-size:22px; }
  @media (max-width:420px) { body { font-size:22px; } h1 { font-size:32px; } .card h2 { font-size:28px; } }
</style>
</head>
<body>
  <h1>麦当劳点餐建议<br>（大字号版）</h1>
  <div class="meta" role="region" aria-label="门店与时段信息">
    <div><strong>门店：</strong>__STORE__</div>
    <div><strong>营业时间：</strong>__HOURS__（__STATUS__）</div>
    <div><strong>当前时段：</strong>__WINDOW__</div>
  </div>
  <main role="main">__CARDS__
  </main>
  <footer role="contentinfo">
    <p><strong>只到这一步，还没有下单。</strong>确认要点的家属或本人，请再明确说一声。</p>
    <p>餐品、价格、供应状态以麦当劳官方渠道实时结果为准。本工具为非官方工具，输出仅供参考。</p>
    <p>辣/冰/软/好拿等标签由本工具依据真实菜单名称推导，非麦当劳官方字段；
       营养数据来自官方营养库，仅供参考，不构成医疗或营养治疗建议。</p>
  </footer>
</body>
</html>
"""


def render_html(payload: dict) -> str:
    """超大字号、高对比、语义化 + aria 标签的卡片，便于直接给长辈看，也便于读屏软件朗读。"""
    store = payload["store"]
    w = payload["window"]

    def esc(v):
        return html.escape(str(v if v is not None else ""))

    cards = []
    for plan in payload["plans"]:
        swaps = "".join(
            '<li>%s <span class="diff">%s</span></li>' % (esc(s["name"]),
                                                          esc(s["diffPrice"] or "不加价"))
            for s in plan.get("swapOptions") or [])
        swaps_html = ('<div class="swaps"><p>可以换成：</p><ul>%s</ul></div>' % swaps) if swaps else ""
        cards.append("""
      <article class="card" aria-label="%s">
        <h2>%s</h2>
        <ul class="lines">%s</ul>
        %s
      </article>""" % (esc(plan["headline"]), esc(plan["headline"]),
                       "".join("<li>%s</li>" % esc(l) for l in plan["lines"]), swaps_html))

    return HTML_TEMPLATE.replace("__STORE__", esc(store.get("storeName")))\
                      .replace("__HOURS__", esc(store.get("businessHours")))\
                      .replace("__STATUS__", "营业中" if store.get("businessStatus") else "休息中")\
                      .replace("__WINDOW__", esc(w.get("window")))\
                      .replace("__CARDS__", "".join(cards))


# ---------------------------------------------------------------- CLI

def main() -> int:
    here = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    parser = argparse.ArgumentParser(description="无障碍/适老化点餐引擎")
    parser.add_argument("--context", default=os.path.join(here, "examples", "sample_context.json"),
                        help="fetch-context 落盘的真实上下文 JSON")
    parser.add_argument("--details", default=os.path.join(here, "examples", "sample_details.json"),
                        help="fetch-detail 落盘的真实套餐组成 JSON")
    parser.add_argument("--profile", default="elderly", choices=sorted(PROFILES))
    parser.add_argument("--avoid", default="", help="额外排除标签，逗号分隔，如 辣,冰/冷")
    parser.add_argument("--budget", type=float, default=None, help="预算上限（元）")
    parser.add_argument("--max-sodium", type=float, default=None,
                        help="整份钠上限（毫克），套餐用真实子项聚合值，用于控钠人群")
    parser.add_argument("--limit", type=int, default=3, help="最多给几个方案")
    parser.add_argument("--out", default="", help="输出 plan JSON 路径")
    parser.add_argument("--markdown", default="", help="输出 Markdown 路径")
    parser.add_argument("--html-out", default="", help="输出大字号 HTML 卡片路径")
    args = parser.parse_args()

    if not os.path.isfile(args.context):
        print("缺少 context 数据文件：%s" % args.context, file=sys.stderr)
        return 2

    context = load_json(args.context)
    if not (context.get("meals") or []):
        print("context 中没有餐品数据，请先用 fetch-context 拉取。", file=sys.stderr)
        return 2
    if os.path.isfile(args.details):
        details_payload = load_json(args.details)
        details_index = {d.get("code"): d for d in (details_payload.get("details") or [])}
    else:
        details_payload, details_index = {}, {}
        print("! 未提供套餐组成数据（--details），方案将不含「里面有…」说明；"
              "可先运行一次本脚本生成候选，再用 mcd_mcp_client.py details-for-plan 补齐。",
              file=sys.stderr)
    if details_payload.get("store_code") and context.get("store", {}).get("storeCode") and \
            details_payload["store_code"] != context["store"]["storeCode"]:
        print("! 套餐组成数据与门店不一致（%s != %s），已忽略组成数据。" % (
            details_payload["store_code"], context["store"]["storeCode"]), file=sys.stderr)
        details_index = {}

    profile = PROFILES[args.profile]
    avoid = list(dict.fromkeys(profile["avoid_default"] +
                               [a.strip() for a in args.avoid.split(",") if a.strip()]))
    window = resolve_window(context.get("server_time"))
    items = build_items(context, details_index)
    kept, excluded = apply_filters(items, avoid, args.max_sodium)
    plans = make_plans(kept, profile, args.budget, max(1, args.limit), window)

    store = context.get("store") or {}
    price_payload = [{"productCode": p["itemCode"], "quantity": 1} for p in plans]
    payload = {
        "generated_at": dt.datetime.now().isoformat(timespec="seconds"),
        "profile": {"key": args.profile, "label": profile["label"], "reasons": profile["reasons"]},
        "window": window,
        "store": {
            "storeCode": store.get("storeCode"), "storeName": store.get("storeName"),
            "businessHours": store.get("businessHours"),
            "businessStatus": store.get("businessStatus"),
        },
        "orderParams": context.get("order_params") or {},
        "filters": {"avoidTags": avoid, "budgetYuan": args.budget, "maxSodiumMg": args.max_sodium},
        "counts": {"menuTotal": len(items), "kept": len(kept), "excluded": len(excluded)},
        "excludedSample": excluded[:10],
        "plans": plans,
        "candidates": sorted(kept, key=lambda i: -score(i, profile))[:20],
        "priceCheck": {
            "howTo": "python scripts/mcd_mcp_client.py price-check --store-code %s "
                     "--order-type %s --be-type %s --items '<下面的 items>'"
                     % (store.get("storeCode") or "",
                        (context.get("order_params") or {}).get("orderType", 1),
                        (context.get("order_params") or {}).get("beType", 1)),
            "items": price_payload,
        },
        "guardrails": {
            "orderAllowed": False,
            "note": "本脚本不调用 create-order；下单必须经用户明确确认后由本人/家属完成。",
            "tagsAreDerived": True,
            "nutritionIsReferenceOnly": True,
            "medicalDisclaimer": "营养与钠含量仅供参考，不构成医疗或营养治疗建议。",
        },
    }

    text = json.dumps(payload, ensure_ascii=False, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(text + "\n")
        print("已写入：%s" % os.path.abspath(args.out))
    else:
        print(text)
    if args.markdown:
        with open(args.markdown, "w", encoding="utf-8") as fh:
            fh.write(render_markdown(payload) + "\n")
        print("已写入：%s" % os.path.abspath(args.markdown))
    if args.html_out:
        with open(args.html_out, "w", encoding="utf-8") as fh:
            fh.write(render_html(payload))
        print("已写入：%s" % os.path.abspath(args.html_out))
    return 0


if __name__ == "__main__":
    sys.exit(main())
