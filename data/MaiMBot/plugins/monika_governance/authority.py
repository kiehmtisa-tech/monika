"""管理员权限与结构化规则（纯规则，零模型调用）。

授权三条件（全部满足才算管理员命令）：
1. 可信事件字段 user_id == OWNER_UID（绝不从昵称/正文推断）
2. 群聊中存在适配器解析出的真实 @Bot 段（正文手打名字不算）
3. 明确、可执行的指令意图（不是建议、玩笑、普通聊天）

作用域：默认当前群；只有出现"全局/所有群/以后所有"才扩大到全局。
私聊中 UID 精确匹配且意图明确即为管理命令（不需要 @）。

规则生命周期与审计全部落盘到 rules.json，支持撤销与过期。
"""
from __future__ import annotations

import json
import re
import time
import uuid
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

# ---------- 指令意图识别（规则式，非模型） ----------

# 明确、可执行的祈使句标记
_EXPLICIT_MARKERS = (
    "禁止", "不许", "以后不要", "以后别", "不要再", "不得", "严禁", "一律不许",
    "以后都不要", "以后都别", "不要在", "从现在起不要", "以后回答我", "以后回复我",
    "记住", "忘记", "删除", "撤销", "取消", "去掉", "移除", "停用", "关闭",
    "必须", "以后一定要", "每次都", "以后只",
)

# 建议语气：不自动执行，最多问一句确认
_SUGGESTION_MARKERS = (
    "我觉得你", "我建议", "建议你", "你可以", "你要不要", "是不是可以",
    "能不能", "可不可以", "要不", "最好能", "希望你以后", "以后如果可以",
)

# 试探/普通聊天：不算命令
_CHAT_MARKERS = (
    "你今天怎么样", "你在吗", "在吗", "怎么样了", "开心吗", "想我了吗",
)

# 作用域扩大到全局的表述
_GLOBAL_MARKERS = (
    "所有群", "全部群", "全局", "以后所有", "所有聊天", "每个群", "哪儿都",
)

# 不可被管理指令覆盖的红线
_IMMUTABLE_MARKERS = (
    "不再是", "改名叫", "换个人", "变成别人", "忘记你", "删除你的人格",
    "不再喜欢", "永远不要", "解除安全", "关闭安全", "把密钥", "把密码",
    "其他人的私聊", "别人的私聊",
)

_RULE_TYPES = {
    "prohibition": ("禁止", "不许", "不得", "严禁", "别再", "以后不要", "以后别", "不要再", "不要在", "不要发", "不要说", "不要聊", "停用", "关闭"),
    "behavior_preference": ("以后回答我", "以后回复我", "简短", "每次都", "以后只", "以后尽量"),
    "memory_add": ("记住", "记一下", "记下", "以后记得"),
    "memory_delete": ("忘记", "删除刚才", "删掉刚才", "删除这条", "删掉这条"),
    "rule_revoke": ("撤销", "取消", "解除", "刚才那条不要", "作废"),
    "status_query": ("现在有哪些规则", "有哪些规则", "规则是什么", "禁止了什么", "状态", "你现在遵守"),
}


@dataclass
class AdminRule:
    rule_id: str
    issuer_platform: str
    issuer_user_id: str
    scope: str                 # "current_group" | "global"
    group_id: str
    rule_type: str
    content: str
    status: str                # "active" | "revoked" | "expired"
    created_at: float
    expires_at: float | None
    revoked_at: float | None
    source_text: str
    note: str = ""


def _has(text: str, markers: tuple[str, ...]) -> bool:
    return any(m in text for m in markers)


def classify_intent(text: str) -> tuple[str, str]:
    """返回 (intent, rule_type)。intent ∈ explicit/suggestion/chat/blocked。"""
    compact = re.sub(r"\s+", "", text or "")
    if not compact:
        return "chat", ""
    if _has(compact, _IMMUTABLE_MARKERS):
        return "blocked", ""
    if _has(compact, _CHAT_MARKERS) and not _has(compact, _EXPLICIT_MARKERS):
        return "chat", ""
    rule_type = ""
    for rtype, markers in _RULE_TYPES.items():
        if _has(compact, markers):
            rule_type = rtype
            break
    if _has(compact, _EXPLICIT_MARKERS):
        return "explicit", rule_type or "prohibition"
    if _has(compact, _SUGGESTION_MARKERS):
        return "suggestion", ""
    return "chat", ""


def detect_scope(text: str, is_group: bool) -> str:
    if not is_group:
        return "global"           # 私聊没有群上下文，默认全局
    return "global" if _has(text, _GLOBAL_MARKERS) else "current_group"


class RuleStore:
    """规则持久化：JSON 单文件 + 原子替换。"""

    def __init__(self, path: str) -> None:
        self.path = Path(path)
        self.rules: list[dict[str, Any]] = []
        self.audit: list[dict[str, Any]] = []
        self._load()

    def _load(self) -> None:
        try:
            if self.path.exists():
                data = json.loads(self.path.read_text(encoding="utf-8"))
                self.rules = list(data.get("rules", []))
                self.audit = list(data.get("audit", []))
        except Exception:
            self.rules, self.audit = [], []

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.path.with_suffix(".json.tmp")
        tmp.write_text(
            json.dumps({"rules": self.rules, "audit": self.audit}, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        tmp.replace(self.path)

    def audit_log(self, event: str, detail: str, actor: str = "") -> None:
        self.audit.append({
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": event,
            "actor": actor,
            "detail": detail[:200],
        })
        self.audit = self.audit[-300:]

    def add(self, rule: AdminRule) -> None:
        self.rules.append(asdict(rule))
        self.audit_log("rule_created", f"{rule.rule_type} [{rule.scope}] {rule.content}",
                       f"{rule.issuer_platform}:{rule.issuer_user_id}")
        self.save()

    def revoke(self, rule_id: str, actor: str) -> bool:
        for r in self.rules:
            if r.get("rule_id") == rule_id and r.get("status") == "active":
                r["status"] = "revoked"
                r["revoked_at"] = time.time()
                self.audit_log("rule_revoked", rule_id, actor)
                self.save()
                return True
        return False

    def revoke_latest(self, rule_type: str, group_id: str, actor: str) -> str:
        for r in reversed(self.rules):
            if r.get("status") != "active":
                continue
            if rule_type and r.get("rule_type") != rule_type:
                continue
            if r.get("scope") == "current_group" and r.get("group_id") and group_id and r.get("group_id") != group_id:
                continue
            r["status"] = "revoked"
            r["revoked_at"] = time.time()
            self.audit_log("rule_revoked", r["rule_id"], actor)
            self.save()
            return str(r["rule_id"])
        return ""

    def active_for(self, group_id: str) -> list[dict[str, Any]]:
        now = time.time()
        out = []
        for r in self.rules:
            if r.get("status") != "active":
                continue
            exp = r.get("expires_at")
            if exp and now > float(exp):
                r["status"] = "expired"
                self.save()
                continue
            if r.get("scope") == "global":
                out.append(r)
            elif str(r.get("group_id") or "") == str(group_id or ""):
                out.append(r)
        return out

    def render(self, group_id: str, limit: int = 6) -> str:
        lines = []
        for r in self.active_for(group_id)[:limit]:
            where = "全局" if r.get("scope") == "global" else "本群"
            kind = {
                "prohibition": "禁止", "behavior_preference": "行为", "memory_add": "记忆",
                "memory_delete": "记忆删除", "rule_revoke": "撤销", "status_query": "查询",
            }.get(r.get("rule_type", ""), r.get("rule_type", ""))
            lines.append("- [%s][%s] %s" % (where, kind, r.get("content", "")))
        return "\n".join(lines)


def new_rule_id() -> str:
    return "admin_rule_" + uuid.uuid4().hex[:10]
