"""安全会话轮换（短期上下文生命周期管理）。

对应滚动记忆方案第三阶段。核心约束：**不删数据库、不改 stream_id**。

MaiBot 原生提供安全清理接口（src/maisaka/context/clear_context.py）：
    mark_clear_context_command(message)          # 把消息标成清理边界
    heartflow_manager.clear_chat_history_context(session_id)   # 清运行时短期上下文
    select_messages_after_latest_clear_marker()  # 恢复时只取边界之后的消息

所以「安全轮换」= 在适当时候插入一条清理边界 + 清运行时上下文。
长期记忆（A-Memorix）、聊天流身份、原始消息记录全部保留。

分级（方案初始阈值，可配）：
    正常   total_tokens <= soft   不处理
    压缩   soft < tokens <= hard  只记观测，交给官方滚动摘要
    轮换   tokens > hard          插入清理边界并清运行时上下文

自动轮换默认**关闭**（避免误伤正在进行的话题），由管理员指令开启；
手动轮换随时可用。全部动作写审计日志。
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_SOFT = 8000
DEFAULT_HARD = 12000
DEFAULT_HISTORY = 30


class ContextGuard:
    """观测 token 并在超限时安全轮换短期上下文。"""

    def __init__(
        self,
        *,
        audit_path: str,
        soft_tokens: int = DEFAULT_SOFT,
        hard_tokens: int = DEFAULT_HARD,
        auto_rotate: bool = False,
        history_size: int = DEFAULT_HISTORY,
    ) -> None:
        self.audit_path = Path(audit_path)
        self.soft_tokens = int(soft_tokens)
        self.hard_tokens = int(hard_tokens)
        self.auto_rotate = bool(auto_rotate)
        self.history: dict[str, deque] = {}
        self.history_size = int(history_size)
        self.last_rotate_at: dict[str, float] = {}
        self.rotate_count = 0
        self.cooldown = 300.0        # 同一会话两次自动轮换的最小间隔
        # 工具调用链保护（验收 #8）：planner 轮次进行中禁止轮换，只排队延迟
        self.busy_since: dict[str, float] = {}
        self.busy_timeout = 600.0    # 超时视为僵死轮次，解除保护
        self.pending_rotations: list[dict[str, Any]] = []
        self.deferred_count = 0

    # ---------- 观测 ----------

    def note_tokens(self, session_id: str, total_tokens: int, prompt_tokens: int = 0,
                    completion_tokens: int = 0, selection_reason: str = "") -> str:
        """记录一次 planner 请求的 token 用量，返回当前等级。"""
        if not session_id:
            return "normal"
        level = self.classify(total_tokens)
        hist = self.history.setdefault(session_id, deque(maxlen=self.history_size))
        hist.append({
            "ts": time.time(),
            "total": int(total_tokens),
            "prompt": int(prompt_tokens),
            "completion": int(completion_tokens),
            "level": level,
            "reason": selection_reason[:80],
        })
        return level

    def classify(self, total_tokens: int) -> str:
        if total_tokens > self.hard_tokens:
            return "reset_candidate"
        if total_tokens > self.soft_tokens:
            return "compress_candidate"
        return "normal"

    def latest(self, session_id: str) -> dict[str, Any]:
        hist = self.history.get(session_id)
        return dict(hist[-1]) if hist else {}

    def stats(self, session_id: str = "") -> dict[str, Any]:
        """给 Dashboard 用的健康快照。"""
        def _summary(items: list[dict]) -> dict[str, Any]:
            if not items:
                return {"count": 0}
            totals = sorted(i["total"] for i in items)
            n = len(totals)
            return {
                "count": n,
                "latest_total": totals[-1],
                "max_total": totals[-1],
                "p50": totals[n // 2],
                "p95": totals[min(n - 1, int(n * 0.95))],
                "over_soft": sum(1 for i in items if i["level"] != "normal"),
                "over_hard": sum(1 for i in items if i["level"] == "reset_candidate"),
            }

        if session_id:
            return _summary(list(self.history.get(session_id, [])))
        all_items = [i for h in self.history.values() for i in h]
        out = _summary(all_items)
        out["sessions"] = len(self.history)
        out["rotate_count"] = self.rotate_count
        out["auto_rotate"] = self.auto_rotate
        out["soft_tokens"] = self.soft_tokens
        out["hard_tokens"] = self.hard_tokens
        return out

    # ---------- 工具调用链保护（验收 #8：轮换不得截断未完成的工具调用） ----------

    def mark_busy(self, session_id: str) -> None:
        """planner.before_request：本轮推理/工具调用开始。"""
        if session_id:
            self.busy_since[session_id] = time.time()

    def mark_activity(self, session_id: str, has_tool_calls: bool) -> None:
        """planner.after_response：
        - 输出含工具调用 → 工具链未完，保持忙碌（下一轮 before_request 会续期）
        - 输出不含工具调用 → 本轮收敛，解除保护
        """
        if not session_id:
            return
        if has_tool_calls:
            self.busy_since[session_id] = time.time()
        else:
            self.busy_since.pop(session_id, None)

    def is_busy(self, session_id: str) -> bool:
        """是否处于工具调用轮次中（超时的僵死状态自动解除）。"""
        since = self.busy_since.get(session_id)
        if since is None:
            return False
        if time.time() - since > self.busy_timeout:
            self.busy_since.pop(session_id, None)
            self._audit({"event": "busy_timeout_released", "session_id": session_id,
                         "busy_for": round(time.time() - since, 1)})
            return False
        return True

    async def drain_pending(self) -> int:
        """重试延迟中的轮换请求（后台任务周期调用）。返回实际执行条数。"""
        if not self.pending_rotations:
            return 0
        done = 0
        remaining: list[dict[str, Any]] = []
        for req in self.pending_rotations:
            sid = str(req.get("session_id") or "")
            if sid and self.is_busy(sid):
                req["attempts"] = int(req.get("attempts") or 1) + 1
                remaining.append(req)
                continue
            result = await self._do_rotate(sid, reason=str(req.get("reason") or "deferred_retry"))
            if result.get("runtime_cleared"):
                done += 1
            else:
                req["attempts"] = int(req.get("attempts") or 1) + 1
                req["last_error"] = result.get("error", "")
                if req["attempts"] <= 20:        # 失败重试上限，避免死循环
                    remaining.append(req)
        self.pending_rotations = remaining
        return done

    # ---------- 轮换 ----------

    def should_rotate(self, session_id: str, *, force: bool = False) -> bool:
        if not session_id:
            return False
        if force:
            return True
        if not self.auto_rotate:
            return False
        hist = self.history.get(session_id)
        if not hist:
            return False
        if hist[-1]["level"] != "reset_candidate":
            return False
        last = self.last_rotate_at.get(session_id, 0.0)
        return (time.time() - last) >= self.cooldown

    async def rotate(self, session_id: str, *, reason: str = "manual",
                     write_marker: bool = True) -> dict[str, Any]:
        """安全轮换：清运行时短期上下文，并写一条清理边界标记。

        工具调用轮次进行中（busy）时**安全延迟**：不执行、不截断未完成的
        工具调用链，转入 pending 队列，由 drain_pending 在轮次结束后补执行。
        """
        if session_id and self.is_busy(session_id):
            self.deferred_count += 1
            entry = {
                "session_id": session_id,
                "reason": reason,
                "deferred": True,
                "runtime_cleared": False,
                "marker_written": False,
                "error": "",
            }
            self._audit(entry)
            self.pending_rotations.append({
                "session_id": session_id,
                "reason": reason,
                "queued_at": time.time(),
                "attempts": 1,
            })
            return entry
        return await self._do_rotate(session_id, reason=reason, write_marker=write_marker)

    async def _do_rotate(self, session_id: str, *, reason: str = "manual",
                         write_marker: bool = True) -> dict[str, Any]:
        """实际执行轮换（调用方须已确认不在工具调用轮次中）。"""
        result: dict[str, Any] = {
            "session_id": session_id,
            "reason": reason,
            "runtime_cleared": False,
            "marker_written": False,
            "error": "",
        }
        try:
            from src.chat.heart_flow.heartflow_manager import heartflow_manager
        except Exception as exc:  # noqa: BLE001
            result["error"] = "import heartflow_manager failed: %s" % exc
            self._audit(result)
            return result

        if write_marker:
            result["marker_written"] = await self._write_clear_marker(session_id)

        try:
            had_runtime = await heartflow_manager.clear_chat_history_context(session_id)
            result["runtime_cleared"] = True
            result["had_runtime"] = bool(had_runtime)
        except Exception as exc:  # noqa: BLE001
            result["error"] = "clear_chat_history_context failed: %s" % exc

        if result["runtime_cleared"]:
            self.last_rotate_at[session_id] = time.time()
            self.rotate_count += 1
        self._audit(result)
        return result

    async def _write_clear_marker(self, session_id: str) -> bool:
        """往会话里写一条 /clear 边界消息，使轮换在重启后仍然生效。

        这就是官方的「历史截断水位线」：`select_messages_after_latest_clear_marker()`
        在恢复上下文时只取该标记之后的消息，原始聊天记录、UID、stream_id、
        A-Memorix 全部不受影响（符合方案约束）。

        构造方式走官方 MaiMessage/SessionMessage 对象（非 pydantic，故不用
        model_validate），再用 mark_clear_context_command 打边界标记。
        """
        try:
            from datetime import datetime

            from src.chat.message_receive.message import SessionMessage
            from src.common.data_models.mai_message_data_model import MessageInfo, UserInfo
            from src.common.data_models.message_component_data_model import MessageSequence
            from src.common.utils.utils_message import MessageUtils
            from src.maisaka.context.clear_context import (
                CLEAR_CONTEXT_COMMAND,
                mark_clear_context_command,
            )
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[ContextGuard] 清理标记依赖导入失败: %s", exc)
            return False

        try:
            now = datetime.now()
            marker = SessionMessage(
                message_id="ctxguard_%d" % int(now.timestamp() * 1000),
                timestamp=now,
                platform="qq",
                received_at=now,
            )
            marker.account_id = ""
            marker.scope = None
            marker.message_info = MessageInfo(
                user_info=UserInfo(user_id="context-guard", user_nickname="context-guard",
                                   user_cardname=""),
                group_info=None,
                additional_config={},
            )
            marker.session_id = session_id
            marker.is_mentioned = False
            marker.is_at = False
            marker.is_emoji = False
            marker.is_picture = False
            marker.is_notify = False
            marker.reply_to = None
            marker.reply_frequency = None
            marker.processed_plain_text = CLEAR_CONTEXT_COMMAND
            marker.raw_message = MessageSequence([]).text(CLEAR_CONTEXT_COMMAND)
            # 官方边界标记：is_command + additional_config[maisaka_context_cleared]
            mark_clear_context_command(marker)
            await MessageUtils.store_message_to_db_async(marker)
            LOGGER.info("[ContextGuard] 清理边界已持久化 session=%s", session_id)
            return True
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[ContextGuard] 写清理标记失败（运行时清理仍会执行）: %s", exc)
            return False

    def _audit(self, entry: dict[str, Any]) -> None:
        try:
            self.audit_path.parent.mkdir(parents=True, exist_ok=True)
            line = {"ts": time.strftime("%Y-%m-%dT%H:%M:%S"), **entry}
            with self.audit_path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[ContextGuard] 审计写入失败: %s", exc)


class CommandRunner:
    """消费 Dragon Dashboard 写入的管理员操作指令（拉模式）。

    Dashboard 与 MaiBot 不在同一进程，跨进程直调 heartflow_manager 不可靠
    （A-Memorix 有单写者锁的教训），所以改为：Dashboard 写指令文件 →
    插件在容器内轮询执行 → 审计一致。
    """

    def __init__(self, command_path: str, guard: ContextGuard) -> None:
        self.command_path = Path(command_path)
        self.guard = guard

    async def poll_once(self) -> int:
        """执行一批待处理指令，返回执行条数。"""
        if not self.command_path.exists():
            return 0
        try:
            lines = self.command_path.read_text(encoding="utf-8").splitlines()
        except Exception:
            return 0
        if not lines:
            return 0

        done = 0
        remaining: list[str] = []
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                cmd = json.loads(line)
            except Exception:
                continue
            if cmd.get("_done"):
                continue
            status = await self._execute(cmd)
            if status is None:
                # 延迟执行（工具调用轮次中）：保留下次重试
                cmd["_deferred_at"] = time.time()
                remaining.append(json.dumps(cmd, ensure_ascii=False))
                continue
            cmd["_done"] = True
            cmd["_executed_at"] = time.time()
            cmd["_ok"] = bool(status)
            remaining.append(json.dumps(cmd, ensure_ascii=False))
            done += 1

        try:
            self.command_path.write_text("\n".join(remaining) + "\n", encoding="utf-8")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[CommandRunner] 指令文件回写失败: %s", exc)
        return done

    async def _execute(self, cmd: dict[str, Any]) -> bool | None:
        """执行单条指令。返回 True/False = 已完成；None = 延迟（稍后重试）。"""
        action = str(cmd.get("action") or "")
        if action == "rotate_session":
            session_id = str(cmd.get("session_id") or "")
            if not session_id:
                return False
            result = await self.guard.rotate(
                session_id,
                reason=str(cmd.get("reason") or "dashboard"),
            )
            if result.get("deferred"):
                LOGGER.info("[CommandRunner] 轮换延迟（工具调用中） session=%s", session_id)
                return None
            LOGGER.info("[CommandRunner] 执行轮换 session=%s ok=%s", session_id,
                        result.get("runtime_cleared"))
            return bool(result.get("runtime_cleared"))

        if action == "toggle_auto_rotate":
            self.guard.auto_rotate = bool(cmd.get("auto_rotate"))
            self.guard._audit({"event": "auto_rotate_toggled",
                               "auto_rotate": self.guard.auto_rotate,
                               "source": str(cmd.get("source") or "dashboard")})
            LOGGER.info("[CommandRunner] 自动轮换切换为 %s", self.guard.auto_rotate)
            return True

        LOGGER.warning("[CommandRunner] 未知指令: %s", action)
        return False
