"""把 3 首迁移前的孤儿 MIDI 补录进 pieces.db（幂等；不动文件）。

背景：SQLite 目录上线前生成的 piece_7_calm / piece_1842_melancholy /
piece_4476_melancholy 只有 JSON 元数据，没进 pieces 表；
piece_1842_melancholy 被诗歌系统的 related_piano_piece_id 引用。
"""
import json
import sys
import time
from pathlib import Path

PIANO = Path("/MaiMBot/data/piano")
PLUGIN = "/MaiMBot/plugins/monika_piano"
sys.path.insert(0, PLUGIN)

import piano_catalog as cat  # noqa: E402

STEMS = ["piece_7_calm", "piece_1842_melancholy", "piece_4476_melancholy"]

existing = set(cat.all_meta(PIANO).keys())
print("目录已有:", len(existing), "首")

added = []
for stem in STEMS:
    if stem in existing:
        print(f"skip {stem}（已在目录）")
        continue
    jp = PIANO / f"{stem}.json"
    mp = PIANO / f"{stem}.mid"
    if not (jp.is_file() and mp.is_file()):
        print(f"skip {stem}（文件不全）")
        continue
    meta = json.loads(jp.read_text(encoding="utf-8"))
    meta["piece_id"] = stem
    if not meta.get("created_at"):
        meta["created_at"] = int(mp.stat().st_mtime)
    if not meta.get("chat_id"):
        meta["chat_id"] = "backfill:pre-catalog"
    meta["midi_path"] = str(mp)
    wp = PIANO / f"{stem}.wav"
    meta["wav_path"] = str(wp) if wp.is_file() else ""
    meta.setdefault("engine_version", "0.3.0")  # 迁移前旧引擎产物
    meta.setdefault("reproducible", False)
    meta["backfilled_at"] = int(time.time())
    cat.add(PIANO, meta)
    added.append(stem)
    print(f"补录 {stem}: seed={meta.get('seed')} mood={meta.get('mood')} "
          f"wav={'有' if wp.is_file() else '无(缺WAV,可ensure_wav重渲染)'}")

print()
print("补录:", added)
print("目录现在:", len(cat.all_meta(PIANO)), "首")
