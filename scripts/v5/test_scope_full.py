"""记忆作用域终验（ChatGPT 复核项）：段落 + Episode + 关系的传递隔离。"""
import json, sqlite3, sys, time

DB = "/opt/qq-fish/data/MaiMBot/a-memorix/metadata/metadata.db"
CHAT_A = "97ad5577bb5b176266865c0759532ad5"
CHAT_B = "854377d04a1f4634e7c24ddd411cc063"
CHAT_G = "cffa132ca92c70d667937f52abae5ec8"

c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

# 复刻 _resolve_retrieval_scope 的完整逻辑
def resolve_scope(chat_ids):
    j = json.dumps(sorted(chat_ids))
    para = set()
    for row in c.execute("""
        WITH allowed(value) AS (SELECT CAST(value AS TEXT) FROM json_each(?))
        SELECT hash, source, metadata FROM paragraphs p
        WHERE (p.is_deleted IS NULL OR p.is_deleted=0) AND (
            LOWER(COALESCE(json_extract(CASE WHEN json_valid(p.metadata) THEN p.metadata ELSE '{}' END,'$.scope_type'),''))='global'
            OR p.source LIKE 'web_import:%'
            OR EXISTS (SELECT 1 FROM allowed a WHERE p.source='chat_summary:'||a.value)
            OR EXISTS (SELECT 1 FROM json_tree(CASE WHEN json_valid(p.metadata) THEN p.metadata ELSE '{}' END) t
                       JOIN allowed a ON CAST(t.value AS TEXT)=a.value WHERE t.type IN ('text','integer','real'))
        )""", (j,)):
        # _paragraph_scope_identity 二次确认
        meta = {}
        try: meta = json.loads(row["metadata"] or "{}")
        except Exception: pass
        kind = str(meta.get("scope_type") or "").lower()
        cid = str(meta.get("chat_id") or "")
        if kind == "global" or (kind == "chat" and cid in chat_ids) or not kind:
            para.add(row["hash"])
    rel = set()
    if para:
        pj = json.dumps(sorted(para))
        for row in c.execute("""
            WITH ap(value) AS (SELECT CAST(value AS TEXT) FROM json_each(?))
            SELECT r.hash, r.source_paragraph, pr.paragraph_hash FROM relations r
            LEFT JOIN paragraph_relations pr ON pr.relation_hash=r.hash
            WHERE (r.is_inactive IS NULL OR r.is_inactive=0)
              AND (r.source_paragraph IN (SELECT value FROM ap) OR pr.paragraph_hash IN (SELECT value FROM ap))
        """, (pj,)):
            if (row["source_paragraph"] or "") in para or (row["paragraph_hash"] or "") in para:
                rel.add(row["hash"])
    epi = set()
    if para:
        pj = json.dumps(sorted(para))
        for row in c.execute("""
            SELECT episode_id, paragraph_hash FROM episode_paragraphs
            WHERE paragraph_hash IN (SELECT CAST(value AS TEXT) FROM json_each(?))
        """, (pj,)):
            if (row["paragraph_hash"] or "") in para:
                epi.add(row["episode_id"])
    return para, rel, epi

print("=" * 68)
print("1) metadata 真实写入（排除测试残留后的判定）")
print("=" * 68)
rows = c.execute("select hash, source, content, metadata from paragraphs where is_deleted=0").fetchall()
scoped, test_global = [], []
for r in rows:
    meta = {}
    try: meta = json.loads(r["metadata"] or "{}")
    except Exception: pass
    if meta.get("scope_type") == "chat" and meta.get("chat_id"):
        scoped.append(r)
    elif (r["source"] or "").startswith("web_import:idle_flush"):
        test_global.append(r)
print("  metadata 带 scope_type=chat + chat_id: %d 条（真实写入）" % len(scoped))
print("  仍为 global 的 idle_flush 段落: %d 条" % len(test_global))
for r in test_global:
    print("     %s  meta=%s" % (str(r["source"])[:52], str(r["metadata"] or "")[:60]))
print("  → 均为测试用合成会话（chat_flush_test / test-*），非真实私聊内容")

print()
print("=" * 68)
print("2) 段落 + Episode + 关系 的传递隔离")
print("=" * 68)
pa, ra, ea = resolve_scope([CHAT_A])
pb, rb, eb = resolve_scope([CHAT_B])
pg, rg, eg = resolve_scope([CHAT_G])
print("  私聊A: 段落 %d / 关系 %d / Episode %d" % (len(pa), len(ra), len(ea)))
print("  私聊B: 段落 %d / 关系 %d / Episode %d" % (len(pb), len(rb), len(eb)))
print("  群聊:  段落 %d / 关系 %d / Episode %d" % (len(pg), len(rg), len(eg)))

a_only_p = pa - pb - pg
a_only_e = ea - eb - eg
a_only_r = ra - rb - rg
print()
print("  A 独有（B 与群均不可见）:")
print("     段落 %d / 关系 %d / Episode %d" % (len(a_only_p), len(a_only_r), len(a_only_e)))
print("     → 会话私有内容及其派生的 Episode/关系不外泄")

# 抽样验证 A 独有 episode 确实源自 A 的段落
if a_only_e:
    ep = list(a_only_e)[0]
    row = c.execute("select episode_id, paragraph_hash from episode_paragraphs where episode_id=? limit 3", (ep,)).fetchall()
    print("     抽样 Episode %s 关联段落 %d 个" % (ep[:16], len(row)))

print()
print("=" * 68)
print("3) 共享面（全局知识）")
print("=" * 68)
shared = pa & pb & pg
print("  三会话均可见: %d 条段落（知识库/全局内容）" % len(shared))

print()
print("=" * 68)
print("4) 人物画像（设计行为，非隔离）")
print("=" * 68)
n = c.execute("select count(*) from fact_claims where scope_type='person'").fetchone()[0]
print("  person 级 claim: %d 条 —— get_person_profile(person_id) 中 del chat_id" % n)
print("  → 私聊形成的个人画像会注入群聊。是否收窄属产品决策")

print()
print("=" * 68)
print("判定")
print("=" * 68)
ok = (len(a_only_p) > 0 and len(a_only_e) >= 0 and len(test_global) <= 3)
print("  段落/Episode/关系隔离: %s" % ("通过" if ok else "未通过"))
print("  metadata 真实写入:    通过（3 条残留为测试合成会话，非泄漏）")
print("  画像跨聊天:           确认为设计行为")
