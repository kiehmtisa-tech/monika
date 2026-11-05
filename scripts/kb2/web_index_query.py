# -*- coding: utf-8 -*-
"""Monika 文学/钢琴网站索引 · 本地路由查询。

按意图关键词匹配 routing，返回推荐类别与优先站点。
零依赖；不联网——只回答"去哪查"，真正检索需另接工具。

用法：
    python3 web_index_query.py 中国古诗词 格律
    python3 web_index_query.py 钢琴曲 乐谱
    python3 web_index_query.py --policy     # 检索预算与治理规则
    python3 web_index_query.py --categories # 列出全部类别与站点数
"""
import json
import sys
from pathlib import Path

CANDIDATES = [
    Path("/opt/qq-fish/resources/web_index/monika_piano_poetry_sites.json"),
    Path(__file__).resolve().parent.parent.parent / "resources" / "web_index" / "monika_piano_poetry_sites.json",
]


def load_index():
    for p in CANDIDATES:
        if p.exists():
            return json.loads(p.read_text(encoding="utf-8")), p
    raise SystemExit("找不到索引 JSON（resources/web_index/monika_piano_poetry_sites.json）")


def match_routing(routing, keywords):
    """关键词命中 intent/categories 的粗匹配。"""
    hits = []
    for r in routing:
        text = r.get("intent", "") + " " + " ".join(r.get("categories", []))
        score = sum(1 for k in keywords if k and k.lower() in text.lower())
        if score:
            hits.append((score, r))
    hits.sort(key=lambda x: -x[0])
    return [r for _, r in hits]


def main():
    idx, path = load_index()
    args = sys.argv[1:]

    if not args:
        print(__doc__)
        print("索引: %s (%d sources)" % (path, len(idx.get("sources", []))))
        return 0

    if args[0] == "--policy":
        pol = idx.get("retrieval_policy", {})
        keys = ["activation", "max_distinct_sites_per_lookup", "max_pages_per_site",
                "max_results_returned_to_model", "model_context_budget_chars",
                "network_timeout_seconds", "result_cache_ttl_hours",
                "bulk_crawling", "automatically_import_to_KB"]
        for k in keys:
            print("%-38s %s" % (k, pol.get(k)))
        print("\ncontent_filter:")
        for k, v in (pol.get("content_filter") or {}).items():
            print("  %-30s %s" % (k, v))
        print("\ncopyright:", json.dumps(pol.get("copyright"), ensure_ascii=False))
        return 0

    if args[0] == "--categories":
        cats = {}
        for s in idx.get("sources", []):
            cats.setdefault(s.get("category", "?"), []).append(s.get("name"))
        for c, names in sorted(cats.items()):
            print("%-24s %d 个: %s" % (c, len(names), "、".join(names[:3]) + ("…" if len(names) > 3 else "")))
        return 0

    keywords = [a for a in args if not a.startswith("-")]
    routes = match_routing(idx.get("routing", []), keywords)
    if not routes:
        print("无匹配路由。可用 intent：")
        for r in idx.get("routing", []):
            print("  -", r.get("intent"))
        return 1

    sources = idx.get("sources", [])
    for r in routes[:3]:
        print("══ 意图: %s" % r.get("intent"))
        for cat in r.get("categories", []):
            picks = sorted([s for s in sources if s.get("category") == cat],
                           key=lambda s: s.get("priority", 9))
            print("  类别 %s:" % cat)
            for s in picks[:3]:
                print("    [P%s] %-36s %s" % (s.get("priority"), s.get("name"), s.get("url")))
                uc = s.get("use_cases") or s.get("notes") or ""
                if uc:
                    print("         用途: %s" % str(uc)[:80])
        rights = [s.get("rights", "") for s in sources if s.get("category") in r.get("categories", [])]
        if rights:
            print("  版权提示: %s" % str(rights[0])[:100])
    print("\n[预算] ≤2站/次, ≤2页/站, ≤4条回模型, ≤2800字上下文; 本地知识足够不联网。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
