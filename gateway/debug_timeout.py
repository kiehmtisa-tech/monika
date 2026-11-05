#!/usr/bin/env python3
"""调试: 网关对 hang 上游的超时行为。"""
import json, os, sys, tempfile, threading, time
from pathlib import Path

TMP = Path(tempfile.mkdtemp(prefix="gwdbg-"))
(TMP / "model_config.toml").write_text("""
[[models]]
model_type = "llm"
model_identifier = "test-model"
name = "test-model"
api_provider = "Mock"
price_in = 10.0
cache_price_in = 1.0
price_out = 20.0
price_periods = []
[[api_providers]]
name = "Mock"
base_url = "http://127.0.0.1:9201/v1"
api_key = "X"
client_type = "openai"
""", "utf-8")
(TMP / "secrets.json").write_text(json.dumps({
    "gateway_keys": {"gw-t": "m"}, "admin_key": "a",
    "provider_keys": {"Mock": "up-secret"}}))
(TMP / "budget.json").write_text(json.dumps({"daily": {"test-model": 1.0}}))
(TMP / "settings.json").write_text("{}")

os.environ.update({
    "GW_MODEL_CONFIG": str(TMP / "model_config.toml"),
    "GW_SECRETS": str(TMP / "secrets.json"),
    "GW_BUDGET": str(TMP / "budget.json"),
    "GW_DB": str(TMP / "gateway.db"),
    "GW_STATUS": str(TMP / "status.json"),
    "GW_SETTINGS": str(TMP / "settings.json"),
    "GW_PORT": "9200",
    "GW_UPSTREAM_READ": "4",
    "GW_UPSTREAM_DEADLINE": "10",
})
sys.path.insert(0, "/app")
import app as gw
import httpx, uvicorn
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse

mock = FastAPI()

@mock.post("/v1/chat/completions")
async def hang(request: Request):
    import asyncio
    await asyncio.sleep(8)
    return JSONResponse(content={"id": "late", "usage": {"prompt_tokens": 1, "completion_tokens": 1}})

def run(obj, port):
    server = uvicorn.Server(uvicorn.Config(obj, host="127.0.0.1", port=port, log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()

run(mock, 9201)
run(gw.app, 9200)
for _ in range(50):
    try:
        if httpx.get("http://127.0.0.1:9200/healthz", timeout=1).status_code == 200:
            break
    except Exception:
        time.sleep(0.2)

print("read timeout configured:", gw.UPSTREAM_READ_TIMEOUT)
t0 = time.time()
try:
    r = httpx.post("http://127.0.0.1:9200/v1/chat/completions",
                   json={"model": "test-model", "messages": [{"role": "user", "content": "hi"}], "max_tokens": 10},
                   headers={"Authorization": "Bearer gw-t"}, timeout=30)
    print(f"status={r.status_code} elapsed={time.time()-t0:.2f} body={r.text[:150]}")
except Exception as e:
    print(f"client exception elapsed={time.time()-t0:.2f}: {e!r}")
time.sleep(1)
import sqlite3
conn = sqlite3.connect(str(TMP / "gateway.db"))
print("reservations:", conn.execute("SELECT state, outcome FROM reservations").fetchall())
print("settlements:", conn.execute("SELECT day, cost, outcome FROM settlements").fetchall())
conn.close()
