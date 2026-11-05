#!/usr/bin/env python3
"""
阶段5 自适应行为 · 模拟验收测试（宿主机直接运行）

运行方式:
  python3 /opt/qq-fish/gateway/test_phase5.py

覆盖:
  - 验收1~8（ChatGPT 阶段5 清单）的可自动化部分
  - 插件闸门 budget_gate.py 单测（保守降频 / 非对称 EMA / 概率换算）
  - 状态滞回（阈值附近不跳变）
  - 运行时配置同步（bot_config 群聊调频 + 记忆节流，写入临时副本）
  - 阶段A 网关预留套件（子进程复用 test_gateway.py）
"""
from __future__ import annotations

import importlib
import json
import shutil
import subprocess
import sys
import tempfile
import tomllib
from datetime import datetime as RealDT
from pathlib import Path

RESULTS: list[tuple[str, bool, object]] = []


def check(name: str, cond: bool, detail: object = "") -> None:
    ok = bool(cond)
    RESULTS.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'} {name}" + (f" | {detail}" if not ok else ""))


# ---------------------------------------------------------------------------
# 1. 导入被测模块
# ---------------------------------------------------------------------------

sys.path.insert(0, "/root/dragon-dashboard/dragon/backend")
from app.routes import ai_budget as ab  # noqa: E402

PLUGIN_DIR = Path("/opt/qq-fish/data/MaiMBot/plugins/idle_proactive_chat")
sys.path.insert(0, str(PLUGIN_DIR))
budget_gate = importlib.import_module("budget_gate")

TMP = Path(tempfile.mkdtemp(prefix="phase5test-"))
BOT_CONFIG_SRC = Path("/opt/qq-fish/docker-config/mmc/bot_config.toml")


# 固定时间：2026-11-03 10:00（白天，验收1“上午”场景）
class FixedDT(RealDT):
    @classmethod
    def now(cls, tz=None):
        return cls(2026, 11, 3, 10, 0, 0, tzinfo=tz)


ORIG: dict = {}


def patch(**kw):
    for k, v in kw.items():
        if k not in ORIG:
            ORIG[k] = getattr(ab, k)
        setattr(ab, k, v)


def unpatch_all():
    for k, v in ORIG.items():
        setattr(ab, k, v)


def setup_env(tmp: Path, budgets, usage, gw_used, avg):
    """把 ai_budget 指向隔离的临时文件与固定消费数据。"""
    d = tmp / "budget"
    d.mkdir(parents=True, exist_ok=True)
    (d / "budget.json").write_text(json.dumps({"daily": budgets}), "utf-8")
    (d / "behavior.json").write_text(json.dumps({
        "mode": "auto", "global_activity": 1.0,
        "throttle": {"deepseek-flash": 1.0, "mimo-v2.6-flash": 1.0},
        "night_weight": 0.1, "mention_reserve_ratio": 0.3,
        "multimodal_enabled": True, "proactive_enabled": True,
        "safety_coefficient": 0.8, "proactive_share": 0.5,
    }), "utf-8")
    shutil.copy(BOT_CONFIG_SRC, tmp / "bot_config.toml")
    patch(
        BUDGET_DIR=d,
        BUDGET_FILE=d / "budget.json",
        BEHAVIOR_FILE=d / "behavior.json",
        POLICY_PATH=tmp / "budget_policy.json",
        BOT_CONFIG_PATH=tmp / "bot_config.toml",
        TALK_BASE_FILE=d / "runtime_talk_base.json",
        get_budgets=lambda: {"daily": dict(budgets), "updated_at": None},
        get_today_usage=lambda day: usage,
        _gateway_used_today=lambda day: dict(gw_used),
        _estimate_avg_cost=lambda day: dict(avg),
        datetime=FixedDT,
    )
    ab._state_hysteresis.clear()


def set_daily(tmp: Path, budgets):
    """验收6：中途改预算（模拟 Dragon Board 保存）。"""
    (tmp / "budget" / "budget.json").write_text(json.dumps({"daily": budgets}), "utf-8")
    patch(get_budgets=lambda: {"daily": dict(budgets), "updated_at": None})


# ---------------------------------------------------------------------------
# 2. 策略计算（验收 1/2/3/4/6/8 + manual 模式）
# ---------------------------------------------------------------------------

def t_policy_normal_baseline():
    """验收2：预算充足 → 正常状态，速率不超过基准，群聊频率不超原值。"""
    usage = {"models": {"deepseek-flash": {"calls": 10, "cost": 0.01},
                        "mimo-v2.6-flash": {"calls": 10, "cost": 0.01}},
             "tasks": []}
    setup_env(TMP, {"deepseek-flash": 2.0, "mimo-v2.6-flash": 2.0, "bge-m3": 0.05},
              usage, {"deepseek-flash": 0.01, "mimo-v2.6-flash": 0.01},
              {"deepseek-flash": 0.001, "mimo-v2.6-flash": 0.001})
    p = ab.compute_policy()
    check("验收2: 充足 → behavior_state=normal", p["behavior_state"] == "normal",
          p["behavior_state"])
    check("验收2: 主动速率 = 基准×份额（不提速）",
          0 < p["proactive_rate_per_hour"] <= 30.0 + 1e-9,
          p["proactive_rate_per_hour"])
    check("验收2: 群聊频率 = 原基准 0.4（不增加）",
          abs(p["group_talk_value"] - 0.4) < 1e-9, p["group_talk_value"])
    check("验收2: 多模态/记忆允许", p["multimodal_allowed"] and p["memory_allowed"])


def t_policy_tight_morning():
    """验收1：上午预算不足 → 状态收紧、主动速率与群聊频率自动下降。"""
    # remaining=0.2, hours≈13.9, avg=0.001 → λ≈11.5 ∈ [9,42) = tight
    used_cost, calls = 0.1, 100
    usage = {"models": {"deepseek-flash": {"calls": calls, "cost": used_cost},
                        "mimo-v2.6-flash": {"calls": calls, "cost": used_cost}},
             "tasks": []}
    setup_env(TMP, {"deepseek-flash": 0.3, "mimo-v2.6-flash": 0.3, "bge-m3": 0.05},
              usage, {"deepseek-flash": used_cost, "mimo-v2.6-flash": used_cost},
              {"deepseek-flash": 0.001, "mimo-v2.6-flash": 0.001})
    p = ab.compute_policy()
    check("验收1: 上午预算不足 → tight", p["behavior_state"] == "tight",
          (p["behavior_state"], p["models"]["deepseek-flash"]["lambda_per_hour"]))
    check("验收1: 主动速率显著下降", 0 < p["proactive_rate_per_hour"] < 10,
          p["proactive_rate_per_hour"])
    check("验收1: 群聊频率减半 → 0.2", abs(p["group_talk_value"] - 0.2) < 1e-9,
          p["group_talk_value"])
    check("验收1: 记忆节流 → ×2", p["memory_delay_factor"] == 2, p["memory_delay_factor"])


def t_policy_mimo_exhausted():
    """验收3：MiMo 耗尽 → 先停多模态/主动，DS 侧语言链路状态保持 normal。"""
    usage = {"models": {"deepseek-flash": {"calls": 10, "cost": 0.02},
                        "mimo-v2.6-flash": {"calls": 500, "cost": 1.0}},
             "tasks": []}
    setup_env(TMP, {"deepseek-flash": 2.0, "mimo-v2.6-flash": 1.0, "bge-m3": 0.05},
              usage, {"deepseek-flash": 0.02, "mimo-v2.6-flash": 1.0},
              {"deepseek-flash": 0.002, "mimo-v2.6-flash": 0.002})
    p = ab.compute_policy()
    check("验收3: MiMo 耗尽 → 多模态停用", p["multimodal_allowed"] is False)
    check("验收3: DS 状态仍 normal（不停语言回复链路）",
          p["models"]["deepseek-flash"]["state"] == "normal",
          p["models"]["deepseek-flash"]["state"])
    check("验收3: DS 记忆节流不误伤（×1）", p["memory_delay_factor"] == 1,
          p["memory_delay_factor"])
    check("验收3: 主动发言停止（planner 不可用）", p["proactive_rate_per_hour"] == 0,
          p["proactive_rate_per_hour"])


def t_policy_ds_tight_memory_delayed():
    """验收4：DeepSeek 紧张 → 主动降频 + 记忆任务延迟分级。"""
    used_cost, calls = 0.17, 100
    usage = {"models": {"deepseek-flash": {"calls": calls, "cost": used_cost},
                        "mimo-v2.6-flash": {"calls": 10, "cost": 0.01}},
             "tasks": []}
    setup_env(TMP, {"deepseek-flash": 0.2, "mimo-v2.6-flash": 2.0, "bge-m3": 0.05},
              usage, {"deepseek-flash": used_cost, "mimo-v2.6-flash": 0.01},
              {"deepseek-flash": 0.0017, "mimo-v2.6-flash": 0.001})
    p = ab.compute_policy()
    ds_state = p["models"]["deepseek-flash"]["state"]
    check("验收4: DS 紧张", ds_state in ("tight", "critical"), ds_state)
    check("验收4: 记忆延迟分级 ≥2", p["memory_delay_factor"] >= 2, p["memory_delay_factor"])
    check("验收4: 主动速率受 λ_min 拖累下降",
          p["proactive_rate_per_hour"] < 30, p["proactive_rate_per_hour"])


def t_budget_change_instant_and_no_reset():
    """验收6：改预算 → 策略与 MaiBot 配置立即更新，已用金额不重置。"""
    usage = {"models": {"deepseek-flash": {"calls": 100, "cost": 0.1},
                        "mimo-v2.6-flash": {"calls": 10, "cost": 0.01}},
             "tasks": []}
    tmp = TMP / "case6"
    setup_env(tmp, {"deepseek-flash": 2.0, "mimo-v2.6-flash": 2.0, "bge-m3": 0.05},
              usage, {"deepseek-flash": 0.1, "mimo-v2.6-flash": 0.01},
              {"deepseek-flash": 0.001, "mimo-v2.6-flash": 0.001})
    before = ab.compute_policy()
    check("验收6: 改前 used=0.1", abs(before["models"]["deepseek-flash"]["used"] - 0.1) < 1e-9,
          before["models"]["deepseek-flash"]["used"])
    set_daily(tmp, {"deepseek-flash": 0.12, "mimo-v2.6-flash": 2.0, "bge-m3": 0.05})
    r1 = ab.refresh_policy()
    after = r1["policy"]
    check("验收6: 改后 used 不变", abs(after["models"]["deepseek-flash"]["used"] - 0.1) < 1e-9,
          after["models"]["deepseek-flash"]["used"])
    check("验收6: 收紧后状态立即变化", after["behavior_state"] != "normal",
          after["behavior_state"])
    check("验收6: 策略文件原子更新",
          json.loads((tmp / "budget_policy.json").read_text("utf-8"))["behavior_state"]
          == after["behavior_state"])
    doc = tomllib.loads((tmp / "bot_config.toml").read_text("utf-8"))
    rt = doc["chat"]["reply_timing"]
    want = after["group_talk_value"]
    check("验收6: bot_config talk_value 已同步", abs(float(rt["talk_value"]) - want) < 1e-9,
          (rt["talk_value"], want))
    day_rule = [r for r in rt["talk_value_rules"]
                if r.get("time") == "08:00-23:59" and r.get("rule_type") == "group"
                and not str(r.get("item_id") or "")]
    check("验收6: 白天规则值已同步",
          day_rule and abs(float(day_rule[0]["value"]) - want) < 1e-9, day_rule)
    am = doc.get("a_memorix", {})
    check("验收6: 记忆节流已同步(阈值×delay, 嵌套表)",
          int(am.get("integration", {}).get("chat_summary_writeback_message_threshold", 0))
          == 72 * after["memory_delay_factor"],
          am.get("integration", {}).get("chat_summary_writeback_message_threshold"))
    check("验收6: 根表无野键",
          "chat_summary_writeback_message_threshold" not in am
          and "refresh_debounce_seconds" not in am, list(am))
    check("验收6: bot_config 仍可解析", isinstance(rt.get("talk_value"), float))


def t_nextday_recovery_no_burst():
    """验收8：次日预算恢复 → 策略回到基准（不超原频率），闸门概率封顶为基线行为。"""
    usage = {"models": {"deepseek-flash": {"calls": 0, "cost": 0.0},
                        "mimo-v2.6-flash": {"calls": 0, "cost": 0.0}},
             "tasks": []}
    tmp = TMP / "case8"
    setup_env(tmp, {"deepseek-flash": 2.0, "mimo-v2.6-flash": 2.0, "bge-m3": 0.05},
              usage, {}, {"deepseek-flash": 0.001, "mimo-v2.6-flash": 0.001})
    r = ab.refresh_policy()
    p = r["policy"]
    check("验收8: 恢复 → normal", p["behavior_state"] == "normal", p["behavior_state"])
    check("验收8: 速率回到基准不超发",
          abs(p["proactive_rate_per_hour"] - 30.0) < 1e-6, p["proactive_rate_per_hour"])
    check("验收8: 群聊频率恢复 0.4", abs(p["group_talk_value"] - 0.4) < 1e-9,
          p["group_talk_value"])
    doc = tomllib.loads((tmp / "bot_config.toml").read_text("utf-8"))
    check("验收8: bot_config 恢复原值",
          abs(float(doc["chat"]["reply_timing"]["talk_value"]) - 0.4) < 1e-9,
          doc["chat"]["reply_timing"]["talk_value"])
    gate = budget_gate.evaluate_gate(tmp / "budget_policy.json", interval_s=300,
                                     random_value=0.0)
    check("验收8: 闸门概率封顶 1（不为花完预算而爆发）", gate["probability"] == 1.0,
          gate["probability"])


def t_behavior_switch_manual():
    """manual 模式：按人工系数执行，不看余额，不接管 bot_config。"""
    usage = {"models": {"deepseek-flash": {"calls": 10, "cost": 0.01},
                        "mimo-v2.6-flash": {"calls": 10, "cost": 0.01}}, "tasks": []}
    tmp = TMP / "case_manual"
    setup_env(tmp, {"deepseek-flash": 2.0, "mimo-v2.6-flash": 2.0, "bge-m3": 0.05},
              usage, {}, {"deepseek-flash": 0.001, "mimo-v2.6-flash": 0.001})
    (tmp / "budget" / "behavior.json").write_text(json.dumps({
        "mode": "manual", "global_activity": 0.3,
        "throttle": {"deepseek-flash": 0.5, "mimo-v2.6-flash": 1.0},
        "night_weight": 0.1, "mention_reserve_ratio": 0.3,
        "multimodal_enabled": True, "proactive_enabled": True,
        "safety_coefficient": 0.8, "proactive_share": 0.5,
    }), "utf-8")
    p = ab.compute_policy()
    check("manual: 群聊频率 = 基准×global_activity",
          abs(p["group_talk_value"] - 0.12) < 1e-9, p["group_talk_value"])
    check("manual: 主动速率按人工系数", abs(p["proactive_rate_per_hour"] - 4.5) < 1e-6,
          p["proactive_rate_per_hour"])
    r = ab.refresh_policy()
    check("manual: 不接管 bot_config",
          r["runtime"].get("applied") is False or bool(r["runtime"].get("note")),
          r["runtime"])


# ---------------------------------------------------------------------------
# 3. 插件闸门单测
# ---------------------------------------------------------------------------

def t_gate_ok_and_cap():
    now = FixedDT.now().timestamp()
    policy = {"generated_epoch": now - 10, "stale_after_s": 300,
              "proactive_enabled": True, "proactive_rate_per_hour": 30.0}
    f = TMP / "gate_ok.json"
    f.write_text(json.dumps(policy), "utf-8")
    pol, reason = budget_gate.load_policy(f, now_epoch=now)
    check("闸门: 正常策略读取", pol is not None and reason == "ok", reason)
    g = budget_gate.evaluate_gate(f, interval_s=300, now_epoch=now, random_value=0.5)
    check("闸门: 正常速率 → 概率封顶1 → 放行", g["allow"] and g["probability"] == 1.0, g)
    policy["proactive_rate_per_hour"] = 999
    f.write_text(json.dumps(policy), "utf-8")
    g = budget_gate.evaluate_gate(f, interval_s=300, now_epoch=now, random_value=0.0)
    check("闸门: 速率被截断到基准60", g["raw_rate_per_hour"] == 60.0, g["raw_rate_per_hour"])


def t_gate_conservative_on_anomaly():
    """验收7：策略文件异常 → 保守降频（且网关硬限额独立不受影响）。"""
    now = FixedDT.now().timestamp()
    pol, reason = budget_gate.load_policy(TMP / "gate_missing.json", now_epoch=now)
    check("验收7: 缺失 → missing", pol is None and reason == "missing", reason)
    bad = TMP / "gate_bad.json"
    bad.write_text("{not json", "utf-8")
    pol, reason = budget_gate.load_policy(bad, now_epoch=now)
    check("验收7: 损坏 → invalid", pol is None and reason == "invalid", reason)
    stale = TMP / "gate_stale.json"
    stale.write_text(json.dumps({"generated_epoch": now - 4000, "stale_after_s": 300,
                                 "proactive_rate_per_hour": 60.0}), "utf-8")
    pol, reason = budget_gate.load_policy(stale, now_epoch=now)
    check("验收7: 过期 → stale", pol is None and reason == "stale", reason)
    g = budget_gate.evaluate_gate(bad, interval_s=300, now_epoch=now, random_value=0.1)
    check("验收7: 异常 → 保守速率3次/时", g["rate_per_hour"] == 3.0, g)
    check("验收7: 保守下概率0.25（不是停摆也不是放飞）",
          abs(g["probability"] - 0.25) < 1e-9 and g["allow"], g["probability"])
    gw_src = Path("/opt/qq-fish/gateway/app.py").read_text("utf-8")
    check("验收7: 网关不读策略文件（硬限额独立）",
          "budget_policy" not in gw_src and "policy_state" not in gw_src)


def t_gate_disabled():
    now = FixedDT.now().timestamp()
    f = TMP / "gate_off.json"
    f.write_text(json.dumps({"generated_epoch": now, "stale_after_s": 300,
                             "proactive_enabled": False,
                             "proactive_rate_per_hour": 30.0}), "utf-8")
    g = budget_gate.evaluate_gate(f, interval_s=300, now_epoch=now, random_value=0.0)
    check("闸门: proactive_enabled=false → 完全静默",
          not g["allow"] and g["rate_per_hour"] == 0.0, g)


def t_gate_asymmetric_ema():
    """降频立即、恢复渐进（防止恢复瞬间爆发）。"""
    check("EMA: 首次直取", budget_gate.smooth_rate(None, 10.0) == 10.0)
    check("EMA: 降频立即生效", budget_gate.smooth_rate(30.0, 5.0) == 5.0)
    up = budget_gate.smooth_rate(5.0, 30.0)
    check("EMA: 恢复渐进(5→30 只到15)", abs(up - 15.0) < 1e-9, up)
    check("EMA: 关闸后从0爬升", abs(budget_gate.smooth_rate(0.0, 30.0) - 12.0) < 1e-9,
          budget_gate.smooth_rate(0.0, 30.0))


def t_gate_probability_conversion():
    """速率→概率按真实检查间隔换算（不把 次/小时 当 百分比）。"""
    check("概率: 30次/时×300s → 1.0", budget_gate.trigger_probability(30, 300) == 1.0)
    check("概率: 3次/时×300s → 0.25",
          abs(budget_gate.trigger_probability(3, 300) - 0.25) < 1e-9)
    check("概率: 0 次/时 → 0", budget_gate.trigger_probability(0, 300) == 0.0)
    rate = budget_gate.resolve_proactive_rate(
        {"lambda_proactive_per_hour": 60.0, "night_factor": 0.1}, "ok")
    check("概率: v1 策略回退计算", abs(rate - 3.0) < 1e-9, rate)


# ---------------------------------------------------------------------------
# 4. 状态滞回 / 费用预测
# ---------------------------------------------------------------------------

def t_hysteresis():
    ab._state_hysteresis.clear()
    seq = [50.0, 40.0, 44.0, 50.0, 8.0]
    got = [ab._hysteresis_state("m", lam, 1.0) for lam in seq]
    # 50→normal; 40恶化立即tight; 44改善不足(<46.2)保持tight; 50≥46.2回normal; 8→critical
    check("滞回: normal→tight 立即", got[1] == "tight", got)
    check("滞回: tight→normal 需超出阈值10%", got[2] == "tight" and got[3] == "normal", got)
    check("滞回: 恶化到 critical 立即", got[4] == "critical", got)
    ab._state_hysteresis.clear()


def t_forecast_shape():
    """费用预测接口结构冒烟（临时目录无台账 → 低置信度、不编造时间）。"""
    tmp = TMP / "case_forecast"
    usage = {"models": {"deepseek-flash": {"calls": 5, "cost": 0.005},
                        "mimo-v2.6-flash": {"calls": 5, "cost": 0.005}}, "tasks": []}
    setup_env(tmp, {"deepseek-flash": 2.0, "mimo-v2.6-flash": 2.0, "bge-m3": 0.05},
              usage, {"deepseek-flash": 0.005, "mimo-v2.6-flash": 0.005},
              {"deepseek-flash": 0.001, "mimo-v2.6-flash": 0.001})
    import asyncio
    data = asyncio.run(ab.forecast())["data"]
    m = data["models"]["deepseek-flash"]
    check("预测: 结构完整", {"eta", "confidence", "calls_remaining_estimate",
                            "burn_per_hour_recent"} <= set(m), list(m))
    check("预测: 无样本 → confidence=none 且不编造耗尽时间",
          m["confidence"] == "none" and m["eta"] is None, m)
    check("预测: 剩余可调用次数可算", m["calls_remaining_estimate"] > 0,
          m["calls_remaining_estimate"])
    check("预测: 全局状态与策略一致", data["behavior_state"] == "normal",
          data["behavior_state"])


def t_forecast_eta_with_samples():
    """有真实燃烧率样本时给出 ETA 与置信度（写入临时网关台账）。"""
    import sqlite3
    tmp = TMP / "case_forecast2"
    usage = {"models": {"deepseek-flash": {"calls": 5, "cost": 0.05},
                        "mimo-v2.6-flash": {"calls": 5, "cost": 0.05}}, "tasks": []}
    setup_env(tmp, {"deepseek-flash": 0.5, "mimo-v2.6-flash": 2.0, "bge-m3": 0.05},
              usage, {}, {"deepseek-flash": 0.05, "mimo-v2.6-flash": 0.05})
    import time as _time
    db = sqlite3.connect(tmp / "budget" / "gateway.db")
    db.execute("CREATE TABLE settlements (id TEXT, model TEXT, day TEXT, cost REAL, at REAL)")
    now = _time.time()
    for i in range(12):  # 12 个样本 × 每分钟一笔 → 近3h燃烧率可算
        db.execute("INSERT INTO settlements VALUES (?,?,?,?,?)",
                   (str(i), "deepseek-flash", "2026-11-03", 0.02, now - 60 * (i + 1)))
    db.commit()
    db.close()
    import asyncio
    data = asyncio.run(ab.forecast())["data"]
    m = data["models"]["deepseek-flash"]
    check("预测: 有样本 → confidence=high", m["confidence"] == "high", m["confidence"])
    check("预测: 燃烧率 > 0", m["burn_per_hour_recent"] > 0, m["burn_per_hour_recent"])
    check("预测: ETA 已推算（今日内）", bool(m["eta"]), m["eta"])


# ---------------------------------------------------------------------------
# 5. 阶段A 网关预留套件（子进程）
# ---------------------------------------------------------------------------

def t_gateway_suite():
    proc = subprocess.run(
        [sys.executable, str(Path(__file__).with_name("test_gateway.py"))],
        capture_output=True, text=True, timeout=600,
    )
    tail = "\n".join((proc.stdout + proc.stderr).splitlines()[-6:])
    check("阶段A: 网关预留套件全绿（t_priority_reserve_* 含在内）",
          proc.returncode == 0, tail)


# ---------------------------------------------------------------------------
# 6. 运行
# ---------------------------------------------------------------------------

def main():
    order = [
        t_policy_normal_baseline,
        t_policy_tight_morning,
        t_policy_mimo_exhausted,
        t_policy_ds_tight_memory_delayed,
        t_budget_change_instant_and_no_reset,
        t_nextday_recovery_no_burst,
        t_behavior_switch_manual,
        t_gate_ok_and_cap,
        t_gate_conservative_on_anomaly,
        t_gate_disabled,
        t_gate_asymmetric_ema,
        t_gate_probability_conversion,
        t_hysteresis,
        t_forecast_shape,
        t_forecast_eta_with_samples,
        t_gateway_suite,
    ]
    for fn in order:
        try:
            fn()
        except Exception as exc:  # noqa: BLE001
            import traceback
            traceback.print_exc()
            check(fn.__name__ + " 未抛异常", False, repr(exc))
    unpatch_all()

    failed = [r for r in RESULTS if not r[1]]
    print("\n========== 阶段5 汇总 ==========")
    print(f"通过 {len(RESULTS) - len(failed)} / {len(RESULTS)}")
    for name, _, detail in failed:
        print(f"  FAIL: {name} | {detail}")
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
