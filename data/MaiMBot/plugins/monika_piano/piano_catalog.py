"""作品目录（SQLite）。纯标准库，不引入新依赖。

为什么要有它：
  旧方案把索引堆在 pieces_index.jsonl 里，每条一行、只增不减。
  问题是 (a) 文件无限增长；(b) `last_piece()` 每次都要全量解析整份文件，
  作品多了变奏/再听会越来越慢。

设计：
  * pieces.db 成为唯一事实来源；pieces_index.jsonl 降级为"一次性导入源 + 只读归档"
  * 双阈值：总量上限（midi_max_mb，安全网）+ 条数上限（max_pieces，实际护栏）
  * 超限时按 created_at 最旧优先整首移除（mid + json + wav + 行）
  * 收藏作品永不删；min_keep_days 内的新作品永不删
  * 支持 dry_run，只出计划不动手
"""
from __future__ import annotations

import json
import logging
import sqlite3
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)

DB_NAME = "pieces.db"
LEGACY_INDEX = "pieces_index.jsonl"
IMPORT_MARK = ".catalog_imported"

SCHEMA = """
CREATE TABLE IF NOT EXISTS pieces (
    piece_id        TEXT PRIMARY KEY,
    seed            INTEGER,
    mood            TEXT,
    bpm             INTEGER,
    bars            INTEGER,
    variation       INTEGER,
    parent_piece_id TEXT,
    chat_id         TEXT,
    created_at      INTEGER,
    engine_version  TEXT,
    midi_path       TEXT,
    wav_path        TEXT,
    midi_bytes      INTEGER,
    meta_json       TEXT
);
CREATE INDEX IF NOT EXISTS idx_pieces_chat ON pieces(chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_pieces_created ON pieces(created_at ASC);
"""


def db_path(outdir: str | Path) -> Path:
    return Path(outdir) / DB_NAME


def _connect(outdir: str | Path) -> sqlite3.Connection:
    path = db_path(outdir)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=10)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    con.executescript(SCHEMA)
    return con


# ---------------------------------------------------------------- 写入

def add(outdir: str | Path, meta: dict) -> bool:
    """写入/更新一条作品记录。失败返回 False（调用方回退到 jsonl）。"""
    mid = Path(meta.get("midi_path") or "")
    try:
        midi_bytes = mid.stat().st_size if mid.is_file() else 0
    except OSError:
        midi_bytes = 0
    try:
        con = _connect(outdir)
        try:
            con.execute(
                "INSERT OR REPLACE INTO pieces (piece_id, seed, mood, bpm, bars, variation,"
                " parent_piece_id, chat_id, created_at, engine_version, midi_path, wav_path,"
                " midi_bytes, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (meta.get("piece_id"), meta.get("seed"), meta.get("mood"),
                 meta.get("bpm"), meta.get("bars"), meta.get("variation"),
                 meta.get("parent_piece_id"), meta.get("chat_id"),
                 int(meta.get("created_at") or time.time()),
                 meta.get("engine_version"), meta.get("midi_path"), meta.get("wav_path"),
                 midi_bytes, json.dumps(meta, ensure_ascii=False)))
            con.commit()
            return True
        finally:
            con.close()
    except sqlite3.Error as exc:
        LOGGER.warning("[PianoCatalog] 写入失败: %s", exc)
        return False


# ---------------------------------------------------------------- 迁移

def init(outdir: str | Path) -> int:
    """建表，并在需要时把 legacy jsonl 导入。返回本次导入条数。"""
    out = Path(outdir)
    jsonl = out / LEGACY_INDEX
    mark = out / IMPORT_MARK
    need = False
    if jsonl.exists():
        if not mark.exists():
            need = True
        else:
            try:
                need = jsonl.stat().st_mtime > mark.stat().st_mtime
            except OSError:
                need = True
    if not need:
        _connect(out).close()
        return 0

    rows: list[tuple] = []
    try:
        with open(jsonl, encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    m = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not m.get("piece_id"):
                    continue
                mid = Path(m.get("midi_path") or "")
                try:
                    mb = mid.stat().st_size if mid.is_file() else 0
                except OSError:
                    mb = 0
                rows.append((m["piece_id"], m.get("seed"), m.get("mood"), m.get("bpm"),
                             m.get("bars"), m.get("variation"), m.get("parent_piece_id"),
                             m.get("chat_id"), int(m.get("created_at") or time.time()),
                             m.get("engine_version"), m.get("midi_path"), m.get("wav_path"),
                             mb, json.dumps(m, ensure_ascii=False)))
    except OSError as exc:
        LOGGER.warning("[PianoCatalog] 读取 legacy 索引失败: %s", exc)
        return 0

    con = _connect(out)
    try:
        con.executemany(
            "INSERT OR IGNORE INTO pieces (piece_id, seed, mood, bpm, bars, variation,"
            " parent_piece_id, chat_id, created_at, engine_version, midi_path, wav_path,"
            " midi_bytes, meta_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        con.commit()
    finally:
        con.close()
    try:
        mark.write_text(str(int(time.time())), encoding="utf-8")
    except OSError:
        pass
    LOGGER.info("[PianoCatalog] 从 %s 导入 %d 条作品记录", LEGACY_INDEX, len(rows))
    return len(rows)


# ---------------------------------------------------------------- 查询

def last(outdir: str | Path, chat_id: str = "") -> dict | None:
    try:
        con = _connect(outdir)
        try:
            if chat_id:
                row = con.execute(
                    "SELECT meta_json FROM pieces WHERE chat_id=? "
                    "ORDER BY created_at DESC, rowid DESC LIMIT 1", (chat_id,)).fetchone()
            else:
                row = con.execute(
                    "SELECT meta_json FROM pieces ORDER BY created_at DESC, rowid DESC LIMIT 1"
                ).fetchone()
        finally:
            con.close()
        return json.loads(row["meta_json"]) if row else None
    except (sqlite3.Error, json.JSONDecodeError) as exc:
        LOGGER.warning("[PianoCatalog] 查询失败: %s", exc)
        return None


def stats(outdir: str | Path) -> dict:
    try:
        con = _connect(outdir)
        try:
            row = con.execute(
                "SELECT COUNT(*) AS n, COALESCE(SUM(midi_bytes),0) AS b FROM pieces").fetchone()
        finally:
            con.close()
        return {"pieces": row["n"], "midi_bytes": row["b"],
                "midi_mb": round(row["b"] / 1048576, 2)}
    except sqlite3.Error:
        return {"pieces": 0, "midi_bytes": 0, "midi_mb": 0.0}


def all_meta(outdir: str | Path) -> dict[str, dict]:
    """piece_id -> meta，供 storage 做文件级扫描。"""
    out: dict[str, dict] = {}
    try:
        con = _connect(outdir)
        try:
            for row in con.execute("SELECT piece_id, meta_json FROM pieces"):
                try:
                    out[row["piece_id"]] = json.loads(row["meta_json"])
                except json.JSONDecodeError:
                    continue
        finally:
            con.close()
    except sqlite3.Error:
        pass
    return out


# ---------------------------------------------------------------- 修剪

def prune(outdir: str | Path, *, max_mb: int = 1024, max_pieces: int = 5000,
          min_keep_days: int = 7, favorites: set[str] | None = None,
          dry_run: bool = True, now: float | None = None) -> dict:
    """按容量/条数双阈值，最旧优先整首移除。"""
    out = Path(outdir)
    favs = favorites or set()
    now = time.time() if now is None else now
    st = stats(out)
    over_size = st["midi_bytes"] > max_mb * 1048576
    over_count = st["pieces"] > max_pieces
    result = {"dry_run": dry_run, "before_pieces": st["pieces"],
              "before_mb": st["midi_mb"], "max_mb": max_mb, "max_pieces": max_pieces,
              "over_size": over_size, "over_count": over_count,
              "planned": [], "planned_mb": 0.0, "deleted": [], "failed": [],
              "protected_recent": 0, "protected_favorite": 0}
    if not (over_size or over_count):
        return result

    try:
        con = _connect(out)
        try:
            rows = con.execute(
                "SELECT piece_id, created_at, midi_bytes, midi_path, wav_path "
                "FROM pieces ORDER BY created_at ASC, rowid ASC").fetchall()
        finally:
            con.close()
    except sqlite3.Error as exc:
        result["failed"].append(f"查询失败: {exc}")
        return result

    cur_pieces, cur_bytes = st["pieces"], st["midi_bytes"]
    victims = []
    for r in rows:
        if cur_pieces <= max_pieces and cur_bytes <= max_mb * 1048576:
            break
        pid = r["piece_id"]
        if pid in favs:
            result["protected_favorite"] += 1
            continue
        if (now - int(r["created_at"] or 0)) < min_keep_days * 86400:
            result["protected_recent"] += 1
            continue
        victims.append(r)
        cur_pieces -= 1
        cur_bytes -= int(r["midi_bytes"] or 0)

    result["planned"] = [r["piece_id"] for r in victims]
    result["planned_mb"] = round(sum(int(r["midi_bytes"] or 0) for r in victims) / 1048576, 3)
    if dry_run or not victims:
        return result

    removed: list[str] = []
    for r in victims:
        pid = r["piece_id"]
        ok = True
        for p in (r["midi_path"], r["wav_path"],
                  str(Path(r["midi_path"] or "").with_suffix(".json"))):
            if not p:
                continue
            try:
                Path(p).unlink(missing_ok=True)
            except OSError as exc:
                ok = False
                result["failed"].append(f"{pid}: {exc}")
        if not ok:
            continue
        removed.append(pid)
    if removed:
        try:
            con = _connect(out)
            try:
                con.executemany("DELETE FROM pieces WHERE piece_id=?",
                                [(p,) for p in removed])
                con.commit()
            finally:
                con.close()
        except sqlite3.Error as exc:
            result["failed"].append(f"删除记录失败: {exc}")
    result["deleted"] = removed
    after = stats(out)
    result["after_pieces"] = after["pieces"]
    result["after_mb"] = after["midi_mb"]
    return result
