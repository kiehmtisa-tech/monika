"""向 MaiBot 核心注入一条模拟私聊消息 (E2E 测试)。在 core 容器内运行。"""
import asyncio
import json
import sys
import time

import websockets

URI = "ws://127.0.0.1:8000/ws"
TEXT = sys.argv[1] if len(sys.argv) > 1 else "/piano"

payload = {
    "message_info": {
        "platform": "qq",
        "message_id": int(time.time() * 1000) % 100000000,
        "time": int(time.time()),
        "user_info": {
            "platform": "qq",
            "user_id": "100000001",
            "user_nickname": "dragon-test",
            "user_cardname": "",
        },
        "group_info": None,
    },
    "message_segment": {"type": "text", "data": TEXT},
    "raw_message": TEXT,
    "account_id": "100000002",
    "scope": None,
}


async def main():
    async with websockets.connect(URI, open_timeout=10, close_timeout=5) as ws:
        await ws.send(json.dumps(payload, ensure_ascii=False))
        print("injected:", TEXT)
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=6)
                print("recv:", raw[:200])
        except asyncio.TimeoutError:
            print("(no more frames)")


asyncio.run(main())
