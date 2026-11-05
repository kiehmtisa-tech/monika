"""
OpenAI 兼容预算网关 (qq-fish budget gateway)

设计要点：
- Fail-Closed：配置缺失/模型未知/预算未配置一律拒绝，绝不绕过上游直连
- 事务性费用预留/结算/释放：SQLite BEGIN IMMEDIATE，杜绝并发超支
- BUDGET_EXCEEDED 使用 HTTP 402 + code 字段，与普通 API 错误严格区分
- 真实 API Key 只存在于 /secrets/gateway_secrets.json；本服务仅监听 Docker 内网
- 真实 API Key 只存在于 /secrets/gateway_secrets.json；本服务仅监听 Docker 内网
- 预算按 Asia/Shanghai 跨日；预算修改即时生效且不清零已用（used 永远从台账实时汇总）
"""

from __future__ import annotations

import asyncio
import hmac
import json
import logging
import math
import os
import re
import sqlite3
import time
import tomllib
import uuid
from contextlib import contextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, Iterator, Optional, Tuple
from zoneinfo import ZoneInfo

import httpx
from fastapi import FastAPI, Request
from fastapi.responses import JSONResponse, Response, StreamingResponse

# ---------------------------------------------------------------------------
# 路径与常量
# ---------------------------------------------------------------------------

BJ = ZoneInfo("Asia/Shanghai")

MODEL_CONFIG_PATH = Path(os.environ.get("GW_MODEL_CONFIG", "/config/model_config.toml"))
SECRETS_PATH = Path(os.environ.get("GW_SECRETS", "/secrets/gateway_secrets.json"))
BUDGET_PATH = Path(os.environ.get("GW_BUDGET", "/budget/budget.json"))
BEHAVIOR_PATH = Path(os.environ.get("GW_BEHAVIOR", "/budget/behavior.json"))
DB_PATH = Path(os.environ.get("GW_DB", "/budget/gateway.db"))
STATUS_PATH = Path(os.environ.get("GW_STATUS", "/budget/gateway_status.json"))
SETTINGS_PATH = Path(os.environ.get("GW_SETTINGS", "/app/settings.json"))

LISTEN_PORT = int(os.environ.get("GW_PORT", "8009"))
ALLOWED_ENDPOINTS = {"chat/completions", "embeddings", "responses"}
IMAGE_TOKEN_EST = 2000          # 每张图片的保守 token 估算
DEFAULT_MAX_TOKENS = 4096
RESERVATION_TTL = 180           # 秒；超过即视为失联并释放
DEFAULT_RESERVE_RATIO = 0.25    # 低优先级任务最多只能花掉 budget×(1-ratio)，其余留给高优先级
STATUS_INTERVAL = 5.0           # 状态文件刷新间隔
SWEEP_INTERVAL = 30.0
UPSTREAM_TOTAL_DEADLINE = float(os.environ.get("GW_UPSTREAM_DEADLINE", "300"))  # 单请求上游总时长硬上限
UPSTREAM_READ_TIMEOUT = float(os.environ.get("GW_UPSTREAM_READ", "180"))  # 流式分块间最大静默

logger = logging.getLogger("budget-gw")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(name)s %(message)s")


def now_dt() -> datetime:
    """当前北京时间。测试可 monkeypatch。"""
    return datetime.now(BJ)


def today_str() -> str:
    return now_dt().date().isoformat()


# ---------------------------------------------------------------------------
# 配置加载（mtime 热加载，任何一项失败 => fail-closed）
# ---------------------------------------------------------------------------


class GatewayConfigError(Exception):
    pass


class Config:
    def __init__(self) -> None:
        self._mtimes: Dict[str, float] = {}
        self.secrets: Dict[str, Any] = {}
        self.settings: Dict[str, Any] = {}
        self.budget: Dict[str, Any] = {}
        self.models: Dict[str, Dict[str, Any]] = {}      # model_identifier -> info
        self.providers: Dict[str, Dict[str, Any]] = {}  # provider name -> {base_url}
        self.reload(force=True)

    @staticmethod
    def _mtime(path: Path) -> float:
        try:
            return path.stat().st_mtime
        except OSError:
            return -1.0

    def reload_if_changed(self) -> None:
        sig = {
            "secrets": self._mtime(SECRETS_PATH),
            "settings": self._mtime(SETTINGS_PATH),
            "budget": self._mtime(BUDGET_PATH),
            "behavior": self._mtime(BEHAVIOR_PATH),
            "model": self._mtime(MODEL_CONFIG_PATH),
        }
        if sig != self._mtimes:
            try:
                self.reload(force=True, sig=sig)
            except Exception as exc:  # noqa: BLE001
                # 加载失败时保留上一份可用配置并告警；首次加载失败会直接抛出（fail-closed）
                logger.error("配置热加载失败，沿用上一份配置: %s", exc)

    def reload(self, force: bool = False, sig: Optional[Dict[str, float]] = None) -> None:
        if sig is None:
            sig = {
                "secrets": self._mtime(SECRETS_PATH),
                "settings": self._mtime(SETTINGS_PATH),
                "budget": self._mtime(BUDGET_PATH),
                "behavior": self._mtime(BEHAVIOR_PATH),
                "model": self._mtime(MODEL_CONFIG_PATH),
            }
        # 预算文件是运行时可变的，允许缺失（缺失=全部 fail-closed）
        for key in ("secrets", "model"):
            if sig[key] < 0:
                raise GatewayConfigError(f"必需配置文件缺失: {key}")

        secrets = json.loads(SECRETS_PATH.read_text("utf-8"))
        if not secrets.get("gateway_keys"):
            raise GatewayConfigError("gateway_keys 为空，拒绝启动")
        if not secrets.get("provider_keys"):
            raise GatewayConfigError("provider_keys 为空，拒绝启动")

        settings: Dict[str, Any] = {}
        if sig["settings"] >= 0:
            settings = json.loads(SETTINGS_PATH.read_text("utf-8"))

        budget: Dict[str, Any] = {}
        if sig["budget"] >= 0:
            budget = json.loads(BUDGET_PATH.read_text("utf-8"))

        # 行为配置（Dragon Board 可写）：仅读取高优先级预留比例，缺失/非法时用默认值
        behavior: Dict[str, Any] = {}
        if sig.get("behavior", -1) >= 0:
            try:
                behavior = json.loads(BEHAVIOR_PATH.read_text("utf-8"))
            except Exception as exc:  # noqa: BLE001
                logger.warning("behavior.json 读取失败，沿用默认预留比例: %s", exc)
                behavior = {}

        with MODEL_CONFIG_PATH.open("rb") as fh:
            mc = tomllib.load(fh)

        models: Dict[str, Dict[str, Any]] = {}
        for m in mc.get("models", []):
            if m.get("model_type") != "llm" and m.get("model_type") != "embedding":
                continue
            ident = (m.get("model_identifier") or "").strip()
            if not ident:
                continue
            models[ident] = {
                "provider": (m.get("api_provider") or "").strip(),
                "task_name": (m.get("name") or "").strip(),
                "price_in": float(m.get("price_in") or 0.0),
                "price_out": float(m.get("price_out") or 0.0),
                "cache_price_in": float(m.get("cache_price_in") or 0.0),
                "price_periods": m.get("price_periods") or [],
                "model_type": m.get("model_type"),
            }

        providers: Dict[str, Dict[str, Any]] = {}
        for p in mc.get("api_providers", []):
            providers[(p.get("name") or "").strip()] = {
                "base_url": (p.get("base_url") or "").strip(),
            }

        self.secrets = secrets
        self.settings = settings
        self.budget = budget
        self.behavior = behavior
        self.models = models
        self.providers = providers
        self._mtimes = sig
        logger.info(
            "配置已加载: models=%d providers=%d budget_keys=%s reserve_ratio=%.2f",
            len(models), len(providers), list((budget.get("daily") or {}).keys()),
            self.reserve_ratio(),
        )

    # -- 便捷查询 --------------------------------------------------------

    def gateway_keys(self) -> list:
        return list((self.secrets.get("gateway_keys") or {}).keys())

    def admin_key(self) -> str:
        return str(self.secrets.get("admin_key") or "")

    def provider_key(self, provider: str) -> str:
        # 只认 secrets，绝不读取 model_config 里的 api_key（隔离真实密钥）
        keys = self.secrets.get("provider_keys") or {}
        k = str(keys.get(provider) or "")
        if not k and "-GW" in provider:
            # 灰度专用 *-GW[-tier] provider：MaiBot 侧指向网关，
            # 网关侧回落到 -GW 之前的原厂商名取真实密钥（如 DeepSeek-GW-low -> DeepSeek）
            k = str(keys.get(provider.split("-GW", 1)[0]) or "")
        return k

    def upstream_provider(self, provider: str) -> str:
        # *-GW provider 的 base_url 指向网关自身，上游必须回落到原厂商地址（防自环）
        if "-GW" in provider:
            return provider.split("-GW", 1)[0]
        return provider

    def reserve_ratio(self) -> float:
        """低优先级任务的预算隔离比例（高优先级保留份额），来自 behavior.json。"""
        try:
            r = float((self.behavior or {}).get("mention_reserve_ratio", DEFAULT_RESERVE_RATIO))
        except (TypeError, ValueError):
            r = DEFAULT_RESERVE_RATIO
        return min(max(r, 0.0), 0.9)

    def upstream_url(self, model: str, endpoint: str) -> str:
        # 测试用覆盖（仅 settings.json 显式声明时生效）
        overrides = self.settings.get("upstream_overrides") or {}
        if model in overrides:
            return str(overrides[model]).rstrip("/") + "/" + endpoint
        mcfg = self.models[model]
        prov = self.upstream_provider(mcfg["provider"])
        base = self.providers[prov]["base_url"]
        return base.rstrip("/") + "/" + endpoint

    def budget_for(self, model: str) -> Optional[float]:
        daily = self.budget.get("daily") or {}
        if model in daily:
            return float(daily[model])
        # 别名：BAAI/bge-m3 -> bge-m3
        short = model.split("/")[-1]
        if short in daily:
            return float(daily[short])
        return None


# ---------------------------------------------------------------------------
# 定价与估算
# ---------------------------------------------------------------------------


def _in_period(period: Dict[str, Any], dt: datetime) -> bool:
    def to_minutes(hhmm: str) -> int:
        h, m = hhmm.split(":")
        return int(h) * 60 + int(m)

    cur = dt.hour * 60 + dt.minute
    start = to_minutes(str(period["start_time"]))
    end = to_minutes(str(period["end_time"]))
    return start <= cur < end


def prices_at(mcfg: Dict[str, Any], dt: datetime) -> Tuple[float, float, float]:
    """返回 (price_in, price_out, cache_price_in) 元/M tokens。"""
    pin, pout, pcache = mcfg["price_in"], mcfg["price_out"], mcfg["cache_price_in"]
    for period in mcfg.get("price_periods") or []:
        try:
            if _in_period(period, dt):
                return (
                    float(period.get("price_in", pin)),
                    float(period.get("price_out", pout)),
                    float(period.get("cache_price_in", pcache)),
                )
        except Exception:  # noqa: BLE001
            continue
    return pin, pout, pcache


def _content_chars(content: Any) -> Tuple[int, int]:
    """返回 (文本字符数, 图片数)。"""
    if content is None:
        return 0, 0
    if isinstance(content, str):
        return len(content), 0
    if isinstance(content, list):
        chars, imgs = 0, 0
        for part in content:
            if isinstance(part, dict):
                ptype = part.get("type", "")
                if ptype in ("text", "input_text", "output_text"):
                    chars += len(str(part.get("text") or ""))
                elif ptype in ("image_url", "input_image", "image"):
                    imgs += 1
                elif "image_url" in part:
                    imgs += 1
            elif isinstance(part, str):
                chars += len(part)
        return chars, imgs
    return len(str(content)), 0


def estimate_cost(cfg: Config, model: str, body: Dict[str, Any]) -> float:
    """请求前保守估算费用（元）。输入按 1.5 字符/token（对中英混合偏保守），输出按 max_tokens。"""
    mcfg = cfg.models[model]
    chars, imgs = 0, 0

    messages = body.get("messages")
    if isinstance(messages, list):
        for msg in messages:
            c, i = _content_chars(msg.get("content") if isinstance(msg, dict) else None)
            chars += c
            imgs += i
    inp = body.get("input")
    if isinstance(inp, str):
        chars += len(inp)
    elif isinstance(inp, list):
        for item in inp:
            if isinstance(item, str):
                chars += len(item)
            elif isinstance(item, dict):
                c, i = _content_chars(item.get("content") or item.get("text"))
                chars += c
                imgs += i

    for key in ("tools", "tool_choice", "response_format"):
        if key in body and body[key]:
            chars += len(json.dumps(body[key], ensure_ascii=False))

    est_in = math.ceil(chars / 1.5) + imgs * IMAGE_TOKEN_EST
    est_out = int(
        body.get("max_tokens")
        or body.get("max_completion_tokens")
        or DEFAULT_MAX_TOKENS
    )
    if body.get("stream") and est_out < DEFAULT_MAX_TOKENS:
        est_out = DEFAULT_MAX_TOKENS  # 流式输出量未知，按默认上限保守估

    pin, pout, _ = prices_at(mcfg, now_dt())
    if mcfg.get("model_type") == "embedding":
        return round(est_in * pin / 1e6, 8)
    return round((est_in * pin + est_out * pout) / 1e6, 8)


def settle_cost(mcfg: Dict[str, Any], usage: Dict[str, Any], dt: datetime) -> float:
    pin, pout, pcache = prices_at(mcfg, dt)
    prompt = int(usage.get("prompt_tokens") or 0)
    completion = int(usage.get("completion_tokens") or 0)
    cached = int(
        usage.get("prompt_cache_hit_tokens")
        or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
        or 0
    )
    cached = min(cached, prompt)
    miss = prompt - cached
    return round((miss * pin + cached * pcache + completion * pout) / 1e6, 8)


# ---------------------------------------------------------------------------
# SQLite 台账（事务性预留/结算/释放）
# ---------------------------------------------------------------------------


@contextmanager
def db_conn() -> Iterator[sqlite3.Connection]:
    conn = sqlite3.connect(str(DB_PATH), timeout=15.0, isolation_level=None)
    try:
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA busy_timeout=15000")
        yield conn
    finally:
        conn.close()


def init_db() -> None:
    DB_PATH.parent.mkdir(parents=True, exist_ok=True)
    with db_conn() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS reservations (
                id TEXT PRIMARY KEY,
                model TEXT NOT NULL,
                provider TEXT NOT NULL,
                day TEXT NOT NULL,
                est_cost REAL NOT NULL,
                state TEXT NOT NULL DEFAULT 'reserved',
                created REAL NOT NULL,
                expires REAL NOT NULL,
                settled_cost REAL,
                settled REAL,
                outcome TEXT,
                est INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                cached_tokens INTEGER
            )
            """
        )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_res_day_model ON reservations(day, model, state)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS settlements (
                id TEXT PRIMARY KEY,
                reservation_id TEXT,
                model TEXT NOT NULL,
                day TEXT NOT NULL,
                cost REAL NOT NULL,
                est INTEGER NOT NULL DEFAULT 0,
                prompt_tokens INTEGER,
                completion_tokens INTEGER,
                cached_tokens INTEGER,
                outcome TEXT,
                at REAL NOT NULL
            )
            """
        )
        conn.execute("CREATE INDEX IF NOT EXISTS idx_set_day_model ON settlements(day, model)")
        # 阶段5：优先级分层（high=可动用全部预算；low=仅限 budget×(1-reserve_ratio)）
        for table in ("reservations", "settlements"):
            cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})")]
            if "tier" not in cols:
                conn.execute(
                    f"ALTER TABLE {table} ADD COLUMN tier TEXT NOT NULL DEFAULT 'high'"
                )
        conn.execute(
            "CREATE INDEX IF NOT EXISTS idx_set_day_model_tier ON settlements(day, model, tier)"
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                at REAL NOT NULL,
                kind TEXT NOT NULL,
                model TEXT,
                detail TEXT
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS meta (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL
            )
            """
        )
        # 启动即清理：上次进程遗留的在途预留一律作废（连接已随进程消失）
        cur = conn.execute(
            "UPDATE reservations SET state='expired', outcome='restart' WHERE state='reserved'"
        )
        if cur.rowcount:
            logger.warning("启动清理: 作废上次进程遗留的在途预留 %d 条", cur.rowcount)


def reserve(model: str, provider: str, est: float, tier: str = "high") -> Tuple[bool, Optional[str], str]:
    """事务性费用预留。返回 (ok, reservation_id, reason)。

    tier="low" 时额外受高优先级保留份额约束：
      低优先级累计（已结算+在途）不得超过 budget × (1 - reserve_ratio)。
    """
    day = today_str()
    budget = cfg.budget_for(model)
    if budget is None:
        return False, None, "no_budget"
    if tier not in ("high", "low"):
        tier = "high"
    now = time.time()
    with db_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            row = conn.execute(
                "SELECT COALESCE(SUM(cost),0) FROM settlements WHERE day=? AND model=?",
                (day, model),
            ).fetchone()
            used = float(row[0] or 0.0)
            row = conn.execute(
                "SELECT COALESCE(SUM(est_cost),0) FROM reservations "
                "WHERE state='reserved' AND day=? AND model=?",
                (day, model),
            ).fetchone()
            reserved = float(row[0] or 0.0)
            if used + reserved + est > budget + 1e-12:
                conn.execute("ROLLBACK")
                _record_event(conn, "budget_exceeded", model,
                              f"used={used:.6f} reserved={reserved:.6f} est={est:.6f} budget={budget:.6f}")
                return False, None, "exceeded"
            if tier == "low":
                ratio = cfg.reserve_ratio()
                low_cap = budget * (1.0 - ratio)
                row = conn.execute(
                    "SELECT COALESCE(SUM(cost),0) FROM settlements "
                    "WHERE day=? AND model=? AND tier='low'",
                    (day, model),
                ).fetchone()
                used_low = float(row[0] or 0.0)
                row = conn.execute(
                    "SELECT COALESCE(SUM(est_cost),0) FROM reservations "
                    "WHERE state='reserved' AND day=? AND model=? AND tier='low'",
                    (day, model),
                ).fetchone()
                reserved_low = float(row[0] or 0.0)
                if used_low + reserved_low + est > low_cap + 1e-12:
                    conn.execute("ROLLBACK")
                    _record_event(
                        conn, "reserve_exceeded", model,
                        f"used_low={used_low:.6f} reserved_low={reserved_low:.6f} "
                        f"est={est:.6f} low_cap={low_cap:.6f} ratio={ratio:.2f}",
                    )
                    return False, None, "reserve_exceeded"
            rid = uuid.uuid4().hex
            conn.execute(
                "INSERT INTO reservations (id, model, provider, day, est_cost, state, created, expires, tier) "
                "VALUES (?,?,?,?,?,'reserved',?,?,?)",
                (rid, model, provider, day, est, now, now + RESERVATION_TTL, tier),
            )
            conn.execute("COMMIT")
            return True, rid, "ok"
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise


def settle(
    rid: Optional[str],
    model: str,
    cost: float,
    outcome: str,
    est: bool = False,
    usage: Optional[Dict[str, Any]] = None,
) -> None:
    """结算：实际费用入账，预留释放。usage 为 None 时按估算入账。"""
    now = time.time()
    day = today_str()
    prompt = completion = cached = None
    if usage:
        prompt = int(usage.get("prompt_tokens") or 0)
        completion = int(usage.get("completion_tokens") or 0)
        cached = int(
            usage.get("prompt_cache_hit_tokens")
            or (usage.get("prompt_tokens_details") or {}).get("cached_tokens")
            or 0
        )
    with db_conn() as conn:
        conn.execute("BEGIN IMMEDIATE")
        try:
            res_day = day
            res_tier = "high"
            if rid:
                row = conn.execute(
                    "SELECT day, tier FROM reservations WHERE id=?", (rid,)
                ).fetchone()
                if row:
                    res_day = row[0]  # 费用计入预留当天（跨日请求不挪预算日）
                    res_tier = row[1] or "high"
                conn.execute(
                    "UPDATE reservations SET state='settled', settled_cost=?, settled=?, "
                    "outcome=?, est=?, prompt_tokens=?, completion_tokens=?, cached_tokens=? "
                    "WHERE id=? AND state='reserved'",
                    (cost, now, outcome, 1 if est else 0, prompt, completion, cached, rid),
                )
            conn.execute(
                "INSERT INTO settlements (id, reservation_id, model, day, cost, est, "
                "prompt_tokens, completion_tokens, cached_tokens, outcome, at, tier) "
                "VALUES (?,?,?,?,?,?,?,?,?,?,?,?)",
                (uuid.uuid4().hex, rid, model, res_day, cost, 1 if est else 0,
                 prompt, completion, cached, outcome, now, res_tier),
            )
            conn.execute("COMMIT")
        except Exception:
            try:
                conn.execute("ROLLBACK")
            except Exception:  # noqa: BLE001
                pass
            raise


def release(rid: Optional[str], outcome: str) -> None:
    """释放预留（上游失败/网关故障），不产生费用。"""
    if not rid:
        return
    with db_conn() as conn:
        conn.execute(
            "UPDATE reservations SET state='released', outcome=?, settled=? "
            "WHERE id=? AND state='reserved'",
            (outcome, time.time(), rid),
        )


def _record_event(conn: sqlite3.Connection, kind: str, model: str, detail: str) -> None:
    conn.execute(
        "INSERT INTO events (at, kind, model, detail) VALUES (?,?,?,?)",
        (time.time(), kind, model, detail),
    )


def record_event(kind: str, model: str, detail: str) -> None:
    try:
        with db_conn() as conn:
            _record_event(conn, kind, model, detail)
    except Exception as exc:  # noqa: BLE001
        logger.error("记录事件失败: %s", exc)


def sweep_stale() -> int:
    now = time.time()
    with db_conn() as conn:
        cur = conn.execute(
            "UPDATE reservations SET state='expired', outcome='ttl' "
            "WHERE state='reserved' AND expires < ?",
            (now,),
        )
        return cur.rowcount


# ---------------------------------------------------------------------------
# 状态快照（供 Dragon Board 读取）
# ---------------------------------------------------------------------------

_reject_counter: Dict[Tuple[str, str], int] = {}  # (day, model) -> 次数
_start_time = time.time()


def build_status() -> Dict[str, Any]:
    day = today_str()
    models_out: Dict[str, Any] = {}
    with db_conn() as conn:
        for model in cfg.models:
            budget = cfg.budget_for(model)
            used = float(
                conn.execute(
                    "SELECT COALESCE(SUM(cost),0) FROM settlements WHERE day=? AND model=?",
                    (day, model),
                ).fetchone()[0]
                or 0.0
            )
            reserved = float(
                conn.execute(
                    "SELECT COALESCE(SUM(est_cost),0) FROM reservations "
                    "WHERE state='reserved' AND day=? AND model=?",
                    (day, model),
                ).fetchone()[0]
                or 0.0
            )
            reqs = int(
                conn.execute(
                    "SELECT COUNT(*) FROM settlements WHERE day=? AND model=?",
                    (day, model),
                ).fetchone()[0]
            )
            low_used = float(
                conn.execute(
                    "SELECT COALESCE(SUM(cost),0) FROM settlements "
                    "WHERE day=? AND model=? AND tier='low'",
                    (day, model),
                ).fetchone()[0]
                or 0.0
            )
            models_out[model] = {
                "budget": budget,
                "used": round(used, 6),
                "reserved": round(reserved, 6),
                "remaining": None if budget is None else round(max(0.0, budget - used - reserved), 6),
                "low_used": round(low_used, 6),
                "low_cap": None if budget is None else round(budget * (1.0 - cfg.reserve_ratio()), 6),
                "requests": reqs,
                "rejected": _reject_counter.get((day, model), 0),
                "gated": True,
            }
        recent = [
            {"at": r[0], "kind": r[1], "model": r[2], "detail": r[3]}
            for r in conn.execute(
                "SELECT at, kind, model, detail FROM events ORDER BY id DESC LIMIT 20"
            ).fetchall()
        ]
    return {
        "at": now_dt().isoformat(),
        "day": day,
        "ok": True,
        "uptime_s": int(time.time() - _start_time),
        "reserve_ratio": cfg.reserve_ratio(),
        "models": models_out,
        "recent_events": recent,
    }


def write_status_file() -> None:
    try:
        payload = json.dumps(build_status(), ensure_ascii=False, indent=2)
        tmp = STATUS_PATH.with_suffix(".tmp")
        tmp.write_text(payload, "utf-8")
        tmp.replace(STATUS_PATH)
    except Exception as exc:  # noqa: BLE001
        logger.error("写状态文件失败: %s", exc)


# ---------------------------------------------------------------------------
# 上游转发
# ---------------------------------------------------------------------------

cfg: Optional[Config] = None  # 启动时赋值
http_client: Optional[httpx.AsyncClient] = None

HOP_BY_HOP = {
    "connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
    "te", "trailers", "transfer-encoding", "upgrade", "host", "content-length",
    "authorization",
}

_USAGE_RE = re.compile(r'"usage"\s*:\s*(\{[^{}]*(?:\{[^{}]*\}[^{}]*)*\})')


def _extract_sse_usage(buf: str) -> Optional[Dict[str, Any]]:
    usage = None
    for line in buf.splitlines():
        line = line.strip()
        if not line.startswith("data:"):
            continue
        data = line[5:].strip()
        if not data or data == "[DONE]":
            continue
        try:
            obj = json.loads(data)
        except Exception:  # noqa: BLE001
            continue
        if isinstance(obj, dict) and isinstance(obj.get("usage"), dict):
            usage = obj["usage"]
    return usage


def _reject_counter_inc(model: str) -> None:
    key = (today_str(), model)
    _reject_counter[key] = _reject_counter.get(key, 0) + 1


def _error(status: int, code: str, message: str, **extra: Any) -> JSONResponse:
    payload = {"error": {"message": message, "type": code, "code": code}}
    payload["error"].update(extra)
    return JSONResponse(status_code=status, content=payload)


def _auth_ok(request: Request) -> bool:
    header = request.headers.get("authorization") or ""
    if not header.lower().startswith("bearer "):
        return False
    token = header[7:].strip()
    for key in cfg.gateway_keys():
        if hmac.compare_digest(token, key):
            return True
    return False


# ---------------------------------------------------------------------------
# 应用
# ---------------------------------------------------------------------------

app = FastAPI(title="qq-fish budget gateway", docs_url=None, redoc_url=None)


@app.on_event("startup")
async def on_startup() -> None:
    global cfg, http_client
    cfg = Config()
    init_db()
    http_client = httpx.AsyncClient(
        timeout=httpx.Timeout(UPSTREAM_TOTAL_DEADLINE, connect=10.0, read=UPSTREAM_READ_TIMEOUT,
                              write=30.0, pool=10.0),
        follow_redirects=False,
    )
    asyncio.create_task(_background_loop())
    logger.info("预算网关启动完成 port=%s", LISTEN_PORT)


@app.on_event("shutdown")
async def on_shutdown() -> None:
    if http_client:
        await http_client.aclose()


async def _background_loop() -> None:
    last_sweep = 0.0
    last_status = 0.0
    while True:
        try:
            now = time.time()
            if now - last_sweep >= SWEEP_INTERVAL:
                n = await asyncio.to_thread(sweep_stale)
                if n:
                    logger.info("过期预留释放 %d 条", n)
                last_sweep = now
            if now - last_status >= STATUS_INTERVAL:
                await asyncio.to_thread(write_status_file)
                last_status = now
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.error("后台任务异常: %s", exc)
        await asyncio.sleep(1.0)


@app.get("/healthz")
async def healthz() -> Dict[str, Any]:
    return {"ok": True, "day": today_str(), "uptime_s": int(time.time() - _start_time)}


@app.get("/admin/status")
async def admin_status(request: Request) -> Response:
    key = request.headers.get("x-admin-key") or ""
    if not cfg.admin_key() or not hmac.compare_digest(key, cfg.admin_key()):
        return _error(401, "unauthorized", "admin key required")
    return JSONResponse(build_status())


@app.get("/v1/models")
async def list_models(request: Request) -> Response:
    if not _auth_ok(request):
        return _error(401, "unauthorized", "invalid gateway key")
    data = [
        {"id": m, "object": "model", "owned_by": cfg.models[m]["provider"]}
        for m in cfg.models
    ]
    return JSONResponse({"object": "list", "data": data})


@app.post("/v1/{path:path}")
async def proxy(path: str, request: Request) -> Response:
    cfg.reload_if_changed()

    if path not in ALLOWED_ENDPOINTS:
        return _error(404, "endpoint_not_allowed", f"endpoint /v1/{path} not allowed")

    if not _auth_ok(request):
        return _error(401, "unauthorized", "invalid gateway key")

    try:
        body = await request.json()
    except Exception:  # noqa: BLE001
        return _error(400, "invalid_request", "request body is not valid JSON")

    if not isinstance(body, dict):
        return _error(400, "invalid_request", "request body must be a JSON object")

    model = str(body.get("model") or "").strip()
    if not model:
        return _error(400, "invalid_request", "missing model")

    # Fail-Closed：未知模型一律拒绝
    if model not in cfg.models:
        _reject_counter_inc(model)
        record_event("model_not_configured", model, "unknown model id")
        return _error(404, "MODEL_NOT_CONFIGURED",
                      f"model '{model}' is not configured in gateway (fail-closed)")

    mcfg = cfg.models[model]
    provider = mcfg["provider"]
    provider_key = cfg.provider_key(provider)
    if not provider_key:
        _reject_counter_inc(model)
        record_event("secret_missing", model, f"provider={provider}")
        return _error(503, "GATEWAY_MISCONFIGURED",
                      f"provider secret missing for '{provider}' (fail-closed)")

    est = estimate_cost(cfg, model, body)
    # 优先级分层：MaiBot 侧由 provider default_headers 注入（低优先级任务 -> "low"）
    tier_hdr = (request.headers.get("x-budget-tier") or "high").strip().lower()
    tier = tier_hdr if tier_hdr in ("high", "low") else "high"
    ok, rid, reason = reserve(model, provider, est, tier)
    if not ok:
        _reject_counter_inc(model)
        if reason == "reserve_exceeded":
            budget = cfg.budget_for(model)
            ratio = cfg.reserve_ratio()
            return _error(
                402, "PRIORITY_RESERVE_EXHAUSTED",
                f"PRIORITY_RESERVE_EXHAUSTED: low-priority quota exhausted for {model} "
                f"(high-priority reserve {ratio:.0%} of budget {budget} is protected)",
                model=model, day=today_str(), budget=budget, reserve_ratio=ratio,
            )
        if reason == "exceeded":
            budget = cfg.budget_for(model)
            used_row = None
            with db_conn() as conn:
                used_row = conn.execute(
                    "SELECT COALESCE(SUM(cost),0) FROM settlements WHERE day=? AND model=?",
                    (today_str(), model),
                ).fetchone()
            used = float(used_row[0] or 0.0)
            return _error(
                402, "BUDGET_EXCEEDED",
                f"BUDGET_EXCEEDED: daily budget exhausted for {model} "
                f"(used {used:.6f}/{budget} CNY, estimated next cost {est:.6f})",
                model=model, day=today_str(), used=used, budget=budget,
            )
        return _error(503, "GATEWAY_MISCONFIGURED",
                      "budget not configured for model (fail-closed)", model=model)

    upstream_url = cfg.upstream_url(model, path)
    headers = {
        "Authorization": f"Bearer {provider_key}",
        "Content-Type": "application/json",
        "Accept": "application/json, text/event-stream",
    }
    # 流式请求强制上游返回 usage（便于精确结算）；上游若不支持，网关自动降级重发一次
    payload = dict(body)
    stream = bool(payload.get("stream"))
    if stream and "stream_options" not in payload:
        payload["stream_options"] = {"include_usage": True}

    try:
        if stream:
            return await _proxy_stream(upstream_url, headers, payload, model, mcfg, rid, est, body)
        return await _proxy_json(upstream_url, headers, payload, model, mcfg, rid, est)
    except httpx.HTTPError as exc:
        release(rid, "upstream_error")
        record_event("upstream_error", model, f"{type(exc).__name__}: {exc}")
        return _error(502, "GATEWAY_UPSTREAM_ERROR",
                      f"upstream unreachable: {exc}", model=model)
    except asyncio.CancelledError:
        release(rid, "client_abort")
        raise
    except Exception as exc:  # noqa: BLE001
        release(rid, "gateway_error")
        record_event("gateway_error", model, f"{type(exc).__name__}: {exc}")
        logger.exception("网关内部错误")
        return _error(500, "GATEWAY_INTERNAL_ERROR", f"internal error: {exc}", model=model)


async def _proxy_json(url, headers, payload, model, mcfg, rid, est) -> Response:
    assert http_client is not None
    resp = await http_client.post(url, json=payload, headers=headers)
    if resp.status_code != 200:
        release(rid, f"upstream_{resp.status_code}")
        hdrs = {"x-gateway-upstream": str(resp.status_code)}
        if resp.status_code == 402:
            record_event("upstream_balance", model, "upstream returned 402")
        return Response(content=resp.content, status_code=resp.status_code,
                        media_type="application/json", headers=hdrs)

    usage: Optional[Dict[str, Any]] = None
    try:
        data = resp.json()
        u = data.get("usage")
        if isinstance(u, dict):
            usage = u
    except Exception:  # noqa: BLE001
        data = None

    dt = now_dt()
    if usage is not None:
        cost = settle_cost(mcfg, usage, dt)
        settle(rid, model, cost, "ok", est=False, usage=usage)
    else:
        settle(rid, model, est, "ok_no_usage", est=True)
    return Response(content=resp.content, status_code=200,
                    media_type="application/json")


async def _proxy_stream(url, headers, payload, model, mcfg, rid, est, orig_body) -> Response:
    assert http_client is not None
    req = http_client.build_request("POST", url, json=payload, headers=headers)
    resp = await http_client.send(req, stream=True)

    if resp.status_code != 200:
        body_bytes = await resp.aread()
        await resp.aclose()
        # 上游拒绝 stream_options（400）时降级重发一次，不算消费
        if (resp.status_code == 400 and "stream_options" in payload
                and b"stream_options" in body_bytes.lower()):
            logger.warning("上游不支持 stream_options，降级重发: model=%s", model)
            payload2 = dict(payload)
            payload2.pop("stream_options", None)
            req = http_client.build_request("POST", url, json=payload2, headers=headers)
            resp = await http_client.send(req, stream=True)
            if resp.status_code != 200:
                body_bytes = await resp.aread()
                await resp.aclose()
                release(rid, f"upstream_{resp.status_code}")
                return Response(content=body_bytes, status_code=resp.status_code,
                                media_type="application/json",
                                headers={"x-gateway-upstream": str(resp.status_code)})
        else:
            release(rid, f"upstream_{resp.status_code}")
            if resp.status_code == 402:
                record_event("upstream_balance", model, "upstream returned 402")
            return Response(content=body_bytes, status_code=resp.status_code,
                            media_type="application/json",
                            headers={"x-gateway-upstream": str(resp.status_code)})

    media_type = resp.headers.get("content-type", "text/event-stream")
    state: Dict[str, Any] = {"usage": None, "settled": False}

    async def gen():
        buf = ""
        try:
            # aiter_bytes 返回已解压字节：客户端拿到的是纯 SSE（我们不转发 content-encoding），
            # 同时保证 usage 嗅探在解压后的内容上进行。
            async for raw in resp.aiter_bytes():
                try:
                    buf += raw.decode("utf-8", errors="replace")
                    usage = _extract_sse_usage(buf)
                    if usage:
                        state["usage"] = usage
                    if len(buf) > 8192:
                        buf = buf[-4096:]
                except Exception:  # noqa: BLE001
                    pass
                yield raw
            # 正常结束 -> 结算
            state["settled"] = True
            usage = state["usage"]
            dt = now_dt()
            if usage:
                cost = settle_cost(mcfg, usage, dt)
                await asyncio.to_thread(settle, rid, model, cost, "ok", False, usage)
            else:
                await asyncio.to_thread(settle, rid, model, est, "ok_no_usage_est", True, None)
        except asyncio.CancelledError:
            # 客户端断开/超时：立即停止上游消费并按估算结算（不再继续花钱）
            if not state["settled"]:
                state["settled"] = True
                await asyncio.to_thread(settle, rid, model, est, "client_abort_est", True, None)
            raise
        except Exception as exc:  # noqa: BLE001
            if not state["settled"]:
                state["settled"] = True
                await asyncio.to_thread(settle, rid, model, est, "stream_error_est", True, None)
            logger.error("流式转发异常 model=%s: %s", model, exc)
            raise
        finally:
            try:
                await resp.aclose()
            except Exception:  # noqa: BLE001
                pass

    return StreamingResponse(
        gen(),
        status_code=200,
        media_type=media_type,
        headers={"cache-control": "no-cache", "x-accel-buffering": "no"},
    )


if __name__ == "__main__":
    import uvicorn

    uvicorn.run(app, host="0.0.0.0", port=LISTEN_PORT, log_level="info")