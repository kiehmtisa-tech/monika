# -*- coding: utf-8 -*-
"""E2E：重发同一张立绘图片并提问，验证 bot 能通过图像记忆认出自己的立绘。
在 core 容器内运行：python3 /tmp/inject_tachie_ask.py
"""
import asyncio
import base64
import json
import time
from pathlib import Path

import websockets

URI = "ws://127.0.0.1:8000/ws"
IMG = Path("/MaiMBot/persona/Tachie/normal.jfif")
QUESTION = "这张图是什么？你认识图里的人吗？"


async def main() -> None:
    seglist = [
        {"type": "image", "data": base64.b64encode(IMG.read_bytes()).decode("ascii")},
        {"type": "text", "data": QUESTION},
    ]
    payload = {
        "message_info": {
            "platform": "qq",
            "message_id": int(time.time() * 1000) % 100000000,
            "time": int(time.time()),
            "user_info": {
                "platform": "qq",
                "user_id": "100000001",
                "user_nickname": "（  丨丨）",
                "user_cardname": "",
            },
            "group_info": None,
        },
        "message_segment": {"type": "seglist", "data": seglist},
        "raw_message": QUESTION,
        "account_id": "100000002",
        "scope": None,
    }
    async with websockets.connect(URI, open_timeout=10, close_timeout=5, max_size=50_000_000) as ws:
        await ws.send(json.dumps(payload, ensure_ascii=False))
        print("injected:", QUESTION)
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=10)
                print("recv:", raw[:150])
        except asyncio.TimeoutError:
            print("(done)")


asyncio.run(main())
