#!/usr/bin/env python3
"""
qq-fish 预算网关测试套件（在网关镜像内运行）

运行方式:
  docker run --rm \
    -v /opt/qq-fish/gateway:/gw \
    -v /opt/qq-fish/docker-config/mmc:/config:ro \
    qq-fish-budget-gateway:1.0 python3 /gw/test_gateway.py
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import tempfile
import threading
import time
from pathlib import Path

# ---------------------------------------------------------------------------
# 1. 准备临时配置（必须在 import app 之前设置环境变量）
# ---------------------------------------------------------------------------

TMP = Path(tempfile.mkdtemp(prefix="gwtest-"))
MODEL_CONFIG = TMP / "model_config.toml"
SECRETS = TMP / "secrets.json"
BUDGET = TMP / "budget.json"
BEHAVIOR = TMP / "behavior.json"
SETTINGS = TMP / "settings.json"
DB = TMP / "gateway.db"
STATUS = TMP / "gateway_status.json"

MODEL_CONFIG.write_text(
    """
[[models]]
model_type = "llm"
model_identifier = "test-model"
name = "test-model"
api_provider = "Mock"
price_in = 10.0
cache_price_in = 1.0
price_out = 20.0
price_periods = []
send_temperature = true
force_stream_mode = false
visual = true
extra_params = {}

[[models]]
model_type = "embedding"
model_identifier = "test-embed"
name = "test-embed"
api_provider = "Mock"
price_in = 5.0
cache_price_in = 0.0
price_out = 0.0
price_periods = []
send_temperature = false
force_stream_mode = false
visual = false
extra_params = {}

[[models]]
model_type = "llm"
model_identifier = "test-model-gw"
name = "test-model-gw"
api_provider = "Mock-GW"
price_in = 10.0
cache_price_in = 1.0
price_out = 20.0
price_periods = []
send_temperature = true
force_stream_mode = false
visual = true
extra_params = {}

[[api_providers]]
name = "Mock"
base_url = "http://127.0.0.1:9101/v1"
api_key = "SHOULD-NOT-BE-USED-FROM-CONFIG"
client_type = "openai"
auth_type = "bearer"
max_retry = 2
timeout = 60
retry_interval = 4

# 灰度 provider：base_url 指向网关自身（防自环验证：网关必须回落到 Mock）
[[api_providers]]
name = "Mock-GW"
base_url = "http://127.0.0.1:9100/v1"
api_key = "SHOULD-NOT-BE-USED-FROM-CONFIG"
client_type = "openai"
auth_type = "bearer"
max_retry = 2
timeout = 60
retry_interval = 4
""",
    "utf-8",
)
SECRETS.write_text(
    json.dumps(
        {
            "gateway_keys": {"gw-testkey": "maibot"},
            "admin_key": "adm-testkey",
            "provider_keys": {"Mock": "upstream-secret-key"},
        }
    ),
    "utf-8",
)
BUDGET.write_text(json.dumps({"daily": {"test-model": 0.05, "test-embed": 0.01}}), "utf-8")
BEHAVIOR.write_text(json.dumps({"mention_reserve_ratio": 0.25}), "utf-8")
SETTINGS.write_text(json.dumps({"upstream_overrides": {}}), "utf-8")

os.environ.update(
    {
        "GW_MODEL_CONFIG": str(MODEL_CONFIG),
        "GW_SECRETS": str(SECRETS),
        "GW_BUDGET": str(BUDGET),
        "GW_BEHAVIOR": str(BEHAVIOR),
        "GW_DB": str(DB),
        "GW_STATUS": str(STATUS),
        "GW_SETTINGS": str(SETTINGS),
        "GW_PORT": "9100",
        "GW_UPSTREAM_READ": "4",
        "GW_UPSTREAM_DEADLINE": "10",
        "TZ": "Asia/Shanghai",
    }
)

sys.path.insert(0, "/app")
import app as gw  # noqa: E402  (环境变量就绪后才导入)

import httpx  # noqa: E402
import uvicorn  # noqa: E402
from fastapi import FastAPI, Request  # noqa: E402
from fastapi.responses import JSONResponse, StreamingResponse  # noqa: E402

# ---------------------------------------------------------------------------
# 2. Mock 上游
# ---------------------------------------------------------------------------

MOCK_COUNTER = {"requests": 0}
mock = FastAPI()


def _usage_from(body):
    if isinstance(body.get("x_test_usage"), dict):
        return body["x_test_usage"]
    return {"prompt_tokens": 100, "completion_tokens": 50, "total_tokens": 150}


@mock.post("/v1/chat/completions")
async def mock_chat(request: Request):
    body = await request.json()
    MOCK_COUNTER["requests"] += 1
    auth = request.headers.get("authorization") or ""
    mode = body.get("x_mode", "ok")
    stream = bool(body.get("stream"))

    # 上游鉴权必须来自 secrets（绝不允许 model_config 里的 key 泄漏到上游）
    assert auth == "Bearer upstream-secret-key", f"upstream auth wrong: {auth}"

    if mode == "http500":
        return JSONResponse(status_code=500, content={"error": {"message": "boom"}})

    if mode == "hang":
        import asyncio
        await asyncio.sleep(8)
        return JSONResponse(content={"id": "late", "usage": _usage_from(body)})

    if mode == "reject_stream_options" and "stream_options" in body:
        return JSONResponse(
            status_code=400,
            content={"error": {"message": "Unknown parameter: 'stream_options' is not supported"}},
        )

    if mode == "echo":
        has_image = False
        for msg in body.get("messages") or []:
            c = msg.get("content")
            if isinstance(c, list):
                for p in c:
                    if isinstance(p, dict) and (
                        p.get("type") in ("image_url", "input_image") or "image_url" in p
                    ):
                        has_image = True
        return JSONResponse(
            content={
                "id": "echo",
                "echo": {
                    "has_image": has_image,
                    "has_tools": bool(body.get("tools")),
                    "stream_options": body.get("stream_options"),
                    "model": body.get("model"),
                },
                "usage": _usage_from(body),
            }
        )

    if mode == "tool":
        return JSONResponse(
            content={
                "id": "tool",
                "choices": [{
                    "index": 0,
                    "finish_reason": "tool_calls",
                    "message": {
                        "role": "assistant",
                        "content": None,
                        "tool_calls": [{
                            "id": "call_1",
                            "type": "function",
                            "function": {"name": "get_weather",
                                         "arguments": "{\"city\":\"BJ\"}"},
                        }],
                    },
                }],
                "usage": _usage_from(body),
            }
        )

    if stream:
        async def gen():
            yield b'data: {"id":"s1","choices":[{"index":0,"delta":{"role":"assistant","content":"hel"}}]}\n\n'
            yield b'data: {"id":"s1","choices":[{"index":0,"delta":{"content":"lo"}}]}\n\n'
            if not body.get("x_no_usage"):
                u = json.dumps({"usage": _usage_from(body)}).encode()
                yield b"data: " + u + b"\n\n"
            yield b"data: [DONE]\n\n"
        if body.get("x_gzip"):
            # 模拟上游 gzip 压缩 SSE（DeepSeek 行为）：单个 gzip 成员，网关必须解压后再转发
            import gzip
            parts = [
                b'data: {"id":"s1","choices":[{"index":0,"delta":{"content":"hi"}}]}\n\n',
                b'data: ' + json.dumps({"usage": _usage_from(body)}).encode() + b'\n\n',
                b'data: [DONE]\n\n',
            ]
            return StreamingResponse(
                [gzip.compress(b"".join(parts))],
                media_type="text/event-stream",
                headers={"content-encoding": "gzip"},
            )
        return StreamingResponse(gen(), media_type="text/event-stream")

    return JSONResponse(
        content={
            "id": "ok",
            "choices": [{"index": 0, "finish_reason": "stop",
                         "message": {"role": "assistant", "content": "pong"}}],
            "usage": _usage_from(body),
        }
    )


@mock.post("/v1/embeddings")
async def mock_embed(request: Request):
    body = await request.json()
    MOCK_COUNTER["requests"] += 1
    auth = request.headers.get("authorization") or ""
    assert auth == "Bearer upstream-secret-key", f"upstream auth wrong: {auth}"
    return JSONResponse(content={"data": [], "usage": _usage_from(body)})


# ---------------------------------------------------------------------------
# 3. 启动服务
# ---------------------------------------------------------------------------

def start_server(obj, port):
    cfg = uvicorn.Config(obj, host="127.0.0.1", port=port, log_level="warning")
    server = uvicorn.Server(cfg)
    threading.Thread(target=server.run, daemon=True).start()
    return server


start_server(mock, 9101)
start_server(gw.app, 9100)

deadline = time.time() + 20
while time.time() < deadline:
    try:
        r = httpx.get("http://127.0.0.1:9100/healthz", timeout=1)
        if r.status_code == 200:
            break
    except Exception:
        time.sleep(0.2)
else:
    print("FATAL: gateway did not start")
    sys.exit(2)

C = httpx.Client(base_url="http://127.0.0.1:9100", timeout=20)
AUTH = {"Authorization": "Bearer gw-testkey"}
GOOD_BODY = {
    "model": "test-model",
    "messages": [{"role": "user", "content": "hello"}],
    "max_tokens": 100,
    "temperature": 0.5,
}

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(detail) if detail and not cond else ""))


def q(sql, args=()):
    conn = sqlite3.connect(str(DB), timeout=10)
    try:
        cur = conn.execute(sql, args)
        conn.commit()  # DML 也需提交
        return cur.fetchall()
    finally:
        conn.close()


def reset_ledger(budget=None):
    q("DELETE FROM reservations")
    q("DELETE FROM settlements")
    if budget is not None:
        set_budget(budget)


def set_budget(d):
    BUDGET.write_text(json.dumps({"daily": d}), "utf-8")
    os.utime(BUDGET, None)
    time.sleep(0.1)


def set_behavior(d):
    BEHAVIOR.write_text(json.dumps(d), "utf-8")
    os.utime(BEHAVIOR, None)
    time.sleep(0.1)


# ---------------------------------------------------------------------------
# 4. 测试用例
# ---------------------------------------------------------------------------

def t_auth():
    r = C.post("/v1/chat/completions", json=GOOD_BODY)
    check("auth: 缺少 key -> 401", r.status_code == 401, r.status_code)
    r = C.post("/v1/chat/completions", json=GOOD_BODY,
               headers={"Authorization": "Bearer wrong"})
    check("auth: 错误 key -> 401", r.status_code == 401, r.status_code)
    r = C.post("/v1/chat/completions", json=GOOD_BODY, headers=AUTH)
    check("auth: 正确 key -> 200", r.status_code == 200, r.text[:200])


def t_unknown_model():
    body = dict(GOOD_BODY, model="no-such-model")
    n0 = MOCK_COUNTER["requests"]
    r = C.post("/v1/chat/completions", json=body, headers=AUTH)
    ok = r.status_code == 404 and r.json()["error"]["code"] == "MODEL_NOT_CONFIGURED"
    check("fail-closed: 未知模型 -> 404 MODEL_NOT_CONFIGURED", ok, r.text[:200])
    check("fail-closed: 未知模型不触达上游", MOCK_COUNTER["requests"] == n0)


def t_endpoint_allowlist():
    r = C.post("/v1/audio/transcriptions", json=GOOD_BODY, headers=AUTH)
    check("allowlist: 非法端点 -> 404", r.status_code == 404, r.status_code)
    r = C.get("/v1/models", headers=AUTH)
    ids = [m["id"] for m in r.json().get("data", [])]
    check("/v1/models 返回已配置模型", r.status_code == 200 and "test-model" in ids, r.text[:200])


def t_exact_settlement():
    usage = {"prompt_tokens": 1000, "completion_tokens": 500,
             "prompt_tokens_details": {"cached_tokens": 200}}
    body = dict(GOOD_BODY, x_test_usage=usage)
    r = C.post("/v1/chat/completions", json=body, headers=AUTH)
    check("结算: 非流式 200", r.status_code == 200, r.text[:200])
    rows = q("SELECT cost, est, outcome FROM settlements ORDER BY at DESC LIMIT 1")
    expected = (800 * 10 + 200 * 1 + 500 * 20) / 1e6  # 0.0182
    ok = rows and abs(rows[0][0] - expected) < 1e-9 and rows[0][1] == 0 and rows[0][2] == "ok"
    check("结算: 精确费用 0.0182（含缓存价）", ok, f"rows={rows} expected={expected}")
    res = q("SELECT state FROM reservations ORDER BY created DESC LIMIT 1")
    check("结算: 预留已 settled", res and res[0][0] == "settled", res)


def t_tool_vlm_passthrough():
    body = dict(
        GOOD_BODY, x_mode="tool",
        tools=[{"type": "function", "function": {
            "name": "get_weather",
            "parameters": {"type": "object", "properties": {"city": {"type": "string"}}}}}],
    )
    r = C.post("/v1/chat/completions", json=body, headers=AUTH)
    tc = r.json().get("choices", [{}])[0].get("message", {}).get("tool_calls")
    check("Tool Calls: 透传完整",
          r.status_code == 200 and tc and tc[0]["function"]["name"] == "get_weather",
          r.text[:200])

    body = dict(
        GOOD_BODY, x_mode="echo",
        messages=[{"role": "user", "content": [
            {"type": "text", "text": "看图"},
            {"type": "image_url", "image_url": {"url": "data:image/png;base64,iVBORw0KGgo="}},
        ]}],
    )
    r = C.post("/v1/chat/completions", json=body, headers=AUTH)
    check("VLM: 图片内容透传上游",
          r.status_code == 200 and r.json()["echo"]["has_image"] is True, r.text[:200])


def t_stream():
    set_budget({"test-model": 1.0, "test-embed": 0.01})  # 流式估算保守(4096 out)
    body = dict(GOOD_BODY, stream=True, x_test_usage={"prompt_tokens": 300,
                                                      "completion_tokens": 120})
    with C.stream("POST", "/v1/chat/completions", json=body, headers=AUTH) as r:
        raw = b"".join(r.iter_raw())
        status = r.status_code
    check("Streaming: SSE 透传", status == 200 and b"data: [DONE]" in raw, raw[:200])
    time.sleep(0.3)
    rows = q("SELECT cost, est, outcome FROM settlements ORDER BY at DESC LIMIT 1")
    expected = (300 * 10 + 120 * 20) / 1e6  # 0.0054
    ok = rows and abs(rows[0][0] - expected) < 1e-9 and rows[0][1] == 0
    check("Streaming: 按上游 usage 精确结算 0.0054", ok, f"rows={rows} expected={expected}")

    body2 = dict(GOOD_BODY, stream=True, x_no_usage=True)
    with C.stream("POST", "/v1/chat/completions", json=body2, headers=AUTH) as r:
        b"".join(r.iter_raw())
    time.sleep(0.3)
    rows = q("SELECT est, outcome FROM settlements ORDER BY at DESC LIMIT 1")
    check("Streaming: 无 usage 时按估算结算并标记",
          rows and rows[0][0] == 1 and "est" in rows[0][1], rows)


def t_gzip_stream():
    """上游 gzip 压缩的 SSE：网关必须解压后转发且能嗅探 usage。"""
    set_budget({"test-model": 1.0, "test-embed": 0.01})
    body = dict(GOOD_BODY, stream=True, x_gzip=True,
                x_test_usage={"prompt_tokens": 400, "completion_tokens": 80})
    with C.stream("POST", "/v1/chat/completions", json=body, headers=AUTH) as r:
        raw = b"".join(r.iter_raw())
        status = r.status_code
    check("gzip SSE: 客户端拿到明文 [DONE]",
          status == 200 and b"data: [DONE]" in raw and b"\x1f\x8b" not in raw,
          f"status={status} head={raw[:60]}")
    time.sleep(0.3)
    rows = q("SELECT cost, est FROM settlements ORDER BY at DESC LIMIT 1")
    expected = (400 * 10 + 80 * 20) / 1e6  # 0.0056
    check("gzip SSE: 嗅探到 usage 并精确结算",
          rows and rows[0][1] == 0 and abs(rows[0][0] - expected) < 1e-9,
          f"rows={rows} expected={expected}")


def t_stream_options_fallback():
    set_budget({"test-model": 1.0, "test-embed": 0.01})
    body = dict(GOOD_BODY, stream=True, x_mode="reject_stream_options")
    n0 = MOCK_COUNTER["requests"]
    with C.stream("POST", "/v1/chat/completions", json=body, headers=AUTH) as r:
        raw = b"".join(r.iter_raw())
        status = r.status_code
    check("stream_options 不被支持时自动降级重发", status == 200 and b"[DONE]" in raw,
          f"status={status} raw={raw[:200]}")
    check("降级重发恰好 2 次上游请求", MOCK_COUNTER["requests"] - n0 == 2,
          MOCK_COUNTER["requests"] - n0)


def t_budget_exceeded():
    set_budget({"test-model": 0.0005, "test-embed": 0.01})  # 低于单次估算(约0.002)
    n0 = MOCK_COUNTER["requests"]
    r = C.post("/v1/chat/completions", json=GOOD_BODY, headers=AUTH)
    err = r.json().get("error", {})
    ok = (r.status_code == 402 and err.get("code") == "BUDGET_EXCEEDED"
          and "BUDGET_EXCEEDED" in err.get("message", ""))
    check("预算耗尽 -> 402 BUDGET_EXCEEDED（与普通错误区分）", ok, r.text[:300])
    time.sleep(0.3)
    check("预算耗尽不触达上游", MOCK_COUNTER["requests"] == n0, MOCK_COUNTER["requests"] - n0)
    rows = q("SELECT COUNT(*) FROM reservations WHERE state='reserved'")
    check("预算耗尽不留残留预留", rows[0][0] == 0, rows)
    set_budget({"test-model": 0.05, "test-embed": 0.01})


def t_budget_not_configured():
    set_budget({"test-embed": 0.01})  # 移除 test-model
    r = C.post("/v1/chat/completions", json=GOOD_BODY, headers=AUTH)
    ok = r.status_code == 503 and r.json()["error"]["code"] == "GATEWAY_MISCONFIGURED"
    check("fail-closed: 预算未配置 -> 503 GATEWAY_MISCONFIGURED", ok, r.text[:200])
    set_budget({"test-model": 0.05, "test-embed": 0.01})


def t_secret_missing():
    old = json.loads(SECRETS.read_text("utf-8"))
    SECRETS.write_text(json.dumps({"gateway_keys": old["gateway_keys"],
                                   "admin_key": old["admin_key"],
                                   "provider_keys": {}}), "utf-8")
    os.utime(SECRETS, None)
    time.sleep(0.15)
    r = C.post("/v1/chat/completions", json=GOOD_BODY, headers=AUTH)
    ok = r.status_code == 503 and r.json()["error"]["code"] == "GATEWAY_MISCONFIGURED"
    check("fail-closed: 上游密钥缺失 -> 503", ok, r.text[:200])
    SECRETS.write_text(json.dumps(old), "utf-8")
    os.utime(SECRETS, None)
    time.sleep(0.15)


def t_upstream_500():
    reset_ledger({"test-model": 0.05, "test-embed": 0.01})
    before = q("SELECT COUNT(*) FROM settlements")[0][0]
    body = dict(GOOD_BODY, x_mode="http500")
    r = C.post("/v1/chat/completions", json=body, headers=AUTH)
    check("上游500: 原样透传状态", r.status_code == 500, r.status_code)
    time.sleep(0.2)
    res = q("SELECT state, outcome FROM reservations ORDER BY created DESC LIMIT 1")
    check("上游500: 预留已释放(不计费)", res and res[0][0] == "released", res)
    after = q("SELECT COUNT(*) FROM settlements")[0][0]
    check("上游500: 不产生结算", before == after, (before, after))


def t_upstream_timeout():
    reset_ledger({"test-model": 0.05, "test-embed": 0.01})
    body = dict(GOOD_BODY, x_mode="hang")
    t0 = time.time()
    try:
        r = C.post("/v1/chat/completions", json=body, headers=dict(AUTH), timeout=6)
        status = r.status_code
    except httpx.TimeoutException:
        status = "client-timeout"
    elapsed = time.time() - t0
    time.sleep(0.5)
    res = q("SELECT state, outcome FROM reservations ORDER BY created DESC LIMIT 1")
    ok_state = res and res[0][0] in ("released", "expired")
    check("上游超时: 网关 502 或客户端超时", status in (502, "client-timeout"), status)
    check("上游超时: 预留释放不计费", ok_state, res)
    check("上游超时: 读超时 4s 生效", elapsed < 6.5, elapsed)


def t_gw_provider_fallback():
    """*-GW provider：base_url 指向网关自身时必须回落原厂商地址与密钥（防自环、防密钥缺失）。"""
    set_budget({"test-model": 1.0, "test-model-gw": 1.0, "test-embed": 0.01})
    r = C.post("/v1/chat/completions", json=dict(GOOD_BODY, model="test-model-gw"),
               headers=AUTH, timeout=15)
    check("GW provider: 请求成功(未自环，且 mock 断言上游密钥正确)",
          r.status_code == 200, r.text[:200])
    rows = q("SELECT provider FROM reservations ORDER BY created DESC LIMIT 1")
    check("GW provider: 台账记录 *-GW provider 名", rows and rows[0][0] == "Mock-GW", rows)


def t_concurrency():
    reset_ledger({"test-model": 0.01, "test-embed": 0.01})
    results = []

    def worker():
        r = C.post("/v1/chat/completions", json=GOOD_BODY, headers=AUTH, timeout=30)
        results.append(r.status_code)

    threads = [threading.Thread(target=worker) for _ in range(10)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = sum(1 for s in results if s == 200)
    rejected = sum(1 for s in results if s == 402)
    check("并发HTTP: 10 并发按预算放行且有拒绝",
          accepted + rejected == 10 and rejected > 0,
          f"accepted={accepted} rejected={rejected} all={results}")
    reserved = q("SELECT COALESCE(SUM(est_cost),0) FROM reservations WHERE state='reserved'")[0][0]
    used = q("SELECT COALESCE(SUM(cost),0) FROM settlements WHERE day=?",
             (gw.today_str(),))[0][0]
    check("并发HTTP: 预留+已用 永不超预算", used + reserved <= 0.01 + 1e-9,
          f"used={used} reserved={reserved}")
    q("UPDATE reservations SET state='released', outcome='test_cleanup' WHERE state='reserved'")
    set_budget({"test-model": 1.0, "test-embed": 0.01})


def t_direct_reserve_transactions():
    """直接压 reserve()：BEGIN IMMEDIATE 事务下无超卖。"""
    reset_ledger({"test-model": 0.01, "test-embed": 0.01})
    est = 0.00204
    out = []
    barrier = threading.Barrier(16)

    def worker():
        barrier.wait()
        ok, rid, reason = gw.reserve("test-model", "Mock", est)
        out.append(ok)

    threads = [threading.Thread(target=worker) for _ in range(16)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    accepted = sum(1 for x in out if x)
    check("事务: 16 线程抢 0.01 预算 -> 恰好 4 个成功", accepted == 4, f"accepted={accepted}")
    total = q("SELECT COALESCE(SUM(est_cost),0) FROM reservations WHERE state='reserved'")[0][0]
    check("事务: 预留总和 <= 预算", total <= 0.01 + 1e-9, total)
    q("UPDATE reservations SET state='released', outcome='test_cleanup' WHERE state='reserved'")
    set_budget({"test-model": 0.05, "test-embed": 0.01})


def t_budget_change_keeps_used():
    """修改预算不清零已用。"""
    reset_ledger({"test-model": 0.05, "test-embed": 0.01})
    usage = {"prompt_tokens": 1000, "completion_tokens": 500}
    r = C.post("/v1/chat/completions", json=dict(GOOD_BODY, x_test_usage=usage), headers=AUTH)
    check("预算变更用例: 首次请求成功", r.status_code == 200, r.text[:200])
    used_before = q("SELECT SUM(cost) FROM settlements WHERE day=?", (gw.today_str(),))[0][0]
    set_budget({"test-model": 0.5, "test-embed": 0.01})  # 调大预算
    time.sleep(0.2)
    used_after = q("SELECT SUM(cost) FROM settlements WHERE day=?", (gw.today_str(),))[0][0]
    check("修改预算不清零已用", used_before and abs(used_before - used_after) < 1e-12,
          (used_before, used_after))
    r = C.post("/v1/chat/completions", json=GOOD_BODY, headers=AUTH)
    check("调大预算后恢复放行", r.status_code == 200, r.status_code)


def t_cross_day():
    """北京时间跨日：费用计入预留当天；新一天从 0 开始。"""
    reset_ledger({"test-model": 1.0, "test-embed": 0.01})

    real_now = gw.now_dt
    try:
        gw.now_dt = lambda: datetime_with(2026, 12, 31, 23, 59, 50)
        r = C.post("/v1/chat/completions",
                   json=dict(GOOD_BODY, x_test_usage={"prompt_tokens": 100,
                                                      "completion_tokens": 50}),
                   headers=AUTH)
        check("跨日: 跨年前请求成功", r.status_code == 200, r.text[:200])

        gw.now_dt = lambda: datetime_with(2027, 1, 1, 0, 0, 10)
        rows = q("SELECT day, cost FROM settlements ORDER BY at")
        check("跨日: 结算计入预留当天(2026-12-31)",
              rows and rows[0][0] == "2026-12-31", rows)
        day_rows = q("SELECT COALESCE(SUM(cost),0) FROM settlements WHERE day='2027-01-01'")
        check("跨日: 新一天已用从 0 开始", day_rows[0][0] == 0, day_rows)
        r = C.post("/v1/chat/completions", json=GOOD_BODY, headers=AUTH)
        check("跨日: 新一天正常放行", r.status_code == 200, r.status_code)
        rows = q("SELECT day FROM settlements WHERE outcome='ok_no_usage' OR est=1 ORDER BY at")
        # 上一请求(新一天)无 usage 时估算入账到 2027-01-01
        check("跨日: 预算日隔离正确",
              all(x[0] in ("2026-12-31", "2027-01-01") for x in rows), rows)
    finally:
        gw.now_dt = real_now
        set_budget({"test-model": 0.05, "test-embed": 0.01})


def datetime_with(y, mo, d, h, mi, s):
    from datetime import datetime, timezone
    return datetime(y, mo, d, h, mi, s, tzinfo=timezone(gw.BJ.utcoffset(
        datetime(y, mo, d))))


def t_restart_recovery():
    """重启恢复：启动时作废所有在途预留。"""
    reset_ledger({"test-model": 0.05, "test-embed": 0.01})
    ok, rid, reason = gw.reserve("test-model", "Mock", 0.002)
    check("重启恢复: 可创建预留", ok, reason)
    gw.init_db()  # 模拟进程重启
    rows = q("SELECT state, outcome FROM reservations WHERE id=?", (rid,))
    check("重启恢复: 在途预留被作废(restart)", rows and rows[0][0] == "expired"
          and rows[0][1] == "restart", rows)
    reserved = q("SELECT COALESCE(SUM(est_cost),0) FROM reservations WHERE state='reserved'")[0][0]
    check("重启恢复: 预算额度释放", reserved == 0, reserved)


def t_ttl_sweep():
    reset_ledger({"test-model": 0.05, "test-embed": 0.01})
    conn = sqlite3.connect(str(DB))
    conn.execute(
        "INSERT INTO reservations (id, model, provider, day, est_cost, state, created, expires) "
        "VALUES ('stale1','test-model','Mock','2026-01-01',0.002,'reserved',?,?)",
        (time.time() - 600, time.time() - 300),
    )
    conn.commit()
    conn.close()
    n = gw.sweep_stale()
    rows = q("SELECT state, outcome FROM reservations WHERE id='stale1'")
    check("TTL: 过期预留被清扫", n >= 1 and rows[0][0] == "expired" and rows[0][1] == "ttl",
          (n, rows))


def t_embeddings():
    set_budget({"test-model": 0.05, "test-embed": 0.01})
    r = C.post("/v1/embeddings",
               json={"model": "test-embed", "input": ["hello", "world"],
                     "x_test_usage": {"prompt_tokens": 200, "completion_tokens": 0}},
               headers=AUTH)
    check("Embeddings: 经网关可用", r.status_code == 200, r.text[:200])
    time.sleep(0.2)
    rows = q("SELECT cost FROM settlements WHERE model='test-embed' ORDER BY at DESC LIMIT 1")
    expected = 200 * 5 / 1e6  # 0.001
    check("Embeddings: 按 embedding 价格结算", rows and abs(rows[0][0] - expected) < 1e-9,
          f"rows={rows} expected={expected}")


def t_admin_status():
    r = C.get("/admin/status", headers={"x-admin-key": "wrong"})
    check("admin: 错误 key -> 401", r.status_code == 401, r.status_code)
    r = C.get("/admin/status", headers={"x-admin-key": "adm-testkey"})
    ok = r.status_code == 200 and r.json().get("ok") is True and "models" in r.json()
    check("admin: /admin/status 正常", ok, r.text[:200])


def t_priority_reserve_direct():
    """低优先级受保留份额约束；高优先级可用满额且不破总预算。"""
    reset_ledger({"test-model": 0.04, "test-embed": 0.01})
    set_behavior({"mention_reserve_ratio": 0.25})  # low cap = 0.04*0.75 = 0.03
    gw.cfg.reload(force=True)
    check("预留分层: reserve_ratio 热加载", abs(gw.cfg.reserve_ratio() - 0.25) < 1e-9,
          gw.cfg.reserve_ratio())

    ok1, rid1, r1 = gw.reserve("test-model", "Mock", 0.02, "low")
    check("low: 首笔在保留份额内放行", ok1 and r1 == "ok", (ok1, r1))

    ok2, _, r2 = gw.reserve("test-model", "Mock", 0.02, "low")
    check("low: 超出 75% 份额 -> reserve_exceeded",
          (not ok2) and r2 == "reserve_exceeded", (ok2, r2))

    ok3, rid3, r3 = gw.reserve("test-model", "Mock", 0.02, "high")
    check("high: 保留份额不拦高优先级（总额度内）", ok3 and r3 == "ok", (ok3, r3))

    ok4, _, r4 = gw.reserve("test-model", "Mock", 0.005, "high")
    check("high: 仍受模型总硬预算约束 -> exceeded",
          (not ok4) and r4 == "exceeded", (ok4, r4))

    tiers = q("SELECT tier FROM reservations WHERE state='reserved'")
    check("预留台账记录 tier", {t[0] for t in tiers} == {"high", "low"}, tiers)

    for rid in (rid1, rid3):
        if rid:
            gw.release(rid, "test_cleanup")
    set_behavior({"mention_reserve_ratio": 0.25})
    gw.cfg.reload(force=True)
    set_budget({"test-model": 0.05, "test-embed": 0.01})


def t_priority_reserve_http():
    """x-budget-tier 头生效：low 请求被 402 拒绝后，high 请求仍可继续。"""
    set_budget({"test-model": 0.05, "test-embed": 0.01})
    set_behavior({"mention_reserve_ratio": 0.5})  # low cap = 0.025
    gw.cfg.reload(force=True)
    reset_ledger({"test-model": 0.05, "test-embed": 0.01})

    low_headers = dict(AUTH, **{"x-budget-tier": "low"})
    rejected = None
    sent = 0
    for _ in range(20):
        r = C.post("/v1/chat/completions", json=GOOD_BODY, headers=low_headers, timeout=15)
        sent += 1
        if r.status_code != 200:
            rejected = r
            break
    ok = rejected is not None and rejected.status_code == 402 \
        and rejected.json().get("error", {}).get("code") == "PRIORITY_RESERVE_EXHAUSTED"
    check("low 头: 用满保留份额后 -> 402 PRIORITY_RESERVE_EXHAUSTED",
          ok, f"sent={sent} resp={None if rejected is None else rejected.text[:200]}")

    low_used = q("SELECT COALESCE(SUM(cost),0) FROM settlements "
                 "WHERE day=? AND model='test-model' AND tier='low'",
                 (gw.today_str(),))[0][0]
    check("low 头: 低优先级结算不超份额 (<=0.025)", low_used <= 0.025 + 1e-9, low_used)

    r = C.post("/v1/chat/completions", json=GOOD_BODY, headers=AUTH, timeout=15)
    check("low 拒绝后 high 请求仍放行（总额度未尽）", r.status_code == 200,
          r.text[:200])

    gw.write_status_file()
    st = json.loads(STATUS.read_text("utf-8"))
    check("状态快照含 reserve_ratio/low_used/low_cap",
          "reserve_ratio" in st and "low_used" in st.get("models", {}).get("test-model", {}),
          {k: st.get(k) for k in ("reserve_ratio",)})

    set_behavior({"mention_reserve_ratio": 0.25})
    gw.cfg.reload(force=True)
    set_budget({"test-model": 0.05, "test-embed": 0.01})


def t_prod_config_parse():
    """生产配置可解析（价格、模型、任务）。"""
    prod = Path("/config/model_config.toml")
    if not prod.exists():
        check("生产配置解析(挂载缺失则跳过)", True, "skipped: /config not mounted")
        return
    import tomllib
    with prod.open("rb") as fh:
        mc = tomllib.load(fh)
    idents = {m.get("model_identifier") for m in mc.get("models", [])}
    provs = {p.get("name"): p.get("base_url") for p in mc.get("api_providers", [])}
    ok = {"deepseek-flash", "mimo-v2.6-flash", "BAAI/bge-m3"} <= idents
    check("生产配置: 三个模型可解析", ok, idents)
    check("生产配置: 服务商可解析",
          provs.get("DeepSeek", "").startswith("https://api.deepseek.com"), provs)


# ---------------------------------------------------------------------------
# 5. 运行
# ---------------------------------------------------------------------------

def main():
    order = [
        t_auth, t_unknown_model, t_endpoint_allowlist, t_exact_settlement,
        t_tool_vlm_passthrough, t_stream, t_gzip_stream, t_stream_options_fallback,
        t_budget_exceeded, t_budget_not_configured, t_secret_missing,
        t_upstream_500, t_upstream_timeout,
        t_gw_provider_fallback, t_concurrency, t_direct_reserve_transactions,
        t_budget_change_keeps_used, t_cross_day,
        t_restart_recovery, t_ttl_sweep,
        t_embeddings, t_admin_status,
        t_priority_reserve_direct, t_priority_reserve_http,
        t_prod_config_parse,
    ]
    for fn in order:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(fn.__name__ + " 未抛异常", False, repr(exc))

    failed = [r for r in RESULTS if not r[1]]
    print("\n========== 汇总 ==========")
    print(f"通过 {len(RESULTS) - len(failed)} / {len(RESULTS)}")
    for name, _, detail in failed:
        print(f"  FAIL: {name} | {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
