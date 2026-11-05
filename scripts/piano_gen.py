#!/usr/bin/env python3
"""Monika Piano — 确定性钢琴生成器 (P0)

固定 seed 生成 8 小节 solo_piano MIDI（A-A'-B-A'' 动机结构），
左手分解和弦 + 右手旋律，含力度弧线与节奏微偏差。
零 LLM 调用，纯本地规则 + 种子随机。

用法:
  python3 piano_gen.py --mood melancholy --seed 1842 --outdir /opt/qq-fish/data/MaiMBot/piano
输出:
  <outdir>/piece_<seed>_<mood>.mid
"""
import argparse
import json
import random
import hashlib
from pathlib import Path

from mido import Message, MidiFile, MidiTrack, MetaMessage, bpm2tempo

# ---------- 乐理常量 ----------
NOTE_OFFSET = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
MINOR_SCALE = [0, 2, 3, 5, 7, 8, 10]

# 和弦进行（罗马数字级数，7 个和弦 + 结尾主和弦凑 8 小节）
PROGRESSIONS = {
    "major": {
        "calm":      [(1, "maj7"), (5, "maj"), (6, "min"), (4, "maj")],
        "warm":      [(1, "maj"), (6, "min7"), (4, "maj"), (5, "dom7")],
        "playful":   [(1, "maj"), (4, "maj"), (5, "dom7"), (1, "maj")],
        "nostalgic": [(1, "maj"), (3, "min"), (6, "min"), (4, "maj")],
        "hopeful":   [(4, "maj"), (1, "maj"), (5, "maj"), (6, "min")],
    },
    "minor": {
        "melancholy": [(1, "min"), (6, "maj"), (3, "maj"), (7, "maj")],
        "nostalgic":  [(1, "min"), (4, "min"), (7, "maj"), (1, "min")],
    },
}

CHORD_INTERVALS = {
    "maj":   [0, 4, 7],  "min": [0, 3, 7],  "min7": [0, 3, 7, 10],
    "maj7":  [0, 4, 7, 11], "dom7": [0, 4, 7, 10],
}

# 节奏型（以 16 分音符为单位的时值列表，一个 4/4 小节 = 16 ticks 单位）
RHYTHMS = [
    [4, 4, 4, 4],            # 四分音符 x4
    [2, 2, 4, 4, 4],         # 八八 四 四 四
    [4, 2, 2, 4, 4],         # 四 八八 四 四
    [6, 2, 4, 4],            # 附点 八 四 四
    [2, 2, 2, 2, 4, 4],      # 四个八分 + 两个四分
]


def scale_note(key_root: int, mode: str, degree: int, octave: int = 0) -> int:
    """degree 可为负/超7，自动跨八度。"""
    scale = MAJOR_SCALE if mode == "major" else MINOR_SCALE
    oct_shift = degree // 7
    idx = degree % 7
    return key_root + 12 * (octave + oct_shift) + scale[idx]


def chord_notes(key_root: int, mode: str, degree: int, quality: str) -> list:
    root = scale_note(key_root, mode, degree - 1)
    return [root + i for i in CHORD_INTERVALS[quality]]


def fit_to_chord(note: int, chord: list, lo: int, hi: int) -> int:
    """把旋律音吸附到和弦音或音阶内，限制音域。"""
    candidates = []
    for c in chord:
        for oct_add in (-12, 0, 12):
            n = c + oct_add
            if lo <= n <= hi:
                candidates.append(n)
    if not candidates:
        return max(lo, min(hi, note))
    return min(candidates, key=lambda n: abs(n - note))


def gen_motif(rng: random.Random, key_root: int, mode: str, lo: int, hi: int) -> dict:
    """2 小节动机：{rhythm, degrees, contour}"""
    rhythm = list(rng.choice(RHYTHMS)) + list(rng.choice(RHYTHMS))
    start_deg = rng.randint(0, 4)
    contour = rng.choice(["arc", "rise", "fall", "zigzag"])
    degrees = [start_deg]
    for i in range(1, len(rhythm)):
        if contour == "arc":
            step = rng.choice([-1, 1]) if i < len(rhythm) // 2 else rng.choice([-2, -1])
        elif contour == "rise":
            step = rng.choice([1, 2])
        elif contour == "fall":
            step = rng.choice([-1, -2])
        else:
            step = rng.choice([-2, -1, 1, 2])
        degrees.append(max(-3, min(9, degrees[-1] + step)))
    return {"rhythm": rhythm, "degrees": degrees, "contour": contour}


def section_notes(rng, motif, chord_seq, key_root, mode, lo, hi, variation=0):
    """把动机铺到 2 小节（两个和弦）上，返回 [(tick, dur, note, vel)]"""
    events = []
    tick = 0
    total_ticks = 32  # 2 小节 x 16
    # 变化策略: A'=尾音升/降2级; B=镜像到高音区; A''=回到主音终止
    deg_shift = {0: 0, 1: (2 if rng.random() < 0.5 else -2), 2: 5, 3: 0}.get(variation, 0)
    for i, dur in enumerate(motif["rhythm"]):
        if tick >= total_ticks:
            break
        bar = min(tick // 16, 3)
        chord = chord_seq[bar % len(chord_seq)]
        deg = motif["degrees"][min(i, len(motif["degrees"]) - 1)] + deg_shift
        note = scale_note(key_root, mode, deg, octave=1)
        if variation == 2:  # B 段抬高音区
            note += 7
        note = fit_to_chord(note, chord, lo, hi)
        # 结束音回落主音
        if tick + dur >= total_ticks and variation == 3:
            note = fit_to_chord(scale_note(key_root, mode, 0, octave=1), chord, lo, hi)
        vel = max(45, min(100, 70 + (6 if tick < 16 else 0) + rng.randint(-8, 8)))
        jitter = rng.choice([-10, -5, 0, 0, 0, 5, 10])  # 节奏微偏差 (ticks)
        events.append((tick + jitter, dur * 30 - 20, note, vel))  # 16单位=480tick
        tick += dur
    return events


def build_midi(mood: str, key: str, mode: str, bpm: int, bars: int, seed: int, out: Path):
    rng = random.Random(seed)
    key_root = 60 + NOTE_OFFSET[key]  # 以 C4 为基准
    prog_bank = PROGRESSIONS[mode]
    if mood in prog_bank:
        prog = prog_bank[mood]
    else:
        prog = prog_bank[list(prog_bank)[0]]
        mode = "major" if mode == "major" else "minor"
    # 8 小节: prog 的 4 个和弦各 2 小节，最后一个和弦换主和弦终止
    chord_seq = []
    for i in range(bars):
        deg, q = prog[(i * len(prog)) // bars]
        if i == bars - 1:
            deg, q = (1, "min" if mode == "minor" else "maj")
        chord_seq.append(chord_notes(key_root, mode, deg, q))
    # 度数序列供旋律吸附
    deg_seq = [prog[(i * len(prog)) // bars][0] for i in range(bars)]
    deg_seq[-1] = 1

    rh_lo, rh_hi = 60, 88   # C4..E6
    lh_lo, lh_hi = 36, 59   # C2..B3

    motif = gen_motif(rng, key_root, mode, rh_lo, rh_hi)
    # 4 个 2 小节段: A, A', B, A''
    rh_events = []
    for v in range(4):
        seg = section_notes(rng, motif, chord_seq[v * 2:(v * 2) + 2], key_root, mode, rh_lo, rh_hi, variation=v)
        rh_events += [(t + v * 1920, d, n, vel) for (t, d, n, vel) in seg]

    mid = MidiFile(ticks_per_beat=480)
    # track 0: 元信息
    t0 = MidiTrack()
    mid.tracks.append(t0)
    t0.append(MetaMessage("set_tempo", tempo=bpm2tempo(bpm), time=0))
    t0.append(MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    t0.append(MetaMessage("track_name", name=f"Monika Piano {mood} seed={seed}", time=0))
    # track 1: 右手旋律
    t1 = MidiTrack()
    mid.tracks.append(t1)
    t1.append(Message("program_change", channel=0, program=0, time=0))
    # track 2: 左手伴奏
    t2 = MidiTrack()
    mid.tracks.append(t2)
    t2.append(Message("program_change", channel=1, program=0, time=0))

    # 右手
    rh_events.sort(key=lambda e: e[0])
    last = 0
    for (tick, dur, note, vel) in rh_events:
        start = max(tick, last)  # 防止 jitter 造成 delta 为负
        t1.append(Message("note_on", channel=0, note=note, velocity=vel, time=start - last))
        t1.append(Message("note_off", channel=0, note=note, velocity=0, time=dur))
        last = start + dur
    # 左手: 每小节 根音-五音-根音-五音 分解（绝对时间，避免游标漂移）
    lh_events = []  # (abs_tick, note)
    for bar in range(bars):
        chord = chord_seq[bar]
        base = max(lh_lo, min(lh_hi, chord[0] - 12))
        fifth = base + 7
        for t, note in [(0, base), (480, fifth), (960, base), (1440, fifth)]:
            lh_events.append((bar * 1920 + t, max(lh_lo, min(lh_hi, note))))
    # 延音踏板: 每小节踩下/松开（并入同一事件流排序）
    pedal_events = []
    for bar in range(bars):
        pedal_events.append((bar * 1920, "down"))
        pedal_events.append((bar * 1920 + 1890, "up"))
    t2_events = [(t, "lh", n) for (t, n) in lh_events] + \
                [(t, "pedal", v) for (t, v) in pedal_events]
    t2_events.sort(key=lambda e: e[0])
    last = 0
    for (tick, kind, val) in t2_events:
        start = max(tick, last)
        if kind == "lh":
            t2.append(Message("note_on", channel=1, note=val, velocity=58, time=start - last))
            t2.append(Message("note_off", channel=1, note=val, velocity=0, time=400))
            last = start + 400
        else:
            value = 127 if val == "down" else 0
            t2.append(Message("control_change", channel=0, control=64, value=value, time=start - last))
            last = start

    out.parent.mkdir(parents=True, exist_ok=True)
    mid.save(out)
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mood", default="melancholy",
                    choices=["calm", "warm", "playful", "melancholy", "nostalgic", "hopeful"])
    ap.add_argument("--key", default="C", choices=list(NOTE_OFFSET))
    ap.add_argument("--mode", default="minor", choices=["major", "minor"])
    ap.add_argument("--bpm", type=int, default=74)
    ap.add_argument("--bars", type=int, default=8, choices=[4, 8, 16])
    ap.add_argument("--seed", type=int, default=1842)
    ap.add_argument("--outdir", default="/opt/qq-fish/data/MaiMBot/piano")
    args = ap.parse_args()

    out = Path(args.outdir) / f"piece_{args.seed}_{args.mood}.mid"
    build_midi(args.mood, args.key, args.mode, args.bpm, args.bars, args.seed, out)
    # 复现校验: 同参数再生成一次比 md5
    tmp = Path("/tmp/_repro.mid")
    build_midi(args.mood, args.key, args.mode, args.bpm, args.bars, args.seed, tmp)
    md5a = hashlib.md5(out.read_bytes()).hexdigest()
    md5b = hashlib.md5(tmp.read_bytes()).hexdigest()
    meta = {
        "piece_id": out.stem, "seed": args.seed, "mood": args.mood,
        "key": f"{args.key} {args.mode}", "bpm": args.bpm, "bars": args.bars,
        "reproducible": md5a == md5b, "md5": md5a, "midi_path": str(out),
    }
    (Path(args.outdir) / f"piece_{args.seed}_{args.mood}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    print(json.dumps(meta, ensure_ascii=False))


if __name__ == "__main__":
    main()
