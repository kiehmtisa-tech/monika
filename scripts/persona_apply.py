#!/usr/bin/env python3
"""人设应用脚本：把人设 JSON 的字段与行为开关写入 bot_config.toml。

用法:
    python3 scripts/persona_apply.py snapshot
    python3 scripts/persona_apply.py apply <profile.json> [--knobs <knobs.toml>] [--sync-talk-rules]

设计要点:
  - 只改白名单字段，按行正则替换，其它配置与注释保持原样（不重排 TOML）
  - 写入前把 bot_config.toml 备份到 backups/persona_<ts>/
  - 写入后 tomllib 解析校验并逐字段核对，失败自动回滚
  - 生效方式：MaiBot 自带文件监视热重载（最小间隔 1s / 超时 20s），无需重启容器
"""
from __future__ import annotations

import argparse
import json
import pathlib
import re
import shutil
import sys
import time
import tomllib

ROOT = pathlib.Path("/opt/qq-fish")
BOT_CONFIG = ROOT / "docker-config/mmc/bot_config.toml"
ACTIVE_DIR = ROOT / "persona/active"
BACKUP_DIR = ROOT / "backups"

PERSONALITY_FIELDS = (
    "personality",
    "behavior_style",
    "reply_style",
    "multiple_reply_style",
    "multiple_probability",
)
CHAT_FIELDS = ("group_chat_prompt", "private_chat_prompts")

# 只允许改这些开关，避免脚本被误用去动模型/密钥/记忆等配置
KNOB_WHITELIST = {
    "response_splitter": {"enable", "mode", "max_split_num", "max_sentence_num"},
    "chinese_typo": {"enable"},
    "chat.reply_timing": {
        "talk_value",
        "mentioned_bot_reply",
        "inevitable_at_reply",
        "planner_message_steering",
    },
    "chat.reply_style": {"private_quote_frequency", "group_quote_frequency"},
    "chat": {"max_context_size", "max_private_context_size"},
}


def die(msg: str) -> "NoReturn":  # type: ignore[name-defined]
    print(f"FAIL: {msg}", file=sys.stderr)
    raise SystemExit(1)


def render(value) -> str:
    """python 值 → TOML 字面量（JSON 语法与 TOML 兼容：字符串/数组/数字/布尔）。"""
    if isinstance(value, bool):
        return "true" if value else "false"
    return json.dumps(value, ensure_ascii=True)


def section_span(text: str, section: str) -> tuple[int, int]:
    m = re.search(r"^\[" + re.escape(section) + r"\]", text, re.M)
    if not m:
        die(f"未找到 section [{section}]")
    nxt = re.search(r"^\[", text[m.end():], re.M)
    end = m.end() + nxt.start() if nxt else len(text)
    return m.start(), end


def get_key(text: str, section: str, key: str) -> str:
    start, end = section_span(text, section)
    block = text[start:end]
    km = re.search(r"^" + re.escape(key) + r"\s*=\s*[^\n]*", block, re.M)
    if not km:
        die(f"[{section}] 内未找到 key {key}")
    return block[km.start():km.end()]


def set_key(text: str, section: str, key: str, value) -> tuple[str, str, str]:
    """替换 section 内 key 的整行值，返回 (新文本, 旧行, 新行)。

    保留行尾注释（值里不含 # 时才保留），避免丢配置说明。
    """
    start, end = section_span(text, section)
    block = text[start:end]
    km = re.search(r"^" + re.escape(key) + r"\s*=\s*[^\n]*", block, re.M)
    if not km:
        die(f"[{section}] 内未找到 key {key}")
    old_line = block[km.start():km.end()]
    rendered = render(value)
    new_line = f"{key} = {rendered}"
    comment = re.search(r"\s+#\s.*$", old_line)
    if comment and "#" not in rendered and "#" not in old_line[: comment.start()]:
        new_line += " " + comment.group(0).strip()
    new_block = block[:km.start()] + new_line + block[km.end():]
    return text[:start] + new_block + text[end:], old_line, new_line


def write_current(profile: dict, profile_path: pathlib.Path | None, knobs: dict) -> pathlib.Path:
    """记录当前生效的人设快照；只应用开关时不覆盖人设快照。"""
    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    applied = ACTIVE_DIR / "CURRENT.json"
    applied.write_text(
        json.dumps(
            {
                "format": "maibot_personality",
                "version": 1,
                "applied_at": time.strftime("%Y-%m-%dT%H:%M:%S%z"),
                "source": str(profile_path) if profile_path else "",
                "knobs": knobs,
                "profile": profile,
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return applied


def brief(text: str, limit: int = 60) -> str:
    return text if len(text) <= limit else text[:limit] + "…"


def parse(path: pathlib.Path) -> dict:
    return tomllib.loads(path.read_text(encoding="utf-8"))


def flatten_knobs(node: dict, prefix: str = "") -> dict:
    """tomllib 会把 [chat.reply_timing] 解析成嵌套 dict，这里摊平成点号 section。"""
    flat: dict = {}
    for key, value in node.items():
        name = f"{prefix}{key}"
        if isinstance(value, dict):
            flat.update(flatten_knobs(value, name + "."))
        else:
            flat.setdefault(prefix.rstrip("."), {})[key] = value
    return flat


def write_atomic(path: pathlib.Path, text: str) -> None:
    tmp = path.with_suffix(path.suffix + f".tmp{int(time.time())}")
    tmp.write_text(text, encoding="utf-8")
    shutil.copymode(path, tmp)
    tmp.replace(path)


def snapshot() -> pathlib.Path:
    doc = parse(BOT_CONFIG)
    p = doc["personality"]
    rs = doc["chat"]["reply_style"]
    profile = {"name": p.get("selected_profile") or f"inline_{time.strftime('%Y%m%d_%H%M%S')}"}
    for key in PERSONALITY_FIELDS:
        if key in p:
            profile[key] = p[key]
    for key in CHAT_FIELDS:
        if key in rs:
            profile[key] = rs[key]
    ACTIVE_DIR.mkdir(parents=True, exist_ok=True)
    out = ACTIVE_DIR / f"snapshot_{time.strftime('%Y%m%d_%H%M%S')}.json"
    out.write_text(
        json.dumps({"format": "maibot_personality", "version": 1, "profile": profile},
                   ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    return out


def apply(profile_path: pathlib.Path | None, knobs_path: pathlib.Path | None, sync_talk_rules: bool) -> None:
    profile: dict = {}
    if profile_path is not None:
        data = json.loads(profile_path.read_text(encoding="utf-8"))
        profile = data.get("profile", data)
        if "personality" not in profile:
            die("人设 JSON 缺少 profile.personality")
    elif not knobs_path:
        die("至少要提供人设 JSON 或开关文件之一")

    knobs: dict = {}
    if knobs_path:
        raw = tomllib.loads(knobs_path.read_text(encoding="utf-8"))
        knobs = flatten_knobs(raw)
        for section, body in knobs.items():
            allowed = KNOB_WHITELIST.get(section)
            if allowed is None:
                die(f"开关 section [{section}] 不在白名单内")
            for key in body:
                if key not in allowed:
                    die(f"开关 [{section}].{key} 不在白名单内")

    original = BOT_CONFIG.read_text(encoding="utf-8")
    text = original
    changes: list[tuple[str, str, str]] = []

    for key in PERSONALITY_FIELDS:
        if key in profile:
            text, old, new = set_key(text, "personality", key, profile[key])
            changes.append((f"[personality].{key}", old, new))
    for key in CHAT_FIELDS:
        if key in profile:
            text, old, new = set_key(text, "chat.reply_style", key, profile[key])
            changes.append((f"[chat.reply_style].{key}", old, new))

    for section, body in knobs.items():
        for key, value in body.items():
            text, old, new = set_key(text, section, key, value)
            changes.append((f"[{section}].{key}", old, new))

    if sync_talk_rules:
        start, end = section_span(text, "chat.reply_timing")
        block = text[start:end]
        km = re.search(r"^talk_value_rules\s*=\s*[^\n]*", block, re.M)
        if not km:
            die("[chat.reply_timing] 内未找到 talk_value_rules")
        old_line = block[km.start():km.end()]
        talk = knobs.get("chat.reply_timing", {}).get("talk_value")
        if talk is None:
            die("--sync-talk-rules 需要同时提供 [chat.reply_timing].talk_value")
        new_line = re.sub(
            r'(time = "08:00-23:59"[^}]*?value = )([0-9.]+)',
            lambda m: m.group(1) + str(talk),
            old_line,
        )
        if new_line == old_line:
            die("talk_value_rules 未匹配到白天规则，未修改")
        text = text[:start] + block[:km.start()] + new_line + block[km.end():] + text[end:]
        changes.append(("[chat.reply_timing].talk_value_rules(白天)", old_line, new_line))

    if text == original:
        if profile:
            write_current(profile, profile_path, knobs)
        print("无变化，跳过写入")
        return

    stamp = time.strftime("%Y%m%d_%H%M%S")
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    backup = BACKUP_DIR / f"persona_{stamp}_bot_config.toml"
    shutil.copy2(BOT_CONFIG, backup)

    write_atomic(BOT_CONFIG, text)

    try:
        doc = parse(BOT_CONFIG)
        got = doc["personality"]
        for key in PERSONALITY_FIELDS:
            if key in profile and got.get(key) != profile[key]:
                raise ValueError(f"回读不一致: [personality].{key}")
        for key in CHAT_FIELDS:
            if key in profile and doc["chat"]["reply_style"].get(key) != profile[key]:
                raise ValueError(f"回读不一致: [chat.reply_style].{key}")
        for section, body in knobs.items():
            node = doc
            for part in section.split("."):
                node = node[part]
            for key, value in body.items():
                if node.get(key) != value:
                    raise ValueError(f"回读不一致: [{section}].{key}")
    except Exception as exc:  # noqa: BLE001
        shutil.copy2(backup, BOT_CONFIG)
        die(f"写入校验失败，已回滚：{exc}")

    # 只应用开关时不覆盖人设快照，避免 CURRENT.json 被清空
    applied = write_current(profile, profile_path, knobs) if profile else None
    history = ACTIVE_DIR / "APPLIED.jsonl"
    with history.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(
            {"at": time.strftime("%Y-%m-%dT%H:%M:%S%z"), "source": str(profile_path),
             "profile_name": profile.get("name"), "backup": str(backup),
             "fields": [c[0] for c in changes]},
            ensure_ascii=False) + "\n")

    print(f"备份: {backup}")
    if applied is not None:
        print(f"已生效人设: {applied}")
    print(f"改动 {len(changes)} 处：")
    for path, old, new in changes:
        print(f"  - {path}")
        print(f"      旧 {brief(old, 70)}")
        print(f"      新 {brief(new, 70)}")
    print("提示: 保存后由 MaiBot 文件监视热重载生效，约 1-20 秒；无需重启容器。")


def main() -> None:
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd", required=True)
    sub.add_parser("snapshot")
    ap_apply = sub.add_parser("apply")
    ap_apply.add_argument("profile", nargs="?")
    ap_apply.add_argument("--knobs")
    ap_apply.add_argument("--sync-talk-rules", action="store_true")
    args = ap.parse_args()

    if args.cmd == "snapshot":
        out = snapshot()
        print(f"当前生效人设已导出: {out}")
        return

    apply(
        pathlib.Path(args.profile) if args.profile else None,
        pathlib.Path(args.knobs) if args.knobs else None,
        args.sync_talk_rules,
    )


if __name__ == "__main__":
    main()
