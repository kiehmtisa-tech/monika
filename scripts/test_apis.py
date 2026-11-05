#!/usr/bin/env python3
"""直连测试 DeepSeek / MiMo / SiliconFlow 三个 API（密钥从 .env 读取，不打印）。"""
import json
import pathlib
import urllib.request
import urllib.error

ROOT = pathlib.Path("/opt/qq-fish")
env = {}
for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
    if "=" in line and not line.startswith("#"):
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()

def call(name, url, key, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "Authorization": f"Bearer {key}"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            body = json.loads(r.read().decode())
            usage = body.get("usage", {})
            txt = ""
            if body.get("choices"):
                txt = (body["choices"][0].get("message") or {}).get("content") or ""
            return f"OK  usage={usage} reply={txt[:60]!r}"
    except urllib.error.HTTPError as e:
        return f"HTTP {e.code}: {e.read().decode()[:200]}"
    except Exception as e:
        return f"ERR {type(e).__name__}: {e}"

print("[1] DeepSeek deepseek-flash:",
      call("ds", "https://api.deepseek.com/v1/chat/completions", env["DEEPSEEK_API_KEY"],
           {"model": "deepseek-flash", "messages": [{"role": "user", "content": "回复两个字：在线"}],
            "max_tokens": 32, "temperature": 0.8}))

print("[2] MiMo mimo-v2.6-flash:",
      call("mimo", "https://api.xiaomimimo.com/v1/chat/completions", env["MIMO_API_KEY"],
           {"model": "mimo-v2.6-flash", "messages": [{"role": "user", "content": "回复两个字：在线"}],
            "max_tokens": 32, "temperature": 0.8}))

print("[3] SiliconFlow bge-m3:",
      call("emb", "https://api.siliconflow.cn/v1/embeddings", env["EMBEDDING_API_KEY"],
           {"model": "BAAI/bge-m3", "input": "大肥鱼在群里潜水"}))
