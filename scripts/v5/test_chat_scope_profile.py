import asyncio, sys
sys.path.insert(0, "/MaiMBot")

async def main():
    from src.A_memorix.core.utils.person_profile_service import PersonProfileService
    from src.A_memorix.core.storage.metadata_store import MetadataStore

    store = MetadataStore("/MaiMBot/data/a-memorix/metadata")
    store.connect()
    svc = PersonProfileService(metadata_store=store)

    PERSON = "b3f4ba98cc0caa19e44408d5f8072beb"
    PRIV = "97ad5577bb5b176266865c0759532ad5"
    GROUP = "cffa132ca92c70d667937f52abae5ec8"

    SENSITIVE = ["喜欢", "酒馆战棋", "愿意听", "不嘲笑", "钢琴", "专利"]

    def leak_score(text):
        return [w for w in SENSITIVE if w in text]

    print("=== 1) 全局画像（不带 chat_id，应含私聊内容） ===")
    g = await svc.query_person_profile(person_id=PERSON, source_note="test")
    gt = g.get("profile_text", "")
    print("  chat_scoped:", g.get("chat_scoped"), "| 泄漏词:", leak_score(gt))

    print()
    print("=== 2) 群聊画像（chat_id=群，应不含私聊内容） ===")
    gr = await svc.query_person_profile(person_id=PERSON, chat_id=GROUP, source_note="test")
    grt = gr.get("profile_text", "")
    print("  chat_scoped:", gr.get("chat_scoped"), "| 泄漏词:", leak_score(grt))
    print("  --- 群聊画像文本 ---")
    print("  " + grt[:400].replace(chr(10), " ⏎ "))

    print()
    print("=== 3) 私聊画像（chat_id=私聊，应含私聊内容） ===")
    pr = await svc.query_person_profile(person_id=PERSON, chat_id=PRIV, source_note="test")
    prt = pr.get("profile_text", "")
    print("  chat_scoped:", pr.get("chat_scoped"), "| 泄漏词:", leak_score(prt))

    print()
    print("=== 判定 ===")
    g_has = len(leak_score(gt)) > 0
    gr_clean = len(leak_score(grt)) == 0
    pr_has = len(leak_score(prt)) > 0
    print("  全局含私聊内容: %s（预期 True）" % g_has)
    print("  群聊无私聊内容: %s（预期 True，收窄生效）" % gr_clean)
    print("  私聊含私聊内容: %s（预期 True）" % pr_has)
    ok = g_has and gr_clean and pr_has
    print("  总判定:", "PASS（chat 级隔离生效）" if ok else "FAIL")

asyncio.run(main())
