#!/usr/bin/env python3
"""生成 NapCat WS token 并写入适配器配置（一次性工具）。"""
import pathlib, secrets, stat

root = pathlib.Path("/opt/qq-fish")
token = secrets.token_hex(24)

tok_file = root / ".napcat_token"
tok_file.write_text(token + "\n")
tok_file.chmod(0o600)

cfg = root / "data/MaiMBot/plugins/MaiBot-SnowLuma-Adapter/config.toml"
t = cfg.read_text(encoding="utf-8")
t = t.replace("enabled = false", "enabled = true", 1)
t = t.replace('server = "127.0.0.1"', 'server = "maim-bot-napcat"', 1)
t = t.replace('token = ""', f'token = "{token}"', 1)
cfg.write_text(t, encoding="utf-8")
cfg.chmod(0o600)

print("token len:", len(token))
print("config updated:", cfg)
