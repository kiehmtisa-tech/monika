"""验收门槛#2：真实抓取 Mutopia id=472，验证 SHA/时长/曲名/许可 + 幂等 + 不动 pieces。"""
import json
import sqlite3
import sys
from pathlib import Path

for _pl in ("/MaiMBot/depends-data/piano-runtime/pylibs",):
    if Path(_pl).is_dir() and _pl not in sys.path:
        sys.path.insert(0, _pl)
sys.path.insert(0, "/MaiMBot/plugins/monika_piano")
import piano_external as pe  # noqa: E402

DB = Path("/MaiMBot/data/piano/pieces.db")
ROOT = Path("/MaiMBot/data/piano")

before = sqlite3.connect(DB).execute("select count(*) from pieces").fetchone()[0]

row = pe.import_mutopia(DB, ROOT,
                        "https://www.mutopiaproject.org/cgibin/piece-info.cgi?id=472")
print(json.dumps({k: row[k] for k in
                  ("score_id", "title", "composer", "license_name", "rights_status",
                   "duration_seconds", "note_count", "solo_piano", "raw_sha256",
                   "attribution", "source_page_url")},
                 ensure_ascii=False, indent=1))

row2 = pe.import_mutopia(DB, ROOT,
                         "https://www.mutopiaproject.org/cgibin/piece-info.cgi?id=472")
print("重复导入同 ID（幂等）:", row2["score_id"] == row["score_id"])

c = sqlite3.connect(DB)
print("external_scores 行数:", c.execute("select count(*) from external_scores").fetchone()[0])
after = c.execute("select count(*) from pieces").fetchone()[0]
print(f"pieces 行数: {before} -> {after} (应不变)")

# 渲染测试（真实 fluidsynth）
import subprocess  # noqa: E402
sys.path.insert(0, "/MaiMBot/plugins/monika_piano")
import piano_engine  # noqa: E402
rt = piano_engine.resolve_runtime("/usr/share/sounds/sf2/TimGM6mb.sf2")
info = pe.render(DB, ROOT, row["score_id"], runtime=rt)
wav = Path(info["wav_path"])
print(f"渲染: cached={info['cached']} wav={wav.name} {wav.stat().st_size}B "
      f"key={info['render_key'][:12]}")
info2 = pe.render(DB, ROOT, row["score_id"], runtime=rt)
print("二次渲染命中缓存:", info2["cached"] is True)
print("许可/归因随行:", info["license"], "|", info["attribution"])
