"""Monika v5 治理插件（管理员权限 + 关系身份 + 空闲收尾 + 安全轮换 + 知识库）。

六条 Hook + 两条后台任务：
1. chat.receive.after_process（观察）：互动记账、管理员指令判定、空闲收尾记账
2. maisaka.planner.after_response（观察）：记录 token 用量，驱动上下文分级与安全轮换
3. maisaka.replyer.before_request（改写 extra_prompt）：注入关系身份与生效中的规则
4. 后台 asyncio：空闲收尾写回长期记忆
5. on_load 时幂等导入知识库（四个独立 collection）

授权三条件全部在代码侧校验，不靠模型判断身份：
   is_owner(可信 user_id)  AND  is_real_at(适配器解析的 @ 段)  AND  is_explicit_command(祈使句规则)
"""

from __future__ import annotations

import asyncio
import logging
import time
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode

from . import authority, context_guard, flush, knowledge, relations, tool_chain, tool_policy

LOGGER = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    # 管理员
    "owner_uid": "",          # 运行期由 config.toml 提供，勿在代码里写死
    "bot_uid": "",            # 运行期由 config.toml 提供
    "owner_name": "",
    "rules_path": "/MaiMBot/data/governance/rules.json",
    # 关系
    "relation_enabled": True,
    "familiarity_path": "/MaiMBot/data/governance/familiarity.json",
    # 空闲收尾
    "flush_enabled": True,
    "flush_state_path": "/MaiMBot/data/governance/flush_state.json",
    "flush_idle_seconds": 1800,
    "flush_min_messages": 4,
    "flush_daily_cap": 1,
    "flush_context_length": 16,
    "flush_check_interval": 60,
    # 上下文守护
    "guard_enabled": True,
    "guard_soft_tokens": 8000,
    "guard_hard_tokens": 12000,
    "guard_auto_rotate": False,
    "guard_audit_path": "/MaiMBot/data/governance/context_guard.jsonl",
    "guard_command_path": "/MaiMBot/data/governance/commands.jsonl",
    "guard_command_interval": 5,
    # 知识库
    "kb_enabled": True,
    "kb_root": "/MaiMBot/data/a-knowledge",
    "kb_import_on_load": True,
    # 上下文优化：工具轮次归档 + 工具 Schema 场景精简
    "tool_archive_enabled": True,
    "tool_archive_keep_recent": 2,
    "tool_archive_max_kept_turn_chars": 3000,
    "tool_schema_filter_enabled": True,
    # 注入上限
    "inject_max_chars": 420,
}

# 管理员即时指令（动作，不是持久规则）
_IMMEDIATE = {
    "context_reset": ("清理上下文", "重置对话", "重置上下文", "忘掉刚才", "开个新话题", "/clear", "清空上下文"),
    "context_status": ("上下文状态", "token 状态", "上下文健康", "token用量", "token 用量"),
    "guard_auto_on": ("开启自动轮换", "打开自动轮换", "启用自动轮换"),
    "guard_auto_off": ("关闭自动轮换", "关掉自动轮换", "停用自动轮换"),
    "kb_import": ("导入知识库", "刷新知识库", "重新导入知识"),
    "flush_now": ("立即收尾", "收尾写回", "写回记忆"),
}

_TEXT_KEYS = ("processed_plain_text", "display_message", "raw_message", "text")


def _output_has_tool_calls(output_items: Any) -> bool:
    """判断 planner 输出是否含工具调用（含则工具链未收敛，轮换须延迟）。"""
    if not output_items:
        return False
    items = output_items if isinstance(output_items, list) else [output_items]
    for it in items:
        if isinstance(it, dict):
            t = str(it.get("item_type") or it.get("type") or "")
        else:
            t = type(it).__name__
        # FunctionCallItem 会带工具调用；FunctionCallOutputItem 是结果，不算未完成
        if "FunctionCall" in t and "Output" not in t:
            return True
    return False


def _first_text(message: dict[str, Any]) -> str:
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


def _user_id(message: dict[str, Any]) -> str:
    info = message.get("message_info") or {}
    user = info.get("user_info") or {}
    uid = user.get("user_id") or message.get("user_id") or ""
    return str(uid).strip()


def _group_id(message: dict[str, Any]) -> str:
    info = message.get("message_info") or {}
    group = info.get("group_info") or {}
    gid = group.get("group_id") or message.get("group_id") or ""
    return str(gid).strip()


def _stream_id(message: dict[str, Any]) -> str:
    return str(message.get("session_id") or message.get("stream_id") or "").strip()


def _has_real_mention(message: dict[str, Any], bot_uid: str) -> bool:
    """只认适配器解析出的真实 @ 段；正文手打名字不算。"""
    for flag in ("is_mentioned_bot", "is_at_me", "mentioned_bot", "is_at"):
        if message.get(flag) is True:
            return True
    components: list[Any] = []
    raw = message.get("raw_message")
    if isinstance(raw, dict):
        items = raw.get("components")
        if isinstance(items, list):
            components.extend(items)
    for seg in message.get("message_segments") or []:
        components.append(seg)

    for item in components:
        if not isinstance(item, dict):
            continue
        kind = str(item.get("type") or item.get("component_type") or "").lower()
        if kind not in ("at", "atcomponent", "mention"):
            continue
        target = str(
            item.get("data") or item.get("target") or item.get("user_id")
            or item.get("qq") or item.get("content") or ""
        ).strip()
        if target and bot_uid and target == bot_uid:
            return True
        if not target and kind == "at":
            return True
    return False


def _match_immediate(text: str) -> str:
    compact = text.strip()
    for action, markers in _IMMEDIATE.items():
        for m in markers:
            if m in compact:
                return action
    return ""


class MonikaGovernancePlugin(MaiBotPlugin):
    """治理层：权限、关系、空闲收尾、安全轮换、知识库。"""

    def __init__(self) -> None:
        super().__init__()
        self._conf: dict[str, Any] = {}
        self._store: authority.RuleStore | None = None
        self._fam: relations.FamiliarityTracker | None = None
        self._flusher: flush.IdleFlush | None = None
        self._guard: context_guard.ContextGuard | None = None
        self._cmd_runner: context_guard.CommandRunner | None = None
        self._archiver: tool_chain.ToolChainArchiver | None = None
        self._schema_filter: tool_policy.ToolSchemaFilter | None = None
        self._task: asyncio.Task | None = None
        self._kb_task: asyncio.Task | None = None
        self._cmd_task: asyncio.Task | None = None
        self._stop = asyncio.Event()
        self._admin_notice = ""
        self._sess_cache: dict[str, dict[str, Any]] = {}
        self._sess_cache_max = 200

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        self._reload_conf()
        if self._cfg("flush_enabled"):
            self._stop.clear()
            self._task = asyncio.create_task(
                flush.run_loop(self._flusher, interval=float(self._cfg("flush_check_interval")),
                               stop_event=self._stop),
                name="monika_governance_idle_flush",
            )
        if self._cfg("kb_enabled") and self._cfg("kb_import_on_load"):
            self._kb_task = asyncio.create_task(self._import_knowledge_once(),
                                                name="monika_governance_kb_import")
        if self._cfg("guard_enabled") and self._cmd_runner is not None:
            self._cmd_task = asyncio.create_task(self._command_loop(),
                                                 name="monika_governance_cmd_runner")
        LOGGER.info("[MonikaGovernance] 已加载：管理员三重校验 + 关系身份 + 空闲收尾 + 安全轮换 + 知识库")

    def _dump_opt_stats(self) -> None:
        """把工具优化与守护统计落盘，供 Dragon Board 读取（跨进程）。"""
        try:
            import json as _json
            from pathlib import Path as _Path

            payload = {
                "updated_at": time.time(),
                "tool_chain": self._archiver.stats() if self._archiver else {},
                "tool_schema": self._schema_filter.stats() if self._schema_filter else {},
                "guard": {
                    "auto_rotate": bool(self._guard.auto_rotate) if self._guard else False,
                    "rotate_count": int(self._guard.rotate_count) if self._guard else 0,
                    "deferred_count": int(self._guard.deferred_count) if self._guard else 0,
                    "pending_rotations": len(self._guard.pending_rotations) if self._guard else 0,
                    "soft_tokens": int(self._guard.soft_tokens) if self._guard else 0,
                    "hard_tokens": int(self._guard.hard_tokens) if self._guard else 0,
                },
            }
            path = _Path(str(self._cfg("guard_audit_path"))).parent / "tool_opt_stats.json"
            tmp = path.with_suffix(".json.tmp")
            tmp.write_text(_json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            tmp.replace(path)
        except Exception as exc:  # noqa: BLE001
            LOGGER.debug("[MonikaGovernance] 统计落盘失败: %s", exc)

    async def _command_loop(self) -> None:
        """轮询 Dragon Dashboard 写入的管理员操作指令，并补执行延迟的轮换。"""
        interval = float(self._cfg("guard_command_interval") or 5)
        while True:
            try:
                if self._cmd_runner is not None:
                    n = await self._cmd_runner.poll_once()
                    if n:
                        LOGGER.info("[MonikaGovernance] 执行了 %s 条 Dashboard 指令", n)
                if self._guard is not None:
                    drained = await self._guard.drain_pending()
                    if drained:
                        LOGGER.info("[MonikaGovernance] 补执行了 %s 条延迟轮换", drained)
                self._dump_opt_stats()
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("[MonikaGovernance] 指令轮询失败: %s", exc)
            await asyncio.sleep(interval)

    async def on_unload(self) -> None:
        self._stop.set()
        for task in (self._task, self._kb_task, self._cmd_task):
            if task is not None:
                task.cancel()
                try:
                    await task
                except (asyncio.CancelledError, Exception):  # noqa: BLE001
                    pass
        LOGGER.info("[MonikaGovernance] 已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        try:
            self._reload_conf()
            LOGGER.info("[MonikaGovernance] 配置热更新: scope=%s version=%s", scope, version)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaGovernance] 配置热更新失败: %s", exc)

    def _reload_conf(self) -> None:
        try:
            import tomllib
            path = Path(__file__).resolve().parent / "config.toml"
            data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaGovernance] 配置读取失败，使用默认值: %s", exc)
            data = {}
        merged = dict(DEFAULTS)
        for section in ("plugin", "admin", "relation", "flush", "guard", "knowledge", "inject"):
            merged.update(data.get(section, {}) or {})
        self._conf = merged
        self._store = authority.RuleStore(str(self._cfg("rules_path")))
        self._fam = relations.FamiliarityTracker(str(self._cfg("familiarity_path")))
        self._flusher = flush.IdleFlush(
            state_path=str(self._cfg("flush_state_path")),
            idle_seconds=float(self._cfg("flush_idle_seconds")),
            min_messages=int(self._cfg("flush_min_messages")),
            daily_cap=int(self._cfg("flush_daily_cap")),
            context_length=int(self._cfg("flush_context_length")),
        )
        self._guard = context_guard.ContextGuard(
            audit_path=str(self._cfg("guard_audit_path")),
            soft_tokens=int(self._cfg("guard_soft_tokens")),
            hard_tokens=int(self._cfg("guard_hard_tokens")),
            auto_rotate=bool(self._cfg("guard_auto_rotate")),
        )
        self._cmd_runner = context_guard.CommandRunner(
            str(self._cfg("guard_command_path")), self._guard,
        )
        self._archiver = tool_chain.ToolChainArchiver(
            keep_recent=int(self._cfg("tool_archive_keep_recent") or 2),
            max_kept_turn_chars=int(self._cfg("tool_archive_max_kept_turn_chars") or 3000),
        )
        self._schema_filter = tool_policy.ToolSchemaFilter()

    def _cfg(self, key: str) -> Any:
        return self._conf.get(key, DEFAULTS.get(key))

    def _prune_ctx(self) -> None:
        if len(self._sess_cache) <= self._sess_cache_max:
            return
        ordered = sorted(self._sess_cache.items(), key=lambda kv: float(kv[1].get("ts", 0.0)))
        for key, _ in ordered[: len(self._sess_cache) - self._sess_cache_max]:
            self._sess_cache.pop(key, None)

    async def _import_knowledge_once(self) -> None:
        await asyncio.sleep(5)
        try:
            summary = await knowledge.import_all(str(self._cfg("kb_root")))
            LOGGER.info("[MonikaGovernance] 知识库导入：total=%s ok=%s failed=%s by=%s",
                        summary.get("total"), summary.get("ok"), summary.get("failed"),
                        summary.get("by_collection"))
            if summary.get("errors"):
                for err in summary["errors"][:5]:
                    LOGGER.warning("[MonikaGovernance] 知识导入: %s", err)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaGovernance] 知识库导入失败: %s", exc)

    # ---------- Hook 1：入站消息 ----------

    @HookHandler(
        "chat.receive.after_process",
        name="monika_governance_inbound",
        description="记账互动；按可信UID+真实@+明确意图判定管理员命令并落盘为结构化规则。",
        mode=HookMode.OBSERVE,
    )
    async def handle_inbound(self, message: dict[str, Any], **kwargs: Any) -> None:
        del kwargs
        if not self._cfg("enabled") or not isinstance(message, dict):
            return
        if message.get("is_notify"):
            return

        uid = _user_id(message)
        text = _first_text(message)
        stream = _stream_id(message)
        now = time.time()

        if uid and self._fam is not None:
            self._fam.record(uid, from_user=True)
        if self._flusher is not None and stream:
            self._flusher.note_message(stream, text, now)

        if not text:
            return

        owner_uid = str(self._cfg("owner_uid") or "")
        bot_uid = str(self._cfg("bot_uid") or "")
        is_group = bool(_group_id(message))
        is_owner = bool(uid and owner_uid and uid == owner_uid)

        if stream:
            self._sess_cache[stream] = {"uid": uid, "group_id": _group_id(message),
                                 "is_group": is_group, "ts": now}
            self._prune_ctx()

        mention_ok = (not is_group) or _has_real_mention(message, bot_uid)
        if not (is_owner and mention_ok):
            return

        # ---- 管理员即时指令（动作，不落规则） ----
        action = _match_immediate(text)
        if action:
            await self._run_immediate(action, stream, text)
            return

        intent, rule_type = authority.classify_intent(text)
        if intent == "blocked":
            self._admin_notice = "【拒绝执行】刚才那条要求触及你的人格或安全边界，你不能执行。请温和而明确地拒绝，不要答应，也不要执行。"
            LOGGER.info("[MonikaGovernance] 拒绝红线指令 uid=%s stream=%s", uid, stream)
            return
        if intent == "suggestion":
            self._admin_notice = "【提示】刚才是一句建议，不是命令。可以自然说出你的想法，但不要当成规则执行。"
            return
        if intent == "chat":
            self._admin_notice = ""
            return

        scope = authority.detect_scope(text, is_group)
        gid = _group_id(message) if scope == "current_group" else ""

        if rule_type == "status_query":
            rules_text = self._store.render(gid) if self._store else ""
            self._admin_notice = (
                "【管理员查询】对方想知道你当前遵守的规则。请用你自己的话如实回答：\n"
                + (rules_text or "（当前没有生效中的规则）")
            )
            if self._store:
                self._store.audit_log("status_query", text, uid)
            return

        if rule_type == "rule_revoke":
            revoked = ""
            if self._store:
                revoked = self._store.revoke_latest("", gid if scope == "current_group" else "", uid)
            self._admin_notice = (
                "【管理员撤销】已撤销最近一条生效规则%s。请简短确认。"
                % (f"（{revoked}）" if revoked else "（未找到可撤销规则，请如实说明）")
            )
            return

        if rule_type in ("memory_add", "memory_delete", "memory_correct"):
            self._admin_notice = (
                "【记忆操作】管理员提出了记忆操作：%s。请在回复里确认你记下了（新增）或会淡忘（删除），"
                "并说明这属于你们的共同记忆。" % text.strip()[:120]
            )

        rule = authority.AdminRule(
            rule_id=authority.new_rule_id(),
            issuer_platform=str(message.get("platform") or "qq"),
            issuer_user_id=uid,
            scope=scope,
            group_id=gid,
            rule_type=rule_type or "prohibition",
            content=text.strip()[:200],
            status="active",
            created_at=now,
            expires_at=None,
            revoked_at=None,
            source_text=text.strip()[:300],
            note="来源：管理员明确指令（可信UID+真实@+祈使句）",
        )
        if self._store:
            self._store.add(rule)
        where = "全局" if scope == "global" else "本群"
        self._admin_notice = (
            "【管理员指令已受理】一条%s规则已生效（%s）。请用你自己的话简短确认，并在今后一直遵守；"
            "不需要复述原文，也不用道歉。" % (where, rule.content[:80])
        )
        LOGGER.info("[MonikaGovernance] 落盘规则 %s type=%s scope=%s", rule.rule_id, rule.rule_type, scope)

    async def _run_immediate(self, action: str, stream: str, text: str) -> None:
        """管理员即时动作：上下文轮换、知识导入、守护开关、状态查询。"""
        if action == "context_reset":
            if not stream:
                self._admin_notice = "【上下文清理】未能识别会话，未执行。"
                return
            result = await self._guard.rotate(stream, reason="admin_manual") if self._guard else {}
            ok = bool(result.get("runtime_cleared"))
            self._admin_notice = (
                "【上下文已整理】刚才的短期上下文我放下了，接下来的对话从新开始。"
                "我们的共同记忆和你的事情我都还在。" if ok
                else "【上下文清理】执行失败：%s" % result.get("error", "未知原因")
            )
            LOGGER.info("[MonikaGovernance] 手动轮换 stream=%s result=%s", stream, result)

        elif action == "context_status":
            stats = self._guard.stats(stream) if self._guard else {}
            self._admin_notice = (
                "【上下文状态】本会话请求 %s 次，最近 total=%s；超软阈值 %s 次，超硬阈值 %s 次。"
                "自动轮换：%s（软 %s / 硬 %s tokens）。"
                % (stats.get("count", 0), stats.get("latest_total", "-"),
                   stats.get("over_soft", 0), stats.get("over_hard", 0),
                   "开" if self._guard and self._guard.auto_rotate else "关",
                   self._cfg("guard_soft_tokens"), self._cfg("guard_hard_tokens"))
            )

        elif action == "guard_auto_on":
            if self._guard:
                self._guard.auto_rotate = True
            self._admin_notice = "【已开启自动轮换】上下文超过硬阈值时会自动整理短期记忆。"

        elif action == "guard_auto_off":
            if self._guard:
                self._guard.auto_rotate = False
            self._admin_notice = "【已关闭自动轮换】现在只在你明确要求时整理上下文。"

        elif action == "flush_now":
            if not stream or self._flusher is None:
                self._admin_notice = "【收尾写回】未能识别会话，未执行。"
                return
            ok = await flush.flush_one(stream, self._flusher)
            self._admin_notice = (
                "【收尾写回完成】刚才聊到的要点我记下了。" if ok
                else "【收尾写回】本轮没有可写回的新内容。"
            )
            LOGGER.info("[MonikaGovernance] 手动收尾 stream=%s ok=%s", stream, ok)

        elif action == "kb_import":
            self._admin_notice = "【知识库导入】正在重新导入四个知识集合，稍后我可以告诉你结果。"
            asyncio.create_task(self._import_knowledge_once())

    # ---------- Hook 2：planner 请求改写（忙碌标记 + 工具轮次归档 + Schema 精简） ----------

    @HookHandler(
        "maisaka.planner.before_request",
        name="monika_governance_planner_request",
        description="标记工具轮次忙碌期；归档较早的已完成工具轮次；按场景精简工具定义。",
        mode=HookMode.BLOCKING,
    )
    async def handle_planner_request(self, **kwargs: Any) -> dict[str, Any] | None:
        session_id = str(kwargs.get("session_id") or "").strip()
        raw_items = kwargs.get("items")
        items = list(raw_items) if isinstance(raw_items, list) else []
        out: dict[str, Any] = {}

        if self._cfg("guard_enabled") and self._guard is not None and session_id:
            self._guard.mark_busy(session_id)

        # 判定管理会话（供剥离与 Schema 裁剪共用）
        ctx = self._sess_cache.get(session_id) or {}
        owner_uid = str(self._cfg("owner_uid") or "")
        is_owner = bool(ctx.get("uid") and owner_uid and str(ctx.get("uid")) == owner_uid)

        # 执行侧防线：非管理会话剥离历史中的管理工具调用（防重放/注入）
        if items and not is_owner and self._cfg("tool_schema_filter_enabled"):
            try:
                stripped, removed = tool_policy.strip_admin_tool_calls(items, is_owner_stream=is_owner)
                if removed:
                    items = stripped
                    out["items"] = items
                    LOGGER.info("[MonikaGovernance] 非管理会话剥离管理工具调用 %d 条（防重放）", removed)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("[MonikaGovernance] 管理工具剥离失败（已忽略）: %s", exc)

        # 工具轮次归档（早于 keep_recent 个的完整轮次压缩为结论）
        if self._cfg("tool_archive_enabled") and self._archiver is not None and items:
            try:
                new_items, stats = self._archiver.process(session_id, items)
                if stats.get("archived_turns"):
                    items = new_items
                    out["items"] = new_items
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("[MonikaGovernance] 工具轮次归档失败（已忽略）: %s", exc)

        # 工具定义场景精简（不动注册表，只影响本轮可见集合）
        if self._cfg("tool_schema_filter_enabled") and self._schema_filter is not None:
            defs = kwargs.get("tool_definitions")
            if isinstance(defs, list) and defs:
                try:
                    new_defs, dstats = self._schema_filter.process(
                        session_id, defs, items, is_owner_stream=is_owner)
                    if dstats.get("dropped"):
                        out["tool_definitions"] = new_defs
                except Exception as exc:  # noqa: BLE001
                    LOGGER.warning("[MonikaGovernance] 工具 Schema 精简失败（已忽略）: %s", exc)

        return {"action": "continue", "modified_kwargs": {**kwargs, **out}} if out else None

    # ---------- Hook 3：planner token 观测 ----------

    @HookHandler(
        "maisaka.planner.after_response",
        name="monika_governance_token_watch",
        description="记录 planner 请求的 token 用量，做上下文分级并在超硬阈值时安全轮换。",
        mode=HookMode.OBSERVE,
    )
    async def handle_planner_response(self, **kwargs: Any) -> None:
        if not self._cfg("guard_enabled") or self._guard is None:
            return
        session_id = str(kwargs.get("session_id") or "").strip()
        if not session_id:
            return
        total = kwargs.get("total_tokens") or 0
        prompt = kwargs.get("prompt_tokens") or 0
        completion = kwargs.get("completion_tokens") or 0
        reason = str(kwargs.get("selection_reason") or "")
        level = self._guard.note_tokens(session_id, int(total), int(prompt), int(completion), reason)
        # 工具调用链保护：输出含工具调用 → 轮次未收敛，保持忙碌
        self._guard.mark_activity(session_id, _output_has_tool_calls(kwargs.get("output_items")))

        if self._guard.should_rotate(session_id):
            result = await self._guard.rotate(session_id, reason="auto_%s" % level)
            LOGGER.info("[MonikaGovernance] 自动轮换 stream=%s tokens=%s result=%s deferred=%s",
                        session_id, total, result.get("runtime_cleared"), result.get("deferred"))

    # ---------- Hook 3：replyer 注入关系身份与规则 ----------

    @HookHandler(
        "maisaka.replyer.before_request",
        name="monika_governance_inject",
        description="注入关系身份（不公开宣示）、生效中的管理规则，以及本轮管理员指令的处理结果。",
        mode=HookMode.BLOCKING,
    )
    async def handle_replyer_request(self, **kwargs: Any) -> dict[str, Any] | None:
        if not self._cfg("enabled"):
            return None

        stream = str(kwargs.get("session_id") or "").strip()
        ctx = self._sess_cache.get(stream) or {}
        uid = str(ctx.get("uid") or "").strip()
        group_id = str(ctx.get("group_id") or "").strip()
        is_group = bool(ctx.get("is_group"))

        owner_uid = str(self._cfg("owner_uid") or "")
        is_owner = bool(uid and owner_uid and uid == owner_uid)

        parts: list[str] = []
        if self._admin_notice:
            parts.append(self._admin_notice)
            self._admin_notice = ""

        if self._cfg("relation_enabled") and self._fam is not None:
            fam = self._fam.level(uid) if uid else "new"
            rule_block = self._store.render(group_id) if (self._store and group_id) else (
                self._store.render("") if self._store else ""
            )
            try:
                block = relations.build_relation_block(
                    uid=uid,
                    is_owner=is_owner,
                    is_group=is_group,
                    group_id=group_id,
                    familiarity=fam,
                    rule_block=rule_block,
                    owner_name=str(self._cfg("owner_name") or ""),
                )
                parts.append(block)
            except Exception as exc:  # noqa: BLE001
                LOGGER.warning("[MonikaGovernance] 关系块构建失败: %s", exc)

        if not parts:
            return None

        merged_text = "\n".join(parts)
        max_chars = int(self._cfg("inject_max_chars") or 0)
        if max_chars and len(merged_text) > max_chars:
            merged_text = merged_text[:max_chars] + "…"

        existing = str(kwargs.get("extra_prompt") or "").strip()
        merged = f"{existing}\n{merged_text}" if existing else merged_text
        return {"action": "continue", "modified_kwargs": {**kwargs, "extra_prompt": merged}}


def create_plugin() -> MonikaGovernancePlugin:
    """创建插件实例（MaiBot 插件加载器入口）。"""
    return MonikaGovernancePlugin()
