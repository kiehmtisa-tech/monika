# -*- coding: utf-8 -*-
"""验证持久化清理边界：官方读取链路是否识别 marker_message。

这一步是「重启后旧历史不重新注入」的直接证据——
select_messages_after_latest_clear_marker 是 runtime.py 恢复上下文时调用的同一个函数。
"""
import sys

from src.chat.message_receive.message import SessionMessage
from src.common.database.database import get_db_session
from src.common.database.database_model import Messages
from src.maisaka.context.clear_context import (
    is_clear_context_marker,
    select_messages_after_latest_clear_marker,
)

SID = "97ad5577bb5b176266865c0759532ad5"

with get_db_session() as s:
    rows = (
        s.query(Messages)
        .filter(Messages.session_id == SID)
        .order_by(Messages.timestamp.desc())
        .limit(10)
        .all()
    )

print("会话最近 %d 条消息：" % len(rows))
objs, markers = [], []
for r in rows:
    cfg = str(r.additional_config or "")
    is_mark = "maisaka_context_cleared" in cfg
    print("  %-24s is_command=%-5s marker=%-5s  %s" % (
        str(r.message_id)[:24], r.is_command, is_mark,
        (r.processed_plain_text or "")[:26].replace("\n", " ")))
    try:
        m = SessionMessage.from_db_instance(r)
        objs.append(m)
        if is_clear_context_marker(m):
            markers.append(m)
    except Exception as exc:  # noqa: BLE001
        print("      from_db_instance 失败:", exc)

print()
print("官方 is_clear_context_marker 识别的边界数：", len(markers))

objs_sorted = sorted(objs, key=lambda m: m.timestamp)
after = select_messages_after_latest_clear_marker(objs_sorted)
print("恢复上下文时保留的消息数：%d （应只含边界之后的）" % len(after))
for m in after[:6]:
    print("   保留:", (m.processed_plain_text or "")[:34].replace("\n", " "))

ok = len(markers) >= 1 and len(after) < len(objs_sorted)
print()
print("验证结果：", "通过（边界生效，旧历史被截断）" if ok else "未通过")
sys.exit(0 if ok else 1)
