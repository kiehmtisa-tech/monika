"""A/B/C/D 对照测量：同一历史请求样本下四种配置的输入规模对比。

A = 基线（原始 items + 原始工具定义）
B = 只启用工具轮次归档
C = 只启用工具 Schema 精简
D = 归档 + 精简

用法：在容器内运行。对最近 N 个 planner dump 逐个重放两种变换，
统计 items 字符、tool schema 字符、估算 tokens（字符/2）。
"""
import json
import glob
import os
import sys

sys.path.insert(0, "/MaiMBot/plugins")
from monika_governance.tool_chain import archive_old_tool_rounds  # noqa: E402
from monika_governance.tool_policy import filter_tool_definitions  # noqa: E402


def chars(items):
    return sum(len(json.dumps(i, ensure_ascii=False)) for i in items)


def main():
    all_dumps = sorted(glob.glob("/MaiMBot/logs/maisaka_prompt/planner/*/*.json"),
                       key=os.path.getmtime)
    # 只用 hook 上线（2026-11-04 10:00）之前的原始请求做基线样本，
    # 避免拿已被归档的 dump 再次测量（那会低估收益）
    cutoff = 1793757600000
    pre = [f for f in all_dumps
           if os.path.basename(f).replace(".json", "").isdigit()
           and int(os.path.basename(f).replace(".json", "")) < cutoff]
    dumps = (pre or all_dumps)[-40:]
    agg = {"A": {"items": 0, "tools": 0, "n": 0, "archived": 0},
           "B": {"items": 0, "tools": 0, "n": 0, "archived": 0},
           "C": {"items": 0, "tools": 0, "n": 0, "archived": 0},
           "D": {"items": 0, "tools": 0, "n": 0, "archived": 0}}

    for f in dumps:
        d = json.load(open(f, encoding="utf-8"))
        items = d.get("request_items") or []
        defs = d.get("tool_definitions") or []
        if not items:
            continue
        base_items = chars(items)
        base_tools = chars(defs)

        b_items, b_stats = archive_old_tool_rounds(items, keep_recent=2)
        c_defs, c_stats = filter_tool_definitions(defs, items=items, is_owner_stream=True)
        d_items, _ = archive_old_tool_rounds(items, keep_recent=2)
        d_defs, _ = filter_tool_definitions(defs, items=d_items, is_owner_stream=True)

        agg["A"]["items"] += base_items; agg["A"]["tools"] += base_tools; agg["A"]["n"] += 1
        agg["B"]["items"] += chars(b_items); agg["B"]["tools"] += base_tools; agg["B"]["n"] += 1
        agg["B"]["archived"] += b_stats.get("archived_turns", 0)
        agg["C"]["items"] += base_items; agg["C"]["tools"] += chars(c_defs); agg["C"]["n"] += 1
        agg["D"]["items"] += chars(d_items); agg["D"]["tools"] += chars(d_defs); agg["D"]["n"] += 1

    n = max(agg["A"]["n"], 1)
    base_total = (agg["A"]["items"] + agg["A"]["tools"]) / n
    print("样本数: %d（最近 40 个 planner 请求）" % n)
    print()
    print("%-4s %12s %12s %12s %10s" % ("配置", "items 字符/次", "工具字符/次", "合计字符/次", "估算 tokens"))
    for k in ("A", "B", "C", "D"):
        it = agg[k]["items"] / n
        tl = agg[k]["tools"] / n
        total = it + tl
        print("%-4s %12.0f %12.0f %12.0f %10.0f" % (k, it, tl, total, total / 2))
    print()
    for k in ("B", "C", "D"):
        total = (agg[k]["items"] + agg[k]["tools"]) / n
        delta = (base_total - total) / base_total * 100
        print("%s 相对基线: -%.1f%%" % (k, delta))
    print()
    print("B/D 的归档轮次总数:", agg["B"]["archived"])
    print()
    print("注：tokens 为字符/2 估算；延迟/超时率/回复正确性需 7 天自然流量观察（已验：")
    print("    live 请求中工具调用与回复正常、协议配对无缺口）。")


main()
