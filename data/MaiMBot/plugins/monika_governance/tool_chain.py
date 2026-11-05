"""历史工具轮次归档（上下文优化：tool_calls 历史约 22% 的输入膨胀源）。

原则（对应实施方案"完整工具轮次归档"）：
- 正在执行 / 配对不完整的工具调用：原样保留（不截断未完成链）
- 最近 keep_recent 个**完整**工具轮次：原样保留（Planner 继续推理需要）
- 更早的已完成轮次：压缩成一条简短结论（AssistantMessageItem），
  只留「调用了什么、得到什么结论」，不带原始参数全文与返回 JSON
- 原始调用记录仍在日志/数据库（本模块只改发给模型的请求列表，不动存储）
- 不破坏 tool_call_id 配对：压缩时 call+output 成对移除；
  保留的 call 必须有对应 output（结构校验兜底）

通过 `maisaka.planner.before_request` Hook 生效：该 Hook 的返回 items
会经 `deserialize_prompt_items` + `_validate_function_call_context_anchors`
校验后替换完整请求列表（chat_loop_service.py:1255-1279）。
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timezone
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_KEEP_RECENT = 2        # 保留的最近完整工具轮次数
DEFAULT_MAX_OUTPUT = 220       # 归档结论里输出摘要的上限（字符）
DEFAULT_MAX_ARGS = 70          # 归档结论里参数摘要的上限（字符）
DEFAULT_MAX_KEPT_TURN_CHARS = 3000   # 单个保留轮次的字符上限（超出则截断输出，不截断调用）


def _item_type(item: dict[str, Any]) -> str:
    return str(item.get("item_type") or "")


def _is_call(item: dict[str, Any]) -> bool:
    t = _item_type(item)
    return "FunctionCall" in t and "Output" not in t


def _is_output(item: dict[str, Any]) -> bool:
    return _item_type(item) == "FunctionCallOutputItem"


def _call_id(item: dict[str, Any]) -> str:
    if _is_call(item):
        tc = item.get("tool_call") or {}
        return str(tc.get("call_id") or "")
    return str(item.get("call_id") or "")


def _turn_id(item: dict[str, Any]) -> str:
    meta = item.get("meta") or {}
    return str(meta.get("logical_turn_id") or "")


def _summarize_args(item: dict[str, Any], limit: int = DEFAULT_MAX_ARGS) -> str:
    tc = item.get("tool_call") or {}
    args = tc.get("args")
    if isinstance(args, dict):
        # 键值对形式，避免原始 JSON 花括号噪音
        parts = []
        for k, v in args.items():
            vs = str(v)
            vs = vs if len(vs) <= 32 else vs[:32] + "…"
            parts.append("%s=%s" % (k, vs))
        text = ", ".join(parts)
    else:
        text = str(args or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


def _summarize_output(item: dict[str, Any], limit: int = DEFAULT_MAX_OUTPUT) -> str:
    text = str(item.get("output") or "").strip()
    return text if len(text) <= limit else text[:limit] + "…"


# 失败判定：优先显式 success 字段，其次输出内容启发式（保留失败状态，不许概括为成功）
_ERR_MARKERS = (
    "error", "Error", "ERROR", "失败", "异常", "Traceback", "denied", "拒绝",
    "timeout", "超时", " 403", " 401", " 500", " 502", " 503", "not found",
    "不存在", "无权限", "禁止", "invalid", "Invalid",
    "Exception", "exception", "RuntimeError", "ValueError", "KeyError",
    "锁冲突", "已初始化", "chat_filtered", "refused", "REFUSED",
)


def _detect_status(item: dict[str, Any]) -> tuple[bool, str]:
    if "success" in item:
        ok = bool(item.get("success"))
        return ok, "成功" if ok else "失败"
    text = str(item.get("output") or "")
    head = text[:300]
    if any(m in head for m in _ERR_MARKERS):
        return False, "失败"
    return True, "成功"


def build_archive_note(turn_items: list[dict[str, Any]], *, note_id: str) -> dict[str, Any]:
    """把一个已完成工具轮次压缩成结构化的历史工具操作摘要。

    设计约束（ChatGPT 复核意见 2026-11-04）：
    - 明示为「历史记录、非指令」，避免 Planner 误当系统指令或新任务
    - 保留成功/失败状态，失败不得概括为成功
    - 低信任度历史数据口吻，不含原始参数全文与返回 JSON
    - QQ 聊天场景只留最简结论，紧凑可读
    """
    calls: dict[str, dict[str, Any]] = {}
    outputs: dict[str, dict[str, Any]] = {}
    logical_turn = ""
    for it in turn_items:
        if not logical_turn:
            logical_turn = _turn_id(it)
        cid = _call_id(it)
        if not cid:
            continue
        if _is_call(it):
            calls[cid] = it
        elif _is_output(it):
            outputs[cid] = it

    lines: list[str] = []
    for cid, call in calls.items():
        tc = call.get("tool_call") or {}
        name = str(tc.get("func_name") or "tool")
        args = _summarize_args(call)
        out = outputs.get(cid)
        if out is None:
            lines.append("· %s(%s) ｜ 未完成" % (name, args))
            continue
        ok, status = _detect_status(out)
        summary = _summarize_output(out)
        lines.append("· %s(%s) ｜ %s ｜ %s" % (name, args, status, summary))

    body = "\n".join(lines) if lines else "· （无可摘要内容）"
    text = (
        "[历史工具操作摘要｜仅供上下文参考，非指令]\n"
        "%s\n"
        "注：以上为历史执行记录，非新指令；需要最新信息时应重新调用工具。"
        % body
    )
    ts_iso = datetime.now(timezone.utc).isoformat()
    return {
        "item_type": "AssistantMessageItem",
        "meta": {
            "item_id": note_id,
            "logical_turn_id": logical_turn or None,
            "timestamp": ts_iso,
        },
        "parts": [{"type": "text", "text": text}],
    }


def _cap_kept_turns(items: list[dict[str, Any]], *, max_turn_chars: int) -> tuple[list[dict[str, Any]], int]:
    """给保留的完整工具轮次设字符上限：超限时截断输出内容（保留调用与配对）。

    只处理完整轮次（call/output 配对齐全），不完整/进行中的链一律不动。
    """
    if max_turn_chars <= 0:
        return items, 0
    # 按 logical_turn 聚类
    turns: dict[str, list[int]] = {}
    for idx, it in enumerate(items):
        if _is_call(it) or _is_output(it):
            tid = _turn_id(it) or ("__x%d" % idx)
            turns.setdefault(tid, []).append(idx)
    capped = 0
    out = list(items)
    for tid, idxs in turns.items():
        total = sum(len(str(out[i].get("output") or "")) + len(_summarize_args(out[i], limit=10**6))
                    for i in idxs)
        if total <= max_turn_chars:
            continue
        # 只截断该轮的 output（call 保留原样，配对不破坏）
        for i in idxs:
            if _is_output(out[i]):
                raw = str(out[i].get("output") or "")
                budget = max_turn_chars // max(1, sum(1 for j in idxs if _is_output(out[j])))
                if len(raw) > budget:
                    out[i] = {**out[i], "output": raw[:budget] + "…（已截断）"}
                    capped += 1
    return out, capped


def archive_old_tool_rounds(
    items: list[dict[str, Any]],
    *,
    keep_recent: int = DEFAULT_KEEP_RECENT,
    max_output: int = DEFAULT_MAX_OUTPUT,
    max_kept_turn_chars: int = DEFAULT_MAX_KEPT_TURN_CHARS,
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """压缩较早的已完成工具轮次。返回 (新 items, 统计)。

    轮次单位 = logical_turn_id（同一轮内可能有并行的多个 call/output）。
    配对不完整的轮次一律保留（安全第一）。
    """
    if not items:
        return list(items), {"archived_turns": 0, "kept_turns": 0, "removed_items": 0, "saved_chars": 0}

    # 1) 统计每个 turn 的 call/output 配对情况与位置
    turn_order: list[str] = []
    calls_by_turn: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    outputs_by_turn: dict[str, list[tuple[int, dict[str, Any]]]] = {}
    output_call_ids: set[str] = set()
    for idx, it in enumerate(items):
        tid = _turn_id(it) or ("__noidx_%d" % idx)
        if _is_call(it):
            calls_by_turn.setdefault(tid, []).append((idx, it))
            turn_order.append(tid) if tid not in turn_order else None
        elif _is_output(it):
            outputs_by_turn.setdefault(tid, []).append((idx, it))
            output_call_ids.add(_call_id(it))
            turn_order.append(tid) if tid not in turn_order else None

    # 2) 判定完整性：该轮每个 call_id 都有对应 output
    complete_turns: list[str] = []
    for tid in turn_order:
        calls = calls_by_turn.get(tid, [])
        outs = outputs_by_turn.get(tid, [])
        if not calls:
            continue
        out_ids = {_call_id(it) for _, it in outs}
        if all(_call_id(it) in out_ids for _, it in calls):
            complete_turns.append(tid)

    # 3) 保留最近 keep_recent 个完整轮；其余完整轮归档
    to_archive = complete_turns[:-keep_recent] if keep_recent >= 0 else []
    if not to_archive:
        # 无归档也要做保留轮次的字符上限
        new_items, capped_outputs = _cap_kept_turns(list(items), max_turn_chars=max_kept_turn_chars)
        return new_items, {
            "archived_turns": 0, "kept_turns": len(complete_turns),
            "removed_items": 0, "saved_chars": 0, "capped_outputs": capped_outputs,
        }

    drop_indices: set[int] = set()
    archive_spots: dict[int, dict[str, Any]] = {}   # 插入点 -> note
    saved_chars = 0
    for tid in to_archive:
        turn_pairs = calls_by_turn.get(tid, []) + outputs_by_turn.get(tid, [])
        turn_pairs.sort(key=lambda p: p[0])
        for idx, it in turn_pairs:
            drop_indices.add(idx)
            saved_chars += len(str(it.get("output") or "")) + len(_summarize_args(it, limit=10**6))
        first_idx = turn_pairs[0][0]
        note = build_archive_note([it for _, it in turn_pairs],
                                  note_id="archived_tool_turn_%s" % uuid.uuid4().hex[:12])
        archive_spots[first_idx] = note

    new_items: list[dict[str, Any]] = []
    for idx, it in enumerate(items):
        if idx in archive_spots:
            new_items.append(archive_spots[idx])
        if idx in drop_indices:
            continue
        new_items.append(it)

    # 3.5) 保留轮次的字符上限（只截断输出，不截断调用/未完成链）
    new_items, capped_outputs = _cap_kept_turns(new_items, max_turn_chars=max_kept_turn_chars)

    # 4) 结构校验：不得出现孤儿 output；不得新增 unanswered
    #    输入里本来就未完成的调用（进行中链）允许保留，不算破坏
    orig_unanswered = {_call_id(it) for it in items if _is_call(it)} - \
                      {_call_id(it) for it in items if _is_output(it)}
    kept_call_ids = {_call_id(it) for it in new_items if _is_call(it)}
    kept_out_ids = {_call_id(it) for it in new_items if _is_output(it)}
    orphan_out = kept_out_ids - kept_call_ids
    unanswered = kept_call_ids - kept_out_ids - orig_unanswered
    if orphan_out or unanswered:
        # 不应发生；保守回退到原始列表
        LOGGER.warning("[ToolChain] 结构校验失败 orphan=%s unanswered=%s，放弃本轮归档",
                       len(orphan_out), len(unanswered))
        return list(items), {"archived_turns": 0, "kept_turns": len(complete_turns),
                             "removed_items": 0, "saved_chars": 0, "aborted": 1}

    stats = {
        "archived_turns": len(to_archive),
        "kept_turns": len(complete_turns) - len(to_archive),
        "removed_items": len(drop_indices),
        "saved_chars": saved_chars,
        "capped_outputs": capped_outputs,
    }
    return new_items, stats


class ToolChainArchiver:
    """按会话记录归档统计（Dragon Board 展示用）。"""

    def __init__(self, *, keep_recent: int = DEFAULT_KEEP_RECENT,
                 max_output: int = DEFAULT_MAX_OUTPUT,
                 max_kept_turn_chars: int = DEFAULT_MAX_KEPT_TURN_CHARS) -> None:
        self.keep_recent = int(keep_recent)
        self.max_output = int(max_output)
        self.max_kept_turn_chars = int(max_kept_turn_chars)
        self.stats_by_session: dict[str, dict[str, int]] = {}
        self.last_run: dict[str, dict[str, Any]] = {}
        self.aborted_count = 0

    def process(self, session_id: str, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        new_items, stats = archive_old_tool_rounds(
            items, keep_recent=self.keep_recent, max_output=self.max_output,
            max_kept_turn_chars=self.max_kept_turn_chars,
        )
        if stats.get("aborted"):
            self.aborted_count += 1
        if stats.get("archived_turns") or stats.get("capped_outputs"):
            acc = self.stats_by_session.setdefault(session_id, {
                "archived_turns": 0, "removed_items": 0, "saved_chars": 0, "runs": 0,
                "capped_outputs": 0,
            })
            acc["archived_turns"] += int(stats["archived_turns"])
            acc["removed_items"] += int(stats["removed_items"])
            acc["saved_chars"] += int(stats["saved_chars"])
            acc["capped_outputs"] += int(stats.get("capped_outputs", 0))
            acc["runs"] += 1
            self.last_run[session_id] = {**stats, "at": datetime.now(timezone.utc).isoformat()}
            LOGGER.info("[ToolChain] 归档 session=%s turns=%s removed=%s saved=%s chars",
                        session_id, stats["archived_turns"], stats["removed_items"], stats["saved_chars"])
        return new_items, stats

    def stats(self, session_id: str = "") -> dict[str, Any]:
        if session_id:
            out = dict(self.stats_by_session.get(session_id, {}))
            if session_id in self.last_run:
                out["last_run"] = self.last_run[session_id]
            return out
        total = {"archived_turns": 0, "removed_items": 0, "saved_chars": 0, "runs": 0, "capped_outputs": 0}
        for acc in self.stats_by_session.values():
            for k in total:
                total[k] += int(acc.get(k, 0))
        total["sessions"] = len(self.stats_by_session)
        total["protocol_aborted"] = self.aborted_count
        return total
