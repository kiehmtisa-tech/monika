#!/usr/bin/env python3
"""人格回归测试辅助：注入一条私聊消息，统计最终 QQ 气泡数与回复内容。

用法: python3 e2e_probe.py "消息文本"
统计口径:
  - 气泡数 = 注入后 90s 内 send_service 的 info 发送记录条数（真实发到 QQ 的消息）
  - 同时输出对应 replyer 的模型输出，便于区分"拆分"与"多次生成"
"""
import glob
import json
import os
import subprocess
import sys
import time

LOG_DIR = "/opt/qq-fish/data/MaiMBot/logs"
PROMPT_DIR = LOG_DIR + "/maisaka_prompt/replyer"


def active_log() -> str:
    files = sorted(glob.glob(LOG_DIR + "/app_*.jsonl"), key=os.path.getmtime)
    return files[-1]


def inject(text: str) -> None:
    subprocess.run(
        ["docker", "exec", "maim-bot-core", "/MaiMBot/.venv/bin/python", "/tmp/inject_msg.py", text],
        check=True, capture_output=True, text=True,
    )


def main() -> None:
    text = sys.argv[1]
    log = active_log()
    offset = os.path.getsize(log)
    started = time.time()
    inject(text)
    print(f"[inject] {text}")

    sends: list[str] = []
    while time.time() - started < 100:
        with open(log, encoding="utf-8", errors="replace") as fh:
            fh.seek(offset)
            chunk = fh.read()
            offset = fh.tell()
        for line in chunk.splitlines():
            try:
                d = json.loads(line)
            except Exception:
                continue
            ev = str(d.get("event", ""))
            if d.get("logger_name") == "send_service" and "Monika:" in ev and d.get("level") == "info":
                sends.append(f"{d.get('timestamp','')[6:]} {ev.split('Monika:',1)[1].strip()}")
        if sends and time.time() - started > 12:
            break
        time.sleep(2)

    print(f"[气泡数] {len(sends)}")
    for s in sends:
        print("   -", s)

    dumps = [p for p in glob.glob(PROMPT_DIR + "/*/*.json") if os.path.getmtime(p) >= started - 1]
    dumps.sort(key=os.path.getmtime)
    if not dumps:
        print("[replyer] 本轮没有新的回复生成（planner 未产出 reply 或上游超时）")
        return
    if True:
        d = json.load(open(dumps[-1], encoding="utf-8"))
        m = d["metadata"]
        out = " | ".join(
            p.get("text", "")
            for it in d.get("output_items") or []
            for p in it.get("parts", [])
            if p.get("type") == "text"
        )
        print(f"[replyer] tok={m.get('prompt_tokens')}/{m.get('completion_tokens')} {m.get('duration_ms')}ms")
        print("   out:", out.replace("\n", " ")[:300])


if __name__ == "__main__":
    main()
