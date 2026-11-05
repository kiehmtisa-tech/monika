#!/usr/bin/env python3
"""把 .env 中的密钥注入 model_config.toml / adapter 配置，并完成一次性初始化。

- 读取 /opt/qq-fish/.env（600 权限）
- 注入 DeepSeek / Xiaomi / SiliconFlow 三个 api_providers 的 api_key
- 添加 SiliconFlow embedding 模型与任务分配
- 回填 bot_config.toml 的 qq_account
- 配置 adapter_policy 放行测试私聊
- 配置 NapCat OneBot 正向 WebSocket
密钥不打印、不写入日志。
"""
import pathlib
import json
import re

ROOT = pathlib.Path("/opt/qq-fish")

def load_env():
    env = {}
    for line in (ROOT / ".env").read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        env[k.strip()] = v.strip()
    return env

env = load_env()
ds_key = env["DEEPSEEK_API_KEY"]
mimo_key = env["MIMO_API_KEY"]
emb_key = env["EMBEDDING_API_KEY"]
token = (ROOT / ".napcat_token").read_text(encoding="utf-8").strip()
bot_qq = env["BOT_QQ_ACCOUNT"]
test_qq = env["TEST_USER_QQ"]

# ---------- 1. model_config.toml ----------
mc_path = ROOT / "docker-config/mmc/model_config.toml"
mc = mc_path.read_text(encoding="utf-8")

mc = mc.replace("__DEEPSEEK_KEY__", ds_key).replace("__MIMO_KEY__", mimo_key)

# 添加 SiliconFlow 服务商（若尚未添加）
if 'name = "SiliconFlow"' not in mc:
    sf_block = f'''
[[api_providers]]
name = "SiliconFlow"
base_url = "https://api.siliconflow.cn/v1"
api_key = "{emb_key}"
client_type = "openai"
auth_type = "bearer"
auth_header_name = "Authorization"
auth_header_prefix = "Bearer"
auth_query_name = "api_key"
default_headers = {{}}
default_query = {{}}
model_list_endpoint = "/models"
reasoning_parse_mode = "none"
tool_argument_parse_mode = "auto"
max_retry = 2
timeout = 60
retry_interval = 4
'''
    mc = mc.rstrip() + "\n" + sf_block

# 添加 bge-m3 嵌入模型（若尚未添加）
if 'name = "bge-m3"' not in mc:
    emb_model = '''
[[models]]
model_type = "embedding" # 文本嵌入模型
model_identifier = "BAAI/bge-m3"
name = "bge-m3"
api_provider = "SiliconFlow"
price_in = 0.0 # 嵌入计费按量，价格待核实
cache_price_in = 0.0
price_out = 0.0
price_periods = []
send_temperature = false
force_stream_mode = false
visual = false
extra_params = {}
'''
    # 插到第一个 [[api_providers]] 之前
    idx = mc.index("[[api_providers]]")
    mc = mc[:idx] + emb_model.lstrip("\n") + "\n" + mc[idx:]

# 分配 embedding 任务
mc = re.sub(
    r'(\[model_task_config\.embedding\][^\[]*?)model_list = \[\]',
    r'\1model_list = ["bge-m3"]',
    mc,
    count=1,
)
mc_path.write_text(mc, encoding="utf-8")
mc_path.chmod(0o600)

# ---------- 2. bot_config.toml ----------
bc_path = ROOT / "docker-config/mmc/bot_config.toml"
bc = bc_path.read_text(encoding="utf-8")
bc = re.sub(r'qq_account = "[^"]*"', f'qq_account = "{bot_qq}"', bc, count=1)
bc_path.write_text(bc, encoding="utf-8")

# ---------- 3. adapter_policy.toml（放行测试私聊，群仍全拒）----------
pol_path = ROOT / "docker-config/mmc/adapter_policy.toml"
pol = pol_path.read_text(encoding="utf-8")
pol = re.sub(
    r'(\[defaults\.private\][^\[]*?)allow_ids = \[[^\]]*\]',
    rf'\1allow_ids = ["{test_qq}"]',
    pol,
    count=1,
)
pol_path.write_text(pol, encoding="utf-8")

# ---------- 4. NapCat OneBot 正向 WebSocket ----------
onebot_path = ROOT / f"docker-config/napcat/onebot11_{bot_qq}.json"
cfg = json.loads(onebot_path.read_text(encoding="utf-8"))
cfg["network"]["websocketServers"] = [
    {
        "enable": True,
        "name": "maibot",
        "host": "0.0.0.0",
        "port": 3001,
        "reportSelfMessage": False,
        "messagePostFormat": "array",
        "token": token,
        "debug": False,
        "heartInterval": 30000,
        "enableForcePushEvent": True,
    }
]
onebot_path.write_text(json.dumps(cfg, ensure_ascii=False, indent=2), encoding="utf-8")
onebot_path.chmod(0o600)

print("OK: model_config / bot_config / adapter_policy / onebot11 已更新")
print("bot_qq =", bot_qq, "| test_qq =", test_qq)
