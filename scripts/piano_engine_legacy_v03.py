"""Monika Piano 引擎 — 确定性钢琴生成 (纯本地, 零 LLM)。

从 piano_gen.py 移植: A-A'-B-A'' 动机变奏、左手根音五音分解、
力度弧线 + 节奏微偏差 + 延音踏板。同参数同 seed 输出逐字节一致。
"""
from __future__ import annotations

import hashlib
import json
import random
import subprocess
import time
from pathlib import Path

from mido import Message, MidiFile, MidiTrack, MetaMessage, bpm2tempo

NOTE_OFFSET = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
MINOR_SCALE = [0, 2, 3, 5, 7, 8, 10]

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
    "maj": [0, 4, 7], "min": [0, 3, 7], "min7": [0, 3, 7, 10],
    "maj7": [0, 4, 7, 11], "dom7": [0, 4, 7, 10],
}

RHYTHMS = [
    [4, 4, 4, 4],
    [2, 2, 4, 4, 4],
    [4, 2, 2, 4, 4],
    [6, 2, 4, 4],
    [2, 2, 2, 2, 4, 4],
]

MOODS = sorted({m for bank in PROGRESSIONS.values() for m in bank})


def _scale_note(key_root: int, mode: str, degree: int, octave: int = 0) -> int:
    scale = MAJOR_SCALE if mode == "major" else MINOR_SCALE
    return key_root + 12 * (octave + degree // 7) + scale[degree % 7]


def _chord_notes(key_root: int, mode: str, degree: int, quality: str) -> list:
    root = _scale_note(key_root, mode, degree - 1)
    return [root + i for i in CHORD_INTERVALS[quality]]


def _fit_to_chord(note: int, chord: list, lo: int, hi: int) -> int:
    candidates = []
    for c in chord:
        for oct_add in (-12, 0, 12):
            n = c + oct_add
            if lo <= n <= hi:
                candidates.append(n)
    if not candidates:
        return max(lo, min(hi, note))
    return min(candidates, key=lambda n: abs(n - note))


def _gen_motif(rng: random.Random, key_root: int, mode: str) -> dict:
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


def _section_notes(rng, motif, chord_seq, key_root, mode, lo, hi, variation=0,
                   dec=None, vel_shift=0, jitter_amp=(-10, 10), b_shift=7):
    dec = dec or rng
    events = []
    tick = 0
    total_ticks = 32
    deg_shift = {0: 0, 1: (2 if dec.random() < 0.5 else -2), 2: b_shift, 3: 0}.get(variation, 0)
    for i, dur in enumerate(motif["rhythm"]):
        if tick >= total_ticks:
            break
        bar = min(tick // 16, 3)
        chord = chord_seq[bar % len(chord_seq)]
        deg = motif["degrees"][min(i, len(motif["degrees"]) - 1)] + deg_shift
        note = _scale_note(key_root, mode, deg, octave=1)
        if variation == 2:
            note += b_shift
        note = _fit_to_chord(note, chord, lo, hi)
        if tick + dur >= total_ticks and variation == 3:
            note = _fit_to_chord(_scale_note(key_root, mode, 0, octave=1), chord, lo, hi)
        vel = max(40, min(105, 70 + vel_shift + (6 if tick < 16 else 0) + dec.randint(-8, 8)))
        jitter = dec.randint(*jitter_amp)
        events.append((tick + jitter, dur * 30 - 20, note, vel))
        tick += dur
    return events


def _build_midi(mood: str, key: str, mode: str, bpm: int, bars: int, seed: int,
                variation: int = 0, out: Path | None = None) -> None:
    # 动机层: 仅由 seed 决定 (变奏保留原动机)
    rng = random.Random(seed)
    # 装饰层: 随 variation 变化 (力度/节奏偏差/左手花样/B段移调)
    dec = random.Random(f"{seed}:{variation}")
    key_root = 60 + NOTE_OFFSET[key]
    prog_bank = PROGRESSIONS[mode]
    prog = prog_bank.get(mood) or prog_bank[next(iter(prog_bank))]
    chord_seq = []
    for i in range(bars):
        deg, q = prog[(i * len(prog)) // bars]
        if i == bars - 1:
            deg, q = (1, "min" if mode == "minor" else "maj")
        chord_seq.append(_chord_notes(key_root, mode, deg, q))

    rh_lo, rh_hi = 60, 88
    lh_lo, lh_hi = 36, 59
    motif = _gen_motif(rng, key_root, mode)
    vel_shift = (variation * 7) % 15 - 7            # 力度整体偏移
    jitter_amp = [(-10, 10), (-16, 16), (-5, 5)][variation % 3]  # 节奏人味幅度
    b_shift = 7 if variation % 2 == 0 else 5        # B 段移调量
    lh_arpeggio = variation % 3 == 2                # 左手换琶音花样

    rh_events = []
    for v in range(4):
        seg = _section_notes(rng, motif, chord_seq[v * 2:(v * 2) + 2], key_root, mode,
                             rh_lo, rh_hi, variation=v, dec=dec,
                             vel_shift=vel_shift, jitter_amp=jitter_amp, b_shift=b_shift)
        rh_events += [(t + v * 1920, d, n, vel) for (t, d, n, vel) in seg]

    mid = MidiFile(ticks_per_beat=480)
    t0 = MidiTrack()
    mid.tracks.append(t0)
    t0.append(MetaMessage("set_tempo", tempo=bpm2tempo(bpm), time=0))
    t0.append(MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    t0.append(MetaMessage("track_name", name=f"Monika Piano {mood} seed={seed}", time=0))

    t1 = MidiTrack()
    mid.tracks.append(t1)
    t1.append(Message("program_change", channel=0, program=0, time=0))
    rh_events.sort(key=lambda e: e[0])
    last = 0
    for (tick, dur, note, vel) in rh_events:
        start = max(tick, last)
        t1.append(Message("note_on", channel=0, note=note, velocity=vel, time=start - last))
        t1.append(Message("note_off", channel=0, note=note, velocity=0, time=dur))
        last = start + dur

    t2 = MidiTrack()
    mid.tracks.append(t2)
    t2.append(Message("program_change", channel=1, program=0, time=0))
    lh_events = []
    for bar in range(bars):
        chord = chord_seq[bar]
        base = max(lh_lo, min(lh_hi, chord[0] - 12))
        fifth = base + 7
        third = base + max(1, min(11, chord[1] - chord[0])) if len(chord) > 1 else base + 4
        if lh_arpeggio:
            pattern = [(0, base), (240, fifth), (480, third), (720, fifth),
                       (960, base), (1200, fifth), (1440, third), (1680, fifth)]
        else:
            pattern = [(0, base), (480, fifth), (960, base), (1440, fifth)]
        for t, note in pattern:
            lh_events.append((bar * 1920 + t, max(lh_lo, min(lh_hi, note))))
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
            t2.append(Message("control_change", channel=0, control=64,
                              value=127 if val == "down" else 0, time=start - last))
            last = start

    out.parent.mkdir(parents=True, exist_ok=True)
    mid.save(out)


def _append_index(meta: dict, index_path: Path) -> None:
    """作品索引: 每行一个 JSON, 供变奏/回溯查询。"""
    try:
        with open(index_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    except OSError as exc:
        # 索引失败不影响主流程
        pass


def last_piece(outdir: str = "/MaiMBot/data/piano", chat_id: str = "") -> dict | None:
    """取某会话最近一次作品 (无 chat_id 匹配则取全局最近)。"""
    index = Path(outdir) / "pieces_index.jsonl"
    if not index.exists():
        return None
    found = None
    try:
        with open(index, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not chat_id or item.get("chat_id") == chat_id:
                    found = item
    except OSError:
        return None
    return found


def generate(mood: str = "melancholy", key: str = "C", mode: str = "minor",
             bpm: int = 74, bars: int = 8, seed: int = 1842,
             variation: int = 0, chat_id: str = "",
             outdir: str = "/MaiMBot/data/piano",
             soundfont: str = "/usr/share/sounds/sf2/TimGM6mb.sf2") -> dict:
    """生成钢琴曲, 返回元数据 dict。同参数幂等; variation>0 为同 seed 变奏。"""
    if mood not in MOODS:
        mood = "melancholy"
    if variation > 0:
        out = Path(outdir) / f"piece_{seed}_{mood}_v{variation}.mid"
    else:
        out = Path(outdir) / f"piece_{seed}_{mood}.mid"
    _build_midi(mood, key, mode, bpm, bars, seed, variation=variation, out=out)
    tmp = Path("/tmp/_piano_repro.mid")
    _build_midi(mood, key, mode, bpm, bars, seed, variation=variation, out=tmp)
    md5 = hashlib.md5(out.read_bytes()).hexdigest()
    reproducible = md5 == hashlib.md5(tmp.read_bytes()).hexdigest()
    tmp.unlink(missing_ok=True)

    wav = out.with_suffix(".wav")
    subprocess.run(
        ["fluidsynth", "-ni", soundfont, str(out), "-F", str(wav), "-r", "44100", "-g", "0.8"],
        check=True, capture_output=True, timeout=120,
    )
    parent = f"piece_{seed}_{mood}" if variation > 0 else None
    meta = {
        "piece_id": out.stem, "seed": seed, "mood": mood,
        "key": f"{key} {mode}", "bpm": bpm, "bars": bars,
        "variation": variation, "parent_piece_id": parent,
        "chat_id": chat_id, "created_at": int(time.time()),
        "reproducible": reproducible, "md5": md5,
        "midi_path": str(out), "wav_path": str(wav),
        "napcat_uri": f"file://{wav}",
    }
    (Path(outdir) / f"{out.stem}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    _append_index(meta, Path(outdir) / "pieces_index.jsonl")
    return meta
