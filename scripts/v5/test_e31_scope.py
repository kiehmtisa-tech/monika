'''E-31/E-32：直接执行 _resolve_retrieval_scope 的作用域 SQL，对比不同 chat_id 的可见集。'''
import json
import sqlite3

DB = '/opt/qq-fish/data/MaiMBot/a-memorix/metadata/metadata.db'
c = sqlite3.connect(DB)
c.row_factory = sqlite3.Row

SCOPE_SQL = '''
WITH allowed_chat_ids(value) AS (
    SELECT CAST(value AS TEXT) FROM json_each(?)
), paragraph_candidates AS (
    SELECT hash, source, metadata,
           CASE WHEN json_valid(metadata) THEN metadata ELSE '{}' END AS metadata_json
    FROM paragraphs
    WHERE is_deleted IS NULL OR is_deleted = 0
)
SELECT hash, source FROM paragraph_candidates p
WHERE LOWER(COALESCE(json_extract(p.metadata_json, '$.scope_type'), '')) = 'global'
   OR p.source LIKE 'web_import:%'
   OR COALESCE(json_extract(p.metadata_json, '$.source'), '') LIKE 'web_import:%'
   OR EXISTS (SELECT 1 FROM allowed_chat_ids allowed
              WHERE p.source = 'chat_summary:' || allowed.value)
   OR EXISTS (SELECT 1 FROM json_tree(p.metadata_json) metadata_value
              JOIN allowed_chat_ids allowed
                ON CAST(metadata_value.value AS TEXT) = allowed.value
              WHERE metadata_value.type IN ('text', 'integer', 'real'))
'''


def scope(chat_ids):
    rows = c.execute(SCOPE_SQL, (json.dumps(sorted(chat_ids)),)).fetchall()
    return {r['hash']: r['source'] for r in rows}


A = '97ad5577bb5b176266865c0759532ad5'
B = 'test-isolation-b-1793755000'
C = 'cffa132ca92c70d667937f52abae5ec8'

sa, sb, sc = scope([A]), scope([B]), scope([C])
print('可见段落数: A=%d B=%d C=%d' % (len(sa), len(sb), len(sc)))

a_only = {h: s for h, s in sa.items() if h not in sb}
print()
print('=== E-31: A 独有（B 不可见） %d 条 ===' % len(a_only))
for s in list({v for v in a_only.values()})[:6]:
    print('  例:', s[:64])

global_shared = {h: s for h, s in sa.items() if h in sb}
print()
print('=== 共享（全局/知识） %d 条 ===' % len(global_shared))
for s in list({v for v in global_shared.values()})[:4]:
    print('  例:', s[:64])

ok_e31 = len(a_only) > 0
print()
print('E-31 判定:', '通过 - A 的会话内容不出现在 B 的作用域' if ok_e31 else '需检查')

shared = scope([B, A])
b_sees_a = sum(1 for h in sa if h in shared)
print()
print('=== E-32 共享组机制 ===')
print('  shared_memory_groups=[A,B] 时 B 可见 A 的段落: %d 条（应等于 A 的总量 %d）' % (b_sees_a, len(sa)))
print('  未配置共享组时 B 可见 A 的段落: %d 条（应为 0）' % sum(1 for h in a_only if h in sb))
print()
print('E-32 判定:', '通过 - 只有显式共享组/全局标记才跨会话'
      if b_sees_a == len(sa) and sum(1 for h in a_only if h in sb) == 0 else '需检查')
