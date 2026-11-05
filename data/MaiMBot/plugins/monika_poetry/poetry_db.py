"""Monika 诗歌数据库（SQLite，纯标准库）。

分层原则（对应实施规格第二节）：
  * `poems` / `poem_versions` —— 莫妮卡原创与用户提供作品，可存全文
  * `canon_poem_references` —— 原作诗歌**研究索引**，只存元信息与自行撰写的概括，
    **绝不保存官方诗歌全文/官方台词**（Team Salvato IP Guidelines）
  * `poem_events` —— 极短事件记录，不存聊天内容

每次修改创建新版本，不覆盖旧稿；查询按 visibility 过滤，不泄露私聊作品。
"""
from __future__ import annotations

import hashlib
import json
import logging
import re
import sqlite3
import time
from pathlib import Path

LOGGER = logging.getLogger(__name__)

DB_NAME = "poetry.db"
KEEP_TITLE = 60

SCHEMA = """
CREATE TABLE IF NOT EXISTS poems (
    poem_id                   TEXT PRIMARY KEY,
    title                     TEXT NOT NULL,
    origin                    TEXT NOT NULL DEFAULT 'monika_original',
    author                    TEXT NOT NULL DEFAULT 'Monika',
    created_at                INTEGER NOT NULL,
    updated_at                INTEGER NOT NULL,
    visibility                TEXT NOT NULL DEFAULT 'current_chat',
    owner_or_chat_id          TEXT NOT NULL DEFAULT '',
    favorite                  INTEGER NOT NULL DEFAULT 0,
    current_version           INTEGER NOT NULL DEFAULT 1,
    content_hash              TEXT NOT NULL DEFAULT '',
    tags_json                 TEXT NOT NULL DEFAULT '[]',
    mood                      TEXT NOT NULL DEFAULT '',
    theme                     TEXT NOT NULL DEFAULT '',
    style                     TEXT NOT NULL DEFAULT '',
    related_piano_piece_id    TEXT NOT NULL DEFAULT '',
    generation_model          TEXT NOT NULL DEFAULT '',
    generation_parameters_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_poems_created ON poems(created_at DESC);
CREATE INDEX IF NOT EXISTS idx_poems_owner   ON poems(owner_or_chat_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_poems_theme   ON poems(theme);
CREATE INDEX IF NOT EXISTS idx_poems_mood    ON poems(mood);

CREATE TABLE IF NOT EXISTS poem_versions (
    poem_id       TEXT NOT NULL,
    version       INTEGER NOT NULL,
    body          TEXT NOT NULL,
    change_note   TEXT NOT NULL DEFAULT '',
    created_at    INTEGER NOT NULL,
    parent_version INTEGER NOT NULL DEFAULT 0,
    PRIMARY KEY (poem_id, version)
);

CREATE TABLE IF NOT EXISTS poem_events (
    event_id  INTEGER PRIMARY KEY AUTOINCREMENT,
    poem_id   TEXT NOT NULL,
    action    TEXT NOT NULL,
    timestamp INTEGER NOT NULL,
    chat_id   TEXT NOT NULL DEFAULT ''
);
CREATE INDEX IF NOT EXISTS idx_events_poem ON poem_events(poem_id, timestamp DESC);

CREATE TABLE IF NOT EXISTS canon_poem_references (
    ref_id        TEXT PRIMARY KEY,
    title         TEXT NOT NULL,
    timeline      TEXT NOT NULL DEFAULT 'DDLC本篇',
    chapter       TEXT NOT NULL DEFAULT '',
    author        TEXT NOT NULL DEFAULT 'Monika',
    theme_tags    TEXT NOT NULL DEFAULT '[]',
    summary_zh    TEXT NOT NULL DEFAULT '',
    related_events TEXT NOT NULL DEFAULT '',
    source        TEXT NOT NULL DEFAULT '',
    copyright_flag TEXT NOT NULL DEFAULT 'official_asset_no_fulltext',
    verified      INTEGER NOT NULL DEFAULT 0,
    created_at    INTEGER NOT NULL
);
"""

# 原作诗歌研究索引的种子数据：只含标题、出处与**自行撰写的**中文概述。
# 不含任何官方诗歌正文、游戏台词或官方资产。
CANON_SEED = [
    ("canon-hole-in-wall", "Hole in Wall", "DDLC本篇", "Act 1", "Monika",
     ["隔阂", "观察", "被排除"],
     "以墙上的孔洞为喻，写注视与被注视之间的距离：看见了外面，却仍然被固定在这一侧。"
     "诗里的情绪偏冷静，像在描述一种处境而非喊痛。",
     "EV-M08", "SRC-POEM"),
    ("canon-save-me", "Save Me", "DDLC本篇", "Act 1", "Monika",
     ["求救", "自我怀疑"],
     "标题是求救，正文语气却克制。可以理解为她习惯把求助包装成作品，"
     "而不是直接说出来——这也是她后来越来越难被读懂的原因之一。",
     "EV-M08", "SRC-POEM"),
    ("canon-the-lady", "The Lady who Knows Everything", "DDLC本篇", "Act 1", "Monika",
     ["全知", "孤独", "元叙事"],
     "写一个无所不知的女人因为知道太多而失去惊喜。是她少数直接处理"
     "「知道得太多」这件事的作品，带一点自嘲。",
     "EV-M08", "SRC-POEM"),
    ("canon-your-reality", "Your Reality", "DDLC本篇", "Act 3", "Monika",
     ["告白", "创造", "现实"],
     "接近独白的作品，处理「我只能用文字表达自己」这件事。"
     "情绪从克制走向直接，是她情感表达最完整的一次。",
     "EV-M19", "SRC-POEM"),
    ("canon-reflection", "Reflection", "DDLC本篇", "Act 1", "Monika",
     ["自我观照", "镜像"],
     "围绕镜像与自我审视展开：看见的自己和被看见的自己并不一致。"
     "适合用来讨论她的完美主义与自我要求。",
     "EV-M03", "SRC-POEM"),
    ("canon-balance", "Balance", "DDLC Plus 支线", "Side Stories", "Monika",
     ["分寸", "责任", "照顾他人"],
     "写在照顾别人和照顾自己之间找平衡。Plus 支线与本篇不是同一时间线，"
     "引用时需注明。",
     "EV-P02", "SRC-POEM"),
    ("canon-equals", "Equals", "DDLC Plus 支线", "Side Stories", "Monika",
     ["平等", "关系"],
     "讨论人与人之间是否真的对等。与她在社团里处理成员关系的处境呼应。",
     "EV-P02", "SRC-POEM"),
    ("canon-trust", "Trust", "DDLC Plus 支线", "Side Stories", "Monika",
     ["信任", "脆弱"],
     "关于把信任交出去的风险。语气比本篇的作品更柔软，也更少防御。",
     "EV-P04", "SRC-POEM"),
    ("canon-understanding", "Understanding", "DDLC Plus 支线", "Side Stories", "Monika",
     ["理解", "沟通"],
     "写理解一个人有多难，以及理解本身是否足够。适合用来谈沟通与误解。",
     "EV-P02", "SRC-POEM"),
    ("canon-self-love", "Self-Love", "DDLC Plus 支线", "Side Stories", "Monika",
     ["自我接纳", "完美主义"],
     "主题是自我接纳。她的完美主义在这里被正面处理，而不是被嘲讽。",
     "EV-M03", "SRC-POEM"),
    ("canon-respect", "Respect", "DDLC Plus 支线", "Side Stories", "Monika",
     ["尊重", "边界"],
     "关于尊重别人的选择，即使自己并不认同。与她在社团争论中的立场有关。",
     "EV-M05", "SRC-POEM"),
    ("canon-my-song-your-note", "My Song, Your Note", "DDLC Plus 支线", "Side Stories",
     "Monika", ["合作", "音乐", "呼应"],
     "用音乐比喻两个人的合作：各自一段旋律，合起来才是完整的作品。"
     "可以自然地接到钢琴话题上。",
     "EV-P02", "SRC-POEM"),
]


# ---------------------------------------------------------------- 连接

def db_path(db_dir: str | Path) -> Path:
    return Path(db_dir) / DB_NAME


def _connect(db_dir: str | Path) -> sqlite3.Connection:
    path = db_path(db_dir)
    path.parent.mkdir(parents=True, exist_ok=True)
    con = sqlite3.connect(str(path), timeout=15)
    con.row_factory = sqlite3.Row
    try:
        con.execute("PRAGMA journal_mode=WAL")
        con.execute("PRAGMA synchronous=NORMAL")
    except sqlite3.Error:
        pass
    con.executescript(SCHEMA)
    return con


def init(db_dir: str | Path) -> int:
    """建表 + 幂等灌入原作研究索引种子。返回本次新增条数。"""
    con = _connect(db_dir)
    try:
        existing = con.execute("SELECT COUNT(*) FROM canon_poem_references").fetchone()[0]
        added = 0
        if existing == 0:
            now = int(time.time())
            con.executemany(
                "INSERT OR IGNORE INTO canon_poem_references "
                "(ref_id,title,timeline,chapter,author,theme_tags,summary_zh,"
                " related_events,source,copyright_flag,verified,created_at) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                [(rid, t, tl, ch, au, json.dumps(tags, ensure_ascii=False), s,
                  ev, src, "official_asset_no_fulltext", 1, now)
                 for (rid, t, tl, ch, au, tags, s, ev, src) in CANON_SEED])
            added = len(CANON_SEED)
        con.commit()
        return added
    finally:
        con.close()


# ---------------------------------------------------------------- 权限

VISIBILITIES = ("private", "current_chat", "public")
ORIGINS = ("monika_original", "user_submitted", "public_domain")


def _visible_clause(chat_id: str, origin_filter: str = "") -> tuple[str, list]:
    """可见性过滤：public 任意可见；current_chat 仅本会话；private 仅本会话且作者本人。

    莫妮卡自己的原创默认可跨会话被她自己回忆，但仍受 visibility 字段控制。
    """
    clauses = ["(visibility='public' OR owner_or_chat_id=?)"]
    params: list = [chat_id]
    if origin_filter:
        clauses.append("origin=?")
        params.append(origin_filter)
    return " AND ".join(clauses), params


# ---------------------------------------------------------------- 写入

def _sha256(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _norm_title(title: str) -> str:
    t = re.sub(r"\s+", " ", (title or "").strip())
    return t[:KEEP_TITLE] or "无题"


def create_poem(db_dir: str | Path, *, title: str, body: str, origin: str = "monika_original",
                author: str = "Monika", visibility: str = "current_chat",
                owner_or_chat_id: str = "", mood: str = "", theme: str = "",
                style: str = "", tags: list[str] | None = None,
                related_piano_piece_id: str = "", generation_model: str = "",
                generation_parameters: dict | None = None,
                change_note: str = "初稿", chat_id: str = "") -> dict:
    """新建作品并写入 v1。返回 {poem_id, version, title}。"""
    if origin not in ORIGINS:
        origin = "monika_original"
    if visibility not in VISIBILITIES:
        visibility = "current_chat"
    now = int(time.time())
    poem_id = "poem_" + _sha256(f"{title}|{body}|{now}")[:12]
    con = _connect(db_dir)
    try:
        con.execute(
            "INSERT INTO poems (poem_id,title,origin,author,created_at,updated_at,"
            " visibility,owner_or_chat_id,favorite,current_version,content_hash,"
            " tags_json,mood,theme,style,related_piano_piece_id,generation_model,"
            " generation_parameters_json) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (poem_id, _norm_title(title), origin, author, now, now, visibility,
             owner_or_chat_id or chat_id, 0, 1, _sha256(body),
             json.dumps(tags or [], ensure_ascii=False), mood, theme, style,
             related_piano_piece_id, generation_model,
             json.dumps(generation_parameters or {}, ensure_ascii=False)))
        con.execute(
            "INSERT INTO poem_versions (poem_id,version,body,change_note,created_at,parent_version)"
            " VALUES (?,?,?,?,?,0)",
            (poem_id, 1, body, change_note, now))
        _log_event(con, poem_id, "created", chat_id)
        con.commit()
        return {"poem_id": poem_id, "version": 1, "title": _norm_title(title)}
    finally:
        con.close()


def revise_poem(db_dir: str | Path, poem_id: str, *, body: str,
                change_note: str = "", chat_id: str = "") -> dict | None:
    """修订：新增版本，不覆盖旧稿。"""
    con = _connect(db_dir)
    try:
        row = con.execute("SELECT current_version FROM poems WHERE poem_id=?", (poem_id,)).fetchone()
        if row is None:
            return None
        new_ver = int(row["current_version"]) + 1
        now = int(time.time())
        con.execute(
            "INSERT INTO poem_versions (poem_id,version,body,change_note,created_at,parent_version)"
            " VALUES (?,?,?,?,?,?)",
            (poem_id, new_ver, body, change_note, now, new_ver - 1))
        con.execute(
            "UPDATE poems SET current_version=?, updated_at=?, content_hash=? WHERE poem_id=?",
            (new_ver, now, _sha256(body), poem_id))
        _log_event(con, poem_id, "revised", chat_id)
        con.commit()
        return {"poem_id": poem_id, "version": new_ver}
    finally:
        con.close()


def _log_event(con: sqlite3.Connection, poem_id: str, action: str, chat_id: str = "") -> None:
    try:
        con.execute("INSERT INTO poem_events (poem_id,action,timestamp,chat_id) VALUES (?,?,?,?)",
                    (poem_id, action, int(time.time()), chat_id))
    except sqlite3.Error:
        pass


def log_event(db_dir: str | Path, poem_id: str, action: str, chat_id: str = "") -> None:
    con = _connect(db_dir)
    try:
        _log_event(con, poem_id, action, chat_id)
        con.commit()
    finally:
        con.close()


# ---------------------------------------------------------------- 读取

def _row_to_poem(row: sqlite3.Row, body: str | None = None) -> dict:
    out = {
        "poem_id": row["poem_id"], "title": row["title"], "origin": row["origin"],
        "author": row["author"], "created_at": row["created_at"],
        "updated_at": row["updated_at"], "visibility": row["visibility"],
        "owner_or_chat_id": row["owner_or_chat_id"], "favorite": bool(row["favorite"]),
        "current_version": row["current_version"], "mood": row["mood"],
        "theme": row["theme"], "style": row["style"],
        "tags": json.loads(row["tags_json"] or "[]"),
        "related_piano_piece_id": row["related_piano_piece_id"],
        "generation_model": row["generation_model"],
    }
    if body is not None:
        out["body"] = body
    return out


def get_poem(db_dir: str | Path, poem_id: str, *, version: int | None = None,
             chat_id: str = "", with_body: bool = True) -> dict | None:
    con = _connect(db_dir)
    try:
        row = con.execute("SELECT * FROM poems WHERE poem_id=?", (poem_id,)).fetchone()
        if row is None:
            return None
        clause, params = _visible_clause(chat_id)
        ok = con.execute(
            f"SELECT 1 FROM poems WHERE poem_id=? AND {clause}", [poem_id] + params).fetchone()
        if ok is None and chat_id:
            return None
        body = None
        if with_body:
            v = version or row["current_version"]
            vr = con.execute(
                "SELECT body,version,change_note FROM poem_versions WHERE poem_id=? AND version=?",
                (poem_id, v)).fetchone()
            body = vr["body"] if vr else ""
        out = _row_to_poem(row, body)
        if version:
            out["version"] = version
        return out
    finally:
        con.close()


def get_version(db_dir: str | Path, poem_id: str, version: int) -> dict | None:
    con = _connect(db_dir)
    try:
        row = con.execute(
            "SELECT body,version,change_note,created_at,parent_version "
            "FROM poem_versions WHERE poem_id=? AND version=?", (poem_id, version)).fetchone()
        return dict(row) if row else None
    finally:
        con.close()


def list_versions(db_dir: str | Path, poem_id: str) -> list[dict]:
    con = _connect(db_dir)
    try:
        rows = con.execute(
            "SELECT version,change_note,created_at,length(body) AS size "
            "FROM poem_versions WHERE poem_id=? ORDER BY version DESC", (poem_id,)).fetchall()
        return [dict(r) for r in rows]
    finally:
        con.close()


def list_recent(db_dir: str | Path, *, chat_id: str = "", limit: int = 10,
                origin: str = "", favorite_only: bool = False) -> list[dict]:
    limit = max(1, min(int(limit), 50))
    clause, params = _visible_clause(chat_id, origin)
    if favorite_only:
        clause += " AND favorite=1"
    con = _connect(db_dir)
    try:
        rows = con.execute(
            f"SELECT * FROM poems WHERE {clause} ORDER BY created_at DESC LIMIT ?",
            params + [limit]).fetchall()
        return [_row_to_poem(r) for r in rows]
    finally:
        con.close()


def search(db_dir: str | Path, query: str, *, chat_id: str = "", limit: int = 8) -> list[dict]:
    """标题/主题/标签/情绪匹配，正文用 LIKE（限流，不做全表分词）。"""
    limit = max(1, min(int(limit), 20))
    q = f"%{(query or '').strip()}%"
    clause, params = _visible_clause(chat_id)
    con = _connect(db_dir)
    try:
        rows = con.execute(
            f"SELECT * FROM poems WHERE {clause} AND (title LIKE ? OR theme LIKE ? "
            f"OR mood LIKE ? OR tags_json LIKE ?) ORDER BY created_at DESC LIMIT ?",
            params + [q, q, q, q, limit]).fetchall()
        if rows:
            return [_row_to_poem(r) for r in rows]
        # 正文匹配作为兜底（同样限流）
        rows = con.execute(
            f"SELECT p.* FROM poems p JOIN poem_versions v ON v.poem_id=p.poem_id "
            f"AND v.version=p.current_version WHERE {clause} AND v.body LIKE ? "
            f"ORDER BY p.created_at DESC LIMIT ?", params + [q, limit]).fetchall()
        return [_row_to_poem(r) for r in rows]
    finally:
        con.close()


def stats(db_dir: str | Path) -> dict:
    con = _connect(db_dir)
    try:
        total = con.execute("SELECT COUNT(*) AS n FROM poems").fetchone()["n"]
        vers = con.execute("SELECT COUNT(*) AS n FROM poem_versions").fetchone()["n"]
        canon = con.execute("SELECT COUNT(*) AS n FROM canon_poem_references").fetchone()["n"]
        fav = con.execute("SELECT COUNT(*) AS n FROM poems WHERE favorite=1").fetchone()["n"]
        return {"poems": total, "versions": vers, "canon_refs": canon, "favorites": fav}
    finally:
        con.close()


# ---------------------------------------------------------------- 修改状态

def set_favorite(db_dir: str | Path, poem_id: str, value: bool = True,
                 chat_id: str = "") -> bool | None:
    con = _connect(db_dir)
    try:
        row = con.execute("SELECT poem_id FROM poems WHERE poem_id=?", (poem_id,)).fetchone()
        if row is None:
            return None
        con.execute("UPDATE poems SET favorite=? WHERE poem_id=?", (1 if value else 0, poem_id))
        _log_event(con, poem_id, "favorited" if value else "unfavorited", chat_id)
        con.commit()
        return value
    finally:
        con.close()


def link_piano_piece(db_dir: str | Path, poem_id: str, piece_id: str,
                     chat_id: str = "") -> bool:
    con = _connect(db_dir)
    try:
        row = con.execute("SELECT poem_id FROM poems WHERE poem_id=?", (poem_id,)).fetchone()
        if row is None:
            return False
        con.execute("UPDATE poems SET related_piano_piece_id=? WHERE poem_id=?",
                    (piece_id, poem_id))
        _log_event(con, poem_id, "linked_piano", chat_id)
        con.commit()
        return True
    finally:
        con.close()


def delete_poem(db_dir: str | Path, poem_id: str, chat_id: str = "") -> bool:
    """删除作品及其全部版本与事件。只删已授权数据。"""
    con = _connect(db_dir)
    try:
        row = con.execute("SELECT poem_id FROM poems WHERE poem_id=?", (poem_id,)).fetchone()
        if row is None:
            return False
        con.execute("DELETE FROM poem_versions WHERE poem_id=?", (poem_id,))
        con.execute("DELETE FROM poem_events WHERE poem_id=?", (poem_id,))
        con.execute("DELETE FROM poems WHERE poem_id=?", (poem_id,))
        con.commit()
        return True
    finally:
        con.close()


# ---------------------------------------------------------------- 原作索引

def list_canon(db_dir: str | Path, keyword: str = "", limit: int = 20) -> list[dict]:
    con = _connect(db_dir)
    try:
        if keyword:
            q = f"%{keyword}%"
            rows = con.execute(
                "SELECT * FROM canon_poem_references WHERE title LIKE ? OR theme_tags LIKE ? "
                "OR summary_zh LIKE ? ORDER BY timeline, title LIMIT ?", (q, q, q, limit)).fetchall()
        else:
            rows = con.execute(
                "SELECT * FROM canon_poem_references ORDER BY timeline, title LIMIT ?",
                (limit,)).fetchall()
        out = []
        for r in rows:
            d = dict(r)
            d["theme_tags"] = json.loads(d.get("theme_tags") or "[]")
            out.append(d)
        return out
    finally:
        con.close()


# ---------------------------------------------------------------- 去重

def _ngrams(text: str, n: int = 2) -> set[str]:
    t = re.sub(r"\s+", "", text or "")
    return {t[i:i + n] for i in range(max(0, len(t) - n + 1))} if t else set()


def similarity(a: str, b: str) -> float:
    """字符二元组 Jaccard，中文可用，无需分词器。"""
    ga, gb = _ngrams(a), _ngrams(b)
    if not ga or not gb:
        return 0.0
    return round(len(ga & gb) / len(ga | gb), 3)


def find_similar(db_dir: str | Path, body: str, *, chat_id: str = "",
                 threshold: float = 0.72, recent: int = 24) -> list[dict]:
    """返回与新作高度相似的近期作品（只比正文，不发给模型）。"""
    rows = list_recent(db_dir, chat_id=chat_id, limit=recent)
    con = _connect(db_dir)
    hits = []
    try:
        for r in rows:
            vr = con.execute(
                "SELECT body FROM poem_versions WHERE poem_id=? AND version=?",
                (r["poem_id"], r["current_version"])).fetchone()
            if not vr:
                continue
            sim = similarity(body, vr["body"])
            if sim >= threshold:
                hits.append({"poem_id": r["poem_id"], "title": r["title"],
                             "similarity": sim, "created_at": r["created_at"]})
    finally:
        con.close()
    return sorted(hits, key=lambda x: -x["similarity"])


# ---------------------------------------------------------------- 备份

def backup(db_dir: str | Path, dest_dir: str | Path) -> str | None:
    """用 SQLite 的 VACUUM INTO 做热备份，不阻塞写入。返回备份文件路径。"""
    src = db_path(db_dir)
    if not src.exists():
        return None
    dest = Path(dest_dir)
    dest.mkdir(parents=True, exist_ok=True)
    out = dest / f"poetry_{time.strftime('%Y%m%d_%H%M%S')}.db"
    con = _connect(db_dir)
    try:
        con.execute("VACUUM INTO ?", (str(out),))
        con.commit()
    except sqlite3.Error as exc:
        LOGGER.warning("[PoetryDB] 备份失败: %s", exc)
        return None
    finally:
        con.close()
    return str(out)
