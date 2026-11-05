"""MaiBot 上下文健康（Planner Context Health）。

对应滚动记忆方案第四阶段「Dragon Board 集成」：
- 展示当前/最近输入 Tokens、System/历史/工具/记忆占比、最近 Planner 耗时与 P50/P95、
  超时次数、上下文压缩次数、最近一次短期会话轮换时间、是否异常膨胀
- 提供管理员操作：安全重置当前短期会话（写指令文件，由插件在容器内执行）
  —— 必须鉴权（main.py 的 require_auth 中间件已覆盖）+ 明确确认机制

数据源（Dashboard 后端跑在宿主，可直接读 /opt/qq-fish）：
- data/MaiMBot/logs/maisaka_prompt/{planner,replyer}/<会话>/<ts>.json   token 与构成
- data/MaiMBot/logs/maisaka_prompt/context_compaction/                  压缩次数
- data/MaiMBot/governance/context_guard.jsonl                           轮换审计
- data/MaiMBot/governance/commands.jsonl                                管理员操作指令（插件轮询执行）
- budget/gateway_status.json                                            预算与模型状态

轮换为什么不直接做：清运行时上下文要在 MaiBot 主进程内调 heartflow_manager，
Dashboard 是另一个进程，跨进程直调不可靠（A-Memorix 有单写者锁的教训）。
改为写指令文件，由 monika-governance 插件在容器内轮询执行，审计一致。
"""
import json
import os
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, HTTPException
from pydantic import BaseModel

router = APIRouter(prefix="/api/maibot-context", tags=["maibot_context"])

MAIBOT_DATA = Path("/opt/qq-fish/data/MaiMBot")
PROMPT_DIR = MAIBOT_DATA / "logs" / "maisaka_prompt"
GOV_DIR = MAIBOT_DATA / "governance"
AUDIT_FILE = GOV_DIR / "context_guard.jsonl"
COMMAND_FILE = GOV_DIR / "commands.jsonl"
BUDGET_FILE = Path("/opt/qq-fish/budget") / "gateway_status.json"

# 与插件 monika_governance/config.toml 的 [guard] 对齐
SOFT_TOKENS = 8000
HARD_TOKENS = 12000


# ---------------- 工具 ----------------

def _load_json(path: Path, default):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return default


def _read_jsonl(path: Path, limit: int = 500) -> List[dict]:
    rows: List[dict] = []
    if not path.exists():
        return rows
    try:
        with path.open("r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rows.append(json.loads(line))
                except Exception:
                    continue
    except Exception:
        return []
    return rows[-limit:]


def _percentile(values: List[int], p: float) -> int:
    if not values:
        return 0
    values = sorted(values)
    idx = min(len(values) - 1, int(len(values) * p))
    return int(values[idx])


# ---------------- 采集 ----------------

def _collect_planner_stats(max_files: int = 200) -> Dict[str, Any]:
    """扫描 planner dump，统计 token 趋势与上下文构成。"""
    files: List[Path] = []
    if PROMPT_DIR.exists():
        for p in (PROMPT_DIR / "planner").glob("*/*.json"):
            files.append(p)
    files.sort(key=lambda p: p.stat().st_mtime, reverse=True)
    files = files[:max_files]

    tokens: List[int] = []
    durations: List[int] = []
    per_session: Dict[str, Dict[str, Any]] = {}
    composition = defaultdict(int)
    total_chars = 0

    for f in files:
        try:
            d = _load_json(f, {})
        except Exception:
            continue
        meta = d.get("metadata", {}) or {}
        total = int(meta.get("total_tokens") or 0)
        prompt = int(meta.get("prompt_tokens") or 0)
        completion = int(meta.get("completion_tokens") or 0)
        duration = int(meta.get("duration_ms") or 0)
        if total:
            tokens.append(total)
        if duration:
            durations.append(duration)

        session = f.parent.name
        s = per_session.setdefault(session, {"count": 0, "latest_total": 0, "latest_ts": 0})
        s["count"] += 1
        ts = int(f.stem) if str(f.stem).isdigit() else int(f.stat().st_mtime * 1000)
        if ts > s["latest_ts"]:
            s["latest_ts"] = ts
            s["latest_total"] = total
            s["latest_prompt"] = prompt
            s["latest_completion"] = completion

        # 上下文构成（按 item_type 的字符数）
        for item in d.get("request_items", []) or []:
            t = str(item.get("item_type") or "?")
            chars = sum(
                len(p.get("text", "")) if isinstance(p, dict) else len(str(p))
                for p in (item.get("parts") or [])
            )
            composition[t] += chars
            total_chars += chars

    return {
        "sampled_files": len(files),
        "tokens": {
            "count": len(tokens),
            "latest": tokens[0] if tokens else 0,
            "max": max(tokens) if tokens else 0,
            "p50": _percentile(tokens, 0.50),
            "p95": _percentile(tokens, 0.95),
            "over_soft": sum(1 for t in tokens if t > SOFT_TOKENS),
            "over_hard": sum(1 for t in tokens if t > HARD_TOKENS),
        },
        "duration_ms": {
            "count": len(durations),
            "p50": _percentile(durations, 0.50),
            "p95": _percentile(durations, 0.95),
            "max": max(durations) if durations else 0,
            "over_30s": sum(1 for d in durations if d > 30000),
            "over_60s": sum(1 for d in durations if d > 60000),
        },
        "composition": [
            {"item_type": k, "chars": v,
             "percent": round(100.0 * v / total_chars, 1) if total_chars else 0.0}
            for k, v in sorted(composition.items(), key=lambda kv: -kv[1])
        ],
        "composition_total_chars": total_chars,
        "sessions": [
            {"session_id": k, "count": v["count"], "latest_total": v["latest_total"],
             "latest_prompt": v.get("latest_prompt", 0),
             "latest_completion": v.get("latest_completion", 0),
             "latest_ts": v["latest_ts"]}
            for k, v in sorted(per_session.items(), key=lambda kv: -kv[1]["latest_ts"])
        ],
    }


def _collect_guard_stats() -> Dict[str, Any]:
    rows = _read_jsonl(AUDIT_FILE, limit=500)
    rotates = [r for r in rows if r.get("runtime_cleared")]
    return {
        "rotate_count": len(rotates),
        "auto_rotate_count": sum(1 for r in rotates if str(r.get("reason", "")).startswith("auto")),
        "manual_rotate_count": sum(1 for r in rotates if r.get("reason") == "admin_manual"),
        "last_rotate": rotates[-1] if rotates else None,
        "soft_tokens": SOFT_TOKENS,
        "hard_tokens": HARD_TOKENS,
    }


def _collect_compaction_count() -> int:
    n = 0
    d = PROMPT_DIR / "context_compaction"
    if d.exists():
        n = sum(1 for _ in d.glob("*/*.json"))
    return n


# ---------------- 接口 ----------------

@router.get("/health")
async def get_context_health() -> Dict[str, Any]:
    """上下文健康总览（只读）。"""
    planner = _collect_planner_stats()
    guard = _collect_guard_stats()
    budget = _load_json(BUDGET_FILE, {})
    return {
        "ok": True,
        "generated_at": time.time(),
        "thresholds": {"soft_tokens": SOFT_TOKENS, "hard_tokens": HARD_TOKENS},
        "planner": planner,
        "guard": guard,
        "compaction_count": _collect_compaction_count(),
        "budget_summary": {
            "behavior_state": budget.get("behavior_state"),
            "global_activity": budget.get("global_activity"),
            "models": {
                k: {"state": v.get("state"), "remaining": v.get("remaining")}
                for k, v in (budget.get("models") or {}).items()
            },
        },
    }


class CommandRequest(BaseModel):
    action: str                      # rotate_session | toggle_auto_rotate
    session_id: Optional[str] = None
    auto_rotate: Optional[bool] = None
    reason: str = "dashboard"
    confirm: bool = False            # 必须显式确认


@router.post("/command")
async def post_context_command(req: CommandRequest) -> Dict[str, Any]:
    """管理员操作：写指令文件，由容器内插件轮询执行。

    必须 confirm=True（前端要求二次确认），且 action 在白名单内。
    """
    if not req.confirm:
        raise HTTPException(status_code=400, detail="需要显式确认（confirm=true）")
    allowed = {"rotate_session", "toggle_auto_rotate"}
    if req.action not in allowed:
        raise HTTPException(status_code=400, detail="不支持的操作: %s" % req.action)
    if req.action == "rotate_session" and not req.session_id:
        raise HTTPException(status_code=400, detail="rotate_session 需要 session_id")

    record = {
        "ts": time.time(),
        "action": req.action,
        "session_id": req.session_id,
        "auto_rotate": req.auto_rotate,
        "reason": req.reason,
        "source": "dragon_dashboard",
    }
    GOV_DIR.mkdir(parents=True, exist_ok=True)
    with COMMAND_FILE.open("a", encoding="utf-8") as fh:
        fh.write(json.dumps(record, ensure_ascii=False) + "\n")
    return {"ok": True, "queued": record}


@router.get("/commands")
async def list_commands(limit: int = 50) -> Dict[str, Any]:
    """查看最近的管理员操作指令（审计）。"""
    return {"ok": True, "commands": _read_jsonl(COMMAND_FILE, limit=limit)}


@router.get("/sessions")
async def list_sessions() -> Dict[str, Any]:
    """会话列表（供前端选择要轮换的会话）。"""
    planner = _collect_planner_stats(max_files=400)
    return {"ok": True, "sessions": planner["sessions"]}
