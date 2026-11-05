"""Monika Piano 引擎 v0.4 — 动机式旋律生成（纯本地, 零 LLM, 零新依赖）。

对照旧版 v0.3 修复的三处实现级缺陷
--------------------------------
1. **tick 单位错误**：旧版节奏值以 16 分音符为单位（1 拍 = 120 tick），代码却按
   30 tick/单位、乐句偏移 1920 tick(1 小节) 写入，导致右手音符全挤在每小节前 1.5 拍，
   全曲右手只到第 3.34 小节，后 4 小节没有主旋律。
2. **mood 全部回退**：旧版按 `PROGRESSIONS[mode]` 取库，而 mode 默认 "minor"，
   该库只有 melancholy/nostalgic 两条，calm/playful/warm/hopeful 一律回退到
   melancholy 的和声与音阶 —— 6 种情绪实际只有 2 种听感。
3. **同音连击**：旧版 `_fit_to_chord` 把每个音就地 clamp 到和弦音，音域又夹在 60–88，
   结果出现连续 9–12 个相同音（听感即“哒哒哒哒”）。

新版结构
--------
A(1–2) 建立 2 小节动机 → A'(3–4) 变奏算子发展 → B(5–6) 轮廓倒影+音区/密度对比
→ A''(7–8) 回归主题并以终止句收束。每音在 5–12 个候选中按
和弦/轮廓/级进/重复/跳进/音域 加权 softmax 采样（不是逐音 random.choice）。

约束：同参数同 seed 逐字节一致；不引入依赖、GPU、LLM、常驻进程。
"""
from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import random
import shutil
import subprocess
import threading
import time
from pathlib import Path

from mido import Message, MetaMessage, MidiFile, MidiTrack, bpm2tempo

try:                       # 插件方式：同包相对导入
    from . import piano_catalog as _catalog
except Exception:          # noqa: BLE001
    try:                   # 脚本方式：ab_gen.py 等按路径加载时
        import piano_catalog as _catalog  # type: ignore
    except Exception:      # noqa: BLE001
        _catalog = None

LOGGER = logging.getLogger(__name__)

ENGINE_VERSION = "0.4.1"
TPB = 480            # ticks per beat
BEAT = TPB
BAR = 4 * BEAT       # 4/4

# 持久化运行时（bind 挂载：宿 ./depends-data -> 容器 /MaiMBot/depends-data）
RUNTIME_DIR = Path("/MaiMBot/depends-data/piano-runtime")
VENDOR_PYLIBS = RUNTIME_DIR / "pylibs"
DEFAULT_SOUNDFONT = "/usr/share/sounds/sf2/TimGM6mb.sf2"

NOTE_OFFSET = {"C": 0, "D": 2, "E": 4, "F": 5, "G": 7, "A": 9, "B": 11}
MAJOR_SCALE = [0, 2, 4, 5, 7, 9, 11]
MINOR_SCALE = [0, 2, 3, 5, 7, 8, 10]
CHORD_INTERVALS = {
    "maj": [0, 4, 7], "min": [0, 3, 7], "min7": [0, 3, 7, 10],
    "maj7": [0, 4, 7, 11], "dom7": [0, 4, 7, 10],
}

# ---- 和声：按 mood 索引（旧版按 mode 索引导致情绪失效） ----
PROGRESSIONS = {
    "calm": [
        [(1, "maj7"), (5, "maj"), (6, "min7"), (4, "maj7")],
        [(1, "maj"), (4, "maj7"), (6, "min"), (5, "maj")],
        [(1, "maj7"), (3, "min"), (4, "maj7"), (1, "maj")],
    ],
    "warm": [
        [(1, "maj"), (6, "min7"), (4, "maj"), (5, "dom7")],
        [(4, "maj"), (1, "maj"), (5, "dom7"), (6, "min7")],
        [(1, "maj"), (4, "maj"), (2, "min"), (5, "dom7")],
    ],
    "hopeful": [
        [(4, "maj"), (1, "maj"), (5, "maj"), (6, "min")],
        [(1, "maj"), (5, "maj"), (6, "min"), (3, "min")],
        [(1, "maj"), (4, "maj"), (5, "maj"), (1, "maj")],
    ],
    "playful": [
        [(1, "maj"), (4, "maj"), (5, "dom7"), (1, "maj")],
        [(1, "maj"), (6, "min"), (2, "min"), (5, "dom7")],
        [(1, "maj"), (5, "maj"), (6, "min"), (4, "maj")],
    ],
    "nostalgic": [
        [(1, "min"), (4, "min"), (7, "maj"), (1, "min")],
        [(6, "maj"), (4, "min"), (1, "min"), (5, "maj")],
        [(1, "min"), (6, "maj"), (3, "maj"), (7, "maj")],
    ],
    "melancholy": [
        [(1, "min"), (6, "maj"), (3, "maj"), (7, "maj")],
        [(1, "min"), (4, "min"), (6, "maj"), (5, "min")],
        [(1, "min"), (7, "maj"), (6, "maj"), (4, "min")],
    ],
}

MOOD_MODE = {"calm": "major", "warm": "major", "hopeful": "major",
             "playful": "major", "nostalgic": "minor", "melancholy": "minor"}

# dens: 期望音符密度; vel: 基准力度; temp: 采样温度; hi: 轮廓上限级数
MOOD_CFG = {
    "calm":       dict(dens=0.55, vel=64, temp=1.20, contour="arc",   hi=10),
    "warm":       dict(dens=0.62, vel=68, temp=1.10, contour="arc",   hi=11),
    "hopeful":    dict(dens=0.68, vel=72, temp=1.05, contour="rise",  hi=12),
    "playful":    dict(dens=0.80, vel=78, temp=0.95, contour="wave",  hi=12),
    "nostalgic":  dict(dens=0.58, vel=66, temp=1.15, contour="fall",  hi=11),
    "melancholy": dict(dens=0.50, vel=60, temp=1.25, contour="fall",  hi=10),
}

# ---- 节奏模板：单位=拍，每格严格合计 4 拍 ----
CELLS = {
    "steady":   [(0.0, 1.0), (1.0, 1.0), (2.0, 2.0)],
    "flow":     [(0.0, 0.5), (0.5, 0.5), (1.0, 1.0), (2.0, 1.0), (3.0, 1.0)],
    "breath":   [(0.0, 1.0), (1.0, 0.5), (1.5, 0.5), (3.0, 1.0)],
    "longtail": [(0.0, 0.5), (0.5, 0.5), (1.0, 1.0), (2.0, 2.0)],
    "sync":     [(0.0, 0.75), (0.75, 0.75), (1.5, 0.5), (2.0, 1.0), (3.0, 1.0)],
    "sparse":   [(0.0, 1.5), (2.0, 2.0)],
    "march":    [(0.0, 1.0), (1.0, 1.0), (2.0, 1.0), (3.0, 1.0)],
    "run":      [(0.0, 0.5), (0.5, 0.5), (1.0, 0.5), (1.5, 0.5), (2.0, 1.0), (3.0, 1.0)],
}

MOOD_CELLS = {
    "calm":       {"main": ["steady", "longtail"], "contrast": ["flow", "longtail"]},
    "warm":       {"main": ["steady", "flow"],     "contrast": ["flow", "march"]},
    "hopeful":    {"main": ["flow", "longtail"],   "contrast": ["run", "march"]},
    "playful":    {"main": ["flow", "sync"],       "contrast": ["run", "march"]},
    "nostalgic":  {"main": ["breath", "longtail"], "contrast": ["flow", "sync"]},
    "melancholy": {"main": ["breath", "sparse"],   "contrast": ["flow", "longtail"]},
}

MOODS = sorted(PROGRESSIONS.keys())

# 右手舒适区 C4–C6；B 段允许更高
DRH_LO, DRH_HI = 0, 14          # 音阶级数（0=C4, 14=C6=84）
RH_LO, RH_HI = 57, 88           # 音高硬边界
DEG_LO, DEG_HI = -1, 15


class ResourceBusyError(RuntimeError):
    """宿主资源不足，本次生成被拒绝。"""


_GEN_LOCK = threading.Lock()   # 并发=1


# ---------------------------------------------------------------- 基础乐理

def _scale_note(key_root: int, mode: str, degree: int) -> int:
    scale = MAJOR_SCALE if mode == "major" else MINOR_SCALE
    return key_root + 12 * (degree // 7) + scale[degree % 7]


def _degree_pitch(key_root: int, mode: str, degree: int) -> int:
    return _scale_note(key_root, mode, degree)


def _chord_pitches(key_root: int, mode: str, degree1: int, quality: str) -> list[int]:
    root = _scale_note(key_root, mode, degree1 - 1)
    return [root + i for i in CHORD_INTERVALS[quality]]


def _snap_to_chord(pitch: int, pcs: set, limit: int = 5) -> int:
    if pitch % 12 in pcs:
        return pitch
    for d in range(1, limit + 1):
        for cand in (pitch - d, pitch + d):
            if cand % 12 in pcs:
                return cand
    return pitch


def _wchoice(rng: random.Random, items, weights):
    total = sum(weights)
    if total <= 0:
        return rng.choice(items)
    r = rng.random() * total
    acc = 0.0
    for it, w in zip(items, weights):
        acc += w
        if r <= acc:
            return it
    return items[-1]


# ---------------------------------------------------------------- 旋律生成

def _contour_target(i: int, n: int, shape: str, lo: int, hi: int) -> float:
    t = i / (n - 1) if n > 1 else 0.0
    if shape == "arc":
        return lo + (hi - lo) * (1.0 - abs(2.0 * t - 1.0) ** 1.35)
    if shape == "rise":
        return lo + (hi - lo) * (0.25 + 0.75 * t)
    if shape == "fall":
        return hi - (hi - lo) * (0.15 + 0.85 * t)
    return lo + (hi - lo) * (0.5 + 0.5 * math.sin(math.pi * 1.5 * t + 0.6))


def _unit_from_cells(c1: str, c2: str) -> list[tuple[float, float]]:
    """两个 4 拍 cell 拼成 8 拍的 2 小节节奏单元，每小节严格 4 拍。"""
    return [(o, d) for (o, d) in CELLS[c1]] + [(o + 4.0, d) for (o, d) in CELLS[c2]]


def _rhythm_unit(dec: random.Random, mood: str, kind: str = "main"):
    """2 小节 = 8 拍的节奏单元，每小节合计严格 4 拍。返回 (unit, (cell1, cell2))。"""
    bank = MOOD_CELLS.get(mood, MOOD_CELLS["melancholy"])
    cells = bank["contrast"] if kind == "contrast" else bank["main"]
    if dec.random() < 0.5:
        c1, c2 = cells[0], (cells[1] if len(cells) > 1 else cells[0])
    else:
        c1, c2 = (cells[1] if len(cells) > 1 else cells[0]), cells[0]
    return _unit_from_cells(c1, c2), (c1, c2)


def _pick_degree(rng: random.Random, *, prev_deg, ideal, pcs, is_strong,
                 key_root, mode, hist_pitch, cfg, lo, hi, prev_iv=0) -> int:
    """在 prev_deg±5 内取 5–12 个候选，加权 softmax 采样。"""
    cands = [d for d in range(int(round(prev_deg)) - 5, int(round(prev_deg)) + 6)
             if lo <= d <= hi]
    if not cands:
        return max(lo, min(hi, int(round(prev_deg))))

    scored = []
    for d in cands:
        p = _degree_pitch(key_root, mode, d)
        pc = p % 12
        on_chord = pc in pcs
        s = 0.0
        # 和弦匹配（强拍权重更高）
        s += (3.4 if is_strong else 1.15) * (1.0 if on_chord else 0.0)
        # 轮廓贴合
        s -= 0.62 * abs(d - ideal)
        # 级进优先
        dd = abs(d - prev_deg)
        if dd == 1:
            s += 1.15
        elif dd == 2:
            s += 0.80
        elif dd == 3:
            s += 0.15
        elif dd == 0:
            s -= 1.10
        else:
            s -= 1.40 * (dd - 3)
        # 大跳后反向级进补偿
        if abs(prev_iv) >= 3:
            if (d - prev_deg) * prev_iv < 0 and dd <= 2:
                s += 0.95
            elif (d - prev_deg) * prev_iv > 0:
                s -= 0.70
        # 连续同音惩罚（同音连击是旧版主要听感问题）
        if len(hist_pitch) >= 1 and hist_pitch[-1] == p:
            s -= 1.25
        if len(hist_pitch) >= 2 and hist_pitch[-1] == p and hist_pitch[-2] == p:
            s -= 4.0
        # 音域惩罚
        if p > 84:
            s -= 0.45 * (p - 84)
        if p < 60:
            s -= 0.45 * (60 - p)
        scored.append((s, d))

    if not scored:
        return max(lo, min(hi, int(round(prev_deg))))
    mx = max(s for s, _ in scored)
    temp = max(0.35, float(cfg.get("temp", 1.1)))
    weights = [math.exp((s - mx) / temp) for s, _ in scored]
    return _wchoice(rng, [d for _, d in scored], weights)


def _gen_motif(rng: random.Random, *, key_root, mode, mood, cfg,
               chords_by_bar, lo_deg, hi_deg) -> tuple[list, list]:
    """A 段：2 小节动机 = (节奏单元, 级数序列)。"""
    unit, cells = _rhythm_unit(rng, mood, "main")
    shape = cfg.get("contour", "arc")
    # 动机跨度收在六度内（约 9 个半音），否则 2 小节内必然出现大跳
    span_hi = min(hi_deg, lo_deg + 6)
    degrees: list[int] = []
    hist_pitch: list[int] = []
    prev_deg = None
    prev_iv = 0
    for i, (onset, _dur) in enumerate(unit):
        bar = int(onset // 4)
        pcs = {p % 12 for p in chords_by_bar[min(bar, len(chords_by_bar) - 1)]}
        pos = onset % 4.0
        is_strong = min(abs(pos), abs(pos - 2.0)) < 1e-6
        ideal = _contour_target(i, len(unit), shape, lo_deg + 1, span_hi)
        if prev_deg is None:
            base = int(round(ideal))
            pick = None
            for d in (base, base - 1, base + 1, base - 2, base + 2):
                if lo_deg <= d <= hi_deg and _degree_pitch(key_root, mode, d) % 12 in pcs:
                    pick = d
                    break
            if pick is None:
                pick = max(lo_deg, min(hi_deg, base))
        else:
            pick = _pick_degree(rng, prev_deg=prev_deg, ideal=ideal, pcs=pcs,
                                is_strong=is_strong, key_root=key_root, mode=mode,
                                hist_pitch=hist_pitch, cfg=cfg, lo=lo_deg, hi=hi_deg,
                                prev_iv=prev_iv)
        prev_iv = 0 if prev_deg is None else pick - prev_deg
        degrees.append(pick)
        hist_pitch.append(_degree_pitch(key_root, mode, pick))
        prev_deg = pick
    return unit, degrees, cells


# ---------------------------------------------------------------- 变奏算子

def _op_rhythmic_variation(unit, dec):
    """保留起音位置，改末音的持续与前一音的断连（不新增音符，总时值不变）。"""
    u = [[o, d] for o, d in unit]
    i = len(u) - 1
    if i >= 2:
        if u[i][1] <= 1.5 and u[i - 1][1] > 0.75:
            u[i][1] += 0.5
            u[i - 1][1] -= 0.5
        elif u[i][1] > 1.5 and u[i - 1][1] < 1.75:
            u[i][1] -= 0.5
            u[i - 1][1] += 0.5
    return [(o, d) for o, d in u]


def _op_diatonic_sequence(degrees, dec, lo, hi, start_min=0):
    d = list(degrees)
    n = len(d)
    if n < 5:
        span = (start_min, n)
    else:
        lo_i = max(start_min, 1)
        if lo_i >= n - 2:
            return d
        start = dec.randrange(lo_i, max(lo_i + 1, n - 2))
        # 不动末音区（末音归 ending_rewrite 管），避免两个算子互相抵消主题
        span = (start, min(n - 2, start + 3))
    step = dec.choice([1, -1])
    for i in range(*span):
        d[i] = max(lo, min(hi, d[i] + step))
    return d


def _op_neighbor_tone(degrees, dec, lo, hi, start_min=0):
    d = list(degrees)
    cands = [i for i in range(max(2, start_min + 1), len(d))
             if abs(d[i] - d[i - 1]) <= 2]
    if cands:
        i = dec.choice(cands)
        d[i - 1] = max(lo, min(hi, d[i] + dec.choice([-1, 1])))
    return d


def _op_contour_inversion(degrees, dec, lo, hi, target_peak):
    """以动机均值为轴做轮廓倒影（上行→下行），再整体移到目标音区。

    旧实现围绕单点 pivot 倒影并直接 clamp，会把整句压成同一个音。
    """
    d = list(degrees)
    mean = sum(d) / len(d)
    inv = [2 * mean - x for x in d]
    shift = target_peak - max(inv)
    out = [int(round(x + shift)) for x in inv]
    out = [max(lo, min(hi, x)) for x in out]
    # 保证 B 段确实出现高点
    if max(out) < min(hi, target_peak):
        out[dec.randrange(len(out))] = min(hi, target_peak)
    return out


def _op_register_shift(degrees, amount, lo, hi):
    return [max(lo, min(hi, x + amount)) for x in degrees]


def _fit_length(degrees, n, dec, lo, hi, direction=0):
    """把级数序列对齐到节奏槽数量：多了截断，少了按级进延伸。"""
    d = list(degrees)
    if len(d) >= n:
        return d[:n]
    step = direction or dec.choice([1, -1])
    while len(d) < n:
        nxt = d[-1] + step
        if not (lo <= nxt <= hi):
            step = -step
            nxt = d[-1] + step
        nxt = max(lo, min(hi, nxt))
        if nxt == d[-1]:
            nxt = max(lo, min(hi, d[-1] + 1))
        d.append(nxt)
    return d


def _op_ending_soft(degrees, dec, pcs, key_root, mode, lo, hi):
    """A' 半终止：只把末音就近靠到当前和弦音，主题骨架尽量不动。"""
    d = list(degrees)
    best, best_cost = d[-1], 99
    for delta in range(-2, 3):
        cand = d[-1] + delta
        if lo <= cand <= hi and _degree_pitch(key_root, mode, cand) % 12 in pcs:
            if abs(delta) < best_cost:
                best, best_cost = cand, abs(delta)
    d[-1] = best
    if len(d) >= 2 and d[-2] == d[-1]:
        d[-2] = max(lo, min(hi, d[-1] + dec.choice([-1, 1])))
    return d


def _fit_strong_steps(degs, unit, bar_off, chords_by_bar, key_root, mode, lo, hi,
                      protect=frozenset()):
    """强拍若不在当前和弦音上，只允许按调内级进退一级（不跨半音硬贴）。

    这是"目标和声约束强拍"的最小干预版本：A 段本身已按自身和弦生成，
    只有派生乐句（A'/B/A''）换和弦后需要这一步。
    """
    out = list(degs)
    for i, (onset, _dur) in enumerate(unit):
        if i >= len(out) or i in protect:
            continue
        pos = onset % 4.0
        if min(abs(pos), abs(pos - 2.0)) > 1e-6:
            continue
        bar = int((bar_off + onset) // 4)
        pcs = {p % 12 for p in chords_by_bar[min(bar, len(chords_by_bar) - 1)]}
        if _degree_pitch(key_root, mode, out[i]) % 12 in pcs:
            continue
        for delta in (-1, 1):
            cand = out[i] + delta
            if lo <= cand <= hi and _degree_pitch(key_root, mode, cand) % 12 in pcs:
                # 不制造新的同音连击
                if i > 0 and out[i - 1] == cand:
                    continue
                out[i] = cand
                break
    return out


def _smooth_degrees(degrees, max_step=3, protect=frozenset()):
    """把超过 max_step 的音级跳进收敛为 max_step，避免旋律乱跳。"""
    out = list(degrees)
    for i in range(1, len(out)):
        if i in protect:
            continue
        d = out[i] - out[i - 1]
        if abs(d) > max_step:
            out[i] = out[i - 1] + (max_step if d > 0 else -max_step)
    return out


def _op_ending_rewrite(degrees, dec, target, lo, hi, n_last=3):
    """末 2–4 音级进解决到目标级数（A' 半终止 / A'' 稳定终止）。"""
    d = list(degrees)
    k = max(2, min(n_last, len(d)))
    direction = 1 if d[len(d) - k] < target else -1
    pattern = [target - direction * (k - 1 - j) for j in range(k)]
    if dec.random() < 0.25:
        pattern = [target + direction * (k - 1 - j) for j in range(k)]
    for j, val in enumerate(pattern):
        d[len(d) - k + j] = max(lo, min(hi, val))
    # 末两音不要同度停顿，改成级进靠近
    if len(d) >= 2 and d[-1] == d[-2]:
        d[-2] = max(lo, min(hi, d[-1] + (1 if d[-1] < hi else -1)))
    return d


# ---------------------------------------------------------------- 乐句装配

def _plan_phrases(dec: random.Random, *, seed_rng, key_root, mode, mood, cfg,
                  chords_by_bar, bars, variation, strength):
    """返回 [(name, bar_offset, unit, degrees, vel_curve)], 覆盖全曲。"""
    lo_deg, hi_deg = DRH_LO, DRH_HI
    unit_a, deg_a, cells_a = _gen_motif(seed_rng, key_root=key_root, mode=mode, mood=mood,
                                        cfg=cfg, chords_by_bar=chords_by_bar,
                                        lo_deg=lo_deg,
                                        hi_deg=min(hi_deg, cfg.get("hi", hi_deg)))
    # 先平滑动机，再派生所有乐句：保证 A'/A'' 的主题前缀与 A 逐音一致
    deg_a = _smooth_degrees(deg_a, max_step=3)

    if bars <= 4:
        plan_names = [("A", 0, 2), ("A''", 2, 2)]
        bars_per = 2
    else:
        bars_per = max(2, bars // 4)
        plan_names = [("A", 0, bars_per), ("A'", bars_per, bars_per),
                      ("B", 2 * bars_per, bars_per), ("A''", 3 * bars_per, bars_per)]

    n_ops = 2 if strength >= 0.55 else 1
    # A' 末小节和弦音高类（半终止落点用）
    a1_last_bar = min(bars - 1, max(0, 2 * bars_per - 1))
    pcs_a1 = {p % 12 for p in chords_by_bar[a1_last_bar]}
    out = []
    for name, bar_off, nb in plan_names:
        if name == "A":
            unit, degs = list(unit_a), list(deg_a)
            curve = "a"
        elif name == "A'":
            # 保留第一小节节奏（可辨识），第二小节换一个同族 cell（不全复制）
            bank_main = MOOD_CELLS.get(mood, MOOD_CELLS["melancholy"])["main"]
            c2_alt = next((c for c in bank_main if c != cells_a[1]), None)
            if strength > 0.2 and c2_alt:
                unit = _unit_from_cells(cells_a[0], c2_alt)
            else:
                unit = list(unit_a)
            degs = _fit_length(list(deg_a), len(unit), dec, lo_deg, hi_deg)
            half = max(1, len(degs) // 2)   # 前半句原样保留，保证可辨识
            ops = ["seq", "neighbor"]
            dec.shuffle(ops)
            for op in ops[:n_ops]:
                if op == "seq":
                    degs = _op_diatonic_sequence(degs, dec, lo_deg, hi_deg, start_min=half)
                elif op == "neighbor":
                    degs = _op_neighbor_tone(degs, dec, lo_deg, hi_deg, start_min=half)
            degs = _op_ending_soft(degs, dec, pcs_a1, key_root, mode, lo_deg, hi_deg)
            # A' 只比 A 略高，不能盖过 B 的高潮
            peak_cap = max(deg_a) + 3
            if degs and max(degs) > peak_cap:
                delta = max(degs) - peak_cap
                degs = [max(lo_deg, x - delta) for x in degs]
            curve = "a1"
        elif name == "B":
            unit, _cells_b = _rhythm_unit(dec, mood, "contrast")
            target_peak = min(DEG_HI, max(deg_a) + (6 if strength > 0.5 else 5))
            degs = _op_contour_inversion(deg_a, dec, lo_deg, DEG_HI, target_peak)
            degs = _op_neighbor_tone(degs, dec, lo_deg, DEG_HI)
            degs = _fit_length(degs, len(unit), dec, lo_deg, DEG_HI, direction=-1)
            curve = "b"
        else:  # A''
            unit = _op_rhythmic_variation(unit_a, dec) if strength > 0.3 else list(unit_a)
            degs = list(deg_a)
            if n_ops >= 2 and len(degs) >= 6:
                degs = _op_diatonic_sequence(degs, dec, lo_deg, hi_deg,
                                             start_min=max(1, len(degs) // 2))
            n_last = 3 if len(degs) >= 8 else 2
            degs = _op_ending_rewrite(degs, dec, target=0, lo=lo_deg, hi=hi_deg, n_last=n_last)
            curve = "a2"

        # 只有 B 段需要二次收敛（倒影后可能出现大跳）；A'/A'' 不再平滑，
        # 否则会把本来与 A 一致的主题前缀改掉。
        protect = set()
        if name == "B" and degs:
            protect = {max(range(len(degs)), key=lambda k: degs[k])}
            degs = _smooth_degrees(degs, max_step=4, protect=protect)

        # 派生乐句换和弦后，强拍做最小级进校正。
        # A 段按自身和弦生成、A'' 回到主和弦，都不需要校正；只有 A'/B 换了和弦。
        if name in ("A'", "B") and degs:
            degs = _fit_strong_steps(degs, unit, bar_off * 4.0, chords_by_bar,
                                     key_root, mode, lo_deg, hi_deg, protect=protect)

        # 多小节乐句：以 2 小节单元平铺
        unit_full, degs_full = [], []
        repeat = max(1, nb // 2)
        for r in range(repeat):
            shift = 0 if r == 0 else (0 if name != "B" else 1)
            unit_full += [(o + 8.0 * r, d) for (o, d) in unit]
            degs_full += [max(lo_deg, min(DEG_HI, x + shift)) for x in degs]
        out.append((name, bar_off * 4.0, unit_full, degs_full, curve))

    # B 段必须是全曲最高点（局部高潮），否则倒影可能把它压到 A' 之下
    b_idx = next((i for i, p in enumerate(out) if p[0] == "B" and p[3]), None)
    if b_idx is not None:
        others = [max(p[3]) for i, p in enumerate(out) if i != b_idx and p[3]]
        if others:
            target = max(others) + 2
            cur = max(out[b_idx][3])
            if cur < target:
                delta = target - cur
                name, off, unit, degs, curve = out[b_idx]
                degs = [max(lo_deg, min(DEG_HI, d + delta)) for d in degs]
                out[b_idx] = (name, off, unit, degs, curve)
    return out


# ---------------------------------------------------------------- 渲染音符

VEL_CURVE = {
    "a":  lambda t: 2.0 + 3.0 * math.sin(math.pi * t),
    "a1": lambda t: 5.0 + 3.0 * math.sin(math.pi * t),
    "b":  lambda t: 10.0 + 9.0 * math.sin(math.pi * min(1.0, t * 1.1)),
    "a2": lambda t: 4.0 - 6.0 * t,
}


def _render_rh(dec, plan, chords_by_bar, key_root, mode, cfg, bpm, jitter_ms):
    events = []
    base_vel = int(cfg.get("vel", 66))
    jitter_ticks = int(round(jitter_ms / 1000.0 * bpm / 60.0 * TPB))
    total = max(1, sum(len(p[3]) for p in plan))
    idx = 0
    for name, beat_off, unit, degs, curve in plan:
        fn = VEL_CURVE.get(curve, VEL_CURVE["a"])
        n = max(1, len(degs))
        peak_i = max(range(len(degs)), key=lambda k: degs[k]) if degs else -1
        prev_pitch = None
        for i, ((onset, dur), deg) in enumerate(zip(unit, degs)):
            abs_beat = beat_off + onset
            bar = int(abs_beat // 4)
            pos = abs_beat - bar * 4.0
            chord = chords_by_bar[min(bar, len(chords_by_bar) - 1)]
            pcs = {p % 12 for p in chord}
            pitch = _degree_pitch(key_root, mode, deg)
            is_strong = min(abs(pos), abs(pos - 2.0)) < 1e-6
            # 强拍的和声倾向在 _pick_degree 打分阶段完成，这里不做 clamp 式硬贴：
            # 旧版正是逐音 clamp 把不同级数压到同一和弦音，才形成连续同音。
            pitch = max(RH_LO, min(RH_HI, pitch))
            # 同音重复：靠时值断开，不改变音高（保留动机）
            articulation = 0.80 if prev_pitch == pitch else 0.90
            if i == n - 1:
                articulation = 0.98
            vel = base_vel + fn(idx / max(1, total - 1)) + dec.randint(-3, 3)
            vel = max(42, min(108, int(round(vel))))
            start_tick = int(round(abs_beat * BEAT))
            if jitter_ticks:
                # 人性化必须留在本小节内：不允许因 jitter 跨小节线
                start_tick += dec.randint(-jitter_ticks, jitter_ticks)
            bar_start = int(round(bar * BAR))
            start_tick = max(bar_start, min(start_tick, bar_start + BAR - 30))
            dur_tick = max(60, int(round(dur * BEAT * articulation)))
            events.append((start_tick, dur_tick, pitch, vel))
            prev_pitch = pitch
            idx += 1
    return events


def _lh_bar(dec, chord, texture):
    """左手织体：返回 [(tick_in_bar, dur_tick, pitch, vel)]"""
    root = chord[0]
    while root > 47:
        root -= 12
    while root < 36:
        root += 12
    third = chord[1] if len(chord) > 1 else chord[0] + 4
    fifth = chord[0] + 7

    def fit(p):
        while p > 59:
            p -= 12
        while p < 36:
            p += 12
        return p

    mid = [fit(chord[0]), fit(third), fit(fifth)]
    out = []
    if texture == "final":
        out.append((0, int(BAR * 0.98), root, 58))
        for p in (fit(third), fit(fifth), fit(chord[0] + 12)):
            out.append((0, int(BAR * 0.98), p, 50))
        return out
    if texture == "sparse":
        out.append((0, int(BEAT * 3.6), root, 54))
        out.append((2 * BEAT, int(BEAT * 1.8), mid[0], 48))
    elif texture == "block":
        for beat in (0, 2):
            out.append((beat * BEAT, int(BEAT * 1.85), root, 58))
            for p in mid:
                out.append((beat * BEAT, int(BEAT * 1.85), p, 48))
    elif texture == "arp":
        seq = [root, fit(fifth), fit(chord[0] + 12), fit(fifth),
               root, fit(fifth), fit(chord[0] + 12), fit(fifth)]
        for k, p in enumerate(seq):
            out.append((int(k * BEAT * 0.5), int(BEAT * 0.46), p, 47 + (k % 2) * 3))
    elif texture == "octave":
        out.append((0, int(BEAT * 1.85), root, 62))
        out.append((0, int(BEAT * 1.85), fit(root + 12), 52))
        for p in mid:
            out.append((2 * BEAT, int(BEAT * 1.85), p, 50))
    return out


def _render_lh(dec, chords_by_bar, bars, plan):
    """按乐句切换织体：A 柱式、A' 分解、B 八度加密、A'' 收稀留白，末小节终止和弦。"""
    texture_of = {}
    n = len(plan)
    for i, (name, beat_off, _unit, _degs, _curve) in enumerate(plan):
        start = int(round(beat_off / 4))
        end = int(round(plan[i + 1][1] / 4)) if i + 1 < n else bars
        end = min(bars, max(start + 1, end))
        for b in range(start, end):
            k = b - start
            if name == "B":
                tex = "octave" if k % 2 == 0 else "arp"
            elif name == "A'":
                tex = "arp" if k % 2 == 0 else "block"
            elif name == "A''":
                tex = "block" if k == 0 else "sparse"
            else:
                tex = "block" if k % 2 == 0 else "sparse"
            texture_of[b] = tex
    events = []
    for bar in range(bars):
        final = (bar == bars - 1)
        tex = "final" if final else texture_of.get(bar, "block")
        for (t, d, p, v) in _lh_bar(dec, chords_by_bar[bar], tex):
            events.append((bar * BAR + t, d, p, v))
    return events


# ---------------------------------------------------------------- 写 MIDI

def _flush_track(mid, note_events, cc_events, channel, program):
    tr = MidiTrack()
    mid.tracks.append(tr)
    tr.append(Message("program_change", channel=channel, program=program, time=0))
    items = []
    for (start, dur, pitch, vel) in note_events:
        items.append((start, 1, "on", pitch, vel))
        items.append((max(start + 1, start + dur), 0, "off", pitch, 0))
    for (start, ctrl, val) in cc_events:
        items.append((start, -1, "cc", ctrl, val))
    items.sort(key=lambda x: (x[0], x[1]))
    last = 0
    for tick, _prio, kind, a, b in items:
        t = max(tick, last)
        if kind == "on":
            tr.append(Message("note_on", channel=channel, note=a, velocity=b, time=t - last))
        elif kind == "off":
            tr.append(Message("note_off", channel=channel, note=a, velocity=0, time=t - last))
        else:
            tr.append(Message("control_change", channel=channel, control=a, value=b, time=t - last))
        last = t


def _pedal_events(bars):
    ev = []
    for bar in range(bars):
        ev.append((bar * BAR, 64, 127))
        ev.append((bar * BAR + BAR - 30, 64, 0))
    return ev


def _build_midi(mood: str, key: str, mode: str, bpm: int, bars: int, seed: int,
                variation: int = 0, out: Path | None = None,
                strength: float | None = None) -> None:
    mood = mood if mood in PROGRESSIONS else "melancholy"
    mode = mode or MOOD_MODE.get(mood, "minor")
    cfg = MOOD_CFG.get(mood, MOOD_CFG["melancholy"])
    if strength is None:
        strength = min(1.0, 0.45 + 0.15 * variation)

    seed_rng = random.Random(seed)                 # 动机层：只由 seed 决定
    harm_rng = random.Random(seed * 7 + 11)        # 和声进行也只由 seed 决定
    dec = random.Random(seed * 1000 + variation)   # 装饰层：随 variation 变化

    key_root = 60 + NOTE_OFFSET.get(key, 0)
    bank = PROGRESSIONS[mood]
    # 和声进行必须与 variation 无关，否则变奏会连 A 段一起改掉
    prog = bank[harm_rng.randrange(len(bank))]

    chords_by_bar = []
    for b in range(bars):
        slot = (b // 2) % len(prog)
        deg1, qual = prog[slot]
        if b == bars - 1:
            deg1, qual = 1, ("maj" if mode == "major" else "min")
        chords_by_bar.append(_chord_pitches(key_root, mode, deg1, qual))

    plan = _plan_phrases(dec, seed_rng=seed_rng, key_root=key_root, mode=mode, mood=mood,
                         cfg=cfg, chords_by_bar=chords_by_bar, bars=bars,
                         variation=variation, strength=strength)

    jitter_ms = [8, 11, 14][min(2, variation % 3)]
    rh_events = _render_rh(dec, plan, chords_by_bar, key_root, mode, cfg, bpm, jitter_ms)
    lh_events = _render_lh(dec, chords_by_bar, bars, plan)
    pedal = _pedal_events(bars)

    mid = MidiFile(ticks_per_beat=TPB)
    t0 = MidiTrack()
    mid.tracks.append(t0)
    t0.append(MetaMessage("set_tempo", tempo=bpm2tempo(bpm), time=0))
    t0.append(MetaMessage("time_signature", numerator=4, denominator=4, time=0))
    t0.append(MetaMessage("track_name", name=f"Monika Piano {mood} seed={seed} v{ENGINE_VERSION}", time=0))

    _flush_track(mid, rh_events, pedal, 0, 0)   # 右手 + 踏板(ch0)
    _flush_track(mid, lh_events, pedal, 1, 0)   # 左手 + 踏板(ch1)

    out.parent.mkdir(parents=True, exist_ok=True)
    mid.save(out)


# ---------------------------------------------------------------- 运行时依赖

def _vendor_paths() -> tuple[Path, Path, Path]:
    return (RUNTIME_DIR / "bin" / "fluidsynth", RUNTIME_DIR / "lib",
            RUNTIME_DIR / "soundfonts" / "TimGM6mb.sf2")


def resolve_runtime(soundfont: str = "") -> dict:
    """优先用持久化副本（镜像重建不丢），缺失时回退系统安装。"""
    vbin, vlib, vsf = _vendor_paths()
    cmd, env, source = ["fluidsynth"], None, ("system" if shutil.which("fluidsynth") else "none")
    if vbin.is_file() and os.access(vbin, os.X_OK):
        cmd = [str(vbin)]
        env = dict(os.environ)
        old = env.get("LD_LIBRARY_PATH", "")
        env["LD_LIBRARY_PATH"] = f"{vlib}:{old}" if old else str(vlib)
        source = "vendored"
    sf = soundfont or DEFAULT_SOUNDFONT
    if not Path(sf).exists() and vsf.is_file():
        sf = str(vsf)
    return {"cmd": cmd, "env": env, "soundfont": sf, "source": source,
            "fluidsynth_ok": source != "none", "soundfont_ok": Path(sf).exists()}


def check_runtime(soundfont: str = "") -> dict:
    """插件启动自检：只报告现状，不执行任何安装。"""
    issues: list[str] = []
    try:
        import mido  # noqa: F401
        mido_ok = True
    except Exception as exc:  # noqa: BLE001
        mido_ok = False
        issues.append(f"mido 导入失败: {exc}")
    rt = resolve_runtime(soundfont)
    if not rt["fluidsynth_ok"]:
        issues.append("fluidsynth 不可用（持久化副本与系统 PATH 都没有）")
    if not rt["soundfont_ok"]:
        issues.append(f"SoundFont 不存在: {rt['soundfont']}")
    return {"ok": not issues, "mido": mido_ok, "fluidsynth": rt["source"],
            "soundfont": rt["soundfont"], "issues": issues}


def _render(mid: Path, wav: Path, soundfont: str = "") -> None:
    rt = resolve_runtime(soundfont)
    if not rt["fluidsynth_ok"]:
        raise FileNotFoundError("fluidsynth 不可用")
    if not rt["soundfont_ok"]:
        raise FileNotFoundError(f"SoundFont 不存在: {rt['soundfont']}")
    wav.parent.mkdir(parents=True, exist_ok=True)
    subprocess.run(
        rt["cmd"] + ["-ni", rt["soundfont"], str(mid), "-F", str(wav),
                     "-r", "44100", "-g", "0.8"],
        check=True, capture_output=True, timeout=120, env=rt["env"],
    )


def ensure_wav(meta: dict, soundfont: str = "") -> str | None:
    """确保 WAV 可用：已存在直接返回；被清理过则用 MIDI 重新渲染。"""
    mid = Path(meta.get("midi_path") or "")
    if not mid.is_file():
        return None
    wav = Path(meta.get("wav_path") or str(mid.with_suffix(".wav")))
    if wav.is_file() and wav.stat().st_size > 4096:
        return str(wav)
    try:
        _render(mid, wav, soundfont)
    except Exception as exc:  # noqa: BLE001
        LOGGER.error(f"[Piano] 重渲染失败 {mid.name}: {exc}")
        return None
    return str(wav) if wav.is_file() and wav.stat().st_size > 4096 else None


# ---------------------------------------------------------------- 资源保护

def _mem_available_mb() -> int:
    try:
        with open("/proc/meminfo", encoding="utf-8") as fh:
            for line in fh:
                if line.startswith("MemAvailable:"):
                    return int(line.split()[1]) // 1024
    except OSError:
        pass
    return -1


def _load1() -> float:
    try:
        with open("/proc/loadavg", encoding="utf-8") as fh:
            return float(fh.read().split()[0])
    except OSError:
        return -1.0


def check_resources(min_mem_mb: int = 200, max_load1: float = 6.0) -> tuple[bool, str]:
    mem = _mem_available_mb()
    load = _load1()
    if 0 <= mem < min_mem_mb:
        return False, f"内存紧张 (MemAvailable {mem}MB < {min_mem_mb}MB)"
    if load > max_load1:
        return False, f"系统负载偏高 (load1 {load:.2f} > {max_load1})"
    return True, ""


# ---------------------------------------------------------------- 对外接口

def _append_index(meta: dict, index_path: Path) -> None:
    """legacy 索引追加（仅在 SQLite 目录不可用时使用）。"""
    try:
        with open(index_path, "a", encoding="utf-8") as f:
            f.write(json.dumps(meta, ensure_ascii=False) + "\n")
    except OSError:
        pass


_catalog_ready: set[str] = set()


def ensure_catalog(outdir: str) -> None:
    """首次使用时建表并迁移 legacy jsonl（幂等）。"""
    if _catalog is None or outdir in _catalog_ready:
        return
    try:
        imported = _catalog.init(outdir)
        _catalog_ready.add(outdir)
        if imported:
            LOGGER.info("[Piano] 作品目录已迁移 %d 条 legacy 记录", imported)
    except Exception as exc:  # noqa: BLE001
        LOGGER.warning("[Piano] 作品目录初始化失败, 继续用 jsonl: %s", exc)


def _record_piece(meta: dict, outdir: str) -> None:
    """作品入库：优先 SQLite 目录，失败回退 jsonl，保证索引不丢。"""
    if _catalog is not None:
        try:
            if _catalog.add(outdir, meta):
                return
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[Piano] 目录写入异常, 回退 jsonl: %s", exc)
    _append_index(meta, Path(outdir) / "pieces_index.jsonl")


def last_piece(outdir: str = "/MaiMBot/data/piano", chat_id: str = "") -> dict | None:
    ensure_catalog(outdir)
    if _catalog is not None:
        try:
            row = _catalog.last(outdir, chat_id)
            if row is not None:
                return row
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[Piano] 目录查询失败, 回退 jsonl: %s", exc)

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


def generate(mood: str = "melancholy", key: str = "C", mode: str = "",
             bpm: int = 74, bars: int = 8, seed: int = 1842,
             variation: int = 0, chat_id: str = "",
             outdir: str = "/MaiMBot/data/piano",
             soundfont: str = "/usr/share/sounds/sf2/TimGM6mb.sf2",
             variation_strength: float | None = None,
             min_mem_mb: int = 200, max_load1: float = 6.0) -> dict:
    """生成一首钢琴曲，返回元数据。同参数同 seed 逐字节一致。"""
    mood = mood if mood in PROGRESSIONS else "melancholy"
    bars = 8 if bars not in (4, 8, 16) else bars
    ok, why = check_resources(min_mem_mb=min_mem_mb, max_load1=max_load1)
    if not ok:
        raise ResourceBusyError(why)

    strength = variation_strength
    if strength is None:
        strength = min(1.0, 0.45 + 0.15 * variation)
    strength = max(0.0, min(1.0, float(strength)))

    with _GEN_LOCK:
        stem = f"piece_{seed}_{mood}" + (f"_v{variation}" if variation > 0 else "")
        out = Path(outdir) / f"{stem}.mid"
        _build_midi(mood, key, mode, bpm, bars, seed, variation=variation,
                    out=out, strength=strength)
        tmp = Path("/tmp/_piano_repro.mid")
        try:
            _build_midi(mood, key, mode, bpm, bars, seed, variation=variation,
                        out=tmp, strength=strength)
            md5 = hashlib.md5(out.read_bytes()).hexdigest()
            reproducible = md5 == hashlib.md5(tmp.read_bytes()).hexdigest()
        finally:
            tmp.unlink(missing_ok=True)

        wav = out.with_suffix(".wav")
        _render(out, wav, soundfont)

    meta = {
        "piece_id": out.stem, "seed": seed, "mood": mood,
        "key": f"{key} {(mode or MOOD_MODE.get(mood, 'minor'))}",
        "bpm": bpm, "bars": bars, "variation": variation,
        "parent_piece_id": f"piece_{seed}_{mood}" if variation > 0 else None,
        "variation_strength": round(strength, 3),
        "engine_version": ENGINE_VERSION,
        "chat_id": chat_id, "created_at": int(time.time()),
        "reproducible": reproducible, "md5": md5,
        "midi_path": str(out), "wav_path": str(wav),
        "napcat_uri": f"file://{wav}",
    }
    (Path(outdir) / f"{out.stem}.json").write_text(
        json.dumps(meta, ensure_ascii=False, indent=2), encoding="utf-8")
    ensure_catalog(outdir)
    _record_piece(meta, outdir)
    return meta
