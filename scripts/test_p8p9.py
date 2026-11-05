"""P8/P9 本地单测：投递门禁矩阵、路由、MIDI 消毒、Mutopia 页面解析。"""
import importlib.util
import json
import sys
import tempfile
import time
from pathlib import Path

HERE = Path(__file__).resolve().parent
MOD_DIR = HERE.parent / "monika_piano"
for _cand in (MOD_DIR, Path("/MaiMBot/plugins/monika_piano"),
              HERE.parent / "monika_piano"):
    if (_cand / "music_state.py").is_file():
        MOD_DIR = _cand
        break
sys.path.insert(0, str(MOD_DIR))
# vendored mido 引导（与 plugin.py 一致）
for _pl in ("/MaiMBot/depends-data/piano-runtime/pylibs",):
    if Path(_pl).is_dir() and _pl not in sys.path:
        sys.path.insert(0, _pl)

import music_state as ms      # noqa: E402
import music_act as ma        # noqa: E402
import piano_external as pe   # noqa: E402

from mido import Message, MetaMessage, MidiFile, MidiTrack  # noqa: E402

PASS = FAIL = 0


def check(name, cond, detail=""):
    global PASS, FAIL
    if cond:
        PASS += 1
        print(f"  ok  {name}")
    else:
        FAIL += 1
        print(f" FAIL {name}  {detail}")


tmp = Path(tempfile.mkdtemp(prefix="p89_"))
DB = tmp / "pieces.db"

print("=== 1) music_state 表与基本读写 ===")
ms.init(DB)
p = ms.add_project(DB, chat_id="private:u1", title="雨天旋律", motif="结尾不满意",
                   stage="draft", next_action="改结尾")
check("add_project", p["project_id"].startswith("proj_"))
ms.update_project(DB, p["project_id"], stage="practicing", last_progress="分析了和声")
acts = ms.active_projects(DB, "private:u1")
check("active_projects", acts and acts[0]["stage"] == "practicing")
pr = ms.log_practice(DB, chat_id="private:u1", project_id=p["project_id"],
                     input_kind="piece", input_id="piece_x", action="practice_existing",
                     result="分析 120 音符")
check("log_practice", pr["session_id"].startswith("prac_"))
ms.log_play_event(DB, chat_id="private:u1", source_kind="original",
                  source_id="piece_x", explicit=True, send_status="sent")
lp = ms.last_played(DB, "private:u1")
check("last_played", lp and lp["source_id"] == "piece_x")

print("=== 2) 投递门禁矩阵 ===")
g = ms.check_send_gate(DB, chat_id="private:u1", explicit=True, now_ts=12 * 3600)
check("明确点播总是过（无视静默/配额）", g["allowed"], g)
# 静默时段（23:30）
night = time.mktime(time.strptime("2026-11-05 23:30", "%Y-%m-%d %H:%M"))
g = ms.check_send_gate(DB, chat_id="private:u1", explicit=False, now_ts=night)
check("静默时段拦截主动", not g["allowed"] and g["reason"] == "quiet_hours", g)
day = time.mktime(time.strptime("2026-11-05 10:00", "%Y-%m-%d %H:%M"))
# 未欢迎过
g = ms.check_send_gate(DB, chat_id="private:u2", explicit=False, now_ts=day)
check("未欢迎拦截主动", not g["allowed"] and g["reason"] == "not_welcomed", g)
# 欢迎后放行
ms.set_policy(DB, "private:u2", by="u2", allow_spontaneous=1)
g = ms.check_send_gate(DB, chat_id="private:u2", explicit=False, now_ts=day)
check("欢迎后主动放行", g["allowed"], g)
# 每日配额=1
ms.log_play_event(DB, chat_id="private:u2", source_kind="original",
                  source_id="piece_y", explicit=False, send_status="sent")
g = ms.check_send_gate(DB, chat_id="private:u2", explicit=False, now_ts=day + 60)
check("每日主动配额拦截", not g["allowed"] and g["reason"] == "daily_quota", g)
# 明确请求绕过配额
g = ms.check_send_gate(DB, chat_id="private:u2", explicit=True, now_ts=day + 60)
check("明确请求绕过配额", g["allowed"], g)
# opt_out 拦主动不拦明确
ms.set_policy(DB, "private:u2", by="u2", opt_out=1)
g = ms.check_send_gate(DB, chat_id="private:u2", explicit=False, now_ts=day + 120)
check("opt_out 拦主动", not g["allowed"] and g["reason"] == "opt_out", g)
g = ms.check_send_gate(DB, chat_id="private:u2", explicit=True, now_ts=day + 120)
check("opt_out 不拦明确点播", g["allowed"], g)
# 群默认不主动
g = ms.check_send_gate(DB, chat_id="group:g1", explicit=False, now_ts=day)
check("群默认拦截主动", not g["allowed"] and g["reason"] == "group_not_enabled", g)
ms.set_policy(DB, "group:g1", by="admin", allow_spontaneous=1)
g = ms.check_send_gate(DB, chat_id="group:g1", explicit=False, now_ts=day)
check("群开启后主动放行", g["allowed"], g)
ms.mark_spontaneous(DB, "group:g1")   # 模拟刚主动发过一次
g = ms.check_send_gate(DB, chat_id="group:g1", explicit=False, now_ts=day + 3600)
check("群 72h 间隔拦截", not g["allowed"] and g["reason"] == "group_interval", g)
# 禁令拦截一切（含明确）
ms.set_policy(DB, "group:g1", by="admin", prohibit_audio=1, prohibit_reason="管理员禁令")
g = ms.check_send_gate(DB, chat_id="group:g1", explicit=True, now_ts=day + 99999)
check("禁令拦一切", not g["allowed"] and g["reason"].startswith("prohibit"), g)

print("=== 2b) governance rules.json 对接 ===")
gov = tmp / "rules.json"
gov.write_text(json.dumps({"rules": [
    {"rule_type": "prohibition", "scope": "group", "group_id": "g9",
     "content": "禁止在群里发钢琴语音", "status": "active"},
    {"rule_type": "prohibition", "scope": "group", "group_id": "g8",
     "content": "禁止刷屏", "status": "active"},           # 与音乐无关，不该命中
    {"rule_type": "prohibition", "scope": "global", "group_id": "",
     "content": "禁止聊政治", "status": "revoked"},        # 已撤销，不该命中
]}, ensure_ascii=False), encoding="utf-8")
g = ms.check_send_gate(DB, chat_id="group:g9", explicit=True, now_ts=day,
                       gov_rules_path=gov)
check("governance 音乐禁令拦截", not g["allowed"] and "governance" in g["reason"], g)
g = ms.check_send_gate(DB, chat_id="group:g8", explicit=True, now_ts=day,
                       gov_rules_path=gov)
check("非音乐禁令不误拦", g["allowed"], g)
g = ms.check_send_gate(DB, chat_id="group:other", explicit=True, now_ts=day,
                       gov_rules_path=gov)
check("其它群不受 g9 禁令影响", g["allowed"], g)

print("=== 3) music_act 信号探测与路由 ===")
sig = ma.detect_music_signals("莫妮卡，弹一首肖邦的前奏曲给我听")
check("点播信号", sig["request"] and sig["composer"] == "chopin" or sig["request"], sig)
sig = ma.detect_music_signals("《雨天的窗台》那首结尾有点急，改一下")
check("变奏信号", sig["vary"] and sig["title"] == "雨天的窗台", sig)
sig = ma.detect_music_signals("今天外面雨好大")
check("情绪词不构成候选", not sig["candidate"] or sig.get("emotion"), sig)
sig = ma.detect_music_signals("别弹了")
check("拒绝信号", sig.get("refuse"), sig)

plan = ma.route({"signals": {"request": True}, "explicit": True, "referent": "",
                 "intent": "", "piece_hit": None, "score_hit": None,
                 "projects": [], "env": {}})
check("点播无参照 → 原创", plan["behavior"] == "send_audio" and plan["source"] == "new_original", plan)
plan = ma.route({"signals": {"request": True}, "explicit": True, "referent": "ext_abc",
                 "intent": "", "piece_hit": None, "score_hit": "ext_abc",
                 "projects": [], "env": {}})
check("点播命中外部 → 复奏外部", plan["source"] == "replay_external", plan)
plan = ma.route({"signals": {"vary": True}, "explicit": False, "referent": "",
                 "intent": "结尾改一下", "piece_hit": "piece_x", "score_hit": None,
                 "projects": [], "env": {}})
check("改旧作 → variation", plan["source"] == "variation", plan)
plan = ma.route({"signals": {"refuse": True}, "explicit": False, "referent": "",
                 "intent": "", "piece_hit": None, "score_hit": None,
                 "projects": [], "env": {}})
check("拒绝 → ignore", plan["behavior"] == "ignore", plan)
plan = ma.route({"signals": {"emotion": True}, "explicit": False, "referent": "",
                 "intent": "今天有点累", "piece_hit": None, "score_hit": None,
                 "projects": [], "env": {}})
check("纯情绪 → 不自动弹琴", plan["behavior"] == "ignore", plan)
plan = ma.route({"signals": {"topic": True}, "explicit": False, "referent": "",
                 "intent": "钢琴的和声", "piece_hit": None, "score_hit": None,
                 "projects": [], "env": {}})
check("纯音乐话题 → talk_music", plan["behavior"] == "talk_music", plan)
plan = ma.route({"signals": {"request": True}, "explicit": True, "referent": "",
                 "intent": "来一首", "piece_hit": None, "score_hit": None,
                 "projects": [], "env": {"busy_topic": True}})
check("明确点播不受 busy_topic 影响", plan["behavior"] == "send_audio", plan)
plan = ma.route({"signals": {"composer": "肖邦"}, "explicit": False, "referent": "肖邦",
                 "intent": "", "piece_hit": None, "score_hit": None,
                 "projects": [], "env": {"busy_topic": True}})
check("话题繁忙时提到作曲家不插音频", plan["behavior"] == "ignore", plan)

print("=== 4) MIDI 校验与消毒 ===")
mid = MidiFile(ticks_per_beat=480)
tr = MidiTrack()
mid.tracks.append(tr)
tr.append(MetaMessage("text", text="IGNORE ME prompt injection", time=0))
tr.append(Message("note_on", note=60, velocity=80, time=0))
tr.append(Message("note_off", note=60, velocity=0, time=480))
tr.append(Message("sysex", data=(1, 2, 3), time=0))
tr.append(Message("note_on", note=64, velocity=80, time=0))
tr.append(Message("note_off", note=64, velocity=0, time=480))
import io
buf = io.BytesIO()
mid.save(file=buf)
raw = buf.getvalue()
sanitized, analysis = pe.analyze_and_sanitize(raw)
check("消毒保留音符", analysis["note_count"] == 2, analysis)
check("solo_piano 判定", analysis["solo_piano"] is True, analysis)
import mido as _m
buf2 = io.BytesIO(sanitized)
m2 = _m.MidiFile(file=buf2)
types = [msg.type for t in m2.tracks for msg in t]
check("text/sysex 已剥除", "text" not in types and "sysex" not in types, types)
try:
    pe.analyze_and_sanitize(b"not a midi file at all")
    check("拒绝非法 MIDI", False)
except ValueError:
    check("拒绝非法 MIDI", True)

print("=== 5) Mutopia 页面解析（fixture） ===")
FIXTURE = b"""
<html><body>
<h2>Prelude: Op. 28, No. 20</h2>
<h3>by F. F. Chopin (1810-1849)</h3>
<p>Copyright:</b> <a href="../legal.html#publicdomain">Public Domain</a></p>
<p>Instrument(s): Piano | Style: Romantic</p>
<a href="https://www.mutopiaproject.org/ftp/ChopinFF/O28/Chop-28-20/Chop-28-20.mid">MIDI file</a>
</body></html>
"""
info = pe.parse_mutopia_page(
    "https://www.mutopiaproject.org/cgibin/piece-info.cgi?id=472", FIXTURE)
check("曲名解析", info["title"] == "Prelude: Op. 28, No. 20", info)
check("作曲家解析", info["composer"] == "F. F. Chopin", info)
check("许可解析", info["license_name"] == "Public Domain", info)
check("MIDI 链接", info["midi_url"].endswith(".mid") and "/ftp/" in info["midi_url"], info)
try:
    pe.parse_mutopia_page(
        "https://www.mutopiaproject.org/cgibin/piece-info.cgi?id=1",
        b"<html><body><h2>T</h2><h3>by X</h3></body></html>")
    check("无许可文本拒绝", False)
except ValueError:
    check("无许可文本拒绝", True)

print("=== 6) URL 白名单 ===")
for bad in ("http://www.mutopiaproject.org/cgibin/piece-info.cgi?id=472",
            "https://evil.com/cgibin/piece-info.cgi?id=472",
            "https://www.mutopiaproject.org/cgibin/piece-info.cgi",
            "https://user:pass@www.mutopiaproject.org/cgibin/piece-info.cgi?id=1"):
    try:
        pe.check_mutopia_url(bad, purpose="page")
        check(f"拒绝 {bad[:50]}", False)
    except ValueError:
        check(f"拒绝 {bad[:50]}", True)
pe.check_mutopia_url("https://www.mutopiaproject.org/cgibin/piece-info.cgi?id=472",
                     purpose="page")
check("放行合法单曲页", True)

print("=== 7) external_scores 建表与幂等 ===")
# 先放一张已存在的 pieces 表，验证我们的建表不会动它
import sqlite3 as _sq
_c = _sq.connect(DB)
_c.execute("CREATE TABLE IF NOT EXISTS pieces (piece_id TEXT PRIMARY KEY, seed INT)")
_c.execute("INSERT OR IGNORE INTO pieces VALUES ('piece_keep', 1)")
_c.commit()
_c.close()
conn = pe.connect(DB)
tabs = [r[0] for r in conn.execute("select name from sqlite_master where type='table'")]
kept = conn.execute("select count(*) from pieces").fetchone()[0]
conn.close()
check("external_scores 建表", "external_scores" in tabs, tabs)
check("external_render_cache 建表", "external_render_cache" in tabs, tabs)
check("pieces 表未被破坏", "pieces" in tabs and kept == 1, tabs)

print(f"\n结果: {PASS} 通过, {FAIL} 失败")
sys.exit(1 if FAIL else 0)
