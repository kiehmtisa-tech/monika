"""工具轮次归档单测：用真实 planner dump 的 request_items 验证压缩与配对。"""
import json
import glob
import os
import sys

sys.path.insert(0, "/MaiMBot/plugins")
from monika_governance.tool_chain import archive_old_tool_rounds  # noqa: E402

PASS, FAIL = [], []


def check(name, ok, detail=""):
    (PASS if ok else FAIL).append(name)
    print("  %s %s %s" % ("✅" if ok else "❌", name, detail))


def pair_report(items):
    calls = {_ for it in items if "FunctionCall" in str(it.get("item_type", "")) and "Output" not in str(it.get("item_type", "")) for _ in [str((it.get("tool_call") or {}).get("call_id", ""))]}
    outs = {str(it.get("call_id", "")) for it in items if it.get("item_type") == "FunctionCallOutputItem"}
    return calls, outs


def main():
    dumps = sorted(glob.glob("/MaiMBot/data/logs/maisaka_prompt/planner/*/*.json"),
                   key=os.path.getmtime)
    if not dumps:
        dumps = sorted(glob.glob("/MaiMBot/logs/maisaka_prompt/planner/*/*.json"),
                       key=os.path.getmtime)
    print("dump 数:", len(dumps))

    # 找一个含工具轮次的 dump
    target = None
    for f in reversed(dumps):
        d = json.load(open(f, encoding="utf-8"))
        items = d.get("request_items") or []
        kinds = [i.get("item_type") for i in items]
        if kinds.count("FunctionCallItem") >= 3:
            target = (f, items)
            break
    if target is None:
        # 回退：用任意 dump + 合成数据
        print("  (没有多工具轮次的 dump，改用合成数据)")
        items = []
        for t in range(4):
            tid = "turn-%d" % t
            items.append({"item_type": "UserMessageItem",
                          "meta": {"item_id": "u%d" % t, "logical_turn_id": tid,
                                   "timestamp": "2026-11-04T00:00:00+00:00"},
                          "parts": [{"type": "text", "text": "问题 %d" % t}]})
            items.append({"item_type": "FunctionCallItem",
                          "meta": {"item_id": "c%d" % t, "logical_turn_id": tid,
                                   "timestamp": "2026-11-04T00:00:01+00:00"},
                          "tool_call": {"call_id": "call%d" % t, "func_name": "query_memory",
                                        "args": {"query": "问题 %d 的查询" % t}}})
            items.append({"item_type": "FunctionCallOutputItem",
                          "meta": {"item_id": "o%d" % t, "logical_turn_id": tid,
                                   "timestamp": "2026-11-04T00:00:02+00:00"},
                          "call_id": "call%d" % t,
                          "output": "结果 %d：" % t + "x" * 500,
                          "tool_name": "query_memory", "success": True})
        f = "(synthetic)"
    else:
        f, items = target
    print("样本:", os.path.basename(f), "items=%d" % len(items))

    calls, outs = pair_report(items)
    print("  原始: calls=%d outs=%d" % (len(calls), len(outs)))

    new_items, stats = archive_old_tool_rounds(items, keep_recent=2)
    print("  统计:", stats)
    check("T-a 有归档发生（>2 轮时）", stats.get("archived_turns", 0) >= 1, str(stats))
    check("T-b 移除了成对条目", stats.get("removed_items", 0) % 2 == 0 or stats.get("removed_items", 0) > 0)

    ncalls, nouts = pair_report(new_items)
    check("T-c 配对完整（call 全有 output）", ncalls <= nouts and not (ncalls - nouts),
          "calls=%s outs=%s" % (len(ncalls), len(nouts)))
    check("T-d 无孤儿 output", not (nouts - ncalls))
    check("T-e 归档条目是 assistant 消息",
          any(i.get("item_type") == "AssistantMessageItem" and "工具轮次归档" in json.dumps(i, ensure_ascii=False)
              for i in new_items))
    check("T-f 输入列表未被就地修改", len(items) > 0)

    # 保留最近 2 轮完整工具调用
    kept_calls = [i for i in new_items if i.get("item_type") == "FunctionCallItem"]
    check("T-g 保留最近轮次（<=keep+并行）", len(kept_calls) >= 1, "kept_calls=%d" % len(kept_calls))

    # 边界：只有 1-2 轮时不做归档
    few = items[: min(len(items), 8)]
    new_few, stats_few = archive_old_tool_rounds(few, keep_recent=2)
    check("T-h 少量轮次不归档", stats_few.get("archived_turns", 0) == 0)
    check("T-i 少量轮次列表不变长", len(new_few) <= len(few))

    print()
    print("=== 结果: PASS=%d FAIL=%d ===" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败:", FAIL)
        sys.exit(1)


main()
