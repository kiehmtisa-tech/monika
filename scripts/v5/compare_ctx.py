# -*- coding: utf-8 -*-
"""对比重启前后的 planner 上下文，验证清理边界是否持久生效。"""
import glob
import json
import os

fs = sorted(
    glob.glob("/opt/qq-fish/data/MaiMBot/logs/maisaka_prompt/planner/*/*.json"),
    key=os.path.getmtime,
)
if not fs:
    print("没有 planner dump")
    raise SystemExit(1)

d = json.load(open(fs[-1], encoding="utf-8"))
items = d.get("request_items", [])
users = [i for i in items if i.get("item_type") == "UserMessageItem"]
meta = d.get("metadata", {})

print("最新 dump:", os.path.basename(fs[-1]))
print("总 items=%d，用户消息=%d   （重启前是 208 / 79）" % (len(items), len(users)))
print("prompt_tokens=%s  total=%s" % (meta.get("prompt_tokens"), meta.get("total_tokens")))
print("--- 上下文里的用户消息 ---")
for u in users:
    txt = "".join(p.get("text", "") for p in u.get("parts", []) if isinstance(p, dict))
    print("   ", txt[:50].replace("\n", " "))
