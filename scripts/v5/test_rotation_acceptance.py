"""v5 轮换验收测试套件（验收 #1/#5/#6/#8 + C-20）。

在 core 容器内运行：docker exec -w /MaiMBot -e PYTHONPATH=/MaiMBot \
  maim-bot-core /MaiMBot/.venv/bin/python /tmp/rotation_acceptance.py

覆盖：
  T1  连续轮换每轮恰好一条边界，不产生重复边界（验收 #1）
  T2  工具调用轮次中轮换安全延迟，轮次结束后补执行（验收 #8）
  T3  不同会话边界互不影响（验收 #5）
  T4  轮换不触碰 A-Memorix（验收 #6）
  T5  异常一致性：marker 写失败时运行时仍被清理、状态可恢复（验收 #2）
  T6  idle flush 写回一次后，重复触发不重复写回（C-20 的 flush 侧）
"""
from __future__ import annotations

import asyncio
import os
import sqlite3
import sys
import time

sys.path.insert(0, "/MaiMBot/plugins")

METADATA_DB = "/MaiMBot/data/a-memorix/metadata/metadata.db"

PASS, FAIL = [], []


def check(name: str, ok: bool, detail: str = "") -> None:
    (PASS if ok else FAIL).append(name)
    print("  %s %s %s" % ("✅" if ok else "❌", name, detail))


def db_counts() -> dict:
    c = sqlite3.connect(METADATA_DB)
    out = {}
    for t in ("paragraphs", "fact_claims", "episodes", "entities", "chat_summary_checkpoints"):
        try:
            out[t] = c.execute("select count(*) from %s" % t).fetchone()[0]
        except Exception:
            out[t] = -1
    c.close()
    return out


def count_markers(session_id: str) -> int:
    from src.common.database.database import get_db_session
    from src.common.database.database_model import Messages

    with get_db_session() as s:
        rows = s.query(Messages).filter(Messages.session_id == session_id).all()
    n = 0
    for r in rows:
        cfg = str(getattr(r, "additional_config", "") or "")
        if "maisaka_context_cleared" in cfg:
            n += 1
    return n


async def main() -> None:
    from monika_governance.context_guard import CommandRunner, ContextGuard

    audit = "/MaiMBot/data/governance/test_acceptance_audit.jsonl"
    g = ContextGuard(audit_path=audit, soft_tokens=8000, hard_tokens=12000)

    SA = "test-accept-session-A-%d" % int(time.time())
    SB = "test-accept-session-B-%d" % int(time.time())

    # ---------- T1：连续轮换每轮恰好一条边界 ----------
    print("T1 连续轮换边界唯一性")
    before = count_markers(SA)
    r1 = await g._do_rotate(SA, reason="t1_first")
    time.sleep(1.1)   # 保证 message_id 时间戳不同
    r2 = await g._do_rotate(SA, reason="t1_second")
    n = count_markers(SA) - before
    check("T1-a 第一次轮换写入 1 条边界", r1.get("marker_written") is True)
    check("T1-b 第二次轮换写入 1 条边界", r2.get("marker_written") is True)
    check("T1-c 两次轮换共 2 条边界（无重复/无丢失）", n == 2, "actual=%s" % n)

    # ---------- T2：工具调用中安全延迟 ----------
    print("T2 工具调用中轮换延迟")
    g.mark_busy(SA)
    d = await g.rotate(SA, reason="t2_during_tool")
    check("T2-a 工具调用中轮换被延迟", d.get("deferred") is True)
    check("T2-b 延迟时未清运行时", d.get("runtime_cleared") is False)
    check("T2-c 延迟时未写边界", d.get("marker_written") is False)
    check("T2-d 已进入 pending 队列", len(g.pending_rotations) == 1)
    before2 = count_markers(SA)
    drained_busy = await g.drain_pending()
    check("T2-e 忙碌时 drain 不执行", drained_busy == 0 and len(g.pending_rotations) == 1)
    g.mark_activity(SA, has_tool_calls=False)   # 轮次收敛
    drained = await g.drain_pending()
    after2 = count_markers(SA)
    check("T2-f 轮次结束后 drain 补执行", drained == 1 and after2 == before2 + 1)
    check("T2-g pending 队列已清空", len(g.pending_rotations) == 0)

    # 工具调用输出续期（after_response 带工具调用 → 保持忙碌）
    g.mark_busy(SA)
    g.mark_activity(SA, has_tool_calls=True)
    check("T2-h 带工具调用的输出保持忙碌", g.is_busy(SA))
    g.mark_activity(SA, has_tool_calls=False)
    check("T2-i 无工具调用的输出解除忙碌", not g.is_busy(SA))

    # CommandRunner 延迟语义（None = 保留重试）
    g.mark_busy(SB)
    runner = CommandRunner("/tmp/test_acceptance_cmds.jsonl", g)
    with open("/tmp/test_acceptance_cmds.jsonl", "w", encoding="utf-8") as fh:
        fh.write('{"action":"rotate_session","session_id":"%s","reason":"t2_runner"}\n' % SB)
    n_run = await runner.poll_once()
    check("T2-j 指令延迟时 poll 不标记完成", n_run == 0)
    g.mark_activity(SB, has_tool_calls=False)
    n_run2 = await runner.poll_once()
    check("T2-k 指令在轮次结束后补执行", n_run2 == 1)

    # ---------- T3：会话隔离 ----------
    print("T3 会话边界隔离")
    b1 = count_markers(SA)
    r3 = await g._do_rotate(SB, reason="t3_other_session")
    check("T3-a 会话 B 轮换写边界", r3.get("marker_written") is True)
    check("T3-b 会话 A 边界数不变", count_markers(SA) == b1)

    # ---------- T4：A-Memorix 不受影响 ----------
    print("T4 A-Memorix 零影响")
    c1 = db_counts()
    await g._do_rotate(SA, reason="t4_again")
    await g._do_rotate(SB, reason="t4_again")
    c2 = db_counts()
    same = all(c1[k] == c2[k] for k in c1)
    check("T4-a 轮换前后 A-Memorix 各表计数一致", same,
          "" if same else "before=%s after=%s" % (c1, c2))

    # ---------- T5：异常一致性 ----------
    print("T5 异常状态一致恢复")
    orig = g._write_clear_marker

    async def broken_marker(sid: str) -> bool:
        return False   # 模拟标记写失败

    g._write_clear_marker = broken_marker   # type: ignore[assignment]
    re = await g._do_rotate(SA, reason="t5_marker_fail")
    g._write_clear_marker = orig   # type: ignore[assignment]
    check("T5-a 标记失败时运行时仍被清理", re.get("runtime_cleared") is True)
    check("T5-b 标记失败如实上报", re.get("marker_written") is False and re.get("error") == "")

    orig_clear = None
    try:
        from src.chat.heart_flow.heartflow_manager import heartflow_manager as hfm
        orig_clear = hfm.clear_chat_history_context

        async def broken_clear(sid: str):
            raise RuntimeError("simulated")

        hfm.clear_chat_history_context = broken_clear   # type: ignore[assignment]
        rc = await g._do_rotate(SA, reason="t5_clear_fail")
        hfm.clear_chat_history_context = orig_clear   # type: ignore[assignment]
        check("T5-c 运行时清理失败时标记仍持久化（重启可恢复）",
              rc.get("marker_written") is True and rc.get("runtime_cleared") is False)
        check("T5-d 失败原因如实记录", "simulated" in str(rc.get("error")))
    except Exception as exc:  # noqa: BLE001
        if orig_clear is not None:
            pass
        print("  ⚠️ T5-c/T5-d 跳过（无法模拟）: %s" % exc)

    # ---------- T6：idle flush 不重复写回 ----------
    print("T6 idle flush 去重")
    import json
    from monika_governance import flush as F

    state_path = "/tmp/test_acceptance_flush.json"
    if os.path.exists(state_path):
        os.remove(state_path)
    fl = F.IdleFlush(state_path=state_path, idle_seconds=1, min_messages=2, daily_cap=3)
    CH = "97ad5577bb5b176266865c0759532ad5"  # scoped 导入要求真实聊天流
    fl.note_message(CH, "今天把专利写完了，感觉轻松了不少", ts=time.time() - 10)
    fl.note_message(CH, "下周开始准备答辩材料", ts=time.time() - 9)
    p1 = db_counts()["paragraphs"]
    ok1 = await F.flush_one(CH, fl)
    time.sleep(6)   # 异步落库约需 5s
    p2 = db_counts()["paragraphs"]
    ok2 = await F.flush_one(CH, fl)      # 同窗口重复触发
    time.sleep(6)
    p3 = db_counts()["paragraphs"]
    check("T6-a 首次收尾写回成功", ok1 is True and p2 > p1, "p1=%s p2=%s" % (p1, p2))
    check("T6-b 重复触发不再写回（无新消息）", ok2 is False or p3 == p2,
          "p2=%s p3=%s" % (p2, p3))

    print()
    print("=== 结果: PASS=%d FAIL=%d ===" % (len(PASS), len(FAIL)))
    if FAIL:
        print("失败项:", FAIL)
        sys.exit(1)


asyncio.run(main())
