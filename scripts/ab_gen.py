"""A/B 基线生成器（容器内运行）。

用法: python ab_gen.py <引擎路径> <输出目录> '<jobs-json>'
jobs: [[mood, seed, bpm], ...]
输出: <outdir> 下的 mid/wav/json + /tmp/ab_gen_summary.json
"""
import importlib.util
import json
import resource
import sys
import time
from pathlib import Path

ENGINE = sys.argv[1]
OUT = Path(sys.argv[2])
JOBS = json.loads(sys.argv[3])
SOUNDFONT = "/usr/share/sounds/sf2/TimGM6mb.sf2"


def avail_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return -1


def load_rss_mb() -> float:
    try:
        with open("/proc/loadavg", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except OSError:
        return -1.0


spec = importlib.util.spec_from_file_location("eng_mod", ENGINE)
eng = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eng)

OUT.mkdir(parents=True, exist_ok=True)
rows = []
for mood, seed, bpm in JOBS:
    m0, l0 = avail_mb(), load_rss_mb()
    t0 = time.time()
    meta = eng.generate(mood=mood, bpm=bpm, bars=8, seed=seed, variation=0,
                        chat_id="ab", outdir=str(OUT), soundfont=SOUNDFONT)
    dt = time.time() - t0
    row = {
        "engine": Path(ENGINE).name, "mood": mood, "seed": seed, "bpm": bpm,
        "sec": round(dt, 2),
        "maxrss_mb": resource.getrusage(resource.RUSAGE_SELF).ru_maxrss // 1024,
        "load1_before": l0, "mem_avail_before_mb": m0,
        "md5": meta["md5"], "reproducible": meta["reproducible"],
        "mid": Path(meta["midi_path"]).name, "wav": Path(meta["wav_path"]).name,
    }
    rows.append(row)
    print(json.dumps(row, ensure_ascii=False), flush=True)

Path("/tmp/ab_gen_summary.json").write_text(
    json.dumps(rows, ensure_ascii=False, indent=2), encoding="utf-8")
