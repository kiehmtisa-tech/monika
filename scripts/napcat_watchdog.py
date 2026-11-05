#!/usr/bin/env python3
"""NapCat 假在线看门狗 v2。

假在线的特征：容器、端口、TCP 连接都正常，但 QQ 登录态失效，
Docker 的 restart:always 和端口 healthcheck 都发现不了。
本脚本用 OneBot 的 get_login_info（带 echo 精确匹配）主动探活。

v2 新增两道保护，防止误杀：
1. 启动宽限：容器启动不足 STARTUP_GRACE 秒直接跳过（重建/启动期 3001 未就绪）
2. 重启冷却：COOLDOWN 秒内不重复重启，避免重启风暴

用法：python3 napcat_watchdog.py [--dry-run]
退出码：0 正常/跳过 / 1 容器不可用 / 2 已触发重启 / 3 冷却中
"""

import asyncio
import json
import os
import subprocess
import sys
import time
from datetime import datetime, timezone

CONTAINER = "maim-bot-napcat"
def _load_ws_token() -> str:
    """从 .napcat_token 读取 WS token，不再硬编码。

    优先 NAPCAT_TOKEN_FILE 指定的文件，其次项目根的 .napcat_token。
    """
    candidates = []
    env_path = (os.environ.get("NAPCAT_TOKEN_FILE") or "").strip()
    if env_path:
        candidates.append(env_path)
    here = os.path.dirname(os.path.abspath(__file__))
    candidates.append("/opt/qq-fish/.napcat_token")
    candidates.append(os.path.join(here, "..", ".napcat_token"))
    for path in candidates:
        try:
            with open(path, encoding="utf-8") as fh:
                value = fh.read().strip()
        except OSError:
            continue
        if value:
            return value
    return ""


WS_TOKEN = _load_ws_token()
DOCKER = "/usr/bin/docker"
LOG = "/opt/qq-fish/logs/napcat_watchdog.log"
STATE = "/opt/qq-fish/logs/napcat_watchdog.state.json"

MAX_ATTEMPTS = 2
RETRY_GAP = 3
RECV_TIMEOUT = 6
STARTUP_GRACE = 180
COOLDOWN = 600
DRY_RUN = "--dry-run" in sys.argv


def log(msg: str) -> None:
    line = "[%s] %s" % (datetime.now().strftime("%Y-%m-%d %H:%M:%S"), msg)
    print(line)
    try:
        os.makedirs(os.path.dirname(LOG), exist_ok=True)
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception as exc:
        print("log write failed: %s" % exc)


def sh(args):
    return subprocess.run(args, capture_output=True, text=True)


def container_ip() -> str:
    r = sh([DOCKER, "inspect", "-f", "{{range .NetworkSettings.Networks}}{{.IPAddress}} {{end}}", CONTAINER])
    if r.returncode != 0:
        return ""
    parts = [p for p in r.stdout.split() if p]
    return parts[0] if parts else ""


def uptime_seconds() -> float:
    r = sh([DOCKER, "inspect", "-f", "{{.State.StartedAt}}", CONTAINER])
    raw = (r.stdout or "").strip()[:19]
    if not raw:
        return -1.0
    try:
        started = datetime.strptime(raw, "%Y-%m-%dT%H:%M:%S").replace(tzinfo=timezone.utc)
        return (datetime.now(timezone.utc) - started).total_seconds()
    except Exception:
        return -1.0


def load_state() -> dict:
    try:
        with open(STATE, encoding="utf-8") as f:
            return json.load(f)
    except Exception:
        return {}


def save_state(d: dict) -> None:
    try:
        os.makedirs(os.path.dirname(STATE), exist_ok=True)
        with open(STATE, "w", encoding="utf-8") as f:
            json.dump(d, f)
    except Exception as exc:
        print("state write failed: %s" % exc)


async def probe(uri: str):
    import websockets

    echo = "watchdog-%d" % int(time.time() * 1000)
    async with websockets.connect(uri, open_timeout=5, close_timeout=2) as ws:
        await ws.send(json.dumps({"action": "get_login_info", "params": {}, "echo": echo}))
        deadline = time.monotonic() + RECV_TIMEOUT
        while time.monotonic() < deadline:
            raw = await asyncio.wait_for(ws.recv(), timeout=RECV_TIMEOUT)
            try:
                msg = json.loads(raw)
            except Exception:
                continue
            if msg.get("echo") != echo:
                continue
            data = msg.get("data") or {}
            if msg.get("retcode") == 0 and data.get("user_id"):
                return True, "uid=%s nick=%s" % (data.get("user_id"), data.get("nickname"))
            return False, "retcode=%s data=%s" % (msg.get("retcode"), data)
    return False, "no matching echo in %ss" % RECV_TIMEOUT


def main() -> int:
    up = uptime_seconds()
    if 0 <= up < STARTUP_GRACE:
        log("SKIP container just started (uptime=%.0fs < %ds)" % (up, STARTUP_GRACE))
        return 0

    ip = container_ip()
    if not ip:
        log("FAIL container ip unavailable -> docker start")
        if not DRY_RUN:
            sh([DOCKER, "start", CONTAINER])
        return 1

    uri = "ws://%s:3001?access_token=%s" % (ip, WS_TOKEN)
    last_error = ""
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            ok, detail = asyncio.run(probe(uri))
            if ok:
                log("OK %s" % detail)
                return 0
            last_error = detail
        except Exception as exc:
            last_error = "%s: %s" % (type(exc).__name__, exc)
        if attempt < MAX_ATTEMPTS:
            time.sleep(RETRY_GAP)

    st = load_state()
    since = time.time() - float(st.get("last_restart", 0) or 0)
    if since < COOLDOWN:
        log("UNHEALTHY %s -> SKIP restart, cooldown %.0fs left" % (last_error, COOLDOWN - since))
        return 3

    log("UNHEALTHY %s -> %s" % (last_error, "dry-run, skip restart" if DRY_RUN else "restarting " + CONTAINER))
    if DRY_RUN:
        return 2

    started = time.time()
    r = sh([DOCKER, "restart", CONTAINER])
    st["last_restart"] = time.time()
    st["last_error"] = last_error
    save_state(st)
    log("restart rc=%s elapsed=%.1fs" % (r.returncode, time.time() - started))
    return 2


if __name__ == "__main__":
    sys.exit(main())
