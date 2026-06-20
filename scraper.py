#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
广州涉海创新节点地图 · 数据抓取脚本
来源：广东省政府采购网 中标（成交）结果公告
输出：data.json（供前端节点图直接读取）

设计原则
--------
1. 失败安全：抓不到 / 出错 / 结果为空 → 直接退出，绝不覆盖已有 data.json。
   页面永远显示上一次成功的数据，不会崩。
2. 两种模式：
   - roster  ：有 roster.json（你的涉海名录）时，把政采中标方匹配到名录企业。
   - discovery：没有名录时，按关键词自动识别涉海中标方，产出候选节点（帮你补名录）。
3. 抓取层（fetch_announcements）是唯一需要你对着真站验证的部分，已单独隔离。
   其余 normalize / 赛道映射 / 模糊匹配 / 出 json，纯本地逻辑，已离线测过。

调试
----
  python scraper.py --demo      # 不联网，用内置样例数据走完整管线，看 data.json 长啥样
  python scraper.py --inspect   # 联网抓一页，原样打印返回结构，方便你核对字段名
  python scraper.py             # 正式跑
"""

import json, os, sys, time, datetime, argparse, re
import requests
from rapidfuzz import fuzz

HERE = os.path.dirname(os.path.abspath(__file__))
OUT      = os.path.join(HERE, "data.json")
ROSTER   = os.path.join(HERE, "roster.json")   # 可选：你的涉海单位名录

# ============================================================
#  ① 抓取层 —— 唯一需要你对着真站验证的部分
#  广东政采网列表由 CMS 后台 JSON 接口出数（robots 禁止抓静态页）。
#  请用浏览器打开「中标（成交）结果公告」列表页 → F12 → Network →
#  找到那条返回公告列表的 XHR，把它的 URL / 参数 / 返回字段名填到下面。
#  下面是 freecms 类平台的常见形态，作为起点，务必以真站实测为准。
# ============================================================
API_URL = "https://gdgpo.czt.gd.gov.cn/freecms/rest/v1/notice/selectInfoMoreChannel.do"
PAGE_SIZE = 50
MAX_PAGES = 4          # 月度跑，抓最近 4 页足够覆盖一个月

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
    "Referer": "https://gdgpo.czt.gd.gov.cn/cms-gd/site/guangdong/cggg/index.html",
    "Accept": "application/json, text/plain, */*",
}

def fetch_announcements(page):
    """抓一页中标公告，返回 list[dict]。失败抛异常，由上层兜底。"""
    params = {
        # ↓↓↓ 以真站 XHR 为准修改这几个 key ↓↓↓
        "pageNo": page,
        "pageSize": PAGE_SIZE,
        "channelCode": "0005",   # 中标（成交）结果公告的栏目码，实测后替换
        "isgovertment": "",
    }
    r = requests.get(API_URL, params=params, headers=HEADERS, timeout=20)
    r.raise_for_status()
    data = r.json()
    # 返回结构因平台而异，这里做多字段名兼容；--inspect 可看真实结构。
    rows = (data.get("data") or data.get("rows") or data.get("list")
            or data.get("infodata") or [])
    return rows

def parse_row(row):
    """把一条原始公告规整成 {title, winner, date, url}。字段名做兼容。"""
    g = lambda *ks: next((row[k] for k in ks if row.get(k)), "")
    title  = g("title", "noticeTitle", "infoTitle", "projectName")
    winner = g("winner", "winningBidder", "supplier", "bidWinner")
    date   = g("publishDate", "addtime", "createTime", "noticeTime", "publishTime")
    url    = g("url", "pageurl", "infoUrl", "detailUrl")
    return {"title": str(title), "winner": str(winner),
            "date": str(date)[:10], "url": str(url)}

# ============================================================
#  ② 赛道映射 —— 关键词 → 五大赛道（索引 0-4，与前端一致）
# ============================================================
TRACK_KEYWORDS = [
    # 0 海岸带技术服务
    ["海岸", "岸线", "海堤", "侵蚀", "滨海", "湿地修复", "海塘", "护岸"],
    # 1 城市地质数字化
    ["地质", "地下空间", "沉降", "勘察", "InSAR", "岩土", "地灾", "测绘", "监测网"],
    # 2 蓝碳与生态补偿
    ["蓝碳", "碳汇", "红树林", "生态补偿", "碳中和", "生态修复", "增汇"],
    # 3 海洋经济数据服务
    ["数据", "统计", "画像", "供应链", "图谱", "信息平台", "数字化平台"],
    # 4 近海装备与新材料
    ["ROV", "浮标", "传感器", "耐腐蚀", "新材料", "装备", "潜水器", "声呐", "防腐"],
]
# 涉海总闸：标题/中标方命中任一才算涉海（discovery 模式用）
MARINE_GATE = ["海洋", "海岸", "海域", "近海", "滨海", "红树", "蓝碳", "碳汇",
               "岸线", "海堤", "海工", "潮间", "海事", "船", "港", "渔", "ROV", "浮标"]

def classify_track(text):
    """返回最匹配的赛道索引；都不沾返回 None。"""
    best, best_idx = 0, None
    for idx, kws in enumerate(TRACK_KEYWORDS):
        hits = sum(1 for k in kws if k in text)
        if hits > best:
            best, best_idx = hits, idx
    return best_idx

def is_marine(text):
    return any(k in text for k in MARINE_GATE)

# ============================================================
#  ③ 名录匹配（roster 模式）
# ============================================================
def load_roster():
    if not os.path.exists(ROSTER):
        return None
    with open(ROSTER, encoding="utf-8") as f:
        return json.load(f)   # [{"name": "...", "track": 2, "aliases": ["简称", "曾用名"]}, ...]

def match_roster(winner, roster, threshold=82):
    """把中标方名称模糊匹配到名录企业。命中返回名录条目，否则 None。"""
    if not winner:
        return None
    best, score = None, 0
    for ent in roster:
        names = [ent["name"]] + ent.get("aliases", [])
        s = max(fuzz.partial_ratio(winner, n) for n in names)
        if s > score:
            best, score = ent, s
    return best if score >= threshold else None

# ============================================================
#  ④ 聚合成节点 → data.json
# ============================================================
def build_nodes(rows, roster):
    """rows: list[parse_row 结果]；返回 (nodes, mode)。"""
    mode = "roster" if roster else "discovery"
    bucket = {}   # name -> 聚合
    for r in rows:
        text = r["title"] + " " + r["winner"]
        if roster:
            ent = match_roster(r["winner"], roster)
            if not ent:
                continue
            name, track = ent["name"], ent["track"]
        else:
            if not (r["winner"] and is_marine(text)):
                continue
            track = classify_track(text)
            if track is None:
                continue
            name = r["winner"]
        b = bucket.setdefault(name, {"name": name, "track": track,
                                     "contracts": 0, "latest": "", "samples": []})
        b["contracts"] += 1
        b["latest"] = max(b["latest"], r["date"])
        if len(b["samples"]) < 3 and r["title"]:
            b["samples"].append(r["title"][:40])

    nodes = []
    for b in bucket.values():
        # 信号分（占位口径）：政采合同数是 L4「确凿证据」+ 信号#4。
        # 单一来源只能支撑这一维，满分前先按合同数缩放，接入更多源后再换正式权重。
        b["score"] = min(95, 40 + b["contracts"] * 12)
        nodes.append(b)
    nodes.sort(key=lambda x: (-x["contracts"], x["name"]))
    return nodes, mode

def write_safe(nodes, mode):
    """失败安全：空结果不覆盖旧文件。"""
    if not nodes:
        print("⚠ 结果为空，保留已有 data.json 不覆盖。")
        return False
    payload = {
        "updated": datetime.date.today().isoformat(),
        "source": "广东省政府采购网 · 中标（成交）结果公告",
        "mode": mode,
        "count": len(nodes),
        "nodes": nodes,
    }
    tmp = OUT + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(payload, f, ensure_ascii=False, indent=2)
    os.replace(tmp, OUT)   # 原子替换
    print(f"✓ 写入 {len(nodes)} 个节点 → data.json（{mode} 模式）")
    return True

# ============================================================
#  样例数据（--demo 用，离线验证管线）
# ============================================================
DEMO_ROWS = [
    {"title": "广州市某海岸带侵蚀防治岸线监测服务项目", "winner": "广州海岸科技有限公司", "publishDate": "2026-05-12"},
    {"title": "南沙区红树林蓝碳碳汇核算技术服务",       "winner": "广州蓝碳生态研究院",   "publishDate": "2026-05-20"},
    {"title": "城市地下空间地质沉降InSAR监测项目",      "winner": "广东地信勘察科技有限公司", "publishDate": "2026-05-08"},
    {"title": "海洋经济涉海企业供应链数据平台建设",     "winner": "广州海数信息技术有限公司", "publishDate": "2026-05-25"},
    {"title": "近海监测浮标与耐腐蚀传感器采购",         "winner": "广州洋感装备有限公司",   "publishDate": "2026-05-18"},
    {"title": "海岸带湿地修复二期工程岸线整治",         "winner": "广州海岸科技有限公司", "publishDate": "2026-04-30"},
    {"title": "办公楼物业管理服务采购",                 "winner": "某物业公司",           "publishDate": "2026-05-01"},  # 非涉海，应被过滤
]

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--demo", action="store_true", help="离线样例跑通管线")
    ap.add_argument("--inspect", action="store_true", help="联网抓一页，打印原始返回结构")
    args = ap.parse_args()

    if args.inspect:
        print(json.dumps(fetch_announcements(1)[:2], ensure_ascii=False, indent=2))
        return

    roster = load_roster()
    print(f"名录：{'已加载 '+str(len(roster))+' 家' if roster else '未提供 → discovery 模式'}")

    if args.demo:
        rows = [parse_row(r) for r in DEMO_ROWS]
    else:
        rows = []
        for p in range(1, MAX_PAGES + 1):
            try:
                raw = fetch_announcements(p)
            except Exception as e:
                print(f"⚠ 第 {p} 页抓取失败：{e}")
                break
            if not raw:
                break
            rows += [parse_row(r) for r in raw]
            time.sleep(1.5)   # 温和限速
        print(f"抓取到 {len(rows)} 条公告")

    nodes, mode = build_nodes(rows, roster)
    ok = write_safe(nodes, mode)
    sys.exit(0 if ok else 0)   # 空结果也算正常退出（失败安全），不让 Actions 标红

if __name__ == "__main__":
    main()
