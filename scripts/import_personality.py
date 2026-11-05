#!/usr/bin/env python3
"""把 Monika_MaiBot_defined_v2.json 导入 MaiBot bot_config.toml 人格字段。

映射关系（人设 JSON → bot_config.toml）：
  profile.personality            → [personality].personality
  profile.behavior_style         → [personality].behavior_style
  profile.reply_style            → [personality].reply_style
  profile.multiple_reply_style   → [personality].multiple_reply_style
  profile.multiple_probability   → [personality].multiple_probability
  profile.group_chat_prompt      → [chat.reply_style].group_chat_prompt
  profile.private_chat_prompts   → [chat.reply_style].private_chat_prompts
"""
import json
import pathlib
import re

ROOT = pathlib.Path("/opt/qq-fish")
persona = json.loads((ROOT / "persona/Monika_MaiBot_defined_v2.json").read_text(encoding="utf-8"))
p = persona["profile"]

bc_path = ROOT / "docker-config/mmc/bot_config.toml"
orig = bc_path.read_text(encoding="utf-8")


def set_key(text: str, section: str, key: str, value) -> str:
    """替换指定 section 内 key = ... 的整行值（支持字符串/数组/数字）。"""
    sec_re = re.compile(r"^\[" + re.escape(section) + r"\]", re.M)
    m = sec_re.search(text)
    if not m:
        raise SystemExit(f"FAIL: 未找到 section [{section}]")
    # section 范围：从 section 标题到下一个 section 标题
    nxt = re.search(r"^\[", text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    block = text[m.start():end]

    key_re = re.compile(r"^" + re.escape(key) + r"\s*=\s*[^\n]*", re.M)
    km = key_re.search(block)
    if not km:
        raise SystemExit(f"FAIL: [{section}] 内未找到 key {key}")

    if isinstance(value, str):
        rendered = json.dumps(value, ensure_ascii=True)      # TOML basic string
    else:
        rendered = json.dumps(value, ensure_ascii=True)      # 数组/数字同样兼容

    new_block = block[:km.start()] + f"{key} = {rendered}" + block[km.end():]
    return text[:m.start()] + new_block + text[end:]


for key in ("personality", "behavior_style", "reply_style", "multiple_reply_style", "multiple_probability"):
    if key in p:
        orig_len = len(orig)
        orig = set_key(orig, "personality", key, p[key])

for key in ("group_chat_prompt", "private_chat_prompts"):
    if key in p:
        orig = set_key(orig, "chat.reply_style", key, p[key])

# 昵称：人设要求以 Monika 身份交流；保留大肥鱼作为别名（被这样称呼时仍能识别）
orig = set_key(orig, "bot", "nickname", "Monika")
orig = set_key(orig, "bot", "alias_names", ["大肥鱼", "肥鱼", "莫妮卡", "monika"])

bc_path.write_text(orig, encoding="utf-8")

print("✅ 人格导入完成：")
for key in ("personality", "behavior_style", "reply_style", "multiple_reply_style", "multiple_probability"):
    if key in p:
        print(f"  [personality].{key} = {json.dumps(p[key], ensure_ascii=False)[:70]}...")
for key in ("group_chat_prompt", "private_chat_prompts"):
    if key in p:
        print(f"  [chat.reply_style].{key} = {json.dumps(p[key], ensure_ascii=False)[:70]}...")
print("  [bot].nickname = Monika, alias_names 保留 大肥鱼/肥鱼/莫妮卡/monika")
