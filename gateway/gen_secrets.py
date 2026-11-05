#!/usr/bin/env python3
"""生成网关 secrets 文件（真实上游 Key 从 model_config.toml 提取，网关凭证随机生成）。"""
import json
import re
import secrets
import stat
from pathlib import Path

mc = Path("/opt/qq-fish/docker-config/mmc/model_config.toml").read_text("utf-8")

# 提取 [[api_providers]] 段里的 name 与 api_key
providers = {}
blocks = re.split(r"\[\[api_providers\]\]", mc)[1:]
for b in blocks:
    name_m = re.search(r'name\s*=\s*"([^"]+)"', b)
    key_m = re.search(r'api_key\s*=\s*"([^"]+)"', b)
    if name_m and key_m:
        providers[name_m.group(1)] = key_m.group(1)

assert "DeepSeek" in providers and "Xiaomi" in providers and "SiliconFlow" in providers, providers.keys()

out = {
    "gateway_keys": {"gw-" + secrets.token_hex(24): "maibot"},
    "admin_key": "adm-" + secrets.token_hex(24),
    "provider_keys": providers,
}
path = Path("/opt/qq-fish/secrets/gateway_secrets.json")
path.parent.mkdir(parents=True, exist_ok=True)
path.write_text(json.dumps(out, ensure_ascii=False, indent=2), "utf-8")
path.chmod(stat.S_IRUSR | stat.S_IWUSR)  # 600
print("written:", path)
print("gateway_keys:", list(out["gateway_keys"].keys()))
print("providers:", list(providers.keys()))
