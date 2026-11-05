# -*- coding: utf-8 -*-
"""Monika Knowledge Router（知识路由插件）。

解决的问题（ChatGPT 审查 Step 2 / 审计 A3）：
  知识库已导入但真实对话几乎从不触发 query_memory（全量日志仅 5 次，且都在测试会话）。
  本插件用本地词法规则识别"该查知识"的消息，预取并过滤来源，把 2-4 条可信参考
  注入 planner 的系统块，降低"该查却没查"的概率；不替代 Planner 自由检索。

零新增模型调用。预取走容器内 WebUI 只读接口（127.0.0.1:8001）。

hooks：
- chat.receive.after_process（分类 + 预取 + 缓存）
- maisaka.planner.before_request（把参考注入系统块；附 respect_filter 隐私守卫）
"""

from __future__ import annotations

import json
import logging
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode

from . import router

LOGGER = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "inject_enabled": True,
    "prefetch_limit": 8,
    "max_inject_items": 3,
    "max_inject_chars": 900,
    "webui_base": "http://127.0.0.1:8001",
    "webui_json": "/MaiMBot/data/webui.json",
    "privacy_guard_mode": "routed",   # routed | always | off
    "metrics_enabled": True,
    "metrics_path": "/MaiMBot/data/knowledge_router/metrics.jsonl",
    "metrics_max_bytes": 2_000_000,
}

_TEXT_KEYS = ("processed_plain_text", "display_message", "raw_message", "text")


@dataclass
class Turn:
    """一个会话当前这一轮的路由缓存。"""

    turn_id: int = 0
    route: router.Route | None = None
    picked: list[router.PrefetchItem] = field(default_factory=list)
    injected: bool = False
    fetched_at: float = 0.0
    metrics_written: bool = False
    started_at: float = field(default_factory=time.time)


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


def _stream_id(message: dict[str, Any]) -> str:
    return str(message.get("session_id") or message.get("stream_id") or "").strip()


class MonikaKnowledgeRouterPlugin(MaiBotPlugin):
    """知识路由：词法分类 → 来源过滤预取 → planner 注入。"""

    PLUGIN_ID = "dragon.monika-knowledge-router"

    def __init__(self) -> None:
        super().__init__()
        self._conf: dict[str, Any] = dict(DEFAULTS)
        self._turns: dict[str, Turn] = {}
        self._stats = {"routed": 0, "injected": 0, "prefetch_fail": 0, "noop": 0}

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        self._reload_conf()
        LOGGER.info("[MonikaRouter] 已加载 enabled=%s", self._cfg("enabled"))

    async def on_unload(self) -> None:
        LOGGER.info("[MonikaRouter] 已卸载 stats=%s", self._stats)

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        try:
            self._reload_conf()
            LOGGER.info("[MonikaRouter] 配置热更新 scope=%s version=%s", scope, version)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaRouter] 配置热更新失败: %s", exc)

    def _reload_conf(self) -> None:
        try:
            import tomllib

            path = Path(__file__).resolve().parent / "config.toml"
            data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaRouter] 配置读取失败，使用默认值: %s", exc)
            data = {}
        merged = dict(DEFAULTS)
        for section in ("plugin", "router", "privacy", "metrics"):
            merged.update(data.get(section, {}) or {})
        self._conf = merged

    def _cfg(self, key: str) -> Any:
        return self._conf.get(key, DEFAULTS.get(key))

    # ---------- Hook 1：入站分类 + 预取 ----------

    @HookHandler(
        "chat.receive.after_process",
        name="monika_router_classify",
        description="词法识别知识检索意图并按来源预取候选；普通闲聊零开销直接跳过。",
        mode=HookMode.OBSERVE,
    )
    async def handle_inbound(self, message: dict[str, Any], **kwargs: Any) -> None:
        del kwargs
        try:
            await self._classify_and_prefetch(message)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaRouter] 入站处理异常: %r", exc)

    async def _classify_and_prefetch(self, message: dict[str, Any]) -> None:
        if not self._cfg("enabled") or not isinstance(message, dict):
            return
        if message.get("is_notify"):
            return
        stream_id = _stream_id(message)
        if not stream_id:
            return

        previous = self._turns.get(stream_id)
        if previous is not None and previous.route is not None and not previous.metrics_written:
            self._flush_metrics(stream_id, previous)
        turn = Turn(turn_id=(previous.turn_id + 1) if previous else 1)
        self._turns[stream_id] = turn

        text = _first_text(message)
        route = router.classify(text)
        if route is None:
            self._stats["noop"] += 1
            return
        self._stats["routed"] += 1
        turn.route = route

        if not self._cfg("inject_enabled"):
            return
        raw_items = await self._prefetch(route)
        turn.picked = router.filter_rerank(
            raw_items, route.scopes, max_items=int(self._cfg("max_inject_items") or 3))
        turn.fetched_at = time.time()
        LOGGER.info(
            "[MonikaRouter] 路由 stream=%s scopes=%s triggers=%s 候选=%d 采纳=%d",
            stream_id, route.scopes, route.triggers[:4], len(raw_items), len(turn.picked))

    async def _prefetch(self, route: router.Route) -> list[dict]:
        """经 WebUI records/search 预取候选（只读 GET）。

        注意：records/search 是关键词匹配（多词为 AND 语义，长 query 会 0 命中），
        因此按单词逐个查询再合并去重。
        """
        terms = [t for t in route.query_terms[:3] if t.strip()]
        if not terms:
            terms = [t for t in route.triggers[:1] if t.strip()]
        if not terms:
            return []
        base = str(self._cfg("webui_base") or "").rstrip("/")
        try:
            token = json.loads(Path(str(self._cfg("webui_json"))).read_text(encoding="utf-8"))["access_token"]
        except Exception as exc:  # noqa: BLE001
            self._stats["prefetch_fail"] += 1
            LOGGER.warning("[MonikaRouter] webui.json 读取失败: %s", exc)
            return []
        merged: list[dict] = []
        seen: set[str] = set()
        per_term = max(3, int(self._cfg("prefetch_limit") or 8) // max(1, len(terms)))
        for term in terms:
            url = "%s/api/webui/memory/records/search?query=%s&limit=%d" % (
                base, urllib.parse.quote(term), per_term)
            req = urllib.request.Request(url, method="GET")
            req.add_header("Cookie", "maibot_session=" + token)
            req.add_header("User-Agent", "monika-knowledge-router/1.0")
            req.add_header("Referer", base + "/")
            try:
                with urllib.request.urlopen(req, timeout=6) as resp:
                    data = json.loads(resp.read().decode("utf-8") or "{}")
            except Exception as exc:  # noqa: BLE001
                self._stats["prefetch_fail"] += 1
                LOGGER.warning("[MonikaRouter] 预取失败 term=%s: %r", term, exc)
                continue
            for item in data.get("items") or []:
                key = str(item.get("source") or "") + "|" + str(item.get("content") or item.get("preview") or "")[:60]
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
        return merged[: int(self._cfg("prefetch_limit") or 8)]

    # ---------- Hook 2：planner 注入 ----------

    @HookHandler(
        "maisaka.planner.before_request",
        name="monika_router_inject",
        description="把过滤后的知识参考追加进 planner 系统块；附 respect_filter 隐私守卫。",
        mode=HookMode.BLOCKING,
    )
    async def handle_planner_request(self, items: Any = None, **kwargs: Any) -> dict[str, Any] | None:
        if not self._cfg("enabled") or not isinstance(items, list):
            return None
        session = str(kwargs.get("session_id") or "").strip()
        turn = self._turns.get(session)
        guard_mode = str(self._cfg("privacy_guard_mode") or "routed")

        note_parts: list[str] = []
        if (self._cfg("inject_enabled") and turn is not None and turn.route is not None
                and turn.picked and not turn.injected):
            note = router.build_injection(
                turn.route, turn.picked, max_chars=int(self._cfg("max_inject_chars") or 900))
            note_parts.append(note)
            turn.injected = True
            self._stats["injected"] += 1

        if guard_mode == "always" or (guard_mode == "routed" and note_parts):
            note_parts.append(
                "【隐私守卫】调用 query_memory 时 respect_filter 必须保持 true，"
                "禁止传 false（会绕过聊天隔离）。")

        if not note_parts:
            return None
        mutated = self._append_system_note(items, "\n".join(note_parts))
        if mutated is None:
            return None
        LOGGER.info("[MonikaRouter] 注入知识参考 session=%s bytes=%d",
                    session, sum(len(p) for p in note_parts))
        return {"action": "continue", "modified_kwargs": {**kwargs, "items": mutated}}

    @staticmethod
    def _append_system_note(items: list[Any], note: str) -> list[Any] | None:
        """把说明追加到最后一个系统消息的末尾（与 monika_policy 同机制）。"""
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

    # ---------- Hook 3：观测落盘 ----------

    @HookHandler(
        "send_service.after_send",
        name="monika_router_metrics",
        description="把本轮路由与注入结果写入观测文件，供验收统计。",
        mode=HookMode.OBSERVE,
    )
    async def handle_after_send(self, message: dict[str, Any], sent: bool, **kwargs: Any) -> None:
        del kwargs
        if not sent or not self._cfg("metrics_enabled") or not isinstance(message, dict):
            return
        stream_id = _stream_id(message)
        turn = self._turns.get(stream_id)
        if turn is None or turn.route is None or turn.metrics_written:
            return
        self._flush_metrics(stream_id, turn)

    def _flush_metrics(self, stream_id: str, turn: Turn) -> None:
        if turn.metrics_written or not self._cfg("metrics_enabled"):
            return
        turn.metrics_written = True
        path = Path(str(self._cfg("metrics_path")))
        try:
            path.parent.mkdir(parents=True, exist_ok=True)
            max_bytes = int(self._cfg("metrics_max_bytes") or 0)
            if max_bytes and path.exists() and path.stat().st_size > max_bytes:
                path.replace(path.with_suffix(".jsonl.1"))
            line = {
                "ts": time.strftime("%Y-%m-%dT%H:%M:%S"),
                "stream": stream_id,
                "turn": turn.turn_id,
                "scopes": turn.route.scopes,
                "triggers": turn.route.triggers[:6],
                "query_terms": turn.route.query_terms[:6],
                "picked_sources": [p.source for p in turn.picked],
                "injected": turn.injected,
                "duration_s": round(time.time() - turn.started_at, 1),
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaRouter] 观测写入失败: %s", exc)


def create_plugin() -> MonikaKnowledgeRouterPlugin:
    """创建插件实例（MaiBot 插件加载器入口）。"""
    return MonikaKnowledgeRouterPlugin()
