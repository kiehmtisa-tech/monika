# -*- coding: utf-8 -*-
"""Monika v5 阶段1：主体身份规范化与测试污染隔离。

原则：
1. 不物理删除生产身份；测试身份移除前先登记到 v5_subject_registry（environment=test）。
2. 别名只删「测试别名」，并从 bot 自身身份上摘掉人格别名（避免主体混淆）。
3. 全部操作可回滚（备份在 backups/v5_*）。
"""
import sqlite3
import sys
import time

DB = "/opt/qq-fish/data/MaiMBot/a-memorix/metadata/metadata.db"

OWNER_UID = "100000001"
BOT_UID = "100000002"
TEST_UIDS = ("100000003", "900000111")

# 需要移除的别名（person_id, alias）
DROP_ALIASES = [
    ("b3f4ba98cc0caa19e44408d5f8072beb", "dragon-test"),   # 测试注入昵称污染主画像
    ("b3f4ba98cc0caa19e44408d5f8072beb", "灰度测试"),        # 灰度测试昵称
    ("0262f13f3194076007ac269c9b089931", "Monika"),         # bot 自身不该有人的别名
    ("0262f13f3194076007ac269c9b089931", "大肥鱼"),          # 同上；该外号由人设文本承载
]

# 文本层规范化：只会改写展示文本，不改主体键
TEXT_REWRITES = [
    ("目标人物", None),        # None 表示替换为该 scope 对应主称呼（运行时解析）
    ("（ 丨丨）", "（  丨丨）"),  # 单空格变体 → canonical 昵称（双空格）
]

APPLY = "--apply" in sys.argv


def person_name(c, pid):
    row = c.execute("select canonical_name from person_identities where person_id=?", (pid,)).fetchone()
    return row[0] if row else ""


def main():
    c = sqlite3.connect(DB, timeout=30)
    c.row_factory = sqlite3.Row

    if not APPLY:
        print("[dry-run] 以下为拟执行动作\n")

    # 1) 登记表
    print("== 1. 建立主体登记表 v5_subject_registry ==")
    ddl = """
    CREATE TABLE IF NOT EXISTS v5_subject_registry (
        person_id TEXT PRIMARY KEY,
        canonical_id TEXT NOT NULL,
        role TEXT NOT NULL,
        environment TEXT NOT NULL,
        note TEXT,
        updated_at REAL NOT NULL
    )
    """
    if APPLY:
        c.execute(ddl)

    rows = c.execute("select * from person_identities").fetchall()
    now = time.time()
    for r in rows:
        pid, uid = r["person_id"], r["user_id"]
        if uid == OWNER_UID:
            entry = (pid, "qq:" + uid, "owner_admin", "production", "唯一管理员；人格关系 special_person，不公开宣示")
        elif uid == BOT_UID:
            entry = (pid, "bot:monika", "bot_self", "production", "Bot 自身 QQ，不作为人物画像注入对象")
        elif uid in TEST_UIDS:
            entry = (pid, "qq:" + uid, "test_identity", "test", "测试/注入账号，排除生产 Prompt")
        else:
            entry = (pid, "qq:" + uid, "friend", "production", "普通用户，人格关系 friend")
        print("  +", entry)
        if APPLY:
            c.execute(
                "insert or replace into v5_subject_registry values (?,?,?,?,?,?)", entry + (now,)
            )

    # 2) 删除测试别名与 bot 人格别名
    print("\n== 2. 移除污染别名 ==")
    for pid, alias in DROP_ALIASES:
        n = c.execute(
            "select count(*) from person_identity_aliases where person_id=? and alias=?", (pid, alias)
        ).fetchone()[0]
        print("  - %s  %-14s (命中 %d 行)  [%s]" % (pid[:8], alias, n, person_name(c, pid)))
        if APPLY and n:
            c.execute(
                "delete from person_identity_aliases where person_id=? and alias=?", (pid, alias)
            )

    # 3) 测试身份移出 person_identities（已登记到 registry，可回溯）
    print("\n== 3. 隔离测试身份 ==")
    for uid in TEST_UIDS:
        row = c.execute("select person_id,canonical_name from person_identities where user_id=?", (uid,)).fetchone()
        if not row:
            print("  - qq:%s 不存在，跳过" % uid)
            continue
        claims = c.execute(
            "select count(*) from fact_claims where scope_id=? or scope_id like ?",
            (row["person_id"], "%" + row["person_id"] + "%"),
        ).fetchone()[0]
        print("  - qq:%s  %s  关联claim=%d → 移出 person_identities（registry 保留 test 标记）"
              % (uid, row["canonical_name"], claims))
        if APPLY:
            c.execute("delete from person_identity_aliases where person_id=?", (row["person_id"],))
            c.execute("delete from person_identities where person_id=?", (row["person_id"],))
            c.execute("delete from person_profile_refresh_queue where person_id=?", (row["person_id"],))

    # 4) bot 自身不再进入画像刷新队列
    print("\n== 4. bot 自身排除出画像队列 ==")
    n = c.execute("select count(*) from person_profile_refresh_queue where person_id=?", ("0262f13f3194076007ac269c9b089931",)).fetchone()[0]
    print("  - bot 队列行 %d 条 → 删除" % n)
    if APPLY:
        c.execute("delete from person_profile_refresh_queue where person_id=?", ("0262f13f3194076007ac269c9b089931",))

    # 5) 文本层规范化
    print("\n== 5. 文本层主体称谓规范化 ==")
    for pid in {r["person_id"] for r in rows}:
        name = person_name(c, pid)
        if not name:
            continue
        q = c.execute(
            "select claim_id, value_text from fact_claims where scope_id=? and value_text like '%目标人物%'", (pid,)
        ).fetchall()
        for r in q:
            new = r["value_text"].replace("目标人物", name)
            print("  ~ %s  %r → %r" % (r["claim_id"][:8], r["value_text"][:38], new[:38]))
            if APPLY:
                c.execute("update fact_claims set value_text=?, updated_at=? where claim_id=?",
                          (new, time.time(), r["claim_id"]))

    for old, new in TEXT_REWRITES:
        if new is None:
            continue
        q = c.execute("select claim_id, value_text from fact_claims where value_text like ?", ("%" + old + "%",)).fetchall()
        for r in q:
            nv = r["value_text"].replace(old, new)
            print("  ~ %s  %r → %r" % (r["claim_id"][:8], r["value_text"][:38], nv[:38]))
            if APPLY:
                c.execute("update fact_claims set value_text=?, updated_at=? where claim_id=?",
                          (nv, time.time(), r["claim_id"]))

    # 6) 主画像入队刷新
    print("\n== 6. 主画像加入刷新队列 ==")
    print("  + b3f4ba98 → pending (reason=identity_normalized)")
    if APPLY:
        c.execute(
            "insert or replace into person_profile_refresh_queue "
            "(person_id,status,reason,source_query_tool_id,retry_count,last_error,requested_at,updated_at) "
            "values (?,?,?,?,?,?,?,?)",
            ("b3f4ba98cc0caa19e44408d5f8072beb", "pending", "identity_normalized", None, 0, None, now, now),
        )

    if APPLY:
        c.commit()
        print("\n已提交。")
    else:
        print("\n[dry-run] 未写入。加 --apply 生效。")
    c.close()


if __name__ == "__main__":
    main()
