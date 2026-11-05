import asyncio, sys
sys.path.insert(0, "/MaiMBot")

async def main():
    from src.chat.message_receive.message import SessionMessage
    from src.common.database.database import get_db_session
    from src.common.database.database_model import Messages
    from src.maisaka.memory.person_profile import build_person_profile_injection_messages

    PRIV = "97ad5577bb5b176266865c0759532ad5"
    GROUP = "cffa132ca92c70d667937f52abae5ec8"
    SENSITIVE = ["喜欢", "酒馆战棋", "愿意听", "不嘲笑", "钢琴"]

    def load_msg(session_id):
        with get_db_session() as s:
            row = s.query(Messages).filter(Messages.session_id == session_id).order_by(Messages.timestamp.desc()).first()
        return SessionMessage.from_db_instance(row)

    print("=== 注入链路 E2E（真实消息） ===")
    for label, sid in [("私聊", PRIV), ("群聊", GROUP)]:
        msg = load_msg(sid)
        msgs = await build_person_profile_injection_messages(anchor_message=msg)
        text = "\n".join(msgs)
        leaks = [w for w in SENSITIVE if w in text]
        print("  %s(session=%s): 注入块=%d 泄漏词=%s" % (label, sid[:12], len(msgs), leaks))

    print()
    print("判定: 群聊无泄漏 + 私聊保留 = chat 级隔离在注入链路生效")

asyncio.run(main())
