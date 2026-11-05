# -*- coding: utf-8 -*-
"""把 image_embedding 任务切到预算网关（SiliconFlow-GW，上游账户有余额），
并注册 qwen3-vl-embedding-gw 模型条目（显式图片输入模板）。"""
from pathlib import Path

p = Path("/opt/qq-fish/docker-config/mmc/model_config.toml")
s = p.read_text(encoding="utf-8")

if "qwen3-vl-embedding-gw" not in s:
    entry = (
        "\n[[models]]\n"
        'model_type = "embedding"\n'
        'model_identifier = "Qwen/Qwen3-VL-Embedding-8B" # 真实上游模型名（网关路由键）\n'
        'name = "qwen3-vl-embedding-gw" # 任务配置引用此名称 → 经预算网关\n'
        'api_provider = "SiliconFlow-GW"\n'
        'price_in = 0.0 # 价格待核实；仅按需索引立绘等少量图片，量级可忽略\n'
        "cache_price_in = 0.0\n"
        "price_out = 0.0\n"
        "price_periods = []\n"
        "send_temperature = false\n"
        "force_stream_mode = false\n"
        "visual = true\n"
        # 网关 base_url 不被识别为硅基流动官方域名，显式给出图片输入模板
        'extra_params = { image_embedding_input = { image = "{data_uri}" } }\n'
    )
    anchor = "[[api_providers]]\nname = \"SiliconFlow-GW\""
    assert s.count(anchor) == 1, "锚点不唯一: %d" % s.count(anchor)
    s = s.replace(anchor, entry + "\n" + anchor)

old = 'model_list = ["qwen3-vl-embedding"] # 图片嵌入：Qwen3-VL-Embedding-8B（按需索引）'
new = 'model_list = ["qwen3-vl-embedding-gw"] # 图片嵌入：Qwen3-VL-Embedding-8B 经预算网关（直连账户余额不足）'
assert old in s, "任务锚点未找到"
s = s.replace(old, new)
p.write_text(s, encoding="utf-8")
print("已切换到网关路由")
