"""Monika 在线曲谱库（P9）：Mutopia 合法 MIDI 的搜索/导入/校验/渲染。

设计边界（对应 Monika_Online_MIDI 规格）：
  * 只自动抓取 Mutopia Project（HTTPS + host 白名单 + 路径规则，重定向也复检）
  * 外部曲谱 ext_ 前缀，与原创 piece_ 严格分开；绝不冒充莫妮卡原创
  * 每首记录作品/作曲家/来源页/许可/归因；无许可文本的页面直接拒绝
  * MIDI 二进制消毒（去 SysEx/文本元事件）后入库；MIDI/WAV 字节绝不进 LLM
  * 渲染复用 piano_engine.resolve_runtime（vendored fluidsynth 优先），缓存键含
    midi_sha256 + sf2_sha256 + 渲染器版本 + 采样率，音色/渲染器变更自动失效
  * pieces 表与旧清理逻辑不动；external_midis 永不自动删，external_wavs 独立 TTL
"""
from __future__ import annotations

import hashlib
import json
import re
import shutil
import sqlite3
import subprocess
import tempfile
from datetime import datetime, timezone
from html.parser import HTMLParser
from pathlib import Path
from urllib.parse import parse_qs, urljoin, urlparse
from urllib.request import Request, build_opener, HTTPRedirectHandler

import mido

MAX_MIDI_BYTES = 2 * 1024 * 1024
MAX_HTML_BYTES = 240 * 1024
MAX_DURATION = 360.0
MAX_TRACKS = 48
MAX_MESSAGES = 180_000
PROVIDER_HOSTS = {"www.mutopiaproject.org", "mutopiaproject.org"}
MAX_SEARCH_RESULTS = 5

SCHEMA = """
CREATE TABLE IF NOT EXISTS external_scores (
    score_id TEXT PRIMARY KEY,
    provider TEXT NOT NULL,
    provider_item_id TEXT NOT NULL,
    title TEXT NOT NULL,
    composer TEXT NOT NULL DEFAULT '',
    source_page_url TEXT NOT NULL,
    midi_url TEXT NOT NULL,
    license_name TEXT NOT NULL,
    license_url TEXT NOT NULL DEFAULT '',
    rights_status TEXT NOT NULL CHECK (rights_status IN ('approved','needs_review')),
    attribution TEXT NOT NULL DEFAULT '',
    raw_sha256 TEXT NOT NULL UNIQUE,
    midi_sha256 TEXT NOT NULL,
    midi_path TEXT NOT NULL,
    duration_seconds REAL NOT NULL,
    track_count INTEGER NOT NULL,
    note_count INTEGER NOT NULL,
    solo_piano INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_played_at TEXT,
    favorite INTEGER NOT NULL DEFAULT 0,
    metadata_json TEXT NOT NULL DEFAULT '{}'
);
CREATE INDEX IF NOT EXISTS idx_external_scores_title ON external_scores(title);
CREATE INDEX IF NOT EXISTS idx_external_scores_composer ON external_scores(composer);
CREATE TABLE IF NOT EXISTS external_render_cache (
    render_key TEXT PRIMARY KEY,
    score_id TEXT NOT NULL REFERENCES external_scores(score_id) ON DELETE CASCADE,
    wav_path TEXT NOT NULL,
    sf2_sha256 TEXT NOT NULL,
    renderer_version TEXT NOT NULL,
    sample_rate INTEGER NOT NULL,
    created_at TEXT NOT NULL,
    last_accessed_at TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_external_render_score ON external_render_cache(score_id);
"""


def now() -> str:
    return datetime.now(timezone.utc).isoformat()


def sha256_data(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as f:
        for chunk in iter(lambda: f.read(512 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


# ------------------------------------------------------------------ URL 安全

class StrictRedirect(HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        check_mutopia_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def check_mutopia_url(url: str, *, purpose: str = "any") -> str:
    p = urlparse(url)
    if p.scheme != "https" or (p.hostname or "").lower() not in PROVIDER_HOSTS:
        raise ValueError("只允许 HTTPS 的 Mutopia Project 地址")
    if p.username or p.password or p.port is not None:
        raise ValueError("URL 不得带凭据或端口")
    if purpose == "page":
        if p.path != "/cgibin/piece-info.cgi" or not parse_qs(p.query).get("id"):
            raise ValueError("需要 Mutopia 单曲页 piece-info.cgi?id=... 地址")
    if purpose == "midi" and (not p.path.startswith("/ftp/")
                              or not p.path.lower().endswith((".mid", ".midi"))):
        raise ValueError("MIDI 必须是 Mutopia /ftp/*.mid 地址")
    return url


def bounded_fetch(url: str, max_bytes: int) -> bytes:
    check_mutopia_url(url)
    req = Request(url, headers={"User-Agent": "MonikaMidiCatalog/0.2 (bounded fetch)"})
    opener = build_opener(StrictRedirect())
    with opener.open(req, timeout=20) as resp:
        check_mutopia_url(resp.geturl())
        length = resp.headers.get("Content-Length", "")
        if length.isdigit() and int(length) > max_bytes:
            raise ValueError("远端资源超过大小限制")
        data = resp.read(max_bytes + 1)
    if len(data) > max_bytes:
        raise ValueError("远端资源超过大小限制")
    return data


# ------------------------------------------------------------------ 页面解析

class _PageParser(HTMLParser):
    """通用：收集链接、标题行、表格单元格文本。"""

    def __init__(self):
        super().__init__()
        self.links: list[tuple[str, str]] = []
        self.headings: list[tuple[str, str]] = []
        self.cells: list[str] = []
        self.parts: list[str] = []
        self._href = None
        self._a_parts: list[str] = []
        self._heading = None
        self._head_parts: list[str] = []
        self._cell = None

    def handle_starttag(self, tag, attrs):
        d = dict(attrs)
        if tag == "a":
            self._href = d.get("href")
            self._a_parts = []
        if tag in ("h1", "h2", "h3", "h4"):
            self._heading = tag
            self._head_parts = []
        if tag == "td":
            self._cell = []

    def handle_data(self, data):
        self.parts.append(data)
        if self._href is not None:
            self._a_parts.append(data)
        if self._heading:
            self._head_parts.append(data)
        if self._cell is not None:
            self._cell.append(data)

    def handle_endtag(self, tag):
        if tag == "a" and self._href is not None:
            self.links.append((" ".join(self._a_parts).strip(), self._href))
            self._href = None
            self._a_parts = []
        if self._heading == tag:
            self.headings.append((tag, " ".join(self._head_parts).strip()))
            self._heading = None
        if tag == "td" and self._cell is not None:
            self.cells.append(" ".join("".join(self._cell).split()))
            self._cell = None


def parse_search_results(page_url: str, html_bytes: bytes, limit: int = MAX_SEARCH_RESULTS) -> list[dict]:
    """make-table.cgi 结果页 → [{item_id, title, composer, page_url}]，最多 limit 条。"""
    page = _PageParser()
    page.feed(html_bytes.decode("utf-8", errors="replace"))
    results: list[dict] = []
    seen: set[str] = set()
    cells = page.cells
    for label, href in page.links:
        m = re.search(r"piece-info\.cgi\?id=(\d+)", href or "")
        if not m or m.group(1) in seen:
            continue
        item_id = m.group(1)
        seen.add(item_id)
        # 链接文本通常是曲名；作曲家取其后第一个 "by ..." 单元格
        title = label.strip() or next((c for c in cells if c and not c.lower().startswith("by ")), "")
        composer = ""
        for c in cells:
            if c.lower().startswith("by "):
                composer = re.sub(r"\s*\([^)]*\)\s*$", "", c[3:]).strip()
                break
        results.append({
            "item_id": item_id,
            "title": title[:120],
            "composer": composer[:80],
            "page_url": f"https://www.mutopiaproject.org/cgibin/piece-info.cgi?id={item_id}",
        })
        if len(results) >= limit:
            break
    return results


def search_mutopia(query: str, *, composer: str = "", instrument: str = "Piano",
                   solo: bool = True, timeout_note: str = "") -> list[dict]:
    """受控搜索：只打 make-table.cgi，返回候选（不下载）。"""
    from urllib.parse import urlencode
    params = {"searchingfor": query or "", "Composer": composer or "",
              "Instrument": instrument or "", "Style": ""}
    if solo:
        params["solo"] = "1"
    url = "https://www.mutopiaproject.org/cgibin/make-table.cgi?" + urlencode(params)
    data = bounded_fetch(url, MAX_HTML_BYTES)
    return parse_search_results(url, data)


def parse_mutopia_page(page_url: str, html_bytes: bytes) -> dict:
    page = _PageParser()
    page.feed(html_bytes.decode("utf-8", errors="replace"))
    text = " ".join(" ".join(page.parts).split())
    match = re.search(
        r"Copyright:\s*(Public Domain|Creative Commons Attribution(?:-ShareAlike)?\s*\d+(?:\.\d+)?)",
        text, re.I)
    if not match:
        raise ValueError("无法从曲谱页确认许可，拒绝导入")
    license_name = match.group(1)
    midi_urls = [urljoin(page_url, href) for label, href in page.links
                 if label.strip().lower() == "midi file"]
    if len(midi_urls) != 1:
        raise ValueError("曲谱页应有且仅有一个 MIDI 下载链接")
    midi_url = check_mutopia_url(midi_urls[0], purpose="midi")
    title = next((s for tag, s in page.headings if tag in ("h1", "h2")), "")
    composer_text = next((s for tag, s in page.headings
                          if tag in ("h3", "h4") and "by " in s.lower()), "")
    composer = re.sub(r"^\s*by\s+", "", composer_text, flags=re.I)
    composer = re.sub(r"\s*\([^)]*\)\s*$", "", composer).strip()
    if not title or not composer:
        raise ValueError("页面缺曲名/作曲家，需人工核对")
    instrument_match = re.search(
        r"Instrument\(s\):\s*(.*?)\s*(?:\||Style:|$)", text, re.I)
    instruments = instrument_match.group(1).strip() if instrument_match else "unknown"
    source_id = parse_qs(urlparse(page_url).query)["id"][0]
    license_href = next((urljoin(page_url, href) for label, href in page.links
                         if label.strip().lower() == license_name.lower()), "")
    return {
        "title": title, "composer": composer, "license_name": license_name,
        "license_url": license_href, "midi_url": midi_url,
        "provider_item_id": source_id, "instruments": instruments,
        "attribution": f"{title} — {composer}; edition from Mutopia Project",
    }


# ------------------------------------------------------------------ MIDI 校验+消毒

def analyze_and_sanitize(raw: bytes) -> tuple[bytes, dict]:
    if len(raw) > MAX_MIDI_BYTES or not raw.startswith(b"MThd"):
        raise ValueError("不是合法的标准 MIDI 文件")
    tmp_in = tempfile.NamedTemporaryFile(suffix=".mid", delete=False)
    try:
        tmp_in.write(raw)
        tmp_in.close()
        midi = mido.MidiFile(tmp_in.name, clip=False)
    finally:
        Path(tmp_in.name).unlink(missing_ok=True)
    if midi.type not in (0, 1) or not 0 < len(midi.tracks) <= MAX_TRACKS:
        raise ValueError("仅支持 SMF type 0/1 且音轨数有上限")
    count = sum(len(track) for track in midi.tracks)
    if count > MAX_MESSAGES:
        raise ValueError("MIDI 事件数超限")
    duration = midi.length
    if not 0 < duration <= MAX_DURATION:
        raise ValueError(f"MIDI 时长 {duration:.1f}s 超出 0–{MAX_DURATION:g}s")
    note_count = sum(1 for t in midi.tracks for m in t
                     if m.type == "note_on" and m.velocity > 0)
    if not 0 < note_count < 150_000:
        raise ValueError("MIDI 无音符或音符过多")
    programs = {m.program for t in midi.tracks for m in t
                if m.type == "program_change" and m.channel != 9}
    drums = any(m.type == "note_on" and m.channel == 9 and m.velocity > 0
                for t in midi.tracks for m in t)
    solo_piano = (not drums) and all(program in range(8) for program in programs)
    # 消毒：去 SysEx 与文本类 meta（歌词/标记/版权文本都是低信任输入），保留全部时值
    safe = mido.MidiFile(type=midi.type, ticks_per_beat=midi.ticks_per_beat)
    banned = {"sysex", "lyrics", "text", "marker", "cue_marker", "device_name",
              "track_name", "instrument_name", "copyright"}
    for t in midi.tracks:
        out = mido.MidiTrack()
        delay = 0
        for msg in t:
            delay += msg.time
            if msg.type in banned:
                continue
            out.append(msg.copy(time=delay))
            delay = 0
        safe.tracks.append(out)
    tmp_out = tempfile.NamedTemporaryFile(suffix=".mid", delete=False)
    try:
        tmp_out.close()
        safe.save(tmp_out.name)
        sanitized = Path(tmp_out.name).read_bytes()
    finally:
        Path(tmp_out.name).unlink(missing_ok=True)
    return sanitized, {
        "duration_seconds": round(duration, 3), "track_count": len(midi.tracks),
        "note_count": note_count, "solo_piano": bool(solo_piano),
        "instrument_programs": sorted(programs), "has_drums": drums,
        "file_type": midi.type,
    }


# ------------------------------------------------------------------ 数据库

def connect(db: Path) -> sqlite3.Connection:
    db.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(db), timeout=15)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA foreign_keys=ON")
    conn.execute("PRAGMA journal_mode=WAL")
    conn.executescript(SCHEMA)
    return conn


def import_mutopia(db: Path, root: Path, page_url: str) -> dict:
    """下载→校验→消毒→入库。重复导入按 raw_sha256 幂等。"""
    check_mutopia_url(page_url, purpose="page")
    info = parse_mutopia_page(page_url, bounded_fetch(page_url, MAX_HTML_BYTES))
    raw = bounded_fetch(info["midi_url"], MAX_MIDI_BYTES)
    sanitized, analysis = analyze_and_sanitize(raw)
    analysis["source_instruments"] = info["instruments"]
    rawsha = sha256_data(raw)
    midisha = sha256_data(sanitized)
    score_id = "ext_" + rawsha[:20]
    midi_dir = root / "external_midis"
    midi_dir.mkdir(parents=True, exist_ok=True)
    midi_path = midi_dir / f"{score_id}.mid"
    if midi_path.exists() and sha256_file(midi_path) != midisha:
        raise RuntimeError("MIDI 文件名冲突或损坏")
    if not midi_path.exists():
        tmp_path = midi_path.with_suffix(".mid.part")
        tmp_path.write_bytes(sanitized)
        tmp_path.replace(midi_path)
    conn = connect(db)
    try:
        with conn:
            conn.execute(
                """INSERT OR IGNORE INTO external_scores
                (score_id,provider,provider_item_id,title,composer,source_page_url,midi_url,
                 license_name,license_url,rights_status,attribution,raw_sha256,midi_sha256,
                 midi_path,duration_seconds,track_count,note_count,solo_piano,created_at,metadata_json)
                 VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
                (score_id, "mutopia", info["provider_item_id"], info["title"], info["composer"],
                 page_url, info["midi_url"], info["license_name"], info["license_url"],
                 "approved" if analysis["solo_piano"] and info["instruments"].lower() == "piano"
                 else "needs_review",
                 info["attribution"], rawsha, midisha, str(midi_path),
                 analysis["duration_seconds"], analysis["track_count"], analysis["note_count"],
                 int(analysis["solo_piano"]), now(), json.dumps(analysis, ensure_ascii=False)))
            row = conn.execute("SELECT * FROM external_scores WHERE raw_sha256=?",
                               (rawsha,)).fetchone()
        return dict(row)
    finally:
        conn.close()


def list_scores(db: Path, q: str = "", limit: int = 10) -> list[dict]:
    conn = connect(db)
    try:
        if q:
            pattern = "%" + q.replace("%", "\\%").replace("_", "\\_") + "%"
            rows = conn.execute(
                """SELECT score_id,title,composer,license_name,rights_status,
                          duration_seconds,solo_piano,favorite,last_played_at
                   FROM external_scores WHERE title LIKE ? ESCAPE '\\'
                   OR composer LIKE ? ESCAPE '\\' OR score_id = ?
                   ORDER BY created_at DESC LIMIT ?""",
                (pattern, pattern, q, max(1, min(limit, 30)))).fetchall()
        else:
            rows = conn.execute(
                """SELECT score_id,title,composer,license_name,rights_status,
                          duration_seconds,solo_piano,favorite,last_played_at
                   FROM external_scores ORDER BY created_at DESC LIMIT ?""",
                (max(1, min(limit, 30)),)).fetchall()
        return [dict(row) for row in rows]
    finally:
        conn.close()


def get_score(db: Path, score_id: str) -> dict | None:
    conn = connect(db)
    try:
        row = conn.execute("SELECT * FROM external_scores WHERE score_id=?",
                           (score_id,)).fetchone()
        return dict(row) if row else None
    finally:
        conn.close()


# ------------------------------------------------------------------ 渲染

def render(db: Path, root: Path, score_id: str, *, runtime: dict | None = None,
           sample_rate: int = 44100) -> dict:
    """按需渲染 WAV。runtime 来自 piano_engine.resolve_runtime()。"""
    if sample_rate not in (22050, 32000, 44100, 48000):
        raise ValueError("不支持的采样率")
    if runtime is None:
        from . import piano_engine
        runtime = piano_engine.resolve_runtime("")
    if not runtime.get("fluidsynth_ok"):
        raise RuntimeError("fluidsynth 不可用")
    cmd = list(runtime["cmd"])
    env = runtime.get("env")
    sf2 = Path(runtime["soundfont"])
    if not sf2.is_file():
        raise ValueError("SoundFont 缺失")

    version = "unknown"
    try:
        version = subprocess.run(cmd + ["--version"], capture_output=True, text=True,
                                 timeout=10).stdout.strip()[:120] or "unknown"
    except Exception:  # noqa: BLE001
        pass

    conn = connect(db)
    try:
        row = conn.execute("SELECT * FROM external_scores WHERE score_id=?",
                           (score_id,)).fetchone()
        if row is None:
            raise ValueError("曲谱不存在")
        if row["rights_status"] != "approved" or not row["solo_piano"]:
            raise ValueError("该曲谱需人工审核后才能演奏")
        mid = Path(row["midi_path"])
        if not mid.is_file() or sha256_file(mid) != row["midi_sha256"]:
            raise RuntimeError("MIDI 文件缺失或损坏")
        sf2sha = sha256_file(sf2)
        key = sha256_data("|".join([row["midi_sha256"], sf2sha, version,
                                    str(sample_rate), "v1"]).encode())[:24]
        wav_dir = root / "external_wavs"
        wav_dir.mkdir(parents=True, exist_ok=True)
        wav = wav_dir / f"{score_id}-{key}.wav"
        cache = conn.execute("SELECT wav_path FROM external_render_cache WHERE render_key=?",
                             (key,)).fetchone()
        cached = cache is not None and wav.exists() and wav.stat().st_size > 44
        if not cached:
            with tempfile.NamedTemporaryFile(prefix="render-", suffix=".wav",
                                             dir=wav_dir, delete=False) as f:
                temp = Path(f.name)
            try:
                p = subprocess.run(cmd + ["-ni", "-F", str(temp), "-r", str(sample_rate),
                                          str(sf2), str(mid)],
                                   capture_output=True, text=True, timeout=120, env=env)
                if p.returncode or not temp.exists() or temp.stat().st_size <= 44:
                    raise RuntimeError(f"fluidsynth 渲染失败: {p.stderr[-400:]}")
                temp.replace(wav)
                try:
                    wav.chmod(0o644)   # tempfile 是 0600，NapCat 容器要能读
                except OSError:
                    pass
            finally:
                temp.unlink(missing_ok=True)
        with conn:
            conn.execute(
                """INSERT INTO external_render_cache
                (render_key,score_id,wav_path,sf2_sha256,renderer_version,sample_rate,
                 created_at,last_accessed_at) VALUES(?,?,?,?,?,?,?,?)
                 ON CONFLICT(render_key) DO UPDATE SET last_accessed_at=excluded.last_accessed_at""",
                (key, score_id, str(wav), sf2sha, version, sample_rate, now(), now()))
            conn.execute("UPDATE external_scores SET last_played_at=? WHERE score_id=?",
                         (now(), score_id))
        return {"score_id": score_id, "wav_path": str(wav), "cached": cached,
                "render_key": key, "attribution": row["attribution"],
                "license": row["license_name"], "title": row["title"],
                "composer": row["composer"], "source_page_url": row["source_page_url"]}
    finally:
        conn.close()


# ------------------------------------------------------------------ 清理兼容

def cleanup_external_wavs(root: Path, *, ttl_days: int = 30,
                          now_ts: float | None = None) -> dict:
    """external_wavs 独立 TTL；external_midis 与 external_scores 行永不自动删。"""
    import time as _time
    now_ts = _time.time() if now_ts is None else now_ts
    wav_dir = root / "external_wavs"
    deleted: list[str] = []
    if wav_dir.is_dir():
        for wav in wav_dir.glob("*.wav"):
            age_days = (now_ts - wav.stat().st_mtime) / 86400
            if age_days > ttl_days:
                try:
                    wav.unlink()
                    deleted.append(wav.name)
                except OSError:
                    pass
    return {"deleted": deleted, "ttl_days": ttl_days,
            "midis_kept": True}
