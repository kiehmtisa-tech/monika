# -*- coding: utf-8 -*-
"""Planner 输入构成拆解：区分固定开销与可裁剪开销。

用途（审核 P1）：在决定是否调阈值之前，先看清 25k tokens 到底由什么组成。
- 固定开销：System Prompt（人设）、工具 Schema、每轮系统提醒
- 可裁剪开销：聊天历史、滚动摘要、A_Memorix 注入、工具结果、planner 自我推理

输出：按 item 分类的字符数与估算 token（中文约 1.5 字符/token，英文约 4），
并给出采样统计（多份 dump 的均值）。
"""
import glob
import json
import os
import re
from collections import defaultdict

PROMPT_DIR = "/opt/qq-fish/data/MaiMBot/logs/maisaka_prompt/planner"
SAMPLE = 40


def est_tokens(text: str) -> int:
    """粗估 token：中文按 1.5 字符/词元，其余按 4 字符/词元。"""
    if not text:
        return 0
    cn = len(re.findall(r"[\u4e00-\u9fff]", text))
    other = len(text) - cn
    return int(cn / 1.5 + other / 4)


def classify(item) -> tuple[str, int]:
    """把一条 request_item 归到构成类别，返回 (类别, 字符数)。"""
    itype = item.get("item_type", "?")
    text = "".join(
        p.get("text", "") for p in item.get("parts", []) if isinstance(p, dict)
    )
    if itype == "SystemMessageItem":
        return "system_prompt（人设+规则）", len(text)
    if itype == "AssistantMessageItem":
        return "planner 自我推理（历史 CoT）", len(text)
    if itype == "FunctionCallOutputItem":
        raw = json.dumps(item.get("output", ""), ensure_ascii=False)
        return "tool_results（工具结果）", len(raw)
    if itype == "FunctionCallItem":
        # 只算调用本身（函数名+参数），不算 meta
        tc = item.get("tool_call") or {}
        return "tool_calls（工具调用）", len(json.dumps(tc, ensure_ascii=False))
    if itype == "ReasoningItem":
        return "reasoning（推理块）", len(text)
    # UserMessageItem 内部再分
    if "【人物画像" in text or "人物画像-内部参考" in text:
        return "a_memorix 注入（画像）", len(text)
    if "[上下文恢复]" in text or "滚动" in text[:40] or "状态摘要" in text[:60]:
        return "滚动摘要/上下文恢复", len(text)
    if text.strip().startswith("<message") or "<message " in text[:20]:
        return "聊天历史（真实消息）", len(text)
    if "系统提醒" in text or "<system-reminder>" in text or "tool_search" in text:
        return "系统提醒（工具可见性等）", len(text)
    if "请你进行分析" in text:
        return "planner 任务指令（固定）", len(text)
    return "其他（时间戳/指令等）", len(text)


def main():
    fs = sorted(glob.glob(os.path.join(PROMPT_DIR, "*/*.json")), key=os.path.getmtime)
    fs = fs[-SAMPLE:]
    if not fs:
        print("无 planner dump")
        return

    agg = defaultdict(lambda: {"chars": 0, "n": 0})
    totals = []
    for f in fs:
        d = json.load(open(f, encoding="utf-8"))
        file_total = 0
        for item in d.get("request_items", []):
            cat, chars = classify(item)
            agg[cat]["chars"] += chars
            agg[cat]["n"] += 1
            file_total += chars
        # 工具 Schema 单独算（不在 request_items 里）
        tools = d.get("tool_definitions", [])
        tchars = len(json.dumps(tools, ensure_ascii=False))
        agg["tool_schema（工具定义）"]["chars"] += tchars
        agg["tool_schema（工具定义）"]["n"] += 1
        file_total += tchars
        meta = d.get("metadata", {})
        totals.append((meta.get("prompt_tokens", 0), meta.get("duration_ms", 0), file_total))

    total_chars = sum(v["chars"] for v in agg.values())
    print("=" * 78)
    print("Planner 输入构成拆解（采样 %d 次请求）" % len(fs))
    print("=" * 78)
    print("%-32s %10s %8s %8s %10s" % ("类别", "总字符", "占比", "估算token", "出现次数"))
    print("-" * 78)
    for cat, v in sorted(agg.items(), key=lambda kv: -kv[1]["chars"]):
        pct = 100.0 * v["chars"] / total_chars if total_chars else 0
        print("%-32s %10d %7.1f%% %8d %10d" % (
            cat, v["chars"], pct, int(v["chars"] / 2.0), v["n"]))
    print("-" * 78)
    print("%-32s %10d %7.1f%%" % ("合计", total_chars, 100.0))

    # 固定 vs 可裁剪
    fixed_cats = {"system_prompt（人设+规则）", "tool_schema（工具定义）",
                  "planner 任务指令（固定）", "系统提醒（工具可见性等）"}
    fixed = sum(v["chars"] for k, v in agg.items() if k in fixed_cats)
    print()
    print("固定开销（每轮必带）：%d 字符 ≈ %.0f%%" % (fixed, 100.0 * fixed / total_chars))
    print("可裁剪开销（随会话增长）：%d 字符 ≈ %.0f%%" % (
        total_chars - fixed, 100.0 * (total_chars - fixed) / total_chars))

    print()
    print("=" * 78)
    print("单次请求统计（采样均值）")
    print("=" * 78)
    if totals:
        pts = [t[0] for t in totals if t[0]]
        dts = [t[1] for t in totals if t[1]]
        if pts:
            pts.sort()
            print("  prompt_tokens: 均值 %d | P50 %d | P95 %d | 峰值 %d" % (
                sum(pts) / len(pts), pts[len(pts) // 2],
                pts[min(len(pts) - 1, int(len(pts) * 0.95))], pts[-1]))
        if dts:
            dts.sort()
            print("  duration_ms:   均值 %d | P50 %d | P95 %d | 峰值 %d" % (
                sum(dts) / len(dts), dts[len(dts) // 2],
                dts[min(len(dts) - 1, int(len(dts) * 0.95))], dts[-1]))


if __name__ == "__main__":
    main()
