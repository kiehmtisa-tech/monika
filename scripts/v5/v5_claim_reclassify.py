# -*- coding: utf-8 -*-
"""Monika v5 阶段3：fact_claims 重分类、语义去重与审计留痕。

设计：
- 不新增表字段，把方案里的 8 类 claim_type 映射到现有的 profile_section + stability
  （A-Memorix 的枚举是固定的：stable/temporal/uncertain + 6 个 section）。
- 判定表是确定性的逐条映射，不调用任何模型，可复现、可审计。
- 去重采用 status=superseded + fact_transitions 留痕，不物理删除。
"""
import json
import sqlite3
import sys
import time

DB = "/opt/qq-fish/data/MaiMBot/a-memorix/metadata/metadata.db"
APPLY = "--apply" in sys.argv

# claim_type -> (profile_section, stability, confidence)
T = {
    "identity":     ("identity_settings", "stable", 0.85),
    "relationship": ("relationship_settings", "stable", 0.85),
    "preference":   ("interaction_preferences", "stable", 0.80),
    "interest":     ("interaction_preferences", "stable", 0.80),
    "episodic":     ("recent_interactions", "temporal", 0.90),
    "transient":    ("recent_interactions", "temporal", 0.70),
    "skill":        ("stable_facts", "temporal", 0.80),
    "project":      ("recent_interactions", "temporal", 0.75),
}

# claim_id 前缀 -> (claim_type, superseded_by前缀 | None, 备注)
PLAN = {
    "bd5ca502": ("interest", None, "长期习惯：会玩酒馆战棋"),
    "4f2d030b": ("episodic", None, "事件：在打酒馆战棋"),
    "0b16f1bf": ("episodic", None, "事件：被发牌员针对"),
    "af15fc38": ("episodic", "0b16f1bf", "与 0b16f1bf 同义（同一次被针对）"),
    "00d16575": ("interest", "bd5ca502", "与 bd5ca502 同义"),
    "639d37bc": ("relationship", None, "愿意听琴"),
    "6f2bf1fe": ("relationship", None, "承诺：不嘲笑弹琴"),
    "0749a8b8": ("transient", "9f6f0051", "已被「听到钢琴音频」事实取代"),
    "77add8f2": ("transient", None, "当时空闲状态"),
    "9bbfe9da": ("episodic", "0b16f1bf", "与 0b16f1bf 同义"),
    "9b4bfcd2": ("relationship", None, "承诺：答应听琴"),
    "513638a6": ("project", None, "项目：加钢琴曲生成功能"),
    "e5d128ad": ("project", None, "项目：让钢琴能出声"),
    "bef75390": ("preference", None, "偏好：想听钢琴曲"),
    "b6eead60": ("skill", None, "技能状态：不会弹钢琴（可被未来更新）"),
    "5ae82e5b": ("episodic", None, "一次性请求：加表情图"),
    "223eac98": ("episodic", None, "一次性判断：表情含义"),
    "ec021886": ("project", "b27c39e0", "项目进度旧快照，已由 11-03 状态取代"),
    "19bce66d": ("identity", None, "长期身份：公司专利事务"),
    "1129bfe6": ("project", "b27c39e0", "项目进度旧快照"),
    "9af91b95": ("episodic", None, "事件：早上淋雨回宿舍"),
    "b229c6d7": ("episodic", "9af91b95", "与 9af91b95 同义"),
    "9f6f0051": ("relationship", None, "共同经历：听过并为钢琴音频给出评价"),
    "f3526e2c": ("episodic", "9f6f0051", "与 9f6f0051 同义"),
    "6d45ac67": ("episodic", "9f6f0051", "与 9f6f0051 同义"),
    "50510bc3": ("project", "b27c39e0", "项目进度旧快照"),
    "b27c39e0": ("project", None, "项目最新进度（11-03）"),
    "71f26ed0": ("episodic", "9af91b95", "与 9af91b95 同义"),
    "479daeda": ("preference", None, "审美偏好：不喜欢音符重复"),
    "3fae2805": ("project", None, "项目总览：表情包与音乐生成"),
    "6fdfc442": ("episodic", None, "事件：打完酒馆战棋"),
    "32928e93": ("preference", None, "习惯：回宿舍换衣服"),
    "f92b50af": ("episodic", None, "事件：写完专利"),
    "d7ce56c5": ("episodic", None, "事件：被领导批评、情绪低落"),
    "ffb1a88b": ("preference", None, "偏好：干活后听轻快曲子"),
    "0ce99b6f": ("preference", None, "偏好：喜欢平静的钢琴曲"),
    "f4dda466": ("preference", None, "偏好：收藏喜欢的钢琴曲"),
    "5f034fcd": ("episodic", None, "事件：下午读诗集"),
    # ddd30cc3 已被人工 retract，保持原状，不参与重分类
}


def main():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    rows = c.execute("select * from fact_claims").fetchall()
    by_pref = {r["claim_id"][:8]: r for r in rows}

    changes, skipped, unknown = [], [], []
    for pref, (ctype, by, note) in PLAN.items():
        r = by_pref.get(pref)
        if not r:
            unknown.append(pref)
            continue
        if r["status"] == "retracted":
            skipped.append((pref, "已 retracted，保持"))
            continue
        sec, stab, conf = T[ctype]
        target_status = "superseded" if by else "active"
        changes.append({
            "claim_id": r["claim_id"], "pref": pref, "text": r["value_text"],
            "old": (r["profile_section"], r["stability"], r["confidence"], r["status"]),
            "new": (sec, stab, conf, target_status),
            "ctype": ctype, "by": by, "note": note,
        })

    for pref in [r["claim_id"][:8] for r in rows]:
        if pref not in PLAN and pref != "ddd30cc3":
            skipped.append((pref, "未在判定表中，保持原状"))

    print("拟变更 %d 条 / 保持 %d 条 / 未知 %s\n" % (len(changes), len(skipped), unknown or "无"))
    hdr = "%-9s %-14s %-21s %-22s %s"
    print(hdr % ("claim", "类型", "旧(sec/stab/conf/st)", "新(sec/stab/conf/st)", "说明"))
    print("-" * 132)
    for ch in sorted(changes, key=lambda x: x["ctype"]):
        o, n = ch["old"], ch["new"]
        print(hdr % (
            ch["pref"], ch["ctype"],
            "%s/%s/%.2f/%s" % (o[0][:14], o[1], o[2], o[3][:6]),
            "%s/%s/%.2f/%s" % (n[0][:20], n[1], n[2], n[3][:6]),
            ch["text"][:44],
        ))

    dist = {}
    for ch in changes:
        key = ch["new"][0] + "/" + ch["new"][1]
        dist[key] = dist.get(key, 0) + 1
    print("\n重分类后分布：")
    for k, v in sorted(dist.items(), key=lambda x: -x[1]):
        print("  %-40s %d" % (k, v))

    if not APPLY:
        print("\n[dry-run] 未写入。加 --apply 生效。")
        c.close()
        return

    now = time.time()
    applied = 0
    for ch in changes:
        sec, stab, conf, status = ch["new"]
        c.execute(
            "update fact_claims set profile_section=?, stability=?, confidence=?, status=?, updated_at=? "
            "where claim_id=?",
            (sec, stab, conf, status, now, ch["claim_id"]),
        )
        c.execute(
            "insert into fact_transitions (old_claim_id,new_claim_id,transition_type,reason,evidence_type,evidence_id,created_at) "
            "values (?,?,?,?,?,?,?)",
            (
                ch["claim_id"],
                None,
                "v5_reclassified" if status == "active" else "v5_superseded",
                "Monika v5 记忆修复：claim_type=%s；%s" % (ch["ctype"], ch["note"]),
                "manual",
                "v5_migration",
                now,
            ),
        )
        if ch["by"]:
            tgt = by_pref.get(ch["by"])
            if tgt:
                c.execute(
                    "insert into fact_transitions (old_claim_id,new_claim_id,transition_type,reason,evidence_type,evidence_id,created_at) "
                    "values (?,?,?,?,?,?,?)",
                    (ch["claim_id"], tgt["claim_id"], "v5_merged",
                     "语义去重：合并到 %s" % tgt["claim_id"][:8], "manual", "v5_migration", now),
                )
        applied += 1

    # 画像刷新入队（生产 person 才有画像）
    people = c.execute(
        "select distinct scope_id from fact_claims where scope_type='person' and status='active'"
    ).fetchall()
    for p in people:
        c.execute(
            "insert or replace into person_profile_refresh_queue "
            "(person_id,status,reason,source_query_tool_id,retry_count,last_error,requested_at,updated_at) "
            "values (?,?,?,?,?,?,?,?)",
            (p["scope_id"], "pending", "v5_reclassified", None, 0, None, now, now),
        )

    c.commit()
    print("\n已提交 %d 条，画像刷新已入队 %d 人。" % (applied, len(people)))
    c.close()


if __name__ == "__main__":
    main()
