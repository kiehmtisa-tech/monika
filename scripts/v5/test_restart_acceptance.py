"""重启验收（验收 #3/#4 + C-20 重启侧）。分两阶段：

PHASE=pre  （重启前）：写入边界与消息（直写 DB，不触发 bot 回复）、flush 基线、一条真实注入测并发
PHASE=post （重启后）：验证最新边界截断、flush 不重复、记忆未动、并发消息已入库

在 core 容器内运行。
"""
from __future__ import annotations

import asyncio
import json
import os
import sqlite3
import subprocess
import sys
import time
from datetime import datetime

sys.path.insert(0, "/MaiMBot/plugins")
PHASE = os.environ.get("PHASE", "post")
STATE = "/tmp/restart_acceptance_state.json"
METADATA_DB = "/MaiMBot/data/a-memorix/metadata/metadata.db"
SID = "test-restart-accept-%d" % int(time.time())   # 独立测试会话，不碰真实聊天

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  %s %s %s" % ("✅" if ok else "❌", name, detail))


def db_counts() -> dict:
    c = sqlite3.connect(METADATA_DB)
    out = {}
    for t in ("paragraphs", "fact_claims", "episodes", "entities"):
        out[t] = c.execute("select count(*) from %s" % t).fetchone()[0]
    c.close()
    return out


async def write_msg(text: str, mid: str) -> None:
    """直写一条会话消息到 DB（不触发回复链路）。"""
    from src.chat.message_receive.message import SessionMessage
    from src.common.data_models.mai_message_data_model import MessageInfo, UserInfo
    from src.common.data_models.message_component_data_model import MessageSequence
    from src.common.utils.utils_message import MessageUtils

    now = datetime.now()
    m = SessionMessage(message_id=mid, timestamp=now, platform="qq", received_at=now)
    m.account_id = ""
    m.scope = None
    m.message_info = MessageInfo(
        user_info=UserInfo(user_id="100000001", user_nickname="restart-test", user_cardname=""),
        group_info=None, additional_config={},
    )
    m.session_id = SID
    m.is_mentioned = m.is_at = m.is_emoji = m.is_picture = m.is_notify = False
    m.reply_to = m.reply_frequency = None
    m.processed_plain_text = text
    m.raw_message = MessageSequence([]).text(text)
    await MessageUtils.store_message_to_db_async(m)


async def main() -> None:
    from monika_governance import flush as F
    from monika_governance.context_guard import ContextGuard

    if PHASE == "pre":
        from src.common.database.database import get_db_session
        from src.common.database.database_model import Messages

        g = ContextGuard(audit_path="/MaiMBot/data/governance/test_restart_audit.jsonl")
        print("PRE: SID=%s" % SID)

        # 消息0（边界前基底）
        await write_msg("消息零：在所有边界之前", "rt_msg0_%d" % int(time.time()))
        await g._do_rotate(SID, reason="restart_test_boundary_1")
        await write_msg("消息一：在边界一之后、边界二之前", "rt_msg1_%d" % int(time.time()))
        await g._do_rotate(SID, reason="restart_test_boundary_2")
        await write_msg("消息二：在边界二之后、边界三之前", "rt_msg2_%d" % int(time.time()))
        await g._do_rotate(SID, reason="restart_test_boundary_3")
        await write_msg("消息三：在最后一条边界之后，重启后必须保留", "rt_msg3_%d" % int(time.time()))

        # #3 并发：真实注入（走完整入库链路）+ 同时轮换真实会话
        real_sid = "97ad5577bb5b176266865c0759532ad5"
        g2 = ContextGuard(audit_path="/MaiMBot/data/governance/test_restart_audit.jsonl")
        inject_task = asyncio.create_task(asyncio.to_thread(
            subprocess.run,
            ["/MaiMBot/.venv/bin/python", "/tmp/inject_msg.py",
             "并发验收：这条消息与轮换同时发生，不得丢失"],
            capture_output=True, timeout=60,
        ))
        await asyncio.sleep(0.3)
        await g2._do_rotate(real_sid, reason="restart_test_concurrent")
        await inject_task
        await asyncio.sleep(10)   # 等入库完成

        # C-20：flush 基线
        fl = F.IdleFlush(state_path="/MaiMBot/data/governance/flush_state.json",
                         idle_seconds=1, min_messages=2, daily_cap=5)
        chat = "test-restart-flush-%d" % int(time.time())
        fl.note_message(chat, "重启测试：今天完成了轮换验收部署", ts=time.time() - 20)
        fl.note_message(chat, "重启测试：下午继续工具链优化", ts=time.time() - 19)
        p_before = db_counts()["paragraphs"]
        ok = await F.flush_one(chat, fl)
        await asyncio.sleep(3)
        p_after = db_counts()["paragraphs"]

        with get_db_session() as s:
            rows = s.query(Messages).filter(Messages.session_id == SID).all()
            real_rows = s.query(Messages).filter(Messages.session_id == real_sid).all()
        markers = sum(1 for r in rows
                      if "maisaka_context_cleared" in str(getattr(r, "additional_config", "") or ""))
        conc_ok = any("并发验收" in (r.processed_plain_text or "") for r in real_rows)
        state = {
            "sid": SID, "flush_chat": chat,
            "p_before": p_before, "p_after": p_after, "flush_ok": bool(ok),
            "marker_count": markers, "db_counts": db_counts(),
            "concurrent_msg_stored": conc_ok,
        }
        with open(STATE, "w", encoding="utf-8") as fh:
            json.dump(state, fh, ensure_ascii=False, indent=2)
        print(json.dumps(state, ensure_ascii=False))
        print("PRE 完成（边界 %s 条，并发消息入库 %s）。请重启 core 后 PHASE=post 运行" % (markers, conc_ok))

    else:
        from src.chat.message_receive.message import SessionMessage
        from src.common.database.database import get_db_session
        from src.common.database.database_model import Messages
        from src.maisaka.context.clear_context import (
            is_clear_context_marker,
            select_messages_after_latest_clear_marker,
        )

        with open(STATE, encoding="utf-8") as fh:
            st = json.load(fh)
        sid = st["sid"]
        print("POST: SID=%s" % sid)

        with get_db_session() as s:
            rows = s.query(Messages).filter(Messages.session_id == sid).order_by(
                Messages.timestamp.asc()).all()
        objs, markers = [], []
        for r in rows:
            try:
                m = SessionMessage.from_db_instance(r)
                objs.append(m)
                if is_clear_context_marker(m):
                    markers.append(m)
            except Exception:
                pass
        after = select_messages_after_latest_clear_marker(objs)
        after_texts = [(m.processed_plain_text or "")[:26] for m in after]
        all_texts = [(r.processed_plain_text or "") for r in rows]

        check("#4-a 三条边界全部持久化", len(markers) >= 3,
              "markers=%s pre=%s" % (len(markers), st.get("marker_count")))
        check("#4-b 只保留最新边界之后的消息",
              any("消息三" in t for t in after_texts) and
              not any("消息一" in t for t in after_texts) and
              not any("消息二" in t for t in after_texts) and
              not any("消息零" in t for t in after_texts),
              "after=%s" % after_texts)
        check("#4-c 原始消息全部保留在 DB（不删除）",
              any("消息零" in t for t in all_texts) and any("消息一" in t for t in all_texts))

        # #3 并发消息（重启前已在真实会话验证入库）
        check("#3-a 与轮换并发的消息未丢失（重启前已入库）",
              bool(st.get("concurrent_msg_stored")))

        # C-20 重启侧
        from monika_governance import flush as F
        fl = F.IdleFlush(state_path="/MaiMBot/data/governance/flush_state.json",
                         idle_seconds=1, min_messages=2, daily_cap=5)
        p1 = db_counts()["paragraphs"]
        ok2 = await F.flush_one(st["flush_chat"], fl)
        await asyncio.sleep(3)
        p2 = db_counts()["paragraphs"]
        check("C-20 重启后同窗口不重复写回", p2 == p1, "p1=%s p2=%s ok=%s" % (p1, p2, ok2))

        # #6 A-Memorix 一致性（flush 的增量已体现在 pre 的 p_after）
        c_now = db_counts()
        c_pre = st["db_counts"]
        diffs = {k: (c_pre[k], c_now[k]) for k in c_pre if c_pre[k] != c_now[k]}
        check("#6 重启未改变 A-Memorix", not diffs, str(diffs) if diffs else "")

        print()
        print("=== 结果: PASS=%d FAIL=%d ===" % (len(PASS), len(FAIL)))
        if FAIL:
            print("失败项:", FAIL)
            sys.exit(1)


asyncio.run(main())
