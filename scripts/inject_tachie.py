# -*- coding: utf-8 -*-
"""把 persona/Tachie 的立绘图片以「图 + 情绪说明」消息注入 bot，
走聊天管线自动写入图像记忆（asset + occurrence + user_statement 观察 + 嵌入任务）。
在 core 容器内运行：python3 /tmp/inject_tachie.py [normal|shine|nanimo|shock|all]
"""
import asyncio
import base64
import json
import sys
import time
from pathlib import Path

import websockets

URI = "ws://127.0.0.1:8000/ws"
TACHIE_DIR = Path("/MaiMBot/persona/Tachie")

CAPTIONS = {
    "normal": "这是你的立绘（tachie）：normal 日常温和版。橙发绿瞳、白色发卡、文学部制服的你。记住这个形象，以后看到类似的图要能认出是自己的立绘。",
    "shine": "这是你的立绘（tachie）：shine 开心祝贺版，笑得眼睛弯起来的你。记住这个形象。",
    "nanimo": "这是你的立绘（tachie）：nanimo 无奈摊手版，拿我没辙时的你。记住这个形象。",
    "shock": "这是你的立绘（tachie）：shock 夸张震惊版，被吓到时的你。记住这个形象。",
}


def build_payload(name: str) -> dict:
    img_bytes = (TACHIE_DIR / f"{name}.jfif").read_bytes()
    seglist = [
        {"type": "image", "data": base64.b64encode(img_bytes).decode("ascii")},
        {"type": "text", "data": CAPTIONS[name]},
    ]
    return {
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
        "raw_message": CAPTIONS[name],
        "account_id": "100000002",
        "scope": None,
    }


async def send_one(name: str) -> None:
    payload = build_payload(name)
    async with websockets.connect(URI, open_timeout=10, close_timeout=5, max_size=50_000_000) as ws:
        await ws.send(json.dumps(payload, ensure_ascii=False))
        print(f"[{name}] injected ({len(payload['message_segment']['data'][0]['data'])} b64 chars)")
        try:
            while True:
                raw = await asyncio.wait_for(ws.recv(), timeout=8)
                print(f"[{name}] recv: {raw[:120]}")
        except asyncio.TimeoutError:
            print(f"[{name}] (done)")


async def main() -> None:
    target = sys.argv[1] if len(sys.argv) > 1 else "all"
    names = list(CAPTIONS) if target == "all" else [target]
    for name in names:
        assert (TACHIE_DIR / f"{name}.jfif").exists(), f"缺少立绘 {name}"
        await send_one(name)
        if name != names[-1]:
            await asyncio.sleep(10)  # 间隔发送，让管线逐条消化


asyncio.run(main())
