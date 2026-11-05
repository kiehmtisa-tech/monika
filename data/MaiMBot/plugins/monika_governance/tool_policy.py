"""工具 Schema 场景精简（planner.before_request 的 tool_definitions 过滤）。

数据依据（225 次 planner 请求采样）：
- 常驻工具 10 个，Schema 合计 ~4213 字符（~2100 tokens）
- query_image_memory / open_private_chat / get_qq_by_msg_id 在非图片、非管理
  场景中 0 调用，但每轮固定携带 ~986 字符（~490 tokens）

策略（保守：只裁明确场景绑定的工具，不确定时全量下发）：
- 固定保留：reply / stop / wait / tool_search / query_memory /
  query_person_profile / send_emoji（行为与记忆核心，全场景可用）
- query_image_memory：仅当本轮上下文含图片
- open_private_chat / get_qq_by_msg_id：仅管理员（owner）私聊场景
- 工具历史中被调用过的工具当轮不裁（防止历史调用失去定义）；
  归档（tool_chain）会把更早的调用压掉，两者配合生效

不在注册表上动刀（不删插件、不关工具），只影响当前请求的可见集合，
各并发聊天流互不影响。
"""
from __future__ import annotations

import logging
from typing import Any

LOGGER = logging.getLogger(__name__)

ALWAYS_ON = {
    "reply", "stop", "wait", "tool_search",
    "query_memory", "query_person_profile", "send_emoji",
}
IMAGE_ONLY = {"query_image_memory"}
ADMIN_ONLY = {"open_private_chat", "get_qq_by_msg_id"}


def _tool_name(defn: dict[str, Any]) -> str:
    if not isinstance(defn, dict):
        return ""
    name = defn.get("name")
    if isinstance(name, str) and name:
        return name
    fn = defn.get("function")
    if isinstance(fn, dict):
        name = fn.get("name")
        if isinstance(name, str):
            return name
    return ""


def _schema_chars(defn: dict[str, Any]) -> int:
    try:
        import json

        return len(json.dumps(defn, ensure_ascii=False))
    except Exception:  # noqa: BLE001
        return 0


def request_has_image(items: list[dict[str, Any]]) -> bool:
    for it in items:
        parts = it.get("parts")
        if not isinstance(parts, list):
            continue
        for p in parts:
            if isinstance(p, dict) and str(p.get("type") or "").lower() == "image":
                return True
    return False


def tool_names_used_in_items(items: list[dict[str, Any]]) -> set[str]:
    used: set[str] = set()
    for it in items:
        t = str(it.get("item_type") or "")
        if "FunctionCall" in t and "Output" not in t:
            name = str((it.get("tool_call") or {}).get("func_name") or "")
            if name:
                used.add(name)
        if t == "FunctionCallOutputItem":
            name = str(it.get("tool_name") or "")
            if name:
                used.add(name)
    return used


def strip_admin_tool_calls(items: list[dict[str, Any]], *, is_owner_stream: bool,
                           admin_tools: set[str] | None = None) -> tuple[list[dict[str, Any]], int]:
    """执行侧防线：非管理会话中剥离管理工具的调用与结果（call+output 成对移除）。

    Schema 隐藏只是第一层（模型看不到工具）。这里防的是历史重放/注入：
    若管理工具调用混在上下文里（旧记录、伪造条目），非管理员会话不应回放它们。
    成对移除不破坏 tool_call_id 配对。
    """
    if is_owner_stream:
        return items, 0
    targets = admin_tools if admin_tools is not None else ADMIN_ONLY
    # 找出要移除的 call_id
    drop_ids: set[str] = set()
    for it in items:
        t = str(it.get("item_type") or "")
        if "FunctionCall" in t and "Output" not in t:
            tc = it.get("tool_call") or {}
            if str(tc.get("func_name") or "") in targets:
                drop_ids.add(str(tc.get("call_id") or ""))
    if not drop_ids:
        return items, 0
    out = []
    removed = 0
    for it in items:
        t = str(it.get("item_type") or "")
        if "FunctionCall" in t and "Output" not in t:
            if str((it.get("tool_call") or {}).get("call_id") or "") in drop_ids:
                removed += 1
                continue
        if t == "FunctionCallOutputItem":
            if str(it.get("call_id") or "") in drop_ids:
                removed += 1
                continue
        out.append(it)
    return out, removed


def filter_tool_definitions(
    definitions: list[dict[str, Any]],
    *,
    items: list[dict[str, Any]],
    is_owner_stream: bool,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """按场景裁剪工具定义。返回 (新定义列表, 统计)。"""
    if not definitions:
        return list(definitions), {"before": 0, "after": 0, "dropped": [], "saved_chars": 0}

    has_image = request_has_image(items)
    used_names = tool_names_used_in_items(items)

    kept: list[dict[str, Any]] = []
    dropped: list[str] = []
    saved = 0
    for defn in definitions:
        name = _tool_name(defn)
        keep = True
        if name in IMAGE_ONLY and not has_image:
            keep = False
        elif name in ADMIN_ONLY and not is_owner_stream:
            keep = False
        elif name and name not in ALWAYS_ON and name not in IMAGE_ONLY and name not in ADMIN_ONLY:
            keep = True   # 未知工具保守保留
        # 历史调用过的工具当轮不裁
        if not keep and name in used_names:
            keep = True
        if keep:
            kept.append(defn)
        else:
            dropped.append(name)
            saved += _schema_chars(defn)

    stats = {
        "before": len(definitions),
        "after": len(kept),
        "dropped": dropped,
        "saved_chars": saved,
        "has_image": has_image,
    }
    return kept, stats


class ToolSchemaFilter:
    """按会话记录 Schema 精简统计（Dragon Board 展示用）。"""

    def __init__(self) -> None:
        self.stats_by_session: dict[str, dict[str, int]] = {}

    def process(self, session_id: str, definitions: list[dict[str, Any]],
                items: list[dict[str, Any]], *, is_owner_stream: bool
                ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        new_defs, stats = filter_tool_definitions(
            definitions, items=items, is_owner_stream=is_owner_stream,
        )
        if stats.get("dropped"):
            acc = self.stats_by_session.setdefault(session_id, {
                "runs": 0, "saved_chars": 0, "dropped_count": 0,
            })
            acc["runs"] += 1
            acc["saved_chars"] += int(stats.get("saved_chars") or 0)
            acc["dropped_count"] += len(stats["dropped"])
            LOGGER.info("[ToolSchema] 精简 session=%s %s->%s dropped=%s saved=%s chars",
                        session_id, stats["before"], stats["after"],
                        stats["dropped"], stats["saved_chars"])
        return new_defs, stats

    def stats(self, session_id: str = "") -> dict[str, Any]:
        if session_id:
            return dict(self.stats_by_session.get(session_id, {}))
        total = {"runs": 0, "saved_chars": 0, "dropped_count": 0}
        for acc in self.stats_by_session.values():
            for k in total:
                total[k] += int(acc.get(k, 0))
        total["sessions"] = len(self.stats_by_session)
        return total
