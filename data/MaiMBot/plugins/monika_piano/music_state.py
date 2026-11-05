"""Monika 音乐生活状态（P8）：真实音乐记忆 + 主动音频投递门禁。

规格要点（Monika_自主钢琴行为 v2）：
  * 两次独立决策：要不要做音乐（ignore/talk/think/practice/offer/send）
    × 用什么资源（new_original/variation/replay/search/practice_existing）
  * 所有"练琴/创作"陈述必须有 practice_sessions / music_projects 真实记录支撑
  * 自主创作与主动发送分离：可以只落库不发；发送必须过门禁
  * 门禁顺序：管理员禁止令 > 用户 opt-out > 静默时段 > 主动配额（明确点播绕过配额）
  * 群聊默认不主动发音频，除非该群显式启用
  * 管理员禁止令只读对接 monika_governance 的 rules.json，不重复实现治理
"""
from __future__ import annotations

import json
import sqlite3
import time
from datetime import datetime
from pathlib import Path

SCHEMA = """
CREATE TABLE IF NOT EXISTS music_projects (
    project_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL,
    title TEXT NOT NULL DEFAULT '',
    motif TEXT NOT NULL DEFAULT '',
    stage TEXT NOT NULL DEFAULT 'idea',
    source_kind TEXT NOT NULL DEFAULT 'original',
    source_id TEXT NOT NULL DEFAULT '',
    last_progress TEXT NOT NULL DEFAULT '',
    next_action TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_music_projects_chat ON music_projects(chat_id, updated_at);
CREATE TABLE IF NOT EXISTS practice_sessions (
    session_id TEXT PRIMARY KEY,
    chat_id TEXT NOT NULL DEFAULT '',
    project_id TEXT NOT NULL DEFAULT '',
    input_kind TEXT NOT NULL DEFAULT '',
    input_id TEXT NOT NULL DEFAULT '',
    output_kind TEXT NOT NULL DEFAULT '',
    output_id TEXT NOT NULL DEFAULT '',
    action TEXT NOT NULL DEFAULT '',
    result TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_practice_chat ON practice_sessions(chat_id, created_at);
CREATE TABLE IF NOT EXISTS play_events (
    event_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    source_kind TEXT NOT NULL,
    source_id TEXT NOT NULL,
    explicit INTEGER NOT NULL DEFAULT 0,
    send_status TEXT NOT NULL DEFAULT 'sent',
    message_id TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_play_chat ON play_events(chat_id, created_at);
CREATE TABLE IF NOT EXISTS music_preferences (
    pref_id INTEGER PRIMARY KEY AUTOINCREMENT,
    chat_id TEXT NOT NULL,
    user_id TEXT NOT NULL DEFAULT '',
    key TEXT NOT NULL,
    value TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT '',
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_pref_chat ON music_preferences(chat_id);
CREATE TABLE IF NOT EXISTS music_delivery_policy (
    scope_key TEXT PRIMARY KEY,
    allow_spontaneous INTEGER NOT NULL DEFAULT 0,
    opt_out INTEGER NOT NULL DEFAULT 0,
    prohibit_audio INTEGER NOT NULL DEFAULT 0,
    prohibit_reason TEXT NOT NULL DEFAULT '',
    last_spontaneous_at REAL NOT NULL DEFAULT 0,
    updated_at REAL NOT NULL,
    updated_by TEXT NOT NULL DEFAULT ''
);
"""

# 主动音频配额（规格建议的保守初值，可配置覆盖）
DEFAULT_POLICY = {
    "quiet_hours_start": "23:00",
    "quiet_hours_end": "08:00",
    "private_spontaneous_per_day": 1,
    "group_spontaneous_interval_hours": 72,
    "group_spontaneous_enabled_default": False,
}


def connect(db_path: str | Path) -> sqlite3.Connection:
    db = Path(db_path)
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.executescript(SCHEMA)
    return conn


def init(db_path: str | Path) -> bool:
    conn = connect(db_path)
    conn.close()
    return True


# ------------------------------------------------------------------ 项目/练习

def add_project(db_path, *, chat_id: str, title: str = "", motif: str = "",
                stage: str = "idea", source_kind: str = "original",
                source_id: str = "", next_action: str = "") -> dict:
    now = time.time()
    pid = f"proj_{int(now)}_{abs(hash((chat_id, title, motif))) % 10000:04d}"
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                """INSERT INTO music_projects
                (project_id,chat_id,title,motif,stage,source_kind,source_id,
                 last_progress,next_action,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (pid, chat_id, title[:120], motif[:400], stage, source_kind, source_id,
                 "created", next_action[:200], now, now))
        return {"project_id": pid, "stage": stage}
    finally:
        conn.close()


def update_project(db_path, project_id: str, **fields) -> None:
    allowed = {"title", "motif", "stage", "source_kind", "source_id",
               "last_progress", "next_action"}
    sets, vals = [], []
    for k, v in fields.items():
        if k in allowed:
            sets.append(f"{k}=?")
            vals.append(str(v)[:400])
    if not sets:
        return
    sets.append("updated_at=?")
    vals.append(time.time())
    vals.append(project_id)
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(f"UPDATE music_projects SET {', '.join(sets)} WHERE project_id=?", vals)
    finally:
        conn.close()


def active_projects(db_path, chat_id: str = "", limit: int = 5) -> list[dict]:
    conn = connect(db_path)
    try:
        if chat_id:
            rows = conn.execute(
                """SELECT * FROM music_projects WHERE chat_id=? AND stage != 'done'
                   ORDER BY updated_at DESC LIMIT ?""", (chat_id, limit)).fetchall()
        else:
            rows = conn.execute(
                """SELECT * FROM music_projects WHERE stage != 'done'
                   ORDER BY updated_at DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def log_practice(db_path, *, chat_id: str = "", project_id: str = "",
                 input_kind: str = "", input_id: str = "",
                 output_kind: str = "", output_id: str = "",
                 action: str = "", result: str = "") -> dict:
    now = time.time()
    sid = f"prac_{int(now)}_{abs(hash((input_id, action, result))) % 10000:04d}"
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                """INSERT INTO practice_sessions
                (session_id,chat_id,project_id,input_kind,input_id,output_kind,output_id,
                 action,result,created_at) VALUES (?,?,?,?,?,?,?,?,?,?)""",
                (sid, chat_id, project_id, input_kind, input_id, output_kind, output_id,
                 action, result[:300], now))
        return {"session_id": sid}
    finally:
        conn.close()


def recent_practice(db_path, limit: int = 5) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM practice_sessions ORDER BY created_at DESC LIMIT ?",
            (limit,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


def log_play_event(db_path, *, chat_id: str, source_kind: str, source_id: str,
                   explicit: bool = True, send_status: str = "sent",
                   message_id: str = "") -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                """INSERT INTO play_events
                (chat_id,source_kind,source_id,explicit,send_status,message_id,created_at)
                VALUES (?,?,?,?,?,?,?)""",
                (chat_id, source_kind, source_id, int(explicit), send_status,
                 message_id, time.time()))
    finally:
        conn.close()


def last_played(db_path, chat_id: str) -> dict | None:
    conn = connect(db_path)
    try:
        row = conn.execute(
            """SELECT * FROM play_events WHERE chat_id=? AND send_status='sent'
               ORDER BY created_at DESC LIMIT 1""", (chat_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ------------------------------------------------------------------ 偏好

PREF_WORDS = {
    "allow_spontaneous": ("偶尔主动发", "主动分享", "欢迎你发", "可以主动发", "随便发"),
    "opt_out": ("别发语音", "不要发语音", "别弹琴", "不要弹琴", "别发音乐", "闭麦", "静音"),
}

def add_preference(db_path, *, chat_id: str, user_id: str = "",
                   key: str, value: str, source: str = "") -> None:
    conn = connect(db_path)
    try:
        with conn:
            conn.execute(
                """INSERT INTO music_preferences (chat_id,user_id,key,value,source,created_at)
                   VALUES (?,?,?,?,?,?)""",
                (chat_id, user_id, key, value, source[:200], time.time()))
    finally:
        conn.close()


def preferences(db_path, chat_id: str, limit: int = 10) -> list[dict]:
    conn = connect(db_path)
    try:
        rows = conn.execute(
            "SELECT * FROM music_preferences WHERE chat_id=? ORDER BY created_at DESC LIMIT ?",
            (chat_id, limit)).fetchall()
        return [dict(r) for r in rows]
    finally:
        conn.close()


# ------------------------------------------------------------------ 投递策略与门禁

def _policy(conn, scope_key: str) -> dict:
    row = conn.execute("SELECT * FROM music_delivery_policy WHERE scope_key=?",
                       (scope_key,)).fetchone()
    return dict(row) if row else {
        "scope_key": scope_key, "allow_spontaneous": 0, "opt_out": 0,
        "prohibit_audio": 0, "prohibit_reason": "", "last_spontaneous_at": 0.0,
        "updated_at": 0.0, "updated_by": ""}


def set_policy(db_path, scope_key: str, *, by: str = "", **flags) -> dict:
    conn = connect(db_path)
    try:
        cur = _policy(conn, scope_key)
        for k in ("allow_spontaneous", "opt_out", "prohibit_audio"):
            if k in flags:
                cur[k] = int(bool(flags[k]))
        if "prohibit_reason" in flags:
            cur["prohibit_reason"] = str(flags["prohibit_reason"])[:200]
        with conn:
            conn.execute(
                """INSERT INTO music_delivery_policy
                (scope_key,allow_spontaneous,opt_out,prohibit_audio,prohibit_reason,
                 last_spontaneous_at,updated_at,updated_by)
                VALUES (?,?,?,?,?,?,?,?)
                ON CONFLICT(scope_key) DO UPDATE SET
                 allow_spontaneous=excluded.allow_spontaneous,
                 opt_out=excluded.opt_out,
                 prohibit_audio=excluded.prohibit_audio,
                 prohibit_reason=excluded.prohibit_reason,
                 updated_at=excluded.updated_at,
                 updated_by=excluded.updated_by""",
                (scope_key, cur["allow_spontaneous"], cur["opt_out"], cur["prohibit_audio"],
                 cur["prohibit_reason"], cur["last_spontaneous_at"], time.time(), by))
        return cur
    finally:
        conn.close()


def mark_spontaneous(db_path, scope_key: str) -> None:
    conn = connect(db_path)
    try:
        cur = _policy(conn, scope_key)
        with conn:
            conn.execute(
                """INSERT INTO music_delivery_policy
                (scope_key,allow_spontaneous,opt_out,prohibit_audio,prohibit_reason,
                 last_spontaneous_at,updated_at,updated_by) VALUES (?,?,?,?,?,?,?,?)
                 ON CONFLICT(scope_key) DO UPDATE SET last_spontaneous_at=excluded.last_spontaneous_at""",
                (scope_key, cur["allow_spontaneous"], cur["opt_out"], cur["prohibit_audio"],
                 cur["prohibit_reason"], time.time(), time.time(), cur["updated_by"]))
    finally:
        conn.close()


def _in_quiet_hours(now_ts: float, policy: dict) -> bool:
    try:
        lt = datetime.fromtimestamp(now_ts)
        sh, sm = (int(x) for x in policy.get("quiet_hours_start", "23:00").split(":"))
        eh, em = (int(x) for x in policy.get("quiet_hours_end", "08:00").split(":"))
        cur = lt.hour * 60 + lt.minute
        start, end = sh * 60 + sm, eh * 60 + em
        if start <= end:
            return start <= cur < end
        return cur >= start or cur < end      # 跨午夜
    except Exception:  # noqa: BLE001
        return False


def _governance_prohibits(gov_rules_path: str | Path, chat_id: str) -> str:
    """只读对接 monika_governance 的 rules.json：有生效的音乐类禁令则返回原因。"""
    p = Path(gov_rules_path)
    if not p.is_file():
        return ""
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return ""
    group_id = chat_id.split(":", 1)[1] if chat_id.startswith("group:") else ""
    keywords = ("钢琴", "弹琴", "音乐", "语音", "琴", "曲", "audio", "piano")
    for r in data.get("rules", []):
        if r.get("status") != "active" or r.get("rule_type") != "prohibition":
            continue
        if r.get("scope") == "group" and r.get("group_id") and r.get("group_id") != group_id:
            continue
        content = r.get("content", "")
        if any(k in content for k in keywords):
            return content[:120]
    return ""


def check_send_gate(db_path, *, chat_id: str, user_id: str = "",
                    explicit: bool = True, now_ts: float | None = None,
                    policy_cfg: dict | None = None,
                    gov_rules_path: str | Path = "") -> dict:
    """发送门禁。explicit=用户明确点播；否则为主动分享。

    返回 {allowed, reason, quota_limited}。顺序：
      管理员/治理禁令 → opt_out → (主动时)静默时段 → (主动时)配额/群开关
    明确点播绕过主动配额与静默时段，但不绕过禁令与 opt_out。
    """
    cfg = dict(DEFAULT_POLICY)
    cfg.update(policy_cfg or {})
    now_ts = time.time() if now_ts is None else now_ts
    conn = connect(db_path)
    try:
        pol = _policy(conn, chat_id)
    finally:
        conn.close()

    # 1) 治理禁止令（群/全局）
    if gov_rules_path:
        reason = _governance_prohibits(gov_rules_path, chat_id)
        if reason:
            return {"allowed": False, "reason": f"governance_prohibit:{reason}",
                    "quota_limited": False}
    # 2) 本插件内的禁止/opt-out
    if pol["prohibit_audio"]:
        return {"allowed": False,
                "reason": f"prohibit:{pol.get('prohibit_reason', '')}", "quota_limited": False}
    if pol["opt_out"] and not explicit:
        return {"allowed": False, "reason": "opt_out", "quota_limited": False}
    if explicit:
        return {"allowed": True, "reason": "explicit_request", "quota_limited": False}

    # --- 以下只约束主动分享 ---
    # 3) 静默时段
    if _in_quiet_hours(now_ts, cfg):
        return {"allowed": False, "reason": "quiet_hours", "quota_limited": True}
    # 4) 私聊：需先前欢迎过 + 每日配额
    if chat_id.startswith("private:"):
        if not pol["allow_spontaneous"]:
            return {"allowed": False, "reason": "not_welcomed", "quota_limited": True}
        day_key = datetime.fromtimestamp(now_ts).strftime("%Y-%m-%d")
        conn = connect(db_path)
        try:
            n = conn.execute(
                """SELECT COUNT(*) FROM play_events
                   WHERE chat_id=? AND explicit=0 AND send_status='sent'
                   AND strftime('%Y-%m-%d', created_at, 'unixepoch')=?""",
                (chat_id, day_key)).fetchone()[0]
        finally:
            conn.close()
        if n >= int(cfg["private_spontaneous_per_day"]):
            return {"allowed": False, "reason": "daily_quota", "quota_limited": True}
    else:
        # 5) 群聊：默认关；开启后 72h 一次
        if not (pol["allow_spontaneous"] or cfg["group_spontaneous_enabled_default"]):
            return {"allowed": False, "reason": "group_not_enabled", "quota_limited": True}
        hours = float(cfg["group_spontaneous_interval_hours"])
        if now_ts - pol["last_spontaneous_at"] < hours * 3600:
            return {"allowed": False, "reason": "group_interval", "quota_limited": True}
    return {"allowed": True, "reason": "spontaneous_ok", "quota_limited": False}


def music_state_summary(db_path, chat_id: str = "") -> dict:
    """给 Planner 的轻量状态摘要（不含 MIDI/音频本体）。"""
    return {
        "projects": [{k: p[k] for k in ("project_id", "title", "stage", "source_id",
                                        "next_action", "last_progress")}
                     for p in active_projects(db_path, chat_id or "", 3)],
        "recent_practice": [{k: p[k] for k in ("session_id", "action", "input_id",
                                               "output_id", "result", "created_at")}
                            for p in recent_practice(db_path, 3)],
        "last_played": (lambda lp: {k: lp[k] for k in
                                    ("source_kind", "source_id", "created_at")} if lp else None)(
            last_played(db_path, chat_id)) if chat_id else None,
    }
