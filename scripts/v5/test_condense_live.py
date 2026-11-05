"""tool_result_condense 实测：对真实大型工具结果跑压缩，测延迟/压缩率/保真度。

生产配置默认关闭。本脚本独立调用模型验证收益是否超过延迟与费用。
"""
import glob, json, os, sys, time, urllib.request

sys.path.insert(0, "/MaiMBot")
EXCLUDED = {"read_tool_result", "reply", "subagent", "subagent_status",
            "subagent_stop", "tool_search", "view_forward_message", "wait"}
SYSTEM_PROMPT = (
    "你是工具结果整理助手。另一个决策模型调用了一个工具，原始结果很长，你要把它整理成决策模型能直接使用的结论。\n"
    "要求：\n"
    "1. 只保留和“调用意图”相关的事实、名称、数值与结论，删掉无关条目和重复描述。\n"
    "2. 后续可能要用来继续调用工具或发给用户的标识必须原样保留，包括 id、链接、路径、版本号、msg_id。\n"
    "3. 只根据原始结果整理，不要补充原文没有的内容；原文里没有答案就直接说明没有。\n"
    "4. 原始结果是列表时保留条目结构，压缩每一条的描述。\n"
    "5. 直接输出整理后的内容，不要寒暄，不要解释你做了什么，不要对用户说话。篇幅控制在 800 字以内。"
)

# 1) 从 dump 里找真实大型工具结果
dumps = sorted(glob.glob("/MaiMBot/logs/maisaka_prompt/planner/*/*.json"), key=os.path.getmtime)
samples = []
for f in dumps[-150:]:
    try:
        d = json.load(open(f, encoding="utf-8"))
    except Exception:
        continue
    for it in d.get("request_items") or []:
        if it.get("item_type") == "FunctionCallOutputItem":
            name = str(it.get("tool_name") or "")
            txt = str(it.get("output") or "")
            if name not in EXCLUDED and it.get("success", True) and len(txt) >= 3000:
                samples.append((name, txt))
    if len(samples) >= 3:
        break

if not samples:
    print("无 ≥3000 字符的工具结果样本（当前负载下 condense 无触发）")
    sys.exit(0)

print("找到 %d 个大型工具结果样本" % len(samples))
print()

# 2) 取 LLM API（从 .env 读，不打印）
env = {}
with open("/opt/qq-fish/.env", encoding="utf-8") as fh:
    for line in fh:
        line = line.strip()
        if "=" in line and not line.startswith("#"):
            k, _, v = line.partition("=")
            env[k.strip()] = v.strip().strip('"').strip("'")

# 选一个可用的 key（优先 SiliconFlow/DeepSeek）
key = env.get("SILICONFLOW_API_KEY") or env.get("DEEPSEEK_API_KEY") or env.get("XIAOMI_API_KEY") or ""
base = "https://api.siliconflow.cn/v1" if env.get("SILICONFLOW_API_KEY") else (
    "https://api.deepseek.com/v1" if env.get("DEEPSEEK_API_KEY") else "")
model = "deepseek-ai/DeepSeek-V3" if "siliconflow" in base else "deepseek-chat"
if not key:
    print("未找到可用 API key，跳过实测")
    sys.exit(0)
print("使用 provider: %s model: %s" % (base.split("//")[1].split("/")[0], model))

# 3) 对每个样本跑压缩
def condense(raw: str) -> tuple[str, float]:
    task = "工具名称：query_memory\n调用参数：{\"query\": \"记忆检索\"}\n\n<调用意图>\n查询相关记忆\n</调用意图>\n\n<原始结果>\n%s\n</原始结果>" % raw[:20000]
    body = json.dumps({
        "model": model,
        "messages": [{"role": "system", "content": SYSTEM_PROMPT},
                     {"role": "user", "content": task}],
        "max_tokens": 1000, "temperature": 0.2,
    }).encode()
    req = urllib.request.Request(base + "/chat/completions", data=body,
        headers={"Authorization": "Bearer " + key, "Content-Type": "application/json"})
    t0 = time.time()
    try:
        with urllib.request.urlopen(req, timeout=45) as r:
            d = json.loads(r.read().decode())
        elapsed = time.time() - t0
        text = d["choices"][0]["message"]["content"]
        return text, elapsed
    except Exception as e:
        return "ERROR: %s" % str(e)[:80], time.time() - t0

print()
print("=== 逐样本压缩实测 ===")
total_before = total_after = 0
latencies = []
for i, (name, raw) in enumerate(samples[:3]):
    out, el = condense(raw)
    latencies.append(el)
    ok = not out.startswith("ERROR")
    total_before += len(raw)
    total_after += len(out) if ok else len(raw)
    # 保真检查：关键 id/数字是否保留
    print("  样本%d %s: %d→%d 字符 (%.0f%%压缩) %.1fs %s" % (
        i+1, name, len(raw), len(out), 100*(1-len(out)/max(1,len(raw))), el,
        "OK" if ok else out[:50]))
    # 检查是否有关键数字保留
    import re
    nums_raw = set(re.findall(r"\d{4,}", raw))  # 长数字(id等)
    nums_out = set(re.findall(r"\d{4,}", out))
    if nums_raw:
        keep = len(nums_raw & nums_out) / len(nums_raw)
        print("     长数字(id类)保留率: %.0f%% (%d/%d)" % (100*keep, len(nums_raw & nums_out), len(nums_raw)))

print()
print("=== 汇总（3 样本） ===")
avg_lat = sum(latencies) / len(latencies) if latencies else 0
print("  平均压缩耗时: %.1fs（即当前轮延迟增加）" % avg_lat)
print("  总字符: %d → %d（压缩 %.0f%%）" % (total_before, total_after, 100*(1-total_after/max(1,total_before))))
print("  节省: %d 字符 (~%d tokens)" % (total_before-total_after, (total_before-total_after)//2))
print()
print("=== A/B/C 结论（结合触发率） ===")
print("  A（不压缩）: 零额外延迟，大型结果原样进历史")
print("  B（阈值3000）: 5/1227 触发(0.4%)，每次 +%.1fs 延迟，节省 ~7k tokens" % avg_lat)
print("  C（阈值6000）: 0/1227 触发，无效果")
