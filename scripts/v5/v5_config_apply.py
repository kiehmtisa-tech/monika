# -*- coding: utf-8 -*-
"""Monika v5 配置应用器：只改指定 key 的值，保留行尾注释与文件其余内容。"""
import re
import shutil
import sys
import time

CFG = "/opt/qq-fish/docker-config/mmc/bot_config.toml"

# key -> (old_value, new_value)
CHANGES = {
    "chat_summary_writeback_message_threshold": ("72", "12"),
    "chat_summary_writeback_context_length": ("72", "16"),
    "context_compaction_summary_max_chars": ("6000", "2000"),
    "person_profile_injection_max_profiles": ("3", "2"),
}

DRY = "--apply" not in sys.argv


def main():
    with open(CFG, "r", encoding="utf-8") as f:
        lines = f.readlines()

    applied, missing = [], []
    for key, (old, new) in CHANGES.items():
        pat = re.compile(r"^(\s*" + re.escape(key) + r"\s*=\s*)" + re.escape(old) + r"(\s*(?:#.*)?)$")
        hit = None
        for i, line in enumerate(lines):
            m = pat.match(line.rstrip("\n"))
            if m:
                hit = i
                newline = m.group(1) + new + m.group(2)
                if not DRY:
                    lines[i] = newline + ("\n" if line.endswith("\n") else "")
                applied.append((i + 1, key, old, new, newline.strip()))
                break
        if hit is None:
            missing.append(key)

    for ln, key, old, new, text in applied:
        print("%4d  %-45s %s -> %s" % (ln, key, old, new))
    for key in missing:
        print("MISS %-45s 未找到匹配行（值可能已不同），跳过并需人工确认" % key)

    if DRY:
        print("\n[dry-run] 未写入。加 --apply 生效。")
        return

    if not applied:
        print("无可应用项，退出。")
        return

    ts = time.strftime("%Y%m%d_%H%M%S")
    bak = "/opt/qq-fish/backups/v5_%s_bot_config.toml" % ts
    shutil.copy2(CFG, bak)
    with open(CFG, "w", encoding="utf-8") as f:
        f.writelines(lines)

    # 校验可解析
    try:
        import tomllib
        with open(CFG, "rb") as f:
            tomllib.load(f)
        print("\n写入成功，TOML 解析通过。备份：%s" % bak)
    except Exception as e:
        shutil.copy2(bak, CFG)
        print("\nTOML 校验失败，已回滚：%s" % e)
        raise SystemExit(1)


if __name__ == "__main__":
    main()
