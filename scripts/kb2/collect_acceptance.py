# -*- coding: utf-8 -*-
"""50 场景验收结果收集器（主机侧运行）。

关联：run_log.jsonl（时间线） × knowledge_router/metrics.jsonl（路由） ×
app 日志（工具调用） × planner dump（token/注入）。
输出 docs/router/acceptance_report.json + 控制台摘要。

用法：python3 collect_acceptance.py
"""
import json
import re
from pathlib import Path

ROOT = Path("/opt/qq-fish")
RUN_LOG = ROOT / "data/MaiMBot/knowledge_router/run_log.jsonl"
METRICS = ROOT / "data/MaiMBot/knowledge_router/metrics.jsonl"
LOGS = ROOT / "data/MaiMBot/logs"
DUMPS = LOGS / "maisaka_prompt/planner/qq_private_100000001"
OUT = ROOT / "docs/router/acceptance_report.json"


def load_jsonl(path: Path) -> list[dict]:
    if not path.exists():
        return []
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()]


def parse_ts(s: str) -> float:
    import datetime as dt
    return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%S").timestamp()


def main() -> None:
    runs = load_jsonl(RUN_LOG)
    metrics = load_jsonl(METRICS)
    print("run_log %d 条 / router metrics %d 条" % (len(runs), len(metrics)))

    # 工具调用时间线（全部 app 日志）
    tool_pat = re.compile(r'"timestamp":\s*"(\d{2}-\d{2} \d{2}:\d{2}:\d{2})".*?工具=([a-z_]+)')
    tools_by_min: dict[str, list[str]] = {}
    for log in sorted(LOGS.glob("app_*.log.jsonl")):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if "工具=" not in line:
                continue
            m = re.search(r'"timestamp":\s*"(\d{2}-\d{2} \d{2}:\d{2}:\d{2})"', line)
            t = re.search(r"工具=([a-z_]+)", line)
            if m and t:
                tools_by_min.setdefault(m.group(1)[:14], []).append(t.group(1))

    # planner dump 索引：文件名是毫秒时间戳
    dumps = {}
    if DUMPS.exists():
        for f in DUMPS.glob("*.json"):
            try:
                dumps[int(f.stem)] = f
            except ValueError:
                pass

    # replyer dump 索引（回复文本在 output_items）
    RDUMPS = LOGS / "maisaka_prompt/replyer/qq_private_100000001"
    rdumps = {}
    if RDUMPS.exists():
        for f in RDUMPS.glob("*.json"):
            try:
                rdumps[int(f.stem)] = f
            except ValueError:
                pass

    # 路由事件：以 app 日志为准（metrics.jsonl 在消息重叠时会漏 Turn 写入）
    # 日志行形如 "[MonikaRouter] 路由 stream=... scopes=[...] triggers=[...] 候选=N 采纳=M"
    route_events = []
    for log in sorted(LOGS.glob("app_*.log.jsonl")):
        for line in log.read_text(encoding="utf-8", errors="replace").splitlines():
            if "[MonikaRouter] 路由" not in line:
                continue
            try:
                d = json.loads(line)
            except Exception:  # noqa: BLE001
                continue
            ev = d.get("event", "")
            ts = d.get("timestamp", "")
            import re as _re
            sc = _re.search(r"scopes=(\[[^\]]*\])", ev)
            tg = _re.search(r"triggers=(\[[^\]]*\])", ev)
            pick = _re.search(r"采纳=(\d+)", ev)
            import ast as _ast
            try:
                scopes = _ast.literal_eval(sc.group(1)) if sc else []
                triggers = _ast.literal_eval(tg.group(1)) if tg else []
            except Exception:  # noqa: BLE001
                scopes, triggers = [], []
            route_events.append({"ts": ts, "scopes": scopes, "triggers": triggers,
                                 "picked": int(pick.group(1)) if pick else 0})
    print("app 日志路由事件 %d 条（含双写，实际约 %d 轮）" % (len(route_events), len(route_events) // 2))

    results = []
    used_events: set[int] = set()
    for run in runs:
        t0 = parse_ts(run["sent_at"])
        # 关联：triggers ⊆ case 文本（可靠主键），时间窗为辅
        route = None
        best_i = None
        for i, m in enumerate(route_events):
            if i in used_events:
                continue
            trigs = m.get("triggers") or []
            if trigs and all(t.lower() in run["text"].lower() for t in trigs):
                route, best_i = m, i
                break
        if best_i is not None:
            used_events.add(best_i)
        # 工具调用：sent_at 所在分钟 + 下一分钟
        import datetime as dt
        stamp = dt.datetime.fromtimestamp(t0).strftime("%y-%m-%d %H:%M")
        nxt = dt.datetime.fromtimestamp(t0 + 60).strftime("%y-%m-%d %H:%M")
        tool_calls = tools_by_min.get(stamp, []) + tools_by_min.get(nxt, [])
        # planner dump：sent_at 后 90s 内最近的
        cand = [ts for ts in dumps if abs(ts / 1000 - t0) <= 90]
        token_info = {}
        if cand:
            f = dumps[max(cand)]
            try:
                meta = json.loads(f.read_text(encoding="utf-8")).get("metadata", {})
                token_info = {"prompt_tokens": meta.get("prompt_tokens"),
                              "completion_tokens": meta.get("completion_tokens"),
                              "duration_ms": round(meta.get("duration_ms") or 0)}
                s = f.read_text(encoding="utf-8")
                token_info["router_in_dump"] = "知识路由" in s
            except Exception:  # noqa: BLE001
                pass
        routed = bool(route and route.get("scopes"))
        injected = bool(route and route.get("picked", 0) > 0)
        expect_ok = (routed == bool(run.get("expect_route")))
        # 回复预览：sent_at 后 90s 内最近的 replyer dump
        reply_preview = ""
        rcand = [ts for ts in rdumps if 0 <= ts / 1000 - t0 <= 90]
        if rcand:
            try:
                rd = json.loads(rdumps[max(rcand)].read_text(encoding="utf-8"))
                for it in (rd.get("output_items") or []):
                    if isinstance(it, dict):
                        for part in (it.get("parts") or []):
                            if isinstance(part, dict) and part.get("text"):
                                reply_preview += part["text"]
                reply_preview = reply_preview.strip()[:120]
            except Exception:  # noqa: BLE001
                pass
        results.append({
            "id": run["id"], "category": run["category"], "text": run["text"][:40],
            "expect_route": run.get("expect_route"), "routed": routed,
            "scopes": (route or {}).get("scopes"), "injected": injected,
            "picked": (route or {}).get("picked"),
            "tool_calls": tool_calls, "query_memory_called": "query_memory" in tool_calls,
            "tokens": token_info, "route_expect_ok": expect_ok,
            "reply_preview": reply_preview,
        })

    n = len(results)
    routed_n = sum(1 for r in results if r["routed"])
    expect_ok_n = sum(1 for r in results if r["route_expect_ok"])
    injected_n = sum(1 for r in results if r["injected"])
    qm_n = sum(1 for r in results if r["query_memory_called"])
    fp = sum(1 for r in results if r["category"] == "chitchat" and r["routed"])
    fn = sum(1 for r in results if r.get("expect_route") and not r["routed"])
    by_cat: dict[str, dict] = {}
    for r in results:
        c = by_cat.setdefault(r["category"], {"n": 0, "routed": 0, "expect_ok": 0})
        c["n"] += 1
        c["routed"] += int(r["routed"])
        c["expect_ok"] += int(r["route_expect_ok"])

    summary = {
        "total": n, "routed": routed_n, "injected": injected_n,
        "route_expect_match": expect_ok_n,
        "query_memory_calls": qm_n,
        "false_positive_chitchat": fp, "false_negative_expected": fn,
        "by_category": by_cat,
    }
    OUT.parent.mkdir(parents=True, exist_ok=True)
    OUT.write_text(json.dumps({"summary": summary, "cases": results},
                              ensure_ascii=False, indent=1), encoding="utf-8")
    print(json.dumps(summary, ensure_ascii=False, indent=1))
    print("报告:", OUT)


if __name__ == "__main__":
    main()
