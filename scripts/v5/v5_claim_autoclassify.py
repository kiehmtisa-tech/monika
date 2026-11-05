# -*- coding: utf-8 -*-
"""Monika v5：新写入 claim 的确定性分类（低 Token，零模型调用）。

背景：A-Memorix 的 ingest_service 对不带分类的 claim 一律写成
     stability=stable / profile_section=stable_facts / confidence=1.0，
     导致一次性事件污染长期画像。本脚本在写回后做规则式纠正。

保守原则：判定不明确的，一律进 recent_interactions/temporal，
         绝不因为"说不清楚"就升级为长期稳定事实。

用法：
    python3 v5_claim_autoclassify.py            # dry-run
    python3 v5_claim_autoclassify.py --apply    # 执行
可挂 cron：*/15 * * * *  cd /opt/qq-fish && python3 scripts/v5/v5_claim_autoclassify.py --apply
"""
import json
import re
import sqlite3
import sys
import time
from pathlib import Path

DB = "/opt/qq-fish/data/MaiMBot/a-memorix/metadata/metadata.db"
STATE = Path("/opt/qq-fish/data/MaiMBot/data/governance/autoclassify_state.json")
APPLY = "--apply" in sys.argv

# 一次性时间标记（有这些词 → 大概率是事件而非长期事实）
_EPISODIC = (
    "今天", "昨日", "昨天", "刚才", "刚刚", "今早", "今晚", "今夜", "今晚",
    "早上", "上午", "下午", "晚上", "夜里", "昨晚", "前天", "刚才", "这一次", "那次",
    "某天", "上次", "刚在", "刚才在",
)
# 稳定表述（覆盖时间标记：即使说"今天总是"，也算稳定偏好）
_STABLE = ("总是", "每次", "一直", "长期", "经常", "习惯", "一般来说", "从来")
_TRANSIENT = ("正在", "还在", "仍在", "尚未", "还没", "没在", "临时", "目前",
              "这两天", "这几天", "最近", "接下来", "打算", "计划", "准备", "暂时")
_PREFERENCE = ("喜欢", "讨厌", "不爱", "爱吃", "爱听", "爱看", "偏爱", "偏好",
               "习惯", "常", "总是", "每次", "收藏", "想听", "想吃", "想要", "觉得好听")
_RELATION = ("答应", "承诺", "愿意", "约定", "保证", "不嘲笑", "请你", "帮我", "陪我")
_SKILL = ("不会", "会弹", "学过", "正在学", "擅长", "练过", "能弹", "能写", "水平")
_IDENTITY = ("在公司", "在上班", "住在", "老家", "学校", "读的", "职业", "工作是",
             "是做", "在读", "毕业", "部门", "岗位")
_PROJECT = ("功能", "开发", "调试", "代码", "脚本", "任务", "项目", "做完", "完成",
            "实现", "改好", "修好", "上线", "测试")

T = {
    "identity":     ("identity_settings", "stable", 0.80),
    "relationship": ("relationship_settings", "stable", 0.80),
    "preference":   ("interaction_preferences", "stable", 0.75),
    "interest":     ("interaction_preferences", "stable", 0.75),
    "episodic":     ("recent_interactions", "temporal", 0.90),
    "transient":    ("recent_interactions", "temporal", 0.70),
    "skill":        ("stable_facts", "temporal", 0.75),
    "project":      ("recent_interactions", "temporal", 0.70),
    "unknown":      ("recent_interactions", "temporal", 0.60),
}


def has_any(text, table):
    return any(word in text for word in table)


def classify(text: str) -> str:
    t = re.sub(r"\s+", "", text or "")
    if not t:
        return "unknown"
    stable = has_any(t, _STABLE)
    episodic_mark = has_any(t, _EPISODIC)
    # 稳定表述优先于时间标记
    if not stable and episodic_mark:
        return "episodic"
    if has_any(t, _TRANSIENT):
        return "transient"
    if has_any(t, _PREFERENCE):
        return "preference"
    if has_any(t, _RELATION):
        return "relationship"
    if has_any(t, _SKILL):
        return "skill"
    if has_any(t, _IDENTITY):
        return "identity"
    if has_any(t, _PROJECT):
        return "project"
    return "unknown"


def load_state():
    try:
        if STATE.exists():
            return json.loads(STATE.read_text(encoding="utf-8"))
    except Exception:
        pass
    return {"last_run": 0.0, "processed": 0}


def main():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row
    state = load_state()
    last = float(state.get("last_run", 0.0))

    rows = c.execute(
        "select claim_id, value_text, profile_section, stability, confidence, updated_at "
        "from fact_claims "
        "where status='active' "
        "  and profile_section='stable_facts' and stability='stable' and confidence >= 0.999 "
        "  and updated_at > ? order by updated_at",
        (last,),
    ).fetchall()

    print("待处理 claim：%d 条（自 %s 起；只看「仍在 ingest 默认态」的新增）\n" % (
        len(rows), time.strftime("%m-%d %H:%M", time.localtime(last)) if last else "全量"))

    changes = []
    for r in rows:
        ctype = classify(r["value_text"])
        sec, stab, conf = T[ctype]
        if (r["profile_section"], r["stability"]) == (sec, stab):
            continue
        # 只允许把 stable_facts/stable 的错标改成正确的，不把已正确的改回去
        changes.append((r["claim_id"], r["value_text"], ctype,
                        (r["profile_section"], r["stability"], r["confidence"]),
                        (sec, stab, conf)))

    hdr = "%-9s %-11s %-24s %-24s %s"
    print(hdr % ("claim", "类型", "旧", "新", "文本"))
    print("-" * 110)
    for cid, text, ctype, old, new in changes:
        print(hdr % (cid[:8], ctype,
                     "%s/%s/%.2f" % (old[0][:16], old[1], old[2]),
                     "%s/%s/%.2f" % (new[0][:22], new[1], new[2]),
                     text[:38]))

    dist = {}
    for _c, _t, ctype, _o, _n in changes:
        dist[ctype] = dist.get(ctype, 0) + 1
    if dist:
        print("\n分类结果：", ", ".join("%s=%d" % kv for kv in sorted(dist.items())))

    if not APPLY:
        print("\n[dry-run] 未写入。加 --apply 生效。")
        c.close()
        return

    now = time.time()
    for cid, text, ctype, _old, new in changes:
        c.execute(
            "update fact_claims set profile_section=?, stability=?, confidence=?, updated_at=? where claim_id=?",
            (new[0], new[1], new[2], now, cid),
        )
        c.execute(
            "insert into fact_transitions (old_claim_id,new_claim_id,transition_type,reason,evidence_type,evidence_id,created_at) "
            "values (?,?,?,?,?,?,?)",
            (cid, None, "auto_reclassified",
             "v5 写回分类：claim_type=%s（规则式，零模型）" % ctype, "rule", "v5_autoclassify", now),
        )
    c.commit()
    state.update({"last_run": now, "processed": int(state.get("processed", 0)) + len(changes)})
    STATE.parent.mkdir(parents=True, exist_ok=True)
    tmp = STATE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(STATE)
    print("\n已提交 %d 条。" % len(changes))
    c.close()


if __name__ == "__main__":
    main()
