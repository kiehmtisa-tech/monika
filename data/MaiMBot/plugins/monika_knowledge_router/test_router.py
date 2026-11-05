# -*- coding: utf-8 -*-
"""monika_knowledge_router 纯函数单测（零依赖，python3 直接跑）。

覆盖：分类命中/不命中、范围优先、experience 隔离、来源过滤、时间线加权、
去重、注入块构造、隐私守卫行存在。
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from router import (  # noqa: E402
    SCOPE_CANON, SCOPE_EXPERIENCE, SCOPE_PIANO, SCOPE_PLUS, SCOPE_POETRY,
    PrefetchItem, Route, build_injection, classify, filter_rerank,
)

FAIL = 0


def check(name: str, cond: bool, detail: str = "") -> None:
    global FAIL
    mark = "PASS" if cond else "FAIL"
    if not cond:
        FAIL += 1
    print(f"[{mark}] {name} {detail}")


def test_classify_plain():
    check("闲聊不路由", classify("今天好累啊") is None)
    check("空文本不路由", classify("") is None)
    check("纯表情不路由", classify("哈哈哈哈") is None)


def test_classify_scopes():
    r = classify("你当初为什么对夏树的漫画有偏见？")
    check("canon 命中", r is not None and SCOPE_CANON in r.scopes, str(r and r.scopes))
    r = classify("Plus 的 Respect 里你是不是偏见漫画？")
    check("plus 命中", r is not None and SCOPE_PLUS in r.scopes, str(r and r.scopes))
    r = classify("这首七律押韵正确吗？")
    check("poetry 命中", r is not None and SCOPE_POETRY in r.scopes, str(r and r.scopes))
    r = classify("帮我看看这段和弦进行，踏板怎么踩？")
    check("piano 命中", r is not None and SCOPE_PIANO in r.scopes, str(r and r.scopes))
    r = classify("你还记得我昨天跟你说的考试结果吗？")
    check("experience 命中", r is not None and r.scopes == [SCOPE_EXPERIENCE], str(r and r.scopes))


def test_classify_priority():
    r = classify("Plus 支线里你和纱世里的故事")
    check("plus+canon 最多 2 scope", r is not None and len(r.scopes) <= 2, str(r and r.scopes))
    check("plus 优先于 canon", r is not None and r.scopes[0] == SCOPE_PLUS, str(r and r.scopes))
    r = classify("上次我跟你说的诗写得怎么样")
    check("experience 混合时仍加入", r is not None and SCOPE_EXPERIENCE in r.scopes, str(r and r.scopes))


def test_query_terms():
    r = classify("你觉得《Hole in Wall》这首诗的意象怎么样")
    check("query 含实体词", r is not None and any("Hole" in t or "意象" in t for t in r.query_terms),
          str(r and r.query_terms))


def test_filter_rerank():
    items = [
        {"source": "web_import:ddlc_canon__01_人物设定", "content": "夏树喜欢漫画，Act 1 与优里争论。"},
        {"source": "web_import:ddlc_canon__11_Plus支线总索引", "content": "Respect：莫妮卡对漫画的偏见（Plus 时间线之外）。"},
        {"source": "web_import:literature_music__01_诗歌形式与修辞", "content": "俳句 5-7-5 音拍。"},
        {"source": "person_fact:abc", "content": "用户说过喜欢轻的结尾。"},
        {"source": "web_import:ddlc_canon__01_人物设定", "content": "夏树喜欢漫画，Act 1 与优里争论。"},
    ]
    out = filter_rerank(items, [SCOPE_CANON], max_items=4)
    check("canon 过滤掉 literature", all("literature" not in p.source for p in out), str([p.source for p in out]))
    check("canon 过滤掉 person_fact", all(not p.source.startswith("person_fact") for p in out))
    check("去重生效", len(out) == len({p.content[:80] for p in out}))
    out2 = filter_rerank(items, [SCOPE_PLUS], max_items=4)
    check("plus 只留 11_/03_", all(("11_" in p.source or "03_" in p.source) for p in out2),
          str([p.source for p in out2]))
    out3 = filter_rerank(items, [SCOPE_EXPERIENCE], max_items=4)
    check("experience 只留 person_fact 等", all(p.source.startswith(("person_fact", "chat_summary", "episode", "raw_scan")) for p in out3),
          str([p.source for p in out3]))


def test_timeline_weight():
    items = [
        {"source": "web_import:ddlc_canon__11_Plus支线总索引", "content": "普通条目"},
        {"source": "web_import:ddlc_canon__11_Plus支线总索引", "content": "本条明确 Plus 独立时间线之外的事件"},
    ]
    out = filter_rerank(items, [SCOPE_PLUS], max_items=2)
    check("时间线词加权", out and "时间线" in out[0].content, str([(p.score, p.content[:12]) for p in out]))


def test_injection():
    route = Route(scopes=[SCOPE_CANON], triggers=["夏树"], query_terms=["夏树"])
    picked = [
        PrefetchItem(source="web_import:ddlc_canon__08_人物关系_本篇", content="莫妮卡与夏树：部长与部员，调解争论。"),
        PrefetchItem(source="web_import:ddlc_canon__05_主线_Act2事件", content="Act 2 夏树的冲突升级。"),
    ]
    text = build_injection(route, picked, max_chars=900)
    check("注入含知识路由头", text.startswith("【知识路由】"))
    check("注入含来源", "ddlc_canon__08" in text)
    check("注入含隐私守卫", "respect_filter 必须保持 true" in text)
    check("注入不超长", len(text) <= 900 + 120, str(len(text)))  # 守卫行容差

    tiny = build_injection(route, picked, max_chars=60)
    check("max_chars 截断生效", tiny.count("\n") <= 3, repr(tiny[:80]))


def main() -> int:
    test_classify_plain()
    test_classify_scopes()
    test_classify_priority()
    test_query_terms()
    test_filter_rerank()
    test_timeline_weight()
    test_injection()
    print("=== %s (%d failed) ===" % ("ALL PASS" if FAIL == 0 else "HAS FAILURES", FAIL))
    return 1 if FAIL else 0


if __name__ == "__main__":
    sys.exit(main())
