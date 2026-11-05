#!/usr/bin/env python3
"""
真实上游端到端测试：在 Docker 内网中通过网关打 DeepSeek / MiMo / SiliconFlow 真实 API。

运行:
  docker run --rm --network qq-fish_maim_bot \
    -v /opt/qq-fish/gateway:/gw \
    -v /opt/qq-fish/secrets:/secrets:ro \
    -v /opt/qq-fish/docker-config/mmc:/config:ro \
    -v /opt/qq-fish/budget:/budget \
    -e GW_URL=http://budget-gateway:8009 \
    qq-fish-budget-gateway:1.0 python3 /gw/e2e_real.py
"""
from __future__ import annotations

import base64
import json
import os
import sqlite3
import sys
import time
import tomllib
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import httpx

GW = os.environ.get("GW_URL", "http://budget-gateway:8009")
SECRETS = json.loads(Path("/secrets/gateway_secrets.json").read_text("utf-8"))
GW_KEY = next(iter(SECRETS["gateway_keys"]))
AUTH = {"Authorization": f"Bearer {GW_KEY}"}
DB = "/budget/gateway.db"
BJ = ZoneInfo("Asia/Shanghai")

with open("/config/model_config.toml", "rb") as fh:
    MC = tomllib.load(fh)
MODELS = {m["model_identifier"]: m for m in MC.get("models", [])}

RESULTS = []


def check(name, cond, detail=""):
    RESULTS.append((name, bool(cond), detail))
    print(("PASS " if cond else "FAIL ") + name + (" | " + str(detail) if detail and not cond else ""))


def q(sql, args=()):
    conn = sqlite3.connect(DB, timeout=10)
    try:
        return conn.execute(sql, args).fetchall()
    finally:
        conn.close()


def expected_cost(model: str, usage: dict) -> float:
    m = MODELS[model]
    pin, pout, pcache = float(m["price_in"]), float(m["price_out"]), float(m.get("cache_price_in") or 0)
    now = datetime.now(BJ)
    for p in m.get("price_periods") or []:
        sh, sm = map(int, p["start_time"].split(":"))
        eh, em = map(int, p["end_time"].split(":"))
        if sh * 60 + sm <= now.hour * 60 + now.minute < eh * 60 + em:
            pin, pout, pcache = float(p["price_in"]), float(p["price_out"]), float(p.get("cache_price_in") or 0)
            break
    prompt = int(usage.get("prompt_tokens") or 0)
    comp = int(usage.get("completion_tokens") or 0)
    cached = int((usage.get("prompt_tokens_details") or {}).get("cached_tokens") or 0)
    cached = min(cached, prompt)
    return round(((prompt - cached) * pin + cached * pcache + comp * pout) / 1e6, 8)


def latest_settlement(model: str):
    rows = q(
        "SELECT cost, est, outcome, prompt_tokens, completion_tokens FROM settlements "
        "WHERE model=? ORDER BY at DESC LIMIT 1", (model,),
    )
    return rows[0] if rows else None


def png_1x1() -> str:
    # 1x1 红色 PNG
    raw = base64.b64decode(
        "iVBORw0KGgoAAAANSUhEUgAAAAEAAAABCAYAAAAfFcSJAAAADUlEQVR42mP8z8BQDwAEhQGAhKmMIQAAAABJRU5ErkJggg=="
    )
    return "data:image/png;base64," + base64.b64encode(raw).decode()


def main():
    # 0. 基础
    r = httpx.get(f"{GW}/healthz", timeout=10)
    check("healthz", r.status_code == 200 and r.json().get("ok"), r.text[:100])

    r = httpx.get(f"{GW}/v1/models", headers=AUTH, timeout=10)
    ids = [m["id"] for m in r.json().get("data", [])]
    check("/v1/models 含三个生产模型",
          {"deepseek-flash", "mimo-v2.6-flash", "BAAI/bge-m3"} <= set(ids), ids)

    # 1. DeepSeek 非流式
    t0 = time.time()
    r = httpx.post(f"{GW}/v1/chat/completions", headers=AUTH, timeout=90, json={
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "用一句话回答：中国的首都是哪里？"}],
        "max_tokens": 100,
        "temperature": 0.7,
    })
    ok = r.status_code == 200
    content = ""
    usage = {}
    if ok:
        data = r.json()
        content = data["choices"][0]["message"].get("content") or ""
        usage = data.get("usage") or {}
    check("DeepSeek 非流式 200 且有内容", ok and bool(content.strip()), r.text[:300])
    check("DeepSeek usage 上报", bool(usage.get("prompt_tokens")), usage)
    time.sleep(0.5)
    st = latest_settlement("deepseek-flash")
    if ok and st:
        exp = expected_cost("deepseek-flash", usage)
        check("DeepSeek 结算与官方价格逐位一致",
              st[1] == 0 and abs(st[0] - exp) < 1e-9,
              f"ledger={st[0]} expected={exp} usage={usage}")
        check("DeepSeek 结算 token 与 usage 一致",
              st[3] == usage.get("prompt_tokens") and st[4] == usage.get("completion_tokens"), st)
    else:
        check("DeepSeek 结算存在", False, st)

    # 2. DeepSeek 流式
    raw = b""
    status = None
    with httpx.Client(timeout=90) as c:
        with c.stream("POST", f"{GW}/v1/chat/completions", headers=AUTH, json={
            "model": "deepseek-flash",
            "messages": [{"role": "user", "content": "数到三，只输出数字。"}],
            "max_tokens": 50,
            "stream": True,
        }) as resp:
            status = resp.status_code
            raw = b"".join(resp.iter_raw())
    check("DeepSeek 流式 200 + [DONE]", status == 200 and b"data: [DONE]" in raw,
          f"status={status} head={raw[:120]}")
    time.sleep(0.5)
    st = latest_settlement("deepseek-flash")
    check("DeepSeek 流式已结算", st is not None, st)

    # 3. DeepSeek Tool Calls（强制 tool_choice）
    r = httpx.post(f"{GW}/v1/chat/completions", headers=AUTH, timeout=90, json={
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": "北京天气如何？"}],
        "tools": [{"type": "function", "function": {
            "name": "get_weather",
            "description": "查询城市天气",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}},
                           "required": ["city"]}}}],
        "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        "thinking": {"type": "disabled"},  # DeepSeek 思考模式不支持强制 tool_choice（MaiBot 同款配置）
        "max_tokens": 200,
    })
    tc = None
    if r.status_code == 200:
        tc = r.json()["choices"][0]["message"].get("tool_calls")
    check("DeepSeek Tool Calls 经网关透传", r.status_code == 200 and bool(tc),
          r.text[:300])

    # 4. DeepSeek VLM
    r = httpx.post(f"{GW}/v1/chat/completions", headers=AUTH, timeout=90, json={
        "model": "deepseek-flash",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "这张图是什么颜色？一句话回答。"},
            {"type": "image_url", "image_url": {"url": png_1x1()}},
        ]}],
        "max_tokens": 100,
    })
    check("DeepSeek VLM 200", r.status_code == 200,
          r.text[:300] if r.status_code != 200 else "")

    # 5. MiMo 非流式
    r = httpx.post(f"{GW}/v1/chat/completions", headers=AUTH, timeout=90, json={
        "model": "mimo-v2.6-flash",
        "messages": [{"role": "user", "content": "用一句话回答：天空为什么是蓝色的？"}],
        "max_tokens": 150,
        "temperature": 0.7,
    })
    ok = r.status_code == 200
    m_usage = {}
    m_content = ""
    if ok:
        m_usage = r.json().get("usage") or {}
        m_content = r.json()["choices"][0]["message"].get("content") or ""
    check("MiMo 非流式 200 且有内容", ok and bool(m_content.strip()), r.text[:300])
    time.sleep(0.5)
    st = latest_settlement("mimo-v2.6-flash")
    if ok and st:
        exp = expected_cost("mimo-v2.6-flash", m_usage)
        check("MiMo 结算与官方价格逐位一致",
              st[1] == 0 and abs(st[0] - exp) < 1e-9,
              f"ledger={st[0]} expected={exp} usage={m_usage}")
    else:
        check("MiMo 结算存在", False, st)

    # 6. MiMo 流式
    raw = b""
    status = None
    with httpx.Client(timeout=90) as c:
        with c.stream("POST", f"{GW}/v1/chat/completions", headers=AUTH, json={
            "model": "mimo-v2.6-flash",
            "messages": [{"role": "user", "content": "只输出 OK"}],
            "max_tokens": 20,
            "stream": True,
        }) as resp:
            status = resp.status_code
            raw = b"".join(resp.iter_raw())
    check("MiMo 流式 200 + [DONE]", status == 200 and b"data: [DONE]" in raw,
          f"status={status} head={raw[:120]}")

    # 7. MiMo Tool Calls
    r = httpx.post(f"{GW}/v1/chat/completions", headers=AUTH, timeout=90, json={
        "model": "mimo-v2.6-flash",
        "messages": [{"role": "user", "content": "上海天气如何？"}],
        "tools": [{"type": "function", "function": {
            "name": "get_weather",
            "description": "查询城市天气",
            "parameters": {"type": "object",
                           "properties": {"city": {"type": "string"}},
                           "required": ["city"]}}}],
        "tool_choice": {"type": "function", "function": {"name": "get_weather"}},
        "max_tokens": 200,
    })
    tc = None
    if r.status_code == 200:
        tc = r.json()["choices"][0]["message"].get("tool_calls")
    check("MiMo Tool Calls 经网关透传", r.status_code == 200 and bool(tc),
          r.text[:300])

    # 8. MiMo VLM
    r = httpx.post(f"{GW}/v1/chat/completions", headers=AUTH, timeout=90, json={
        "model": "mimo-v2.6-flash",
        "messages": [{"role": "user", "content": [
            {"type": "text", "text": "这张图是什么颜色？一句话回答。"},
            {"type": "image_url", "image_url": {"url": png_1x1()}},
        ]}],
        "max_tokens": 100,
    })
    check("MiMo VLM 200", r.status_code == 200,
          r.text[:300] if r.status_code != 200 else "")

    # 9. Embeddings (bge-m3 via SiliconFlow)
    r = httpx.post(f"{GW}/v1/embeddings", headers=AUTH, timeout=60, json={
        "model": "BAAI/bge-m3",
        "input": ["预算网关测试"],
    })
    dim = 0
    if r.status_code == 200:
        data = r.json().get("data") or []
        if data:
            dim = len(data[0].get("embedding") or [])
    check("Embeddings bge-m3 200 且维度 1024", r.status_code == 200 and dim == 1024,
          f"status={r.status_code} dim={dim} body={r.text[:200]}")
    time.sleep(0.5)
    st = latest_settlement("BAAI/bge-m3")
    check("Embeddings 已入网关台账", st is not None, st)

    # 10. 状态快照
    r = httpx.get(f"{GW}/admin/status", headers={"x-admin-key": SECRETS["admin_key"]}, timeout=10)
    ok = r.status_code == 200
    if ok:
        js = r.json()
        ok = js.get("ok") and all(
            js["models"].get(m, {}).get("gated") for m in
            ("deepseek-flash", "mimo-v2.6-flash", "BAAI/bge-m3")
        )
    check("/admin/status 正常且三模型均在控", ok, r.text[:300])

    # 11. 状态文件已写出（Dragon Board 读取）
    sf = Path("/budget/gateway_status.json")
    ok = sf.exists()
    if ok:
        js = json.loads(sf.read_text("utf-8"))
        ok = js.get("ok") is True and len(js.get("models", {})) >= 3
    check("gateway_status.json 已生成", ok, str(sf))

    failed = [x for x in RESULTS if not x[1]]
    print("\n========== 真实上游 E2E 汇总 ==========")
    print(f"通过 {len(RESULTS) - len(failed)} / {len(RESULTS)}")
    for name, _, detail in failed:
        print(f"  FAIL: {name} | {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
