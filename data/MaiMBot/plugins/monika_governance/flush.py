"""低流量会话的空闲收尾写回（idle flush）。

补的是「12 条阈值触发」之外的空档：只积了 4~11 条消息、但包含重要信息的私聊，
在空闲 30 分钟后做一次收尾写回，避免整段经历丢失。

**写入通道**：走 MaiBot WebUI 的记忆导入 API（`/api/webui/memory/import/paste`）。
原因：A-Memorix 有单写者锁（锁持有者是 MaiBot 主进程），插件进程调用
`memory_service.ingest_summary` 会被拒并返回**假成功**。WebUI 接口在主进程内执行，
可靠落库。摘要采用规则式要点提取（不调 LLM），符合低 Token 约束。

约束：
- 只在会话真正空闲后才触发，不是每条消息都调用模型
- 每个会话每天最多额外触发 1 次
- 只摘要尚未写回的消息（用自维护 checkpoint）
- 只有出现有意义内容才写回（寒暄/表情不生成记忆）
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
import urllib.request
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_WEBUI = "http://127.0.0.1:8001"      # 容器内 WebUI 端口
DEFAULT_TOKEN_FILE = "/MaiMBot/data/webui.json"

# 纯寒暄/低信息量：不为它们生成记忆
_NOISE_EXACT = {
    "嗯", "哦", "噢", "好", "好的", "好呀", "好哒", "好嘞", "行", "行吧",
    "哈哈", "哈哈哈", "嘿嘿", "嘻嘻", "啊", "呃", "嗯嗯", "嗯呢", "ok", "okay",
    "睡了", "晚安", "早", "早安", "早呀", "在", "在吗", "在吗？", "来了", "走了",
    "收到", "知道了", "谢谢", "谢啦", "抱抱", "[表情]", "[图片]", "[语音]",
}


class IdleFlush:
    def __init__(
        self,
        *,
        state_path: str,
        idle_seconds: float = 1800.0,
        min_messages: int = 4,
        daily_cap: int = 1,
        context_length: int = 16,
        min_info_chars: int = 8,
        recent_keep: int = 24,
    ) -> None:
        self.state_path = Path(state_path)
        self.idle_seconds = float(idle_seconds)
        self.min_messages = int(min_messages)
        self.daily_cap = int(daily_cap)
        self.context_length = int(context_length)
        self.min_info_chars = int(min_info_chars)
        self.recent_keep = int(recent_keep)
        self.state: dict[str, dict[str, Any]] = {}
        self.recent: dict[str, list[str]] = {}
        self._load()

    # ---------- 状态 ----------

    def _load(self) -> None:
        try:
            if self.state_path.exists():
                data = json.loads(self.state_path.read_text(encoding="utf-8"))
                if isinstance(data, dict):
                    self.state = data.get("chats", data)
                    self.recent = data.get("recent", {})
        except Exception:
            self.state, self.recent = {}, {}

    def _save(self) -> None:
        self.state_path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self.state_path.with_suffix(".json.tmp")
        payload = {"chats": self.state, "recent": self.recent}
        tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp.replace(self.state_path)

    # ---------- 消息记账 ----------

    def note_message(self, chat_id: str, text: str, ts: float | None = None) -> None:
        if not chat_id:
            return
        item = self.state.setdefault(chat_id, {
            "last_ts": 0.0, "pending": 0, "meaningful_pending": 0,
            "last_flush_ts": 0.0, "last_flush_day": "", "flushes_today": 0,
        })
        item["last_ts"] = float(ts or time.time())
        item["pending"] = int(item.get("pending", 0)) + 1
        if self._is_meaningful(text):
            item["meaningful_pending"] = int(item.get("meaningful_pending", 0)) + 1
            buf = self.recent.setdefault(chat_id, [])
            buf.append((text or "").strip()[:200])
            if len(buf) > self.recent_keep:
                del buf[: len(buf) - self.recent_keep]
        self._save()

    @staticmethod
    def _is_meaningful(text: str) -> bool:
        compact = (text or "").strip()
        if not compact:
            return False
        if compact in _NOISE_EXACT:
            return False
        stripped = compact.replace(" ", "")
        return len(stripped) >= 8

    def build_digest(self, chat_id: str) -> str:
        """把最近的有意义消息拼成一段要点（规则式，不调模型）。"""
        items = self.recent.get(chat_id) or []
        if not items:
            return ""
        lines = [t for t in items if t]
        body = "\n".join("- " + t for t in lines[-12:])
        return ("[会话要点] 来源 chat_id=%s | 生成方式=idle_flush 规则提取 | 时间=%s\n\n"
                % (chat_id, time.strftime("%Y-%m-%d %H:%M")) + body)

    # ---------- 触发判定 ----------

    def candidates(self, now: float | None = None) -> list[tuple[str, dict[str, Any]]]:
        now = float(now or time.time())
        today = time.strftime("%Y-%m-%d", time.localtime(now))
        out = []
        for chat_id, item in list(self.state.items()):
            if int(item.get("pending", 0)) < self.min_messages:
                continue
            if int(item.get("meaningful_pending", 0)) < 1:
                continue
            if now - float(item.get("last_ts", 0.0)) < self.idle_seconds:
                continue
            if item.get("last_flush_day") != today:
                item["flushes_today"] = 0
            if int(item.get("flushes_today", 0)) >= self.daily_cap:
                continue
            out.append((chat_id, item))
        return out

    def mark_flushed(self, chat_id: str, ok: bool) -> None:
        item = self.state.get(chat_id)
        if not item:
            return
        now = time.time()
        item["last_flush_ts"] = now
        if ok:
            item["pending"] = 0
            item["meaningful_pending"] = 0
            item["last_flush_day"] = time.strftime("%Y-%m-%d")
            item["flushes_today"] = int(item.get("flushes_today", 0)) + 1
            self.recent.pop(chat_id, None)      # 已写回，清空待摘要缓冲
        self._save()


def _post_paste(content: str, name: str, *, chat_id: str = "",
                webui_base: str = DEFAULT_WEBUI,
                token_file: str = DEFAULT_TOKEN_FILE) -> dict[str, Any]:
    """调用 WebUI 记忆导入接口（主进程内执行，可靠落库）。

    传 chat_id 时以 scope_type=chat 导入：落库段落带 chat 作用域元数据，
    检索时只在该会话作用域内可见（E-31，不会跨会话泄漏）。
    """
    try:
        token = str(json.loads(Path(token_file).read_text(encoding="utf-8")).get("access_token", ""))
    except Exception:
        return {"success": False, "detail": "webui token 读取失败"}
    if not token:
        return {"success": False, "detail": "webui token 为空"}
    payload: dict[str, Any] = {"content": content, "name": name, "input_mode": "text"}
    if chat_id:
        payload["chat_id"] = chat_id
        payload["scope_type"] = "chat"
    req = urllib.request.Request(
        webui_base.rstrip("/") + "/api/webui/memory/import/paste",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Cookie": "maibot_session=" + token,   # 鉴权在 Cookie，Authorization 无效
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "detail": str(exc)}


async def flush_one(chat_id: str, flapper: IdleFlush) -> bool:
    """对单个会话做一次空闲收尾写回。

    经 WebUI paste 以 **scope_type=chat** 导入（chat_id 会话作用域）：
    - 立即落库（主进程通道，无启动队列延迟）
    - 检索时仅该会话可见（E-31：不会跨会话泄漏到别的群/私聊 Prompt）
    """
    digest = flapper.build_digest(chat_id)
    if not digest.strip():
        LOGGER.info("[IdleFlush] 会话 %s 无有意义内容，跳过收尾", chat_id)
        return False

    name = "idle_flush_%s_%s" % (chat_id[:16], int(time.time()))
    result = await asyncio.to_thread(_post_paste, digest, name, chat_id=chat_id)
    ok = bool(result.get("success"))
    if ok:
        # checkpoint 语义（C-20）：写回成功即消费掉这批消息，重启/重试不重复写同一窗口
        flapper.recent.pop(chat_id, None)
        item = flapper.state.get(chat_id)
        if item is not None:
            item["pending"] = 0
            item["meaningful_pending"] = 0
            item["last_flush_ts"] = time.time()
            item["last_flush_day"] = time.strftime("%Y-%m-%d")
            item["flushes_today"] = int(item.get("flushes_today", 0)) + 1
        flapper._save()
        LOGGER.info("[IdleFlush] 收尾写回成功 chat=%s（会话作用域，checkpoint 已推进）", chat_id)
    else:
        LOGGER.warning("[IdleFlush] 收尾写回失败 chat=%s detail=%s", chat_id,
                       str(result.get("detail", ""))[:80])
    return ok


async def run_loop(flapper: IdleFlush, interval: float = 60.0, stop_event: asyncio.Event | None = None) -> None:
    """后台循环：只在会话空闲达标后触发，不做逐条扫描。"""
    while True:
        try:
            for chat_id, _item in flapper.candidates():
                ok = await flush_one(chat_id, flapper)
                flapper.mark_flushed(chat_id, ok)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[IdleFlush] 扫描失败: %s", exc)
        if stop_event is not None:
            try:
                await asyncio.wait_for(stop_event.wait(), timeout=interval)
                return
            except asyncio.TimeoutError:
                pass
        else:
            await asyncio.sleep(interval)
