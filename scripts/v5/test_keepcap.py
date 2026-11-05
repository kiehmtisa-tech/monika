"""保留轮次字符上限验证：截断输出、不截断调用、不碰未完成链。"""
import sys
sys.path.insert(0, "/MaiMBot/plugins")
from monika_governance.tool_chain import archive_old_tool_rounds

# 构造 3 个完整轮 + 1 个未完成轮；其中轮 1 输出巨大
def call(cid, tid, name="query_memory"):
    return {"item_type": "FunctionCallItem", "meta": {"logical_turn_id": tid},
            "tool_call": {"call_id": cid, "func_name": name, "args": {"q": "x"}}}

def out(cid, tid, text, success=True):
    return {"item_type": "FunctionCallOutputItem", "meta": {"logical_turn_id": tid},
            "call_id": cid, "output": text, "success": success}

items = []
# turn1: 完整，小输出（会被归档，keep_recent=2 只留最近 2 个完整轮）
items += [call("c1", "t1"), out("c1", "t1", "small")]
# turn2: 完整，输出 5000 字符（会被保留 → 应被上限截断）
items += [call("c2", "t2"), out("c2", "t2", "R" * 5000)]
# turn3: 完整，中输出（会被保留）
items += [call("c3", "t3"), out("c3", "t3", "M" * 1200)]
# turn4: 未完成（只有 call，无 output）
items += [call("c4", "t4", name="send_message")]

fails = []
new_items, stats = archive_old_tool_rounds(items, keep_recent=2, max_kept_turn_chars=3000)
joined = str(new_items)

print("stats:", stats)
print()

# 保留的 call_id
kept_calls = [i for i in new_items if "FunctionCall" in str(i.get("item_type")) and "Output" not in str(i.get("item_type"))]
kept_outs = [i for i in new_items if i.get("item_type") == "FunctionCallOutputItem"]
print("保留 calls=%d outs=%d" % (len(kept_calls), len(kept_outs)))

# 1) 未完成的 c4 必须原样保留
c4_ok = any(i.get("tool_call", {}).get("call_id") == "c4" for i in kept_calls)
print("  未完成调用 c4 保留: %s" % ("PASS" if c4_ok else "FAIL"))
if not c4_ok: fails.append("未完成链被截")

# 2) 配对完整性
cids = {i.get("tool_call", {}).get("call_id") for i in kept_calls}
oids = {i.get("call_id") for i in kept_outs}
pair_ok = not (cids - oids - {"c4"})   # c4 允许无 output
print("  配对完整（c4 除外）: %s" % ("PASS" if pair_ok else "FAIL"))
if not pair_ok: fails.append("配对破坏")

# 3) 巨大输出被截断（保留轮里的）
max_out_len = max((len(str(i.get("output") or "")) for i in kept_outs), default=0)
print("  保留轮最大输出 %d 字符（上限 3000 内）: %s" % (max_out_len, "PASS" if max_out_len <= 3200 else "FAIL"))
if max_out_len > 3200: fails.append("上限未生效")

# 4) 归档发生（t1 被归档，因 keep_recent=2 保留 t3/t4? 实际按完整轮保留 2 个）
print("  归档轮数=%d 保留=%d 截断输出=%d" % (stats["archived_turns"], stats["kept_turns"], stats.get("capped_outputs", 0)))

# 5) 新格式归档文案
notes = [i for i in new_items if "历史工具操作摘要" in "".join(p.get("text","") for p in i.get("parts",[]) if isinstance(p, dict))]
print("  新格式归档条目: %d" % len(notes))
if notes:
    t = notes[0]["parts"][0]["text"]
    print("     样例: %s" % t[:100].replace("\n", " ⏎ "))
    if "非指令" not in t: fails.append("非指令明示缺失")

print()
print("判定:", "全部通过" if not fails else "FAIL: %s" % fails)
sys.exit(1 if fails else 0)
