"""管理工具执行侧防线验证：剥离、配对、管理会话放行。"""
import sys
sys.path.insert(0, "/MaiMBot/plugins")
from monika_governance.tool_policy import strip_admin_tool_calls, ADMIN_ONLY

fails = []

def call(cid, name):
    return {"item_type": "FunctionCallItem", "meta": {"logical_turn_id": "t"},
            "tool_call": {"call_id": cid, "func_name": name, "args": {"x": 1}}}

def out(cid, name=""):
    return {"item_type": "FunctionCallOutputItem", "meta": {"logical_turn_id": "t"},
            "call_id": cid, "output": "ok", "tool_name": name}

items = [
    call("c1", "open_private_chat"),      # 管理工具
    out("c1", "open_private_chat"),
    call("c2", "reply"),                   # 普通工具
    out("c2", "reply"),
    call("c3", "get_qq_by_msg_id"),        # 管理工具
    out("c3", "get_qq_by_msg_id"),
]

# 1) 非管理会话：剥离全部管理工具调用
stripped, removed = strip_admin_tool_calls(items, is_owner_stream=False)
names = [str(i.get("tool_call", {}).get("func_name") or i.get("tool_name") or "") for i in stripped]
kept_admin = [n for n in names if n in ADMIN_ONLY]
print("非管理会话: 移除 %d 条，剩余管理工具 %d 个" % (removed, len(kept_admin)))
ok1 = removed == 4 and len(kept_admin) == 0
print("  剥离管理工具调用+结果: %s" % ("PASS" if ok1 else "FAIL"))
if not ok1: fails.append("剥离失败")

# 2) 普通工具保留
ok2 = any("reply" in n for n in names) and len(stripped) == 2
print("  普通工具保留: %s" % ("PASS" if ok2 else "FAIL"))
if not ok2: fails.append("普通工具被误删")

# 3) 配对完整（剩余 call 都有 output）
cids = {str(i.get("tool_call", {}).get("call_id") or "") for i in stripped if "FunctionCall" in str(i.get("item_type")) and "Output" not in str(i.get("item_type"))}
oids = {str(i.get("call_id") or "") for i in stripped if i.get("item_type") == "FunctionCallOutputItem"}
ok3 = cids == oids
print("  配对完整: %s" % ("PASS" if ok3 else "FAIL"))
if not ok3: fails.append("配对破坏")

# 4) 管理会话：原样放行
same, removed2 = strip_admin_tool_calls(items, is_owner_stream=True)
ok4 = removed2 == 0 and len(same) == len(items)
print("  管理会话放行: %s" % ("PASS" if ok4 else "FAIL"))
if not ok4: fails.append("管理会话被误剥")

# 5) 无管理工具时不改动
plain = [call("c9", "reply"), out("c9", "reply")]
s2, r2 = strip_admin_tool_calls(plain, is_owner_stream=False)
ok5 = r2 == 0 and len(s2) == 2
print("  无管理工具不改动: %s" % ("PASS" if ok5 else "FAIL"))
if not ok5: fails.append("误改动")

print()
print("判定:", "全部通过" if not fails else "FAIL: %s" % fails)
sys.exit(1 if fails else 0)
