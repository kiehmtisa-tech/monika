# -*- coding: utf-8 -*-
"""Monika v5 阶段0 现场审计（只读）。在宿主机 /opt/qq-fish 跑。"""
import json
import os
import sqlite3
import time
from datetime import datetime

BASE = "/opt/qq-fish"
AM = os.path.join(BASE, "data/MaiMBot/a-memorix")
DB = os.path.join(AM, "metadata/metadata.db")


def sec(t):
    print("\n" + "=" * 8, t)


c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

sec("claims 全量")
rows = c.execute(
    "select claim_id,scope_type,scope_id,fact_key,value_text,polarity,stability,"
    "profile_section,authority,status,confidence,first_observed_at,last_confirmed_at "
    "from fact_claims order by first_observed_at"
).fetchall()
print("count =", len(rows))
for r in rows:
    ts = datetime.fromtimestamp(r["first_observed_at"]).strftime("%m-%d %H:%M")
    print(
        "%s | %s:%s | key=%s | val=%r | pol=%s | stab=%s | sec=%s | auth=%s | st=%s | conf=%.2f | %s"
        % (
            r["claim_id"][:8], r["scope_type"], r["scope_id"], r["fact_key"],
            (r["value_text"] or "")[:50], r["polarity"], r["stability"],
            r["profile_section"], r["authority"], r["status"], r["confidence"], ts,
        )
    )

sec("claims 分布")
for col in ("stability", "profile_section", "authority", "status", "scope_type"):
    dist = c.execute("select %s, count(*) from fact_claims group by 1" % col).fetchall()
    print(" ", col, dict((k, v) for k, v in dist))

sec("person_identities")
for r in c.execute("select * from person_identities order by created_at"):
    print(" ", dict(r))

sec("person_identity_aliases")
for r in c.execute("select * from person_identity_aliases order by person_id, alias"):
    print(" ", dict(r))

sec("chat_summary_checkpoints")
for r in c.execute("select * from chat_summary_checkpoints"):
    d = dict(r)
    d.pop("result_json", None)
    print(" ", d)

sec("vector active.json")
with open(os.path.join(AM, "vectors/active.json"), "r", encoding="utf-8") as f:
    act = json.load(f)
print(json.dumps(act, ensure_ascii=False, indent=2)[:2000])

sec("vector spaces")
spdir = os.path.join(AM, "vectors/spaces")
for sp in sorted(os.listdir(spdir)):
    p = os.path.join(spdir, sp)
    print("\n space", sp[:16], "...")
    try:
        with open(os.path.join(p, "space.json"), "r", encoding="utf-8") as f:
            sj = json.load(f)
        keep = {k: sj.get(k) for k in sj if k in (
            "space_id", "embedding_model", "dimension", "created_at", "provider",
            "base_url", "model", "name", "kind", "updated_at")}
        print("  space.json:", json.dumps(keep, ensure_ascii=False))
    except Exception as e:
        print("  space.json err", e)
    for sub in ("paragraph", "entity", "graph", "relation"):
        d = os.path.join(p, sub)
        if not os.path.isdir(d):
            continue
        meta = os.path.join(d, "vectors_metadata.json")
        n = "?"
        dim = "?"
        if os.path.exists(meta):
            try:
                with open(meta, "r", encoding="utf-8") as f:
                    m = json.load(f)
                n = m.get("count", m.get("size", m.get("total", "?")))
                dim = m.get("dimension", m.get("dim", "?"))
            except Exception:
                pass
        vbin = os.path.join(d, "vectors.bin")
        size = os.path.getsize(vbin) if os.path.exists(vbin) else 0
        print("   %-10s count=%s dim=%s bin=%.1fKB" % (sub, n, dim, size / 1024.0))

sec("checkpoint / 维护状态")
for t in ("person_profile_refresh_queue", "episode_rebuild_sources", "storage_cleanup_jobs"):
    try:
        n = c.execute("select count(*) from %s" % t).fetchone()[0]
        print(" ", t, n)
    except Exception as e:
        print(" ", t, "err", e)

sec("最近 checkpoints 与日志中的空索引错误")
logdir = os.path.join(BASE, "data/MaiMBot/logs")
hits = 0
last = []
for fn in sorted(os.listdir(logdir)):
    if not fn.startswith("app_") or not fn.endswith(".log.jsonl"):
        continue
    path = os.path.join(logdir, fn)
    if os.path.getmtime(path) < time.time() - 6 * 3600:
        continue
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as f:
            for line in f:
                if "Indices are empty" in line or "No data to search" in line:
                    hits += 1
                    if len(last) < 3:
                        last.append(line.strip()[:220])
    except Exception:
        pass
print("  'Indices are empty' 命中行数（近 6h 日志）:", hits)
for l in last:
    print("   ", l)

sec("配置关键项")
cfg = os.path.join(BASE, "docker-config/mmc/bot_config.toml")
want = [
    "chat_summary_writeback", "person_fact_writeback", "memory_query_default_limit",
    "person_profile_injection_max_profiles", "enable_person_profile_injection",
    "heuristic_memory_recall_enabled", "context_compaction_summary_max_chars",
    "max_private_context_size", "max_context_size", "tool_result_condense",
    "enable_memory_query_tool", "enable_person_profile_query_tool",
    "relation_vectorization", "embedding_model", "data_dir",
]
with open(cfg, "r", encoding="utf-8") as f:
    for i, line in enumerate(f, 1):
        s = line.strip()
        if s.startswith("#") or "=" not in s:
            continue
        if any(w in s for w in want):
            print("  %4d  %s" % (i, s[:150]))
print("\n审计完成", datetime.now().isoformat(timespec="seconds"))
