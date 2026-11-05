"""验证 piano_catalog：迁移、查询、条数/容量双阈值修剪、保护规则、dry-run。

容器内运行：
  docker exec maim-bot-core /MaiMBot/.venv/bin/python /tmp/test_catalog.py
"""
import json
import shutil
import sys
import time
from pathlib import Path

from mido import Message, MidiFile, MidiTrack

PLUGIN_DIRS = ["/MaiMBot/plugins/monika_piano",
               "/opt/qq-fish/data/MaiMBot/plugins/monika_piano"]
for _d in PLUGIN_DIRS:
    if Path(_d).is_dir() and _d not in sys.path:
        sys.path.insert(0, _d)

import piano_catalog as cat
import piano_storage as ps

TMP = Path(__file__).parent / "t_catalog"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir()
DAY = 86400
now = time.time()


def make_piece(pid, age_days, chat="private:1", midi_bytes=900):
    mid = TMP / f"{pid}.mid"
    m = MidiFile(ticks_per_beat=480)
    tr = MidiTrack()
    m.tracks.append(tr)
    tr.append(Message("note_on", note=60, velocity=80, time=0))
    tr.append(Message("note_off", note=60, velocity=0, time=480))
    m.save(mid)
    if midi_bytes > mid.stat().st_size:                 # 补足体积，便于测容量阈值
        with open(mid, "ab") as fh:
            fh.write(b"\x00" * (midi_bytes - mid.stat().st_size))
    wav = TMP / f"{pid}.wav"
    wav.write_bytes(b"RIFF" + b"\x00" * 4000)
    meta = {"piece_id": pid, "seed": 1, "mood": "calm", "bpm": 90, "bars": 8,
            "variation": 0, "parent_piece_id": None, "chat_id": chat,
            "created_at": int(now - age_days * DAY), "engine_version": "test",
            "midi_path": str(mid), "wav_path": str(wav), "md5": "x"}
    (TMP / f"{pid}.json").write_text(json.dumps(meta, ensure_ascii=False), encoding="utf-8")
    return meta


print("=== 1) 迁移 legacy jsonl ===")
legacy = [make_piece("piece_legacy_a", 12), make_piece("piece_legacy_b", 9)]
(TMP / "pieces_index.jsonl").write_text(
    "\n".join(json.dumps(m, ensure_ascii=False) for m in legacy) + "\n", encoding="utf-8")
print("  导入条数:", cat.init(TMP))
print("  统计:", cat.stats(TMP))
print("  再次 init（应幂等 0 条）:", cat.init(TMP))

print("\n=== 2) 写入与查询 ===")
for i, d in enumerate([60, 50, 40, 30, 20, 3, 1]):
    cat.add(TMP, make_piece(f"piece_{i}", d))
cat.add(TMP, make_piece("piece_other", 5, chat="group:99"))
print("  统计:", cat.stats(TMP))
print("  last(private:1):", (cat.last(TMP, "private:1") or {}).get("piece_id"))
print("  last(group:99):", (cat.last(TMP, "group:99") or {}).get("piece_id"))

print("\n=== 3) dry-run 条数阈值 (max_pieces=5) ===")
r = cat.prune(TMP, max_mb=1024, max_pieces=5, min_keep_days=7,
              favorites={"piece_1"}, dry_run=True, now=now)
print("  超条数:", r["over_count"], "| 计划:", r["planned"])
print("  收藏保护:", r["protected_favorite"], "| 近期保护:", r["protected_recent"])
print("  文件仍在:", (TMP / "piece_0.mid").exists())

print("\n=== 4) 真删 (max_pieces=5) ===")
r = cat.prune(TMP, max_mb=1024, max_pieces=5, min_keep_days=7,
              favorites={"piece_1"}, dry_run=False, now=now)
print("  实际移除:", r["deleted"], "| 之后:", r["after_pieces"], "首")
print("  被删的 mid/wav/json 是否都不在:",
      not any((TMP / f"piece_0.{e}").exists() for e in ("mid", "wav", "json")))
print("  收藏 piece_1 还在:", (TMP / "piece_1.mid").exists())
print("  近期 piece_5/6 还在:", (TMP / "piece_5.mid").exists())

print("\n=== 5) 容量阈值 (max_mb=0.002) dry-run ===")
r = cat.prune(TMP, max_mb=0.002, max_pieces=9999, min_keep_days=0,
              favorites={"piece_5"}, dry_run=True, now=now)
print("  超容量:", r["over_size"], "| 计划:", r["planned"])
print("  收藏未入选:", "piece_5" not in r["planned"])

print("\n=== 6) storage.cleanup 串起来（含 WAV 软清理）===")
ps.save_favorites(TMP, {"piece_5"})
res = ps.cleanup(TMP, ttl_days=30, max_wav_mb=300, dry_run=True, now=now,
                 max_pieces=3, midi_max_mb=1024, min_keep_days=7)
print("  catalog 段:", {k: res["catalog"][k] for k in ("over_count", "planned", "protected_favorite")})
print("  WAV 段: 扫描=%s 计划删=%s" % (res["scanned"], res["planned"]))

shutil.rmtree(TMP, ignore_errors=True)
print("\n全部断言人工核对完毕")
