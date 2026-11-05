"""Monika Piano 诊断指标 + A/B 对比。

用法:
  python ab_metrics.py <dir> [<dir> ...]
按文件名配对（legacy 与原版同名），输出终端表格 + /tmp/ab_metrics.json。

指标口径见任务书第 2/10 节：
  consecutive_same_pitch_ratio  连续相同音次数 / 相邻音对数
  exact_bar_copy_ratio          整小节(音高+时值)完全重复的比例
  rhythmic_pattern_diversity    不同节奏型数量 / 小节数
  strong_beat_chord_tone_ratio  强拍音落在当前小节和弦音上的比例
  final_resolution              末小节低音是否回到主音 + 末音是否和弦音
  phrase_*_sim                  A 与 A'/B/A'' 的音程与节奏相似度 (0=完全不同, 1=逐字复制)
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

from mido import MidiFile

STRONG_TOL = 24  # tick 容差


def _lcs_ratio(a: list, b: list) -> float:
    if not a or not b:
        return 0.0
    m, n = len(a), len(b)
    dp = [[0] * (n + 1) for _ in range(m + 1)]
    for i in range(1, m + 1):
        for j in range(1, n + 1):
            dp[i][j] = dp[i - 1][j - 1] + 1 if a[i - 1] == b[j - 1] else max(dp[i - 1][j], dp[i][j - 1])
    return round(dp[m][n] / max(m, n), 3)


def load_notes(path: Path):
    """返回 (tpb, notes, warnings, total_ticks)。notes=[{start,dur,note,vel,ch,track}]"""
    mid = MidiFile(str(path))
    tpb = mid.ticks_per_beat
    warns: list[str] = []
    notes = []
    max_tick = 0
    for ti, track in enumerate(mid.tracks):
        t = 0
        active: dict[tuple[int, int], tuple[int, int]] = {}
        for msg in track:
            if msg.time < 0:
                warns.append(f"负 delta-time: track{ti} {msg.type} {msg.time}")
            t += msg.time
            max_tick = max(max_tick, t)
            if msg.type == "note_on" and msg.velocity > 0:
                if not (0 <= msg.note <= 127):
                    warns.append(f"音高越界: {msg.note}")
                key = (msg.channel, msg.note)
                if key in active:
                    warns.append(f"重复 note_on 未关闭: ch{msg.channel} note{msg.note} @{t}")
                    st, v = active.pop(key)
                    notes.append({"start": st, "dur": t - st, "note": msg.note,
                                  "vel": v, "ch": msg.channel, "track": ti})
                active[key] = (t, msg.velocity)
            elif msg.type in ("note_on", "note_off"):
                key = (msg.channel, msg.note)
                if key in active:
                    st, v = active.pop(key)
                    notes.append({"start": st, "dur": t - st, "note": msg.note,
                                  "vel": v, "ch": msg.channel, "track": ti})
                elif msg.type == "note_off":
                    warns.append(f"孤立 note_off: ch{msg.channel} note{msg.note} @{t}")
        for (ch, note), (st, v) in active.items():
            warns.append(f"音符未关闭: ch{ch} note{note} @{st}")
    return tpb, notes, warns, max_tick


def split_hands(notes: list) -> tuple[list, list]:
    """按声道分组，平均音高更高者为右手主旋律。"""
    by_voice: dict[int, list] = {}
    for n in notes:
        by_voice.setdefault(n["ch"], []).append(n)
    if len(by_voice) >= 2:
        ch_hi = max(by_voice, key=lambda c: sum(x["note"] for x in by_voice[c]) / len(by_voice[c]))
        rh = by_voice[ch_hi]
        lh = [n for c, v in by_voice.items() if c != ch_hi for n in v]
        return sorted(rh, key=lambda x: x["start"]), sorted(lh, key=lambda x: x["start"])
    ordered = sorted(notes, key=lambda x: x["start"])
    return [n for n in ordered if n["note"] >= 60], [n for n in ordered if n["note"] < 60]


def bar_view(notes: list, tpb: int) -> dict[int, list]:
    bt = tpb * 4
    out: dict[int, list] = {}
    for n in notes:
        out.setdefault(n["start"] // bt, []).append(n)
    for b in out:
        out[b].sort(key=lambda x: x["start"])
    return out


def analyze(path: Path) -> dict:
    tpb, notes, warns, max_tick = load_notes(path)
    rh, lh = split_hands(notes)
    bars_total = max(1, round(max_tick / (tpb * 4)))
    bt = tpb * 4
    rh_bars = bar_view(rh, tpb)
    lh_bars = bar_view(lh, tpb)

    # --- 连续同音 ---
    same = sum(1 for a, b in zip(rh, rh[1:]) if a["note"] == b["note"])
    pairs = max(1, len(rh) - 1)
    run = best_run = 1
    for a, b in zip(rh, rh[1:]):
        run = run + 1 if a["note"] == b["note"] else 1
        best_run = max(best_run, run)

    # --- 音程 ---
    ivs = [abs(b["note"] - a["note"]) for a, b in zip(rh, rh[1:])]

    # --- 整小节重复 / 节奏多样性 ---
    # 节奏型按"小节内起音位置"(16 分音符网格) 判定，不用时值：
    # 时值会被 articulation / 断奏影响，起音位置才是听众感知的节奏。
    sig = {}
    rhythms = set()
    for b in range(bars_total):
        seg = rh_bars.get(b, [])
        if not seg:
            continue
        pitches = tuple(n["note"] for n in seg)
        onsets = tuple(round(((n["start"] % bt) / tpb) * 4) for n in seg)
        sig[b] = (pitches, onsets)
        rhythms.add(onsets)
    dup = 0
    seen = set()
    for b, s in sig.items():
        if s in seen:
            dup += 1
        seen.add(s)

    # --- 强拍和弦音 ---
    # 和声每 2 小节一换，且左手织体存在"留白"小节（只有根音），
    # 因此按 2 小节窗口合并左手音高类作为当前和弦，避免稀疏小节误判。
    strong_total = strong_hit = 0
    bt = tpb * 4
    bar_pcs = {}
    for b in sorted(lh_bars):
        win = [b - (b % 2), b - (b % 2) + 1]
        pcs = {x["note"] % 12 for w in win for x in lh_bars.get(w, [])}
        bar_pcs[b] = pcs if pcs else {x["note"] % 12 for x in lh_bars.get(b, [])}
    for n in rh:
        off = n["start"] % bt
        if min(abs(off - 0), abs(off - 2 * tpb)) <= STRONG_TOL:
            strong_total += 1
            bar = n["start"] // bt
            pcs = bar_pcs.get(bar, set())
            if pcs and n["note"] % 12 in pcs:
                strong_hit += 1

    # --- 终止 ---
    lh_roots = [min((x["note"] for x in lh_bars[b]), default=None) for b in sorted(lh_bars)]
    tonic_pc = (lh_roots[0] % 12) if lh_roots and lh_roots[0] is not None else None
    last_root = lh_roots[-1] if lh_roots else None
    final_pc_ok = (last_root is not None and tonic_pc is not None
                   and last_root % 12 == tonic_pc)
    last_bar_pcs = {x["note"] % 12 for x in lh_bars.get(bars_total - 1, [])}
    last_rh_in_chord = bool(rh) and (rh[-1]["note"] % 12 in last_bar_pcs)

    # --- 乐句（每 2 小节） ---
    phrases = []
    for p in range(bars_total // 2):
        seg = [n for b in (2 * p, 2 * p + 1) for n in rh_bars.get(b, [])]
        if not seg:
            phrases.append({"iv": [], "rhythm": [], "pitch": [], "onset": [],
                            "mean_pitch": 0, "peak": 0})
            continue
        piv = [b["note"] - a["note"] for a, b in zip(seg, seg[1:])]
        phrases.append({
            "iv": piv,
            "rhythm": [round(n["dur"] / tpb * 4) / 4 for n in seg],
            "pitch": [n["note"] for n in seg],
            "mean_pitch": round(sum(n["note"] for n in seg) / len(seg), 1),
            "peak": max(n["note"] for n in seg),
            "onset": [round((n["start"] % (tpb * 4)) / tpb, 2) for n in seg],
        })

    phrase_rhythms = []
    for p in range(bars_total // 2):
        seg = [n for b in (2 * p, 2 * p + 1) for n in rh_bars.get(b, [])]
        if not seg:
            phrase_rhythms.append(())
            continue
        base = 2 * p * bt
        phrase_rhythms.append(tuple(round(((n["start"] - base) / tpb) * 4) for n in seg))
    non_empty_pr = [x for x in phrase_rhythms if x]

    def sim(i, j):
        if len(phrases) <= max(i, j):
            return None
        a, b = phrases[i], phrases[j]
        return {"pitch_iv": _lcs_ratio(a["iv"], b["iv"]),
                "rhythm": _lcs_ratio(a["rhythm"], b["rhythm"]),
                "onset": _lcs_ratio(a["onset"], b["onset"])}

    return {
        "file": path.name,
        "bars": bars_total,
        "bars_silent": bars_total - len(sig),
        "rh_notes": len(rh),
        "lh_notes": len(lh),
        "duration_sec_est_ignored": None,
        "consecutive_same_pitch_ratio": round(same / pairs, 3),
        "max_same_pitch_run": best_run,
        "exact_bar_copy_ratio": round(dup / max(1, len(sig)), 3),
        "rhythmic_pattern_diversity": round(len(rhythms) / max(1, len(sig)), 3),
        "phrases_with_melody": len(non_empty_pr),
        "phrase_rhythm_distinct": len(set(non_empty_pr)),
        "unique_pitches": len({n["note"] for n in rh}),
        "pitch_range": (max(n["note"] for n in rh) - min(n["note"] for n in rh)) if rh else 0,
        "mean_interval": round(sum(ivs) / len(ivs), 2) if ivs else 0,
        "max_interval": max(ivs) if ivs else 0,
        "strong_beat_chord_tone_ratio": round(strong_hit / strong_total, 3) if strong_total else 0,
        "final_root_to_tonic": final_pc_ok,
        "final_note_is_chord_tone": last_rh_in_chord,
        "phrase_peak": [p["peak"] for p in phrases],
        "phrase_peak_bar": [phrases.index(p) for p in phrases],
        "sim_A_A1": sim(0, 1), "sim_A_B": sim(0, 2), "sim_A_A2": sim(0, 3),
        "warnings": warns[:12],
        "warn_count": len(warns),
    }


def main(argv: list[str]) -> int:
    dirs = [Path(a) for a in argv[1:]]
    files = []
    for d in dirs:
        files += sorted(d.glob("*.mid"))
    if not files:
        print("没有找到 .mid 文件")
        return 1
    results = []
    for f in files:
        try:
            r = analyze(f)
            r["dir"] = f.parent.name
            results.append(r)
        except Exception as exc:  # noqa: BLE001
            print(f"[失败] {f.name}: {exc}")
    Path("/tmp/ab_metrics.json").write_text(
        json.dumps(results, ensure_ascii=False, indent=2), encoding="utf-8")

    hdr = ("file", "notes", "空小节", "同音率", "最长同音", "整节重复", "节奏多样性",
           "音级数", "音域", "平均音程", "最大音程", "强拍和弦音", "终止")
    fmt = "{:<28}{:>6}{:>7}{:>9}{:>9}{:>9}{:>8}{:>7}{:>6}{:>9}{:>8}{:>9}{:>6}"
    print(fmt.format(*hdr))
    for r in results:
        print(fmt.format(
            f"{r['dir']}/{r['file'][:22]}", r["rh_notes"], r["bars_silent"],
            r["consecutive_same_pitch_ratio"],
            r["max_same_pitch_run"], r["exact_bar_copy_ratio"], r["rhythmic_pattern_diversity"],
            r["unique_pitches"], r["pitch_range"], r["mean_interval"], r["max_interval"],
            r["strong_beat_chord_tone_ratio"],
            "OK" if r["final_root_to_tonic"] else "NO"))
    print()
    for r in results:
        s = r["sim_A_A1"]
        print(f"{r['dir']}/{r['file'][:24]:<26} 乐句峰值={r['phrase_peak']} "
              f"有声乐句={r['phrases_with_melody']}/4 节奏型={r['phrase_rhythm_distinct']} "
              f"A~A'={s['pitch_iv'] if s else '-'}/{s['rhythm'] if s else '-'} "
              f"A~B={r['sim_A_B']['pitch_iv'] if r['sim_A_B'] else '-'} "
              f"A~A''={r['sim_A_A2']['pitch_iv'] if r['sim_A_A2'] else '-'} "
              f"warn={r['warn_count']}")
    bad = [r for r in results if r["warn_count"]]
    print(f"\n合法性: {len(results) - len(bad)}/{len(results)} 通过" +
          (f"；问题文件: {[r['file'] for r in bad]}" if bad else ""))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
