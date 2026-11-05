"""Monika 会话节奏插件（v3 方案 P2）。

做三件很小的事，全部在本地完成，不新增任何模型调用：
1. 收到消息时用词法规则判定它的"社交作用"（已照做/告别/分享/情绪/求助/调侃/噪声）；
2. 回复生成前，把对应的一句节奏参考作为「额外回复要求」塞给 replyer（只提示，不裁剪输出）；
3. 记录每条入站消息对应的 reply 调用次数与最终气泡数，供人工复核（v3 方案 P1 观测口径）。

配置见同目录 config.toml；hooks：
- chat.receive.after_process（观察）
- maisaka.replyer.before_request（改写 extra_prompt）
- send_service.after_send（观察）
"""

from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.types import HookMode

from . import rhythm

LOGGER = logging.getLogger(__name__)

DEFAULTS: dict[str, Any] = {
    "enabled": True,
    "states": [],                      # 空 = 全部状态都提示；可填 ["ACK_DONE","PARTING"]
    "hint_max_chars": 60,
    "metrics_enabled": True,
    "metrics_path": "/MaiMBot/data/rhythm/metrics.jsonl",
    "metrics_max_bytes": 2_000_000,
}

_TEXT_KEYS = ("processed_plain_text", "display_message", "raw_message", "text")


@dataclass
class Turn:
    """一个会话里当前这一轮的观测记录。"""

    turn_id: int = 0
    intent: str | None = None
    hint: str = ""
    hint_used: bool = False
    replies: int = 0
    bubbles: int = 0
    started_at: float = field(default_factory=time.time)
    sent_ids: set[str] = field(default_factory=set)
    written: tuple[int, int] | None = None


def _first_text(message: dict[str, Any]) -> str:
    """从序列化消息里尽量稳地取到纯文本。"""
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


class MonikaRhythmPlugin(MaiBotPlugin):
    """会话节奏与观测插件。"""

    def __init__(self) -> None:
        super().__init__()
        self._conf: dict[str, Any] = {}
        self._turns: dict[str, Turn] = {}
        self._hint_total = 0

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        self._reload_conf()
        LOGGER.info("[MonikaRhythm] 插件已加载：词法节奏提示 + 气泡观测（零额外模型调用）")

    async def on_unload(self) -> None:
        for stream_id, turn in list(self._turns.items()):
            self._flush(stream_id, turn)
        self._turns.clear()
        LOGGER.info("[MonikaRhythm] 插件已卸载：累计注入节奏提示 %s 次", self._hint_total)

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        try:
            self._reload_conf()
            LOGGER.info("[MonikaRhythm] 配置已热更新: scope=%s version=%s", scope, version)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaRhythm] 配置热更新失败: %s", exc)

    def _reload_conf(self) -> None:
        try:
            import tomllib

            path = Path(__file__).resolve().parent / "config.toml"
            data = tomllib.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaRhythm] 配置读取失败，使用默认值: %s", exc)
            data = {}
        merged = dict(DEFAULTS)
        merged.update(data.get("plugin", {}) or {})
        merged.update(data.get("rhythm", {}) or {})
        merged.update(data.get("metrics", {}) or {})
        self._conf = merged

    def _cfg(self, key: str) -> Any:
        return self._conf.get(key, DEFAULTS.get(key))

    # ---------- Hook 1：入站消息分类 ----------

    @HookHandler(
        "chat.receive.after_process",
        name="monika_rhythm_classify",
        description="按词法判定入站消息的社交作用，供本轮回复参考；同时轮转上一轮的观测记录。",
        mode=HookMode.OBSERVE,
    )
    async def handle_inbound(self, message: dict[str, Any], **kwargs: Any) -> None:
        del kwargs
        if not self._cfg("enabled") or not isinstance(message, dict):
            return
        if message.get("is_notify"):
            return
        stream_id = _stream_id(message)
        if not stream_id:
            return

        previous = self._turns.get(stream_id)
        if previous is not None:
            self._flush(stream_id, previous)

        text = _first_text(message)
        intent = rhythm.classify(text, is_picture=bool(message.get("is_picture")))
        self._turns[stream_id] = Turn(
            turn_id=(previous.turn_id + 1) if previous else 1,
            intent=intent,
            started_at=time.time(),
        )

    # ---------- Hook 2：给 replyer 追加一句节奏参考 ----------

    @HookHandler(
        "maisaka.replyer.before_request",
        name="monika_rhythm_hint",
        description="把本轮节奏参考追加到 replyer 的额外回复要求里；只提示，不改写输出。",
        mode=HookMode.BLOCKING,
    )
    async def handle_replyer_request(self, **kwargs: Any) -> dict[str, Any] | None:
        stream_id = str(kwargs.get("session_id") or "").strip()
        turn = self._turns.get(stream_id)
        if not self._cfg("enabled") or turn is None:
            return None

        # 每次 replyer 请求都计数（含重试），用于观测"一条入站被回复了几次"
        turn.replies += 1
        if turn.hint_used:
            return None

        hint = rhythm.build_hint(turn.intent, max_chars=int(self._cfg("hint_max_chars") or 0))
        allowed = self._cfg("states") or []
        if not hint or (allowed and turn.intent not in allowed):
            return None

        existing = str(kwargs.get("extra_prompt") or "").strip()
        merged = f"{existing}\n【节奏参考】{hint}" if existing else f"【节奏参考】{hint}"
        turn.hint_used = True
        turn.hint = hint
        self._hint_total += 1
        LOGGER.info("[MonikaRhythm] 注入节奏提示 stream=%s intent=%s", stream_id, turn.intent)
        return {"action": "continue", "modified_kwargs": {**kwargs, "extra_prompt": merged}}

    # ---------- Hook 3：气泡数观测 ----------

    @HookHandler(
        "send_service.after_send",
        name="monika_rhythm_bubble_count",
        description="累计本轮真实发出的消息条数，写入本地观测文件。",
        mode=HookMode.OBSERVE,
    )
    async def handle_after_send(self, message: dict[str, Any], sent: bool, **kwargs: Any) -> None:
        del kwargs
        if not sent or not self._cfg("metrics_enabled") or not isinstance(message, dict):
            return
        stream_id = _stream_id(message)
        turn = self._turns.get(stream_id)
        if turn is None:
            return
        # 同一条消息可能被 Hook 多次看到，按 message_id 去重，避免气泡数虚高
        message_id = str(message.get("message_id") or "").strip()
        if message_id and message_id in turn.sent_ids:
            return
        if message_id:
            turn.sent_ids.add(message_id)
        turn.bubbles += 1
        self._flush(stream_id, turn)

    # ---------- 观测落盘 ----------

    def _flush(self, stream_id: str, turn: Turn) -> None:
        if not self._cfg("metrics_enabled"):
            return
        # 计数没变化就不重复写，避免同一轮出现多行相同记录
        snapshot = (turn.replies, turn.bubbles)
        if turn.written == snapshot:
            return
        turn.written = snapshot
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
                "intent": turn.intent,
                "hint": bool(turn.hint),
                "replies": turn.replies,
                "bubbles": turn.bubbles,
                "duration_s": round(time.time() - turn.started_at, 1),
            }
            with path.open("a", encoding="utf-8") as fh:
                fh.write(json.dumps(line, ensure_ascii=False) + "\n")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaRhythm] 观测写入失败: %s", exc)


def create_plugin() -> MonikaRhythmPlugin:
    """创建插件实例（MaiBot 插件加载器入口）。"""
    return MonikaRhythmPlugin()
