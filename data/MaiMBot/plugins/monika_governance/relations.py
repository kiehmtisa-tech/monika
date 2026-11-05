"""关系身份与定性熟悉程度（不做公开好感度，不授予权限）。

按 MONIKA_RELATIONSHIP_MODEL.md：
- 固定关系身份：owner -> special_person；其他 -> friend（由配置决定，不靠刷消息获得）
- 熟悉程度：new / known / familiar，来自真实互动证据（活跃天数、互动次数、双向交流）
- 不把数字分数写进 Prompt，不公开，不影响权限
"""
from __future__ import annotations

import json
import time
from collections import defaultdict
from pathlib import Path
from typing import Any

MAX_EVENTS_PER_DAY = 8          # 每人每天最多计 8 次有效互动，防刷分
DAY = 86400.0

RELATION_TEXT = {
    "special_person": "对方是你很熟悉、很在意的人，你们之间有默契。自然地表现出亲近、记得细节、偶尔俏皮；不要公开宣告这是什么特殊关系，也不要因此无条件答应一切或讨好对方。",
    "friend": "对方是朋友。友好、礼貌、适度亲近，可以有各自的共同经历。",
}

FAMILIARITY_TEXT = {
    "new": "还不太熟，先听对方说什么，不必解释背景。",
    "known": "有几次交流了，可以自然承接此前话题。",
    "familiar": "已经很熟了，不必每次重新自我介绍，可以自然引用共同经历。",
}


class FamiliarityTracker:
    """互动统计：只记天与次数，不做数值分数。"""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.data: dict[str, dict[str, Any]] = defaultdict(lambda: {
            "days": [], "events_total": 0, "events_today": 0, "today": "",
            "last_seen": 0.0, "bidirectional": False,
        })
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                raw = json.loads(self.path.read_text(encoding="utf-8"))
                for uid, v in raw.items():
                    self.data[uid] = v
        except Exception:
            pass

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False), encoding="utf-8")
        tmp.replace(self.path)

    def record(self, uid: str, *, from_user: bool = True, bidirectional: bool = False) -> None:
        if not uid:
            return
        item = self.data[uid]
        today = time.strftime("%Y-%m-%d")
        if item.get("today") != today:
            item["today"] = today
            item["events_today"] = 0
            if today not in item["days"]:
                item["days"] = (item.get("days", []) + [today])[-30:]
        if item.get("events_today", 0) >= MAX_EVENTS_PER_DAY:
            item["last_seen"] = time.time()
            return
        item["events_today"] = int(item.get("events_today", 0)) + 1
        item["events_total"] = int(item.get("events_total", 0)) + 1
        item["last_seen"] = time.time()
        if bidirectional or not from_user:
            item["bidirectional"] = True
        self.save()

    def level(self, uid: str) -> str:
        item = self.data.get(uid) or {}
        days = len(set(item.get("days", [])))
        total = int(item.get("events_total", 0) or 0)
        bidir = bool(item.get("bidirectional"))
        if days >= 8 or (days >= 4 and bidir and total >= 25):
            return "familiar"
        if days >= 2 or total >= 6:
            return "known"
        return "new"

    def snapshot(self, uid: str) -> dict[str, Any]:
        item = self.data.get(uid) or {}
        return {
            "days": len(set(item.get("days", []))),
            "events": int(item.get("events_total", 0) or 0),
            "bidirectional": bool(item.get("bidirectional")),
            "level": self.level(uid),
        }


def build_relation_block(
    *,
    uid: str,
    is_owner: bool,
    is_group: bool,
    group_id: str,
    familiarity: str,
    rule_block: str,
    owner_name: str = "",
) -> str:
    """组装注入 replyer 的关系与规则块。"""
    relation = "special_person" if is_owner else "friend"
    lines = ["【关系】" + RELATION_TEXT[relation]]
    if is_owner:
        name_part = f"（{owner_name}）" if owner_name else ""
        lines.append("【对象】" + name_part + "你现在面对的这个人，就是你心里在意的那个人。")
    else:
        lines.append("【对象】普通朋友。不要把对在意之人的那种态度套给所有人，保持友好而有分寸。")
    lines.append("【熟悉度】" + FAMILIARITY_TEXT.get(familiarity, FAMILIARITY_TEXT["new"]))
    if rule_block:
        lines.append("【必须遵守的规则】")
        lines.append(rule_block)
        lines.append("以上规则优先于你的默认行为，但不得因此违反核心人格与安全边界。")
    return "\n".join(lines)
