"""本地验证 piano_storage：TTL / 容量上限 / 收藏保护 / 缺 MIDI 保护 / 每天一次。

在服务器上跑：
  docker exec maim-bot-core /MaiMBot/.venv/bin/python /tmp/test_storage.py
"""
import json
import os
import sys
import tempfile
import time
from pathlib import Path

from mido import Message, MidiFile, MidiTrack

PLUGIN_DIRS = ["/MaiMBot/plugins/monika_piano",          # 容器内
               "/opt/qq-fish/data/MaiMBot/plugins/monika_piano"]  # 宿主机
for _d in PLUGIN_DIRS:
    if Path(_d).is_dir() and _d not in sys.path:
        sys.path.insert(0, _d)

import piano_storage as ps

TMP = Path(__file__).parent / "t_storage"
if TMP.exists():
    for p in TMP.iterdir():
        if p.is_file():
            p.unlink()
TMP.mkdir(exist_ok=True)

DAY = 86400
now = time.time()


def make(pid, age_days, with_midi=True, note=True):
    wav = TMP / f"{pid}.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 5000)
    mid = TMP / f"{pid}.mid"
    if with_midi:
        midf = MidiFile(ticks_per_beat=480)
        tr = MidiTrack()
        midf.tracks.append(tr)
        if note:
            tr.append(Message("note_on", note=72, velocity=80, time=0))
            tr.append(Message("note_off", note=72, velocity=0, time=480))
        midf.save(mid)
    ts = now - age_days * DAY
    os.utime(wav, (ts, ts))
    return {"piece_id": pid, "created_at": int(ts)}


metas = [
    make("piece_old_1", 60),                 # 超期 + 有 MIDI → 应删
    make("piece_old_2", 45),                 # 超期但收藏 → 不删
    make("piece_old_3", 40, with_midi=False),  # 超期但无 MIDI → 保护
    make("piece_old_4", 35, note=False),     # 超期但 MIDI 无音符 → 保护
    make("piece_new_1", 2),                  # 未超期 → 不删
]
(TMP / "pieces_index.jsonl").write_text(
    "\n".join(json.dumps(m) for m in metas) + "\n", encoding="utf-8")
ps.save_favorites(TMP, {"piece_old_2"})

print("=== scan ===")
for r in ps.scan(TMP):
    print(" ", r["piece_id"], f"{r['age_days']}天", f"{r['size_mb']}MB",
          "收藏" if r["favorite"] else "", "MIDI-OK" if r["midi_ok"] else "MIDI-坏")

print("\n=== dry-run (ttl=30, max=300) ===")
r = ps.cleanup(TMP, ttl_days=30, max_wav_mb=300, dry_run=True, now=now)
print("  计划删除:", r["planned"], "| 可释放 MB:", r["planned_mb"])
print("  缺 MIDI 保护:", r["protected_keep_midi_missing"])
print("  文件还在:", sorted(p.name for p in TMP.glob("*.wav")))

print("\n=== 真清理 ===")
r = ps.cleanup(TMP, ttl_days=30, max_wav_mb=300, dry_run=False, now=now)
print("  实际删除:", r["deleted"], "| 失败:", r["failed"])
print("  剩余 WAV:", sorted(p.name for p in TMP.glob("*.wav")))
print("  MIDI 全在:", sorted(p.name for p in TMP.glob("*.mid")))
print("  索引还在:", (TMP / "pieces_index.jsonl").exists())
print("  今天已跑过 (needs_run):", ps.needs_run(TMP, now=now))

print("\n=== 容量上限：max=0 强制按最旧清理 ===")
r = ps.cleanup(TMP, ttl_days=9999, max_wav_mb=0, dry_run=True, now=now + DAY)
print("  计划删除:", r["planned"], "| 收藏保留:", "piece_old_2" not in r["planned"])
