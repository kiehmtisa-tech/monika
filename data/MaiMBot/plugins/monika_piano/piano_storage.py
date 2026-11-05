"""钢琴作品库维护：WAV 生命周期管理（不含作曲逻辑）。

设计原则（对应任务书"作品库清理"一节）：
  * MIDI / JSON 元数据 / pieces_index.jsonl 永不自动删除 —— 它们是可复现的源
  * 普通 WAV 默认保留 N 天，另有总容量上限；超限时优先清理最旧的
  * 用户收藏的作品永久保留
  * 删 WAV 前必须确认 MIDI 存在且可解析（否则保留，不删唯一副本）
  * 支持 dry-run：只输出拟删除清单与可释放空间
  * 清理每天最多执行一次（状态文件记录日期）
"""
from __future__ import annotations

import json
import time
from pathlib import Path

try:                       # 插件方式
    from . import piano_catalog as _catalog
except Exception:          # noqa: BLE001
    try:                   # 脚本方式
        import piano_catalog as _catalog  # type: ignore
    except Exception:      # noqa: BLE001
        _catalog = None

INDEX_NAME = "pieces_index.jsonl"
FAV_NAME = "favorites.json"
STATE_NAME = "cleanup_state.json"
KEEP_SUFFIXES = {".mid", ".json", ".jsonl"}


def _read_index(outdir: Path) -> dict[str, dict]:
    """piece_id -> meta。优先 SQLite 目录，回退 legacy jsonl。"""
    if _catalog is not None:
        try:
            _catalog.init(outdir)            # 幂等，确保 legacy 已迁移
            metas = _catalog.all_meta(outdir)
            if metas:
                return metas
        except Exception:  # noqa: BLE001
            pass
    path = outdir / INDEX_NAME
    out: dict[str, dict] = {}
    if not path.exists():
        return out
    try:
        with open(path, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    item = json.loads(line)
                except json.JSONDecodeError:
                    continue
                pid = item.get("piece_id")
                if pid:
                    out[pid] = item
    except OSError:
        pass
    return out


def load_favorites(outdir: Path) -> set[str]:
    path = outdir / FAV_NAME
    if not path.exists():
        return set()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(data, list):
            return {str(x) for x in data}
        return {str(x) for x in (data.get("piece_ids") or [])}
    except (OSError, json.JSONDecodeError):
        return set()


def save_favorites(outdir: Path, ids: set[str]) -> None:
    path = outdir / FAV_NAME
    payload = {"piece_ids": sorted(ids), "updated_at": int(time.time())}
    path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")


def toggle_favorite(outdir: Path, piece_id: str) -> bool:
    """返回 True 表示现在是收藏状态。"""
    favs = load_favorites(outdir)
    if piece_id in favs:
        favs.discard(piece_id)
    else:
        favs.add(piece_id)
    save_favorites(outdir, favs)
    return piece_id in favs


def _midi_ok(mid: Path) -> bool:
    """MIDI 存在且可解析、至少有一个音符。"""
    if not mid.exists() or mid.stat().st_size < 32:
        return False
    try:
        from mido import MidiFile
        midf = MidiFile(str(mid))
        for track in midf.tracks:
            for msg in track:
                if msg.type == "note_on" and msg.velocity > 0:
                    return True
    except Exception:  # noqa: BLE001
        return False
    return False


def scan(outdir: str | Path) -> list[dict]:
    """列出作品库里的 WAV 及其状态（不做任何修改）。"""
    out = Path(outdir)
    index = _read_index(out)
    favs = load_favorites(out)
    now = time.time()
    rows = []
    for wav in sorted(out.glob("*.wav")):
        pid = wav.stem
        meta = index.get(pid, {})
        mid = wav.with_suffix(".mid")
        created = meta.get("created_at") or int(wav.stat().st_mtime)
        rows.append({
            "piece_id": pid,
            "wav": wav.name,
            "size_mb": round(wav.stat().st_size / 1048576, 2),
            "age_days": round((now - created) / 86400, 1),
            "favorite": pid in favs,
            "midi_ok": _midi_ok(mid),
            "mood": meta.get("mood", ""),
            "variation": meta.get("variation", 0),
        })
    return rows


def cleanup(outdir: str | Path, *, ttl_days: int = 30, max_wav_mb: int = 300,
            dry_run: bool = True, now: float | None = None,
            max_pieces: int = 5000, midi_max_mb: int = 1024,
            min_keep_days: int = 7) -> dict:
    """两级维护：
      1) 硬阈值——作品库条数/容量超限时按最旧整首移除（含乐谱）
      2) 软阈值——普通 WAV 超期/超容量时只删音频，保留乐谱
    dry_run 时只出计划。
    """
    out = Path(outdir)
    if not out.is_dir():
        return {"error": f"目录不存在: {out}"}
    now = time.time() if now is None else now
    favs = load_favorites(out)

    # ---- 1) 作品库硬阈值（整首移除） ----
    cat_res = None
    if _catalog is not None:
        try:
            cat_res = _catalog.prune(out, max_mb=midi_max_mb, max_pieces=max_pieces,
                                     min_keep_days=min_keep_days, favorites=favs,
                                     dry_run=dry_run, now=now)
        except Exception as exc:  # noqa: BLE001
            cat_res = {"error": f"{type(exc).__name__}: {exc}"}

    # ---- 2) WAV 软阈值（只删音频） ----
    rows = scan(out)

    victims: list[dict] = []
    protected_no_midi: list[str] = []

    # 规则 1：超过 TTL
    for r in rows:
        if r["favorite"]:
            continue
        if r["age_days"] <= ttl_days:
            continue
        if not r["midi_ok"]:
            protected_no_midi.append(r["piece_id"])
            continue
        victims.append(r)

    # 规则 2：容量上限（在 TTL 结果之外继续按最旧优先）
    total_mb = sum(r["size_mb"] for r in rows)
    chosen = {v["piece_id"] for v in victims}
    if total_mb > max_wav_mb:
        need = total_mb - max_wav_mb
        freed = sum(v["size_mb"] for v in victims)
        for r in sorted(rows, key=lambda x: x["age_days"], reverse=True):
            if freed >= need:
                break
            if r["favorite"] or r["piece_id"] in chosen or not r["midi_ok"]:
                continue
            victims.append(r)
            chosen.add(r["piece_id"])
            freed += r["size_mb"]

    freed_mb = round(sum(v["size_mb"] for v in victims), 2)
    deleted, failed = [], []
    if not dry_run:
        for v in victims:
            try:
                (out / v["wav"]).unlink()
                deleted.append(v["piece_id"])
            except OSError as exc:
                failed.append(f"{v['piece_id']}: {exc}")

    result = {
        "dry_run": dry_run,
        "scanned": len(rows),
        "total_wav_mb": round(total_mb, 1),
        "ttl_days": ttl_days,
        "max_wav_mb": max_wav_mb,
        "planned": [v["piece_id"] for v in victims],
        "planned_mb": freed_mb,
        "deleted": deleted,
        "failed": failed,
        "protected_keep_midi_missing": protected_no_midi,
        "favorites": len(favs),
        "catalog": cat_res,
        "ts": int(now),
    }
    # dry-run 也记录日期，"每天最多一次"对两种模式都成立
    _write_state(out, result)
    return result


def _write_state(outdir: Path, result: dict) -> None:
    try:
        (outdir / STATE_NAME).write_text(
            json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    except OSError:
        pass


def _read_state(outdir: Path) -> dict:
    try:
        return json.loads((outdir / STATE_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def last_run_date(outdir: str | Path) -> str:
    """上次实际清理的日期（YYYY-MM-DD），没有则空串。"""
    ts = _read_state(Path(outdir)).get("ts")
    if not ts:
        return ""
    return time.strftime("%Y-%m-%d", time.localtime(ts))


def needs_run(outdir: str | Path, now: float | None = None) -> bool:
    """今天是否还没跑过。"""
    now = time.time() if now is None else now
    return last_run_date(outdir) != time.strftime("%Y-%m-%d", time.localtime(now))
