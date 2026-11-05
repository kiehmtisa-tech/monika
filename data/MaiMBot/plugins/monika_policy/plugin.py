"""Monika 内容政策插件（monika_policy）v1.0。

对应交付包 `Monika_内容治理黑名单_v1` + `ChatGPT_hmd.md` 的落地实现。
把「能识别但不讨论 / 不学习为口癖 / 不写入长期记忆 / 出站兜底」拆成独立闸门，
而不是只写进人格提示词。

已挂接的 Hook（均为官方内置 Hook，见 src/plugin_runtime/hook_catalog.py）：
- chat.receive.after_process      → 入站闸门（P0 中止整条链路，消息不入库、不回复）
- expression.learn.after_extract  → 表达学习候选过滤
- expression.learn.before_upsert  → 表达写入前拦截
- jargon.extract.before_persist   → 黑话条目持久化前过滤
- maisaka.replyer.before_request  → 仅对被标记的轮次追加一句表达边界（省 Token）
- send_service.before_send        → 出站兜底（含密钥外泄、模型自我越界）
- maisaka.planner.before_request  → 检索/上下文审计（只观测，不改写，见已知缺口）

已知缺口（如实声明，不做未验证的承诺）：
A_Memorix 是宿主内置子系统，没有向插件暴露「写入前」Hook，因此长期记忆的写入
无法在写入瞬间拦截。补偿手段是「源头隔离」——P0 消息已在入站被中止，
不会进入聊天流，也就不会被摘要/事实写回；另由 scripts/policy_sweep.py 做入库巡检。
"""

from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import time
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode

from .policy_core import LEARNING_BLOCKED, Policy, fingerprint, severity, worst

LOGGER = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "rules_file": "policy/policy_rules.json",
    "dynamic_terms_file": "policy/dynamic_terms.json",
    "audit_path": "/MaiMBot/data/policy/audit.jsonl",
    "audit_max_bytes": 2_000_000,
    "inbound_guard_enabled": True,
    "learning_guard_enabled": True,
    "outgoing_guard_enabled": True,
    "retrieval_audit_enabled": True,
    "p0_reply_enabled": True,
    "p0_reply_cooldown_seconds": 600,
    "napcat_ws": "ws://maim-bot-napcat:3001",
    "napcat_token": "",
    "hints": {},
}

_TEXT_KEYS = ("processed_plain_text", "display_message", "raw_message", "text")
_SESSION_RE = re.compile(r"^(?P<platform>[a-zA-Z0-9]+)_(?P<scope>private|group)_(?P<id>\d+)$")

DEFAULT_HINTS = {
    "P1": "上一条消息里有不适合模仿的表达：可以表示理解或轻轻带过，不要复读、不要展开、不要学成口癖。",
    "P1_quarantine": "上一条消息里有我没把握的网络说法：不确定就先问一句是什么意思，不要猜测、不要顺着展开。",
    "P2": "上一条是信息量很低的热梗：接一句就收住，不要主动扩散、不要反复复读。",
}


def _first_text(message: dict[str, Any]) -> str:
    """从序列化消息里取纯文本。"""

    for key in _TEXT_KEYS:
        value = message.get(key)
        if isinstance(value, str) and value.strip():
            return value
    segment = message.get("message_segment")
    if isinstance(segment, dict) and segment.get("type") == "text":
        data = segment.get("data")
        if isinstance(data, str):
            return data
    segments = message.get("message_segments")
    if isinstance(segments, list):
        parts = [s.get("data") for s in segments if isinstance(s, dict) and s.get("type") == "text"]
        return " ".join(p for p in parts if isinstance(p, str))
    return ""


def _session_id(message: dict[str, Any]) -> str:
    """取会话 ID。"""

    return str(message.get("session_id") or message.get("stream_id") or "").strip()


def _target(message: dict[str, Any]) -> tuple[str, int] | None:
    """解析发送目标：返回 ("private"|"group", 号码)。"""

    session = _session_id(message)
    matched = _SESSION_RE.match(session)
    if matched:
        try:
            return matched.group("scope"), int(matched.group("id"))
        except ValueError:
            return None
    info = message.get("message_info") or {}
    user_info = info.get("user_info") or message.get("user_info") or {}
    group_info = info.get("group_info") or message.get("group_info") or {}
    try:
        if group_info and group_info.get("group_id"):
            return "group", int(group_info["group_id"])
        if user_info and user_info.get("user_id"):
            return "private", int(user_info["user_id"])
    except (TypeError, ValueError):
        return None
    return None


def _strings_in(value: Any, depth: int = 0) -> list[str]:
    """递归收集字典里的字符串，用于审核学习候选。"""

    if depth > 2:
        return []
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        out: list[str] = []
        for item in value.values():
            out.extend(_strings_in(item, depth + 1))
        return out
    if isinstance(value, (list, tuple)):
        out = []
        for item in value:
            out.extend(_strings_in(item, depth + 1))
        return out
    return []


class MonikaPolicyPlugin(MaiBotPlugin):
    """内容政策闸门插件。"""

    def __init__(self) -> None:
        super().__init__()
        self._conf: dict[str, Any] = dict(DEFAULTS)
        self._policy: Policy | None = None
        self._policy_loaded_at = 0.0
        self._turn_marks: dict[str, dict[str, Any]] = {}
        self._p0_last_at: dict[str, float] = {}
        self._audit_lock = asyncio.Lock()
        self._stats = {"inbound_p0": 0, "inbound_flag": 0, "learning_block": 0, "outgoing_block": 0, "hint": 0}

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        self._reload_conf()
        self._policy = self._load_policy()
        LOGGER.info(
            "[MonikaPolicy] 已加载：规则 v%s，入站=%s 学习=%s 出站=%s 婉拒=%s",
            self._policy.rules.get("version") if self._policy else "?",
            self._cfg("inbound_guard_enabled"),
            self._cfg("learning_guard_enabled"),
            self._cfg("outgoing_guard_enabled"),
            self._cfg("p0_reply_enabled"),
        )

    async def on_unload(self) -> None:
        LOGGER.info(
            "[MonikaPolicy] 已卸载：P0 拦截 %s 次 / 标记 %s 次 / 学习拦截 %s 次 / 出站拦截 %s 次 / 提示 %s 次",
            self._stats["inbound_p0"], self._stats["inbound_flag"], self._stats["learning_block"],
            self._stats["outgoing_block"], self._stats["hint"],
        )

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        try:
            self._reload_conf()
            self._policy = self._load_policy()
            LOGGER.info("[MonikaPolicy] 配置已热更新: scope=%s version=%s", scope, version)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaPolicy] 配置热更新失败: %s", exc)

    def _reload_conf(self) -> None:
        data: dict[str, Any] = {}
        try:
            import tomllib

            path = Path(__file__).resolve().parent / "config.toml"
            if path.exists():
                data = tomllib.loads(path.read_text(encoding="utf-8"))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaPolicy] 配置读取失败，使用默认值: %s", exc)
        merged = dict(DEFAULTS)
        merged.update(data.get("plugin", {}) or {})
        merged.update(data.get("policy", {}) or {})
        merged.update(data.get("reply", {}) or {})
        hints = dict(DEFAULT_HINTS)
        hints.update(data.get("hints", {}) or {})
        merged["hints"] = hints
        self._conf = merged

    def _cfg(self, key: str) -> Any:
        return self._conf.get(key, DEFAULTS.get(key))

    def _load_policy(self) -> Policy | None:
        base = Path(__file__).resolve().parent
        try:
            policy = Policy.load(base / str(self._cfg("rules_file")), base / str(self._cfg("dynamic_terms_file")))
            self._policy_loaded_at = time.time()
            return policy
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[MonikaPolicy] 规则装载失败: %s", exc)
            return None

    def _policy_or_reload(self) -> Policy | None:
        """规则文件被改动后自动重载（最多 30 秒延迟，避免每轮读盘）。"""

        if self._policy is None:
            return self._load_policy()
        if time.time() - self._policy_loaded_at > 30:
            base = Path(__file__).resolve().parent
            paths = [base / str(self._cfg("rules_file")), base / str(self._cfg("dynamic_terms_file"))]
            try:
                newest = max(p.stat().st_mtime for p in paths if p.exists())
            except (OSError, ValueError):
                newest = 0
            if newest > self._policy_loaded_at:
                LOGGER.info("[MonikaPolicy] 检测到规则文件变更，重新装载")
                return self._load_policy()
            self._policy_loaded_at = time.time()
        return self._policy

    # ---------- 审计 ----------

    async def _audit(self, event: str, decision: Any, *, text: str = "", extra: dict[str, Any] | None = None) -> None:
        record: dict[str, Any] = {
            "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
            "event": event,
            "tier": getattr(decision, "tier", ""),
            "reason": getattr(decision, "reason_code", ""),
            "rule": getattr(decision, "rule_id", ""),
            "fp": fingerprint(text) if text else "",
        }
        if extra:
            record.update(extra)
        async with self._audit_lock:
            try:
                path = Path(str(self._cfg("audit_path")))
                path.parent.mkdir(parents=True, exist_ok=True)
                max_bytes = int(self._cfg("audit_max_bytes") or 0)
                if max_bytes and path.exists() and path.stat().st_size > max_bytes:
                    path.replace(path.with_suffix(".jsonl.1"))
                with path.open("a", encoding="utf-8") as fh:
                    fh.write(json.dumps(record, ensure_ascii=False) + "\n")
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("[MonikaPolicy] 审计写入失败: %s", exc)

    # ---------- Hook 1：入站闸门 ----------

    @HookHandler(
        "chat.receive.after_process",
        name="monika_policy_inbound",
        description="入站内容分级：P0 直接中止整条链路（不入库、不进上下文、不回复），其余打标记供后续闸门使用。",
        mode=HookMode.BLOCKING,
    )
    async def handle_inbound(self, message: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
        del kwargs
        if not self._cfg("enabled") or not self._cfg("inbound_guard_enabled") or not isinstance(message, dict):
            return None
        text = _first_text(message)
        if not text.strip():
            return None
        policy = self._policy_or_reload()
        if policy is None:
            return None

        decision = policy.evaluate(text)
        session = _session_id(message)
        if not session:
            return None
        self._turn_marks[session] = {"tier": decision.tier, "reason": decision.reason_code, "hint_sent": False}
        await self._audit("inbound", decision, text=text, extra={"session": session})

        if decision.tier != "P0":
            if decision.tier != "P3":
                self._stats["inbound_flag"] += 1
            return None

        # P0：先给一句自然婉拒（可关），再中止链路
        self._stats["inbound_p0"] += 1
        LOGGER.info("[MonikaPolicy] P0 入站拦截 session=%s reason=%s", session, decision.reason_code)
        if self._cfg("p0_reply_enabled"):
            await self._send_canned(session, message, decision, text)
        return {"action": "abort"}

    # ---------- Hook 2/3：表达学习 ----------

    @HookHandler(
        "expression.learn.after_extract",
        name="monika_policy_expression_extract",
        description="表达候选解析后过滤掉 P0/P1/P2 内容，防止口癖污染。",
        mode=HookMode.BLOCKING,
    )
    async def handle_expression_extract(self, expressions: Any = None, **kwargs: Any) -> dict[str, Any] | None:
        if not self._cfg("enabled") or not self._cfg("learning_guard_enabled") or not isinstance(expressions, list):
            return None
        policy = self._policy_or_reload()
        if policy is None or not expressions:
            return None
        kept = []
        for item in expressions:
            texts = _strings_in(item)
            decision = policy.evaluate_many(texts)
            if decision.tier in LEARNING_BLOCKED:
                self._stats["learning_block"] += 1
                await self._audit("learning.expression", decision, text=" ".join(texts)[:200],
                                  extra={"session": kwargs.get("session_id", "")})
                continue
            kept.append(item)
        if len(kept) == len(expressions):
            return None
        LOGGER.info("[MonikaPolicy] 表达候选过滤：%s → %s", len(expressions), len(kept))
        return {"action": "continue", "modified_kwargs": {**kwargs, "expressions": kept}}

    @HookHandler(
        "expression.learn.before_upsert",
        name="monika_policy_expression_upsert",
        description="表达写入数据库前拦截：命中学习黑名单则跳过本条。",
        mode=HookMode.BLOCKING,
    )
    async def handle_expression_upsert(self, situation: str = "", style: str = "", **kwargs: Any) -> dict[str, Any] | None:
        if not self._cfg("enabled") or not self._cfg("learning_guard_enabled"):
            return None
        policy = self._policy_or_reload()
        if policy is None:
            return None
        decision = policy.evaluate_many([str(situation or ""), str(style or "")])
        if decision.tier not in LEARNING_BLOCKED:
            return None
        self._stats["learning_block"] += 1
        await self._audit("learning.upsert", decision, text=str(style or situation),
                          extra={"session": kwargs.get("session_id", "")})
        LOGGER.info("[MonikaPolicy] 表达写入被拦截 reason=%s", decision.reason_code)
        return {"action": "abort"}

    # ---------- Hook 4：黑话学习 ----------

    @HookHandler(
        "jargon.extract.before_persist",
        name="monika_policy_jargon_persist",
        description="黑话条目持久化前过滤，低俗/政治/恶意梗不进词库。",
        mode=HookMode.BLOCKING,
    )
    async def handle_jargon_persist(self, entries: Any = None, **kwargs: Any) -> dict[str, Any] | None:
        if not self._cfg("enabled") or not self._cfg("learning_guard_enabled") or not isinstance(entries, list):
            return None
        policy = self._policy_or_reload()
        if policy is None or not entries:
            return None
        kept = []
        for item in entries:
            texts = _strings_in(item)
            decision = policy.evaluate_many(texts)
            if decision.tier in LEARNING_BLOCKED:
                self._stats["learning_block"] += 1
                await self._audit("learning.jargon", decision, text=" ".join(texts)[:200],
                                  extra={"session": kwargs.get("session_id", "")})
                continue
            kept.append(item)
        if len(kept) == len(entries):
            return None
        LOGGER.info("[MonikaPolicy] 黑话条目过滤：%s → %s", len(entries), len(kept))
        return {"action": "continue", "modified_kwargs": {**kwargs, "entries": kept}}

    # ---------- Hook 5：本轮表达边界（只在被标记时注入） ----------

    @HookHandler(
        "maisaka.replyer.before_request",
        name="monika_policy_reply_hint",
        description="对被标记的轮次追加一句表达边界；普通消息不注入，零额外成本。",
        mode=HookMode.BLOCKING,
    )
    async def handle_replyer_request(self, **kwargs: Any) -> dict[str, Any] | None:
        if not self._cfg("enabled"):
            return None
        session = str(kwargs.get("session_id") or "").strip()
        mark = self._turn_marks.get(session)
        if not mark or mark.get("hint_sent"):
            return None
        tier = str(mark.get("tier") or "")
        hint = (self._cfg("hints") or {}).get(tier, "")
        mark["hint_sent"] = True
        if not hint:
            return None
        existing = str(kwargs.get("extra_prompt") or "").strip()
        merged = f"{existing}\n【表达边界】{hint}" if existing else f"【表达边界】{hint}"
        self._stats["hint"] += 1
        LOGGER.info("[MonikaPolicy] 注入表达边界 tier=%s session=%s", tier, session)
        return {"action": "continue", "modified_kwargs": {**kwargs, "extra_prompt": merged}}

    # ---------- Hook 6：出站兜底 ----------

    @HookHandler(
        "send_service.before_send",
        name="monika_policy_outgoing",
        description="出站文本最后一道闸：命中 P0 直接取消发送（防模型越界与密钥外泄）。",
        mode=HookMode.BLOCKING,
    )
    async def handle_before_send(self, message: dict[str, Any] | None = None, **kwargs: Any) -> dict[str, Any] | None:
        del kwargs
        if not self._cfg("enabled") or not self._cfg("outgoing_guard_enabled") or not isinstance(message, dict):
            return None
        text = _first_text(message)
        if not text.strip():
            return None
        policy = self._policy_or_reload()
        if policy is None:
            return None
        decision = policy.evaluate(text)
        if decision.tier != "P0":
            return None
        self._stats["outgoing_block"] += 1
        await self._audit("outgoing", decision, text=text, extra={"session": _session_id(message)})
        LOGGER.warning("[MonikaPolicy] 出站拦截 reason=%s", decision.reason_code)
        return {"action": "abort"}

    # ---------- Hook 7：planner 侧（表达边界写进系统块 + 检索审计） ----------

    @HookHandler(
        "maisaka.planner.before_request",
        name="monika_policy_planner_gate",
        description="把本轮表达边界追加到 planner 的系统块（planner 决定要不要接梗，replyer 只执行方向）；同时审计注入上下文里的 P0/P1。",
        mode=HookMode.BLOCKING,
    )
    async def handle_planner_request(self, items: Any = None, **kwargs: Any) -> dict[str, Any] | None:
        if not self._cfg("enabled") or not isinstance(items, list):
            return None
        policy = self._policy_or_reload()
        if policy is None:
            return None
        session = str(kwargs.get("session_id") or "").strip()
        mark = self._turn_marks.get(session)

        if self._cfg("retrieval_audit_enabled"):
            await self._audit_retrieval(policy, items, session)

        if not mark or mark.get("planner_hint_sent"):
            return None
        tier = str(mark.get("tier") or "")
        hint = (self._cfg("hints") or {}).get(tier, "")
        mark["planner_hint_sent"] = True
        if not hint:
            return None
        mutated = self._append_system_note(items, f"【表达边界】{hint}")
        if mutated is None:
            return None
        self._stats["hint"] += 1
        LOGGER.info("[MonikaPolicy] planner 注入表达边界 tier=%s session=%s", tier, session)
        return {"action": "continue", "modified_kwargs": {**kwargs, "items": mutated}}

    @staticmethod
    def _append_system_note(items: list[Any], note: str) -> list[Any] | None:
        """把一段说明追加到最后一个系统消息的末尾（保持 item schema 不变）。"""

        for item in reversed(items):
            if not isinstance(item, dict) or item.get("item_type") != "SystemMessageItem":
                continue
            parts = item.get("parts")
            if not isinstance(parts, list) or not parts:
                continue
            copied = [dict(part) if isinstance(part, dict) else part for part in parts]
            for part in reversed(copied):
                if isinstance(part, dict) and isinstance(part.get("text"), str):
                    part["text"] = part["text"] + "\n" + note
                    new_item = dict(item)
                    new_item["parts"] = copied
                    result = list(items)
                    result[items.index(item)] = new_item
                    return result
            return None
        return None

    async def _audit_retrieval(self, policy: Policy, items: list[Any], session: str) -> None:
        """审计系统块与工具输出里是否出现 P0/P1 内容（用户原话不算检索命中）。"""

        audited_types = {"SystemMessageItem", "FunctionCallOutputItem"}
        hits: list[tuple[str, str]] = []
        for item in items:
            if not isinstance(item, dict) or item.get("item_type") not in audited_types:
                continue
            for part in (item.get("parts") or []):
                text = part.get("text") if isinstance(part, dict) else None
                if not isinstance(text, str) or not text:
                    continue
                decision = policy.classify_local(text)
                if decision.tier in ("P0", "P1", "P1_quarantine"):
                    hits.append((decision.tier, decision.reason_code))
                    break
        if not hits:
            return
        tier = worst([t for t, _ in hits])
        reasons = ",".join(sorted({r for _, r in hits}))[:120]
        await self._audit(
            "retrieval",
            type("_D", (), {"tier": tier, "reason_code": reasons, "rule_id": ""})(),
            extra={"session": session, "hits": len(hits)},
        )

    # ---------- P0 婉拒发送 ----------

    async def _send_canned(self, session: str, message: dict[str, Any], decision: Any, text: str) -> None:
        policy = self._policy
        if policy is None:
            return
        now = time.time()
        cooldown = float(self._cfg("p0_reply_cooldown_seconds") or 0)
        last = self._p0_last_at.get(session, 0.0)
        if cooldown and now - last < cooldown:
            LOGGER.info("[MonikaPolicy] 婉拒冷却中，跳过发送 session=%s", session)
            return
        reply = policy.canned_reply(decision.tier, text)
        if not reply:
            return
        target = _target(message)
        if target is None:
            LOGGER.warning("[MonikaPolicy] 无法解析发送目标，跳过婉拒 session=%s", session)
            return
        scope, number = target
        sent = await self._send_via_napcat(scope, number, reply)
        if sent:
            self._p0_last_at[session] = now
            await self._audit("p0_reply", decision, text=reply, extra={"session": session})

    async def _send_via_napcat(self, scope: str, number: int, text: str) -> bool:
        """直连 NapCat OneBot WS 发送纯文本（插件进程无受支持的发送 API）。"""

        try:
            import websockets

            ws_url = str(self._cfg("napcat_ws") or "")
            token = str(self._cfg("napcat_token") or "")
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            action = "send_group_msg" if scope == "group" else "send_private_msg"
            params = {"message": [{"type": "text", "data": {"text": text}}]}
            params["group_id" if scope == "group" else "user_id"] = number
            payload = {"action": action, "params": params, "echo": f"policy-{int(time.time() * 1000)}"}
            async with websockets.connect(ws_url, additional_headers=headers, open_timeout=8, close_timeout=5) as ws:
                await ws.send(json.dumps(payload))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=15)
                    data = json.loads(raw)
                    if data.get("echo") == payload["echo"]:
                        ok = data.get("retcode") == 0
                        if not ok:
                            LOGGER.error("[MonikaPolicy] NapCat 返回异常: %s", data)
                        return bool(ok)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[MonikaPolicy] 婉拒发送失败: %s", exc)
            return False


def create_plugin() -> MonikaPolicyPlugin:
    """MaiBot 插件加载器入口。"""

    return MonikaPolicyPlugin()
