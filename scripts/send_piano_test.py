#!/usr/bin/env python3
"""P1 验证: 通过 NapCat OneBot WS 发送 record 音频到私聊。
在 maim-bot-core 容器内运行（与 napcat 同在 maim_bot 网络）。
用法: python3 send_piano_test.py [user_id] [file_path_in_napcat]
"""
import asyncio
import json
import os
import sys

import websockets

NAPCAT_WS = "ws://maim-bot-napcat:3001"
def _load_token() -> str:
    """从环境变量或 .napcat_token 读取 WS token，不再硬编码。"""
    env_token = (os.environ.get("NAPCAT_WS_TOKEN") or "").strip()
    if env_token:
        return env_token
    candidates = [(os.environ.get("NAPCAT_TOKEN_FILE") or "").strip(),
                  "/opt/qq-fish/.napcat_token",
                  "/MaiMBot/data/.napcat_token"]
    for path in candidates:
        if not path:
            continue
        try:
            with open(path, encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            continue
        if value:
            return value
    return ""


TOKEN = _load_token()
DEFAULT_USER = int(os.environ["TEST_USER_QQ"]) if os.environ.get("TEST_USER_QQ", "").strip().isdigit() else 0
DEFAULT_FILE = "file:///MaiMBot/data/piano/piece_1842_melancholy.mp3"


async def main():
    user_id = int(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_USER
    audio = sys.argv[2] if len(sys.argv) > 2 else DEFAULT_FILE
    if not TOKEN:
        print("缺少 WS token：设置 NAPCAT_WS_TOKEN 环境变量，或提供 .napcat_token 文件")
        return
    if not user_id:
        print("缺少目标 QQ：用第一个参数指定，或设置 TEST_USER_QQ")
        return
    payload = {
        "action": "send_private_msg",
        "params": {
            "user_id": user_id,
            "message": [{"type": "record", "data": {"file": audio}}],
        },
        "echo": "piano-p1-test",
    }
    async with websockets.connect(
        NAPCAT_WS,
        additional_headers={"Authorization": f"Bearer {TOKEN}"},
        open_timeout=10,
        close_timeout=5,
    ) as ws:
        await ws.send(json.dumps(payload))
        # 收事件流，等 echo 匹配的响应（最多 60s，语音转换可能较慢）
        while True:
            try:
                raw = await asyncio.wait_for(ws.recv(), timeout=60)
            except asyncio.TimeoutError:
                print(json.dumps({"ok": False, "error": "timeout_no_echo"}, ensure_ascii=False))
                return
            data = json.loads(raw)
            if data.get("echo") == "piano-p1-test":
                print(json.dumps(data, ensure_ascii=False))
                return
            # 其他帧是事件推送，忽略


if __name__ == "__main__":
    asyncio.run(main())
