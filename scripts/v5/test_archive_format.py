"""归档文案结构化验证：格式 / 失败保留 / 非指令明示 / 真实 dump 抽样。"""
import json, glob, os, sys
sys.path.insert(0, "/MaiMBot/plugins")
from monika_governance.tool_chain import build_archive_note, archive_old_tool_rounds

fails = []

# 1) 成功/失败状态保留
turn = [
    {"item_type": "FunctionCallItem", "meta": {"logical_turn_id": "t1"},
     "tool_call": {"call_id": "c1", "func_name": "memory_query", "args": {"query": "群友兴趣"}}},
    {"item_type": "FunctionCallOutputItem", "meta": {"logical_turn_id": "t1"},
     "call_id": "c1", "output": "找到与《怪物猎人》相关的记忆 3 条"},
    {"item_type": "FunctionCallItem", "meta": {"logical_turn_id": "t1"},
     "tool_call": {"call_id": "c2", "func_name": "get_weather", "args": {"city": "北京"}}},
    {"item_type": "FunctionCallOutputItem", "meta": {"logical_turn_id": "t1"},
     "call_id": "c2", "output": "Error: timeout after 30s"},
]
note = build_archive_note(turn, note_id="t")
txt = note["parts"][0]["text"]
print("=== 生成样例 ===")
print(txt)
print()

checks = [
    ("含非指令明示", "非指令" in txt),
    ("含历史记录提示", "历史执行记录" in txt or "历史工具操作摘要" in txt),
    ("成功状态保留", "成功" in txt),
    ("失败状态保留", "失败" in txt),
    ("失败未被概括为成功", "get_weather" in txt and "失败" in txt.split("get_weather")[1].split("\n")[0]),
    ("无原始 JSON 全文", "{" not in txt.split("｜")[-1][:80]),
    ("结构化分隔", "｜" in txt),
    ("明示需重新调用", "重新调用" in txt),
]
for name, ok in checks:
    print("  %s: %s" % (name, "PASS" if ok else "FAIL"))
    if not ok:
        fails.append(name)

# 2) 无 success 字段时的启发式判定
turn2 = [
    {"item_type": "FunctionCallItem", "meta": {"logical_turn_id": "t2"},
     "tool_call": {"call_id": "c1", "func_name": "search", "args": {"q": "x"}}},
    {"item_type": "FunctionCallOutputItem", "meta": {"logical_turn_id": "t2"},
     "call_id": "c1", "output": "Exception: 写者锁冲突"},
]
t2 = build_archive_note(turn2, note_id="t2")["parts"][0]["text"]
heuristic_ok = "失败" in t2
print("  启发式失败判定: %s" % ("PASS" if heuristic_ok else "FAIL"))
if not heuristic_ok:
    fails.append("启发式失败判定")

# 3) 真实 dump 抽样：找出已有归档条目并统计格式
dumps = sorted(glob.glob("/MaiMBot/logs/maisaka_prompt/planner/*/*.json"), key=os.path.getmtime)
old_fmt = new_fmt = 0
samples = []
for f in dumps[-60:]:
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    for it in d.get("request_items") or []:
        txt = "".join(p.get("text", "") for p in it.get("parts", []) if isinstance(p, dict))
        if "[工具轮次归档]" in txt:
            old_fmt += 1
            samples.append(txt[:100])
        elif "[历史工具操作摘要" in txt:
            new_fmt += 1
            samples.append(txt[:100])
print()
print("=== 真实 dump 抽样（最近 60 个请求） ===")
print("  旧格式归档条目: %d | 新格式: %d" % (old_fmt, new_fmt))
for s in samples[:5]:
    print("   ", s.replace("\n", " ")[:90])

print()
print("判定:", "全部通过" if not fails else "FAIL: %s" % fails)
sys.exit(1 if fails else 0)
