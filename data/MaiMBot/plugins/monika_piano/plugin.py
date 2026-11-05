"""Monika Piano 插件 — /piano 命令 + play_piano Tool。

本地模板触发, 0 新增 LLM 调用 (Tool 由对话模型现有调用承载, 仅传枚举参数)。
支持作品记忆与变奏续写: 同 seed 不同 variation 保留旋律动机。
"""
from __future__ import annotations

import asyncio
import json
import logging
import re
import sys
import time
from pathlib import Path

import websockets

from maibot_sdk import HookHandler, MaiBotPlugin
from maibot_sdk.components import Command, Tool
from maibot_sdk.types import HookMode, ToolParameterInfo, ToolParamType

# 持久化运行时引导：必须在导入 piano_engine 之前把 pylibs 加进 sys.path。
# 容器 /MaiMBot/.venv 在镜像层，镜像更新会丢 mido；这里的副本在 bind 挂载上。
_VENDOR_PYLIBS = Path("/MaiMBot/depends-data/piano-runtime/pylibs")
if _VENDOR_PYLIBS.is_dir() and str(_VENDOR_PYLIBS) not in sys.path:
    sys.path.insert(0, str(_VENDOR_PYLIBS))

_ENGINE_ERR = ""
try:
    from . import piano_engine
    from . import piano_storage
except Exception as _exc:  # noqa: BLE001
    piano_engine = None      # type: ignore[assignment]
    piano_storage = None     # type: ignore[assignment]
    _ENGINE_ERR = f"{type(_exc).__name__}: {_exc}"

_EXT_ERR = ""
try:
    from . import piano_external
except Exception as _exc:  # noqa: BLE001
    piano_external = None   # type: ignore[assignment]
    _EXT_ERR = f"{type(_exc).__name__}: {_exc}"

_MUSIC_ERR = ""
try:
    from . import music_state
    from . import music_act
except Exception as _exc:  # noqa: BLE001
    music_state = None      # type: ignore[assignment]
    music_act = None        # type: ignore[assignment]
    _MUSIC_ERR = f"{type(_exc).__name__}: {_exc}"

LOGGER = logging.getLogger(__name__)


def _param(name: str, ptype, desc: str, enum=None):
    return ToolParameterInfo(name=name, param_type=ptype, description=desc,
                             required=False, enum_values=enum)

MOOD_ALIAS = {
    "忧伤": "melancholy", "伤感": "melancholy", "难过": "melancholy", "sad": "melancholy",
    "平静": "calm", "安静": "calm", "calm": "calm",
    "温柔": "warm", "温暖": "warm", "warm": "warm",
    "开心": "playful", "欢快": "playful", "活泼": "playful", "happy": "playful",
    "怀旧": "nostalgic", "回忆": "nostalgic", "nostalgic": "nostalgic",
    "希望": "hopeful", "明亮": "hopeful", "hopeful": "hopeful",
    "melancholy": "melancholy",
}

MOOD_CN = {"melancholy": "忧伤", "calm": "平静", "warm": "温暖",
           "playful": "欢快", "nostalgic": "怀旧", "hopeful": "充满希望"}

DEFAULTS = {
    "mood": "melancholy", "bpm": 74, "bars": 8,
    "output_dir": "/MaiMBot/data/piano",
    "soundfont": "/usr/share/sounds/sf2/TimGM6mb.sf2",
    "napcat_ws": "ws://maim-bot-napcat:3001",
    "cooldown_seconds": 60,
    "variation_strength": None,      # null = 由 variation 自动推导 (0.45 + 0.15*v)
    "min_mem_available_mb": 200,     # 低于此值拒绝生成，避免与其它容器抢内存
    "max_load1": 6.0,
    "cleanup_enabled": True,         # 作品库维护（每天最多一次）
    "cleanup_dry_run": False,        # true=只记录计划不真删
    "wav_ttl_days": 30,              # 普通 WAV 保留天数
    "max_wav_mb": 300,               # WAV 总容量上限
    "max_pieces": 5000,              # 作品条数上限（实际生效的护栏）
    "midi_max_mb": 1024,             # 乐谱+元数据总容量安全网
    "min_keep_days": 7,              # 新作品至少保留天数
    "allow_users": [],   # 空 = 不限制；白名单形如 ["100000001"]
    "allow_groups": [],
    # ---- P9 在线曲谱库 ----
    "external_db": "/MaiMBot/data/piano/pieces.db",   # external_scores/external_render_cache 所在库
    "external_wav_ttl_days": 30,      # external_wavs 独立 TTL（external_midis 永不自动删）
    # ---- P8 自主音乐行为 ----
    "gov_rules_path": "/MaiMBot/data/governance/rules.json",  # 只读对接治理禁止令
    "quiet_hours_start": "23:00",
    "quiet_hours_end": "08:00",
    "private_spontaneous_per_day": 1,
    "group_spontaneous_interval_hours": 72,
    "group_spontaneous_enabled_default": False,
    "admin_user_ids": [],            # 运行期由 config.toml 提供
}

VARIATION_WORDS = {"变奏", "改一下", "variation"}
FAV_WORDS = {"收藏", "fav", "favorite"}
UNFAV_WORDS = {"取消收藏", "unfav", "unfavorite"}
REPLAY_WORDS = {"再听", "重听", "再来一次", "replay"}

class MonikaPianoPlugin(MaiBotPlugin):
    def __init__(self):
        super().__init__()
        self._last_play: dict[str, float] = {}
        self._conf: dict = {}
        self._deps: dict = {}
        self._signals: dict[str, dict] = {}

    async def on_load(self) -> None:
        self._reload_conf()
        self._deps = self._check_deps()
        if self._deps.get("ok"):
            LOGGER.info("[MonikaPiano] 运行时自检通过: fluidsynth=%s soundfont=%s",
                        self._deps.get("fluidsynth"), self._deps.get("soundfont"))
        else:
            LOGGER.error("[MonikaPiano] 运行时依赖缺失，钢琴生成已禁用: %s",
                         "; ".join(self._deps.get("issues") or []))
            LOGGER.error("[MonikaPiano] 修复方式: bash /opt/qq-fish/scripts/piano_deps.sh restore")
        # P8/P9 初始化：音乐状态表 + 外部曲谱表（均为幂等 CREATE IF NOT EXISTS）
        try:
            if music_state is not None:
                music_state.init(self._music_db())
            if piano_external is not None:
                conn = piano_external.connect(Path(self._music_db()))
                conn.close()
            LOGGER.info("[MonikaPiano] 音乐状态/外部曲谱表就绪 (P8/P9)")
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[MonikaPiano] P8/P9 初始化失败: %s", exc)
        LOGGER.info("[MonikaPiano] 插件已加载: /piano + play_piano + monika_music_act "
                    "+ find/import/play_piano_score (本地生成, 零额外 LLM)")

    def _music_db(self) -> str:
        return str(self._cfg("external_db") or (str(self._cfg("output_dir")) + "/pieces.db"))

    def _check_deps(self) -> dict:
        """轻量依赖自检：只检测，不安装。"""
        if piano_engine is None:
            return {"ok": False, "mido": False, "fluidsynth": "none", "soundfont": "",
                    "issues": [f"引擎导入失败: {_ENGINE_ERR}"]}
        try:
            return piano_engine.check_runtime(str(self._cfg("soundfont") or ""))
        except Exception as exc:  # noqa: BLE001
            return {"ok": False, "mido": False, "fluidsynth": "none", "soundfont": "",
                    "issues": [f"自检异常: {exc}"]}

    async def on_unload(self) -> None:
        LOGGER.info("[MonikaPiano] 插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        try:
            if scope == "piano" and isinstance(config_data, dict):
                self._conf = config_data
                LOGGER.info("[MonikaPiano] 配置已热更新")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(f"[MonikaPiano] 配置热更新失败: {exc}")

    # ---------- 配置 ----------

    def _reload_conf(self) -> None:
        try:
            import tomllib
            cfg_path = Path(__file__).resolve().parent / "config.toml"
            if cfg_path.exists():
                with open(cfg_path, "rb") as f:
                    data = tomllib.load(f)
                self._conf = data.get("piano", {}) or {}
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning(f"[MonikaPiano] 配置读取失败, 用默认值: {exc}")

    def _cfg(self, key: str):
        val = self._conf.get(key)
        if val is not None:
            return val
        return DEFAULTS.get(key)

    # ---------- 授权 ----------

    def _allowed(self, user_id: str, group_id: str) -> bool:
        allow_users = [str(u) for u in (self._cfg("allow_users") or [])]
        allow_groups = [str(g) for g in (self._cfg("allow_groups") or [])]
        if allow_users and str(user_id) not in allow_users:
            return False
        if group_id and allow_groups and str(group_id) not in allow_groups:
            return False
        return True

    # ---------- 冷却 ----------

    def _cooldown_ok(self, stream_id: str) -> tuple[bool, float]:
        cooldown = float(self._cfg("cooldown_seconds") or 60)
        now = time.monotonic()
        remaining = cooldown - (now - self._last_play.get(stream_id, 0.0))
        return remaining <= 0, max(0.0, remaining)

    # ---------- 作品库清理 ----------

    async def _maybe_cleanup(self) -> None:
        """每天最多执行一次；只清 WAV，MIDI/JSON/索引永久保留。"""
        if piano_storage is None or not bool(self._cfg("cleanup_enabled")):
            return
        outdir = str(self._cfg("output_dir"))
        try:
            if not piano_storage.needs_run(outdir):
                return
            dry = bool(self._cfg("cleanup_dry_run"))
            res = await asyncio.to_thread(
                piano_storage.cleanup, outdir,
                ttl_days=int(self._cfg("wav_ttl_days") or 30),
                max_wav_mb=int(self._cfg("max_wav_mb") or 300),
                max_pieces=int(self._cfg("max_pieces") or 5000),
                midi_max_mb=int(self._cfg("midi_max_mb") or 1024),
                min_keep_days=int(self._cfg("min_keep_days") or 7),
                dry_run=dry,
            )
            LOGGER.info(
                "[MonikaPiano] WAV 维护(%s): 扫描=%s 合计=%.1fMB 计划删=%s 可释放=%.1fMB "
                "实删=%s 收藏=%s 因缺MIDI保留=%s",
                "dry-run" if dry else "exec", res.get("scanned"), res.get("total_wav_mb", 0),
                len(res.get("planned") or []), res.get("planned_mb", 0),
                len(res.get("deleted") or []), res.get("favorites", 0),
                len(res.get("protected_keep_midi_missing") or []))
            cat = res.get("catalog") or {}
            if cat.get("error"):
                LOGGER.warning("[MonikaPiano] 作品目录修剪异常: %s", cat["error"])
            elif cat.get("over_size") or cat.get("over_count"):
                LOGGER.info(
                    "[MonikaPiano] 作品目录修剪(%s): %s首/%.2fMB 上限%s首/%sMB "
                    "计划移除=%s 实删=%s 收藏保护=%s 近期保护=%s 之后=%s首",
                    "dry-run" if dry else "exec", cat.get("before_pieces"),
                    cat.get("before_mb", 0), cat.get("max_pieces"), cat.get("max_mb"),
                    len(cat.get("planned") or []), len(cat.get("deleted") or []),
                    cat.get("protected_favorite", 0), cat.get("protected_recent", 0),
                    cat.get("after_pieces", "-"))
            else:
                LOGGER.info("[MonikaPiano] 作品目录 %s 首 / %.2fMB，未达阈值，无需修剪",
                            cat.get("before_pieces", 0), cat.get("before_mb", 0))
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaPiano] 作品库清理失败: %s", exc)

    # ---------- 共用: 生成 + 发送 ----------

    async def _play(self, *, mood: str, seed: int, bpm: int, bars: int,
                    variation: int, stream_id: str, group_id: str, user_id: str,
                    explicit: bool = True, source_kind: str = "original",
                    parent_id: str = "") -> dict:
        """生成并发送, 返回 {ok, reply, meta}。explicit=用户明确点播（绕过主动配额）。"""
        chat_id = f"group:{group_id}" if group_id else f"private:{user_id}"
        # 投递门禁（明确点播只受禁令/opt_out 约束）
        gate = self._gate(chat_id, user_id, explicit=explicit)
        if not gate["allowed"]:
            return {"ok": False, "reply": f"这次先不发音频哦（{gate['reason']}）", "meta": None}
        ok, remaining = self._cooldown_ok(stream_id)
        if not ok:
            return {"ok": False,
                    "reply": f"刚弹完一首, 手指要休息一下嘛…过 {int(remaining)} 秒再弹给你听~",
                    "meta": None}

        # 依赖自检：只有缺失时才重新检测（恢复依赖后无需重载插件）
        if piano_engine is None or not self._deps.get("ok"):
            self._deps = self._check_deps()
            if not self._deps.get("ok"):
                LOGGER.error("[MonikaPiano] 依赖缺失，拒绝生成: %s",
                             "; ".join(self._deps.get("issues") or []))
                return {"ok": False,
                        "reply": "钢琴还没调好音…等我修一下琴再弹给你听好不好", "meta": None}

        await self._maybe_cleanup()

        self._last_play[stream_id] = time.monotonic()
        # TOML 无 null，用负数表示"跟随 variation 自动推导"
        vs = self._cfg("variation_strength")
        vs = None if vs is None or float(vs) < 0 else float(vs)
        try:
            meta = await asyncio.to_thread(
                piano_engine.generate,
                mood=mood, bpm=bpm, bars=bars, seed=seed, variation=variation,
                chat_id=chat_id,
                outdir=str(self._cfg("output_dir")),
                soundfont=str(self._cfg("soundfont")),
                variation_strength=vs,
                min_mem_mb=int(self._cfg("min_mem_available_mb") or 200),
                max_load1=float(self._cfg("max_load1") or 6.0),
            )
        except piano_engine.ResourceBusyError as exc:
            LOGGER.warning(f"[MonikaPiano] 资源不足，跳过本次生成: {exc}")
            self._last_play.pop(stream_id, None)   # 资源拒绝不计入冷却
            return {"ok": False,
                    "reply": "现在手有点抖…等我缓一下再弹给你听好不好", "meta": None}
        except FileNotFoundError:
            return {"ok": False, "reply": "钢琴还没调好音 (容器缺 fluidsynth)…稍后再试试?", "meta": None}
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(f"[MonikaPiano] 生成失败: {exc}", exc_info=True)
            return {"ok": False, "reply": "呜…这次弹砸了, 换个心情再让我试试好不好", "meta": None}

        sent = await self._send_record(group_id, user_id, meta["napcat_uri"])
        self._log_play(chat_id, source_kind, meta["piece_id"], explicit=explicit,
                       send_status="sent" if sent else "failed")
        if not sent:
            return {"ok": False,
                    "reply": f"练了一段{MOOD_CN.get(mood, mood)}的曲子 (seed={meta['seed']}), 但录音笔好像坏了…没发出去 :(",
                    "meta": meta}
        if variation > 0:
            reply = (f"这段是根据上次那首改的变奏 v{variation}, "
                     f"主题还是原来的味道, 细节重新润过了~ {meta['bpm']} BPM ♪")
        else:
            reply = (f"锵锵~ 练琴成果汇报: 一段{MOOD_CN.get(mood, mood)}的旋律, "
                     f"{meta['bpm']} BPM, {meta['bars']} 小节, 送给你听 ♪")
        LOGGER.info(f"[MonikaPiano] 完成 piece={meta['piece_id']} md5={meta['md5'][:8]} sent={sent}")
        return {"ok": True, "reply": reply, "meta": meta}

    async def _send_record(self, group_id: str, user_id: str, file_uri: str) -> bool:
        """通过 NapCat OneBot WS 发送 record 语音 (P1 已验证链路)。"""
        ws_url = str(self._cfg("napcat_ws"))
        token = str(self._cfg("napcat_token") or "").strip() or None
        try:
            headers = {"Authorization": f"Bearer {token}"} if token else {}
            if group_id:
                action, params = "send_group_msg", {
                    "message_type": "group", "group_id": int(group_id),
                    "message": [{"type": "record", "data": {"file": file_uri}}],
                }
            else:
                action, params = "send_private_msg", {
                    "message_type": "private", "user_id": int(user_id),
                    "message": [{"type": "record", "data": {"file": file_uri}}],
                }
            payload = {"action": action, "params": params, "echo": f"piano-{time.time()}"}
            async with websockets.connect(ws_url, additional_headers=headers,
                                          open_timeout=10, close_timeout=5) as ws:
                await ws.send(json.dumps(payload))
                while True:
                    raw = await asyncio.wait_for(ws.recv(), timeout=60)
                    data = json.loads(raw)
                    if data.get("echo") == payload["echo"]:
                        ok = data.get("retcode") == 0
                        if not ok:
                            LOGGER.error(f"[MonikaPiano] NapCat 返回: {data}")
                        return ok
        except Exception as exc:  # noqa: BLE001
            LOGGER.error(f"[MonikaPiano] 语音发送异常: {exc}", exc_info=True)
            return False

    # ---------- 命令 ----------

    @Command(
        "piano",
        description="让莫妮卡即兴弹一段原创钢琴曲 (语音发送, 本地生成)",
        pattern=r"^/piano(?:\s+(?P<args>.+))?\s*$",
        aliases=["弹琴", "钢琴"],
    )
    async def handle_piano(
        self,
        stream_id: str = "",
        group_id: str = "",
        platform: str = "",
        user_id: str = "",
        matched_groups: dict | None = None,
        **kwargs,
    ):
        if not self._allowed(user_id, group_id):
            await self.ctx.send.text("这首曲子现在只想弹给特定的人听哦, 抱抱~", stream_id)
            return False, "未授权", True

        args_text = str((matched_groups or {}).get("args", "") or "").strip()
        params, error = self._parse_args(args_text)
        if error:
            await self.ctx.send.text(error, stream_id)
            return False, error, True

        chat_id = self._chat_id(group_id, user_id)
        outdir = str(self._cfg("output_dir"))
        action = params.get("action", "play")

        # --- 找谱 / 导谱 / 弹谱（P9，零 LLM） ---
        if action in ("find_score", "import_score", "play_score"):
            q = params.get("query", "")
            if action == "find_score":
                text = await self._find_midi_impl(q)
            elif action == "import_score":
                text = await self._import_midi_impl(q)
            else:
                text = await self._play_score_impl(q, {
                    "stream_id": stream_id, "group_id": group_id, "user_id": user_id})
            await self.ctx.send.text(text, stream_id)
            return False, text[:80], True

        # --- 收藏 / 取消收藏（不发送语音，不占冷却） ---
        if action in ("fav", "unfav"):
            if piano_engine is None or piano_storage is None:
                await self.ctx.send.text("钢琴还没调好音…等我修一下琴", stream_id)
                return False, "依赖缺失", True
            last = piano_engine.last_piece(outdir, chat_id)
            if last is None:
                await self.ctx.send.text("我还没在这弹过琴呢, 先听我弹一段好不好? 试试 /piano", stream_id)
                return False, "无历史作品", True
            now_fav = piano_storage.toggle_favorite(Path(outdir), last["piece_id"])
            reply = (f"这首我记下了, 以后不会弄丢~ ({last['piece_id']})" if now_fav
                     else f"好, 这首不再特别保留了 ({last['piece_id']})")
            await self.ctx.send.text(reply, stream_id)
            return False, reply, True

        # --- 再听一次：WAV 被清理过就用 MIDI 重新渲染 ---
        if action == "replay":
            if piano_engine is None:
                await self.ctx.send.text("钢琴还没调好音…等我修一下琴", stream_id)
                return False, "依赖缺失", True
            last = piano_engine.last_piece(outdir, chat_id)
            if last is None:
                await self.ctx.send.text("我还没在这弹过琴呢, 先听我弹一段好不好? 试试 /piano", stream_id)
                return False, "无历史作品", True
            ok, remaining = self._cooldown_ok(stream_id)
            if not ok:
                await self.ctx.send.text(f"刚弹完一首, 手指要休息一下嘛…过 {int(remaining)} 秒再弹给你听~",
                                         stream_id)
                return False, "冷却中", True
            self._last_play[stream_id] = time.monotonic()
            wav_before = Path(last.get("wav_path") or "")
            had_wav = wav_before.is_file()
            wav = await asyncio.to_thread(piano_engine.ensure_wav, last,
                                          str(self._cfg("soundfont") or ""))
            if not wav:
                await self.ctx.send.text("那份录音找不到了, 乐谱也不太完整…我再弹一首新的给你吧",
                                         stream_id)
                return False, "无法恢复音频", True
            sent = await self._send_record(group_id, user_id, f"file://{wav}")
            LOGGER.info("[MonikaPiano] 再听 piece=%s 重渲染=%s sent=%s",
                        last["piece_id"], "否" if had_wav else "是", sent)
            reply = ("上次那首又弹了一遍, 听出来了吗? ♪" if sent
                     else "录音笔好像坏了…没能发出去 :(")
            await self.ctx.send.text(reply, stream_id)
            return False, reply, True

        if params.get("variation_of_last"):
            last = piano_engine.last_piece(outdir, chat_id)
            if last is None:
                await self.ctx.send.text("我还没在这弹过琴呢, 先听我弹一段新的好不好? 试试 /piano", stream_id)
                return False, "无历史作品", True
            params["seed"] = int(last.get("seed", 0))
            params["mood"] = last.get("mood", DEFAULTS["mood"])
            params["variation"] = int(last.get("variation", 0)) + 1

        result = await self._play(
            mood=params["mood"], seed=params["seed"], bpm=params["bpm"], bars=params["bars"],
            variation=params.get("variation", 0), stream_id=stream_id,
            group_id=group_id, user_id=user_id,
        )
        await self.ctx.send.text(result["reply"], stream_id)
        return False, result["reply"], True

    @staticmethod
    def _chat_id(group_id: str, user_id: str) -> str:
        return f"group:{group_id}" if group_id else f"private:{user_id}"

    def _parse_args(self, args_text: str) -> tuple[dict, str]:
        """'/piano [mood|变奏] [seed=N] [bpm=N] [bars=N]'，以及 找谱/导谱/弹谱 子命令。"""
        params = {"mood": DEFAULTS["mood"], "seed": int(time.time()) % 100000,
                  "bpm": int(DEFAULTS["bpm"]), "bars": int(DEFAULTS["bars"]),
                  "variation": 0, "variation_of_last": False, "action": "play",
                  "query": ""}
        raw = (args_text or "").strip()
        # 子命令前缀：找谱/导谱/弹谱（后接参数，允许空格）
        for prefix, act in (("找谱", "find_score"), ("导谱", "import_score"),
                            ("弹谱", "play_score")):
            if raw.startswith(prefix):
                params["action"] = act
                params["query"] = raw[len(prefix):].strip()
                return params, ""
        for tok in (args_text.split() if args_text else []):
            low = tok.lower()
            if low in UNFAV_WORDS:
                params["action"] = "unfav"
            elif low in FAV_WORDS:
                params["action"] = "fav"
            elif low in REPLAY_WORDS:
                params["action"] = "replay"
            elif low in VARIATION_WORDS or (low.startswith("v") and low[1:].isdigit()):
                params["variation_of_last"] = True
                if low.startswith("v") and low[1:].isdigit():
                    params["variation"] = max(1, int(low[1:]))
            elif low.startswith("seed="):
                try:
                    params["seed"] = max(0, int(low[5:]))
                except ValueError:
                    return params, "seed 要一个整数哦, 比如 /piano melancholy seed=42"
            elif low.startswith("bpm="):
                try:
                    params["bpm"] = min(180, max(40, int(low[4:])))
                except ValueError:
                    return params, "bpm 要 40~180 的整数哦"
            elif low.startswith("bars="):
                try:
                    params["bars"] = {4: 4, 8: 8, 16: 16}.get(int(low[5:]), 8)
                except ValueError:
                    return params, "bars 只支持 4 / 8 / 16 哦"
            elif low in MOOD_ALIAS:
                params["mood"] = MOOD_ALIAS[low]
            else:
                moods = ("/".join(piano_engine.MOODS) if piano_engine
                         else "melancholy/calm/warm/playful/nostalgic/hopeful")
                return params, (
                    "这个参数我不认识呢…试试:\n"
                    "/piano [mood] [seed=N] · /piano 变奏 · /piano 再听 · /piano 收藏\n"
                    f"mood 可选: {moods} 或中文(忧伤/平静/温暖/开心/怀旧/希望)"
                )
        return params, ""

    # ---------- Tool (自然语言触发, 参数极简) ----------

    @Tool(
        "play_piano",
        description="让莫妮卡即兴弹一段原创钢琴曲并作为QQ语音发送。仅在用户明确想听钢琴曲/让莫妮卡弹琴时调用。",
        parameters=[
            _param("mood", ToolParamType.STRING, "情绪",
                   enum=["melancholy", "calm", "warm", "playful", "nostalgic", "hopeful"]),
            _param("bpm", ToolParamType.INTEGER, "速度 40-180"),
            _param("bars", ToolParamType.INTEGER, "小节数 4/8/16"),
            _param("seed", ToolParamType.INTEGER, "随机种子, 复现或变奏旧曲时用"),
            _param("variation", ToolParamType.INTEGER, "变奏编号>0, 基于同 seed 旧曲变奏"),
        ],
    )
    async def handle_play_piano(self, mood: str = "", bpm: int = 0, bars: int = 0,
                                seed: int = 0, variation: int = 0, **kwargs):
        stream_id = str(kwargs.get("stream_id", "") or "")
        group_id = str(kwargs.get("group_id", "") or "")
        user_id = str(kwargs.get("user_id", "") or "")
        if piano_engine is None:
            return "钢琴依赖缺失，暂不可用"
        if not self._allowed(user_id, group_id):
            return "未授权: 当前用户不在钢琴白名单内"
        mood_v = MOOD_ALIAS.get(mood.strip().lower(), "") or (
            mood.strip().lower() if mood.strip().lower() in piano_engine.MOODS else DEFAULTS["mood"])
        result = await self._play(
            mood=mood_v,
            seed=int(seed) if seed else int(time.time()) % 100000,
            bpm=min(180, max(40, int(bpm))) if bpm else int(DEFAULTS["bpm"]),
            bars={4: 4, 8: 8, 16: 16}.get(int(bars), 8) if bars else int(DEFAULTS["bars"]),
            variation=int(variation) if variation else 0,
            stream_id=stream_id or self._chat_id(group_id, user_id),
            group_id=group_id, user_id=user_id,
        )
        # 返回给 LLM 的工具结果保持极短 (Token 控制)
        if result["ok"]:
            m = result["meta"]
            return f"已发送语音: {m['piece_id']} ({m['mood']}, {m['bpm']}bpm, 变奏v{m['variation']})"
        return f"未发送: {result['reply'][:50]}"

    # ---------- P8: 候选信号采集（OBSERVE, 零侵入） ----------

    @HookHandler(
        "chat.receive.after_process",
        name="monika_piano_signal",
        description="词法扫描入站消息的音乐线索，仅存本地信号供 monika_music_act 二级路由参考；无 LLM 调用。",
        mode=HookMode.OBSERVE,
    )
    async def handle_inbound_signal(self, message: dict, **kwargs) -> None:
        if music_act is None or not isinstance(message, dict):
            return
        text = ""
        for key in ("processed_plain_text", "display_message", "raw_message", "text"):
            if message.get(key):
                text = str(message[key])
                break
        if not text:
            return
        sid = str(message.get("session_id") or message.get("stream_id") or "")
        sig = music_act.detect_music_signals(text)
        if sig.get("candidate") or sig.get("refuse"):
            self._signals[sid] = {"sig": sig, "text": text[:200], "ts": time.time()}
            if sig.get("refuse"):
                # 用户明确拒绝 → 立即进入本会话音乐打扰冷却（优先遵守最新意愿）
                if music_state is not None:
                    try:
                        music_state.set_policy(self._music_db(), sid, by="user_refuse",
                                               opt_out=1)
                    except Exception:  # noqa: BLE001
                        pass
                self._signals[sid]["refused"] = True
            LOGGER.info("[MonikaPiano] 音乐信号 sid=%s sig=%s", sid,
                        {k: v for k, v in sig.items() if v})

    def _drain_signal(self, stream_id: str) -> dict:
        sig = self._signals.pop(stream_id, {})
        # 信号时效 10 分钟
        if sig and time.time() - sig.get("ts", 0) > 600:
            return {}
        return sig

    # ---------- P8: 投递门禁 ----------

    def _gate(self, chat_id: str, user_id: str, explicit: bool) -> dict:
        if music_state is None:
            return {"allowed": True, "reason": "no_state_module", "quota_limited": False}
        return music_state.check_send_gate(
            self._music_db(), chat_id=chat_id, user_id=user_id, explicit=explicit,
            policy_cfg={"quiet_hours_start": str(self._cfg("quiet_hours_start")),
                        "quiet_hours_end": str(self._cfg("quiet_hours_end")),
                        "private_spontaneous_per_day": int(self._cfg("private_spontaneous_per_day") or 1),
                        "group_spontaneous_interval_hours": float(self._cfg("group_spontaneous_interval_hours") or 72),
                        "group_spontaneous_enabled_default": bool(self._cfg("group_spontaneous_enabled_default"))},
            gov_rules_path=str(self._cfg("gov_rules_path") or ""))

    def _log_play(self, chat_id: str, source_kind: str, source_id: str,
                  explicit: bool, send_status: str) -> None:
        if music_state is None:
            return
        try:
            music_state.log_play_event(self._music_db(), chat_id=chat_id,
                                       source_kind=source_kind, source_id=source_id,
                                       explicit=explicit, send_status=send_status)
            if not explicit and send_status == "sent":
                music_state.mark_spontaneous(self._music_db(), chat_id)
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaPiano] play_events 记录失败: %s", exc)

    # ---------- P9: 外部曲谱库 ----------

    @Tool(
        "find_piano_midi",
        description="查找钢琴曲谱 MIDI：先查本地曲库(piece_/ext_)，未命中再搜 Mutopia 合法来源，"
                    "返回带来源与许可的候选(≤5条)。只查不下载。用户提到真实曲名/作曲家想听时调用。"
                    "重要：每个关键词只搜一次。结果里没有用户要的那首就直接说没有——"
                    "不要反复搜索、不要导入名字相近但不同的曲子，可以请用户给 Mutopia 链接。",
        parameters=[
            _param("query", ToolParamType.STRING, "曲名或作曲家关键词，如 '肖邦 前奏曲' / 'chopin prelude'"),
        ],
    )
    async def handle_find_midi(self, query: str = "", **kwargs):
        return await self._find_midi_impl(query)

    async def _find_midi_impl(self, query: str = "") -> str:
        if piano_external is None:
            return f"外部曲谱模块不可用: {_EXT_ERR[:60]}"
        q = (query or "").strip()
        if not q:
            return "请给出曲名或作曲家"
        out = []
        # 1) 本地外部曲谱库
        try:
            local = piano_external.list_scores(Path(self._music_db()), q, 5)
            for r in local:
                out.append(f"[本地] {r['score_id']} 《{r['title']}》 {r['composer']} "
                           f"({r['license_name']})")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaPiano] 本地曲谱查询失败: %s", exc)
        # 2) Mutopia 搜索（不下载）；结果做相关性过滤，防止"名字沾边"的杂项误导
        if len(out) < 5:
            try:
                remote = await asyncio.to_thread(piano_external.search_mutopia, q)
                tokens = [t for t in re.split(r"[\s,，、]+", q.lower()) if t]
                relevant = []
                for r in remote:
                    hay = f"{r['title']} {r['composer']}".lower()
                    # 中文查询按字面包含；英文按词交集
                    if any(t in hay for t in tokens):
                        relevant.append(r)
                if not relevant and remote:
                    out.append(f"(Mutopia 有含关键词的其它曲目，但没有与「{q}」明确同名/同作曲家的，"
                               f"不冒充。可给单曲页链接直接导入。)")
                for r in relevant[: 5 - len(out)]:
                    out.append(f"[Mutopia] id={r['item_id']} 《{r['title']}》 {r['composer']} "
                               f"— {r['page_url']}")
            except Exception as exc:  # noqa: BLE001
                out.append(f"(在线搜索暂不可用: {type(exc).__name__})")
        if not out:
            return f"没有找到与「{q}」相关的曲谱。可以换个关键词，或给我 Mutopia 单曲页地址。"
        return "曲谱候选:\n" + "\n".join(out) + \
               "\n(本地 ext_ 可直接 /piano 弹谱 <score_id>；Mutopia 可 /piano 导谱 <page_url>)"

    @Tool(
        "import_piano_midi",
        description="从 Mutopia 单曲页下载合法 MIDI 并入库（校验许可/音轨/时长，消毒后保存）。"
                    "用户明确要某首真实作品且本地没有时调用。外部作品不是莫妮卡原创。"
                    "重要：只导入与用户点播曲名/作曲家一致的曲子；名字沾边但不同的不要导。",
        parameters=[
            _param("source_page_url", ToolParamType.STRING,
                   "Mutopia 单曲页地址 https://www.mutopiaproject.org/cgibin/piece-info.cgi?id=NNN"),
        ],
    )
    async def handle_import_midi(self, source_page_url: str = "", **kwargs):
        return await self._import_midi_impl(source_page_url)

    async def _import_midi_impl(self, source_page_url: str = "") -> str:
        if piano_external is None:
            return f"外部曲谱模块不可用: {_EXT_ERR[:60]}"
        url = (source_page_url or "").strip()
        try:
            row = await asyncio.to_thread(
                piano_external.import_mutopia, Path(self._music_db()),
                Path(self._cfg("output_dir")), url)
        except ValueError as exc:
            return f"拒绝导入: {exc}"
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[MonikaPiano] 导入失败: %s", exc)
            return f"导入失败: {type(exc).__name__}: {exc}"
        LOGGER.info("[MonikaPiano] 导入外部曲谱 %s《%s》%s", row["score_id"], row["title"],
                    row["license_name"])
        return (f"已入库: {row['score_id']} 《{row['title']}》 — {row['composer']}\n"
                f"许可: {row['license_name']} | 归因: {row['attribution']}\n"
                f"注意: 这是已有作品的曲谱版，不是我的原创。")

    @Tool(
        "play_piano_score",
        description="演奏外部曲谱库中的曲子(ext_ 开头 score_id)：按需渲染 WAV 并发送 QQ 语音，"
                    "带来源与许可说明。用户明确想听那首曲子时调用。",
        parameters=[
            _param("score_id", ToolParamType.STRING, "find_piano_midi/import_piano_midi 返回的 ext_ 开头 ID"),
        ],
    )
    async def handle_play_score(self, score_id: str = "", **kwargs):
        return await self._play_score_impl(score_id, kwargs)

    async def _play_score_impl(self, score_id: str = "", kwargs: dict | None = None) -> str:
        kwargs = kwargs or {}
        if piano_external is None or piano_engine is None:
            return "外部曲谱模块不可用"
        stream_id = str(kwargs.get("stream_id", "") or "")
        group_id = str(kwargs.get("group_id", "") or "")
        user_id = str(kwargs.get("user_id", "") or "")
        chat_id = self._chat_id(group_id, user_id)
        sid = (score_id or "").strip()
        if not sid.startswith("ext_"):
            return "score_id 需要 ext_ 开头（原创用 play_piano）"
        gate = self._gate(chat_id, user_id, explicit=True)
        if not gate["allowed"]:
            return f"无法演奏: {gate['reason']}"
        try:
            info = await asyncio.to_thread(
                piano_external.render, Path(self._music_db()), Path(self._cfg("output_dir")),
                sid, runtime=piano_engine.resolve_runtime(str(self._cfg("soundfont") or "")))
        except ValueError as exc:
            return f"拒绝演奏: {exc}"
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[MonikaPiano] 外部曲谱渲染失败: %s", exc)
            return f"渲染失败: {type(exc).__name__}"
        sent = await self._send_record(group_id, user_id, f"file://{info['wav_path']}")
        self._log_play(chat_id, "external", sid, explicit=True,
                       send_status="sent" if sent else "failed")
        if sent:
            return (f"已发送: 《{info['title']}》 — {info['composer']} "
                    f"({'缓存' if info['cached'] else '现渲染'})\n"
                    f"来源: {info['source_page_url']} | 许可: {info['license']} | {info['attribution']}")
        return f"渲染好了但发送失败: 《{info['title']}》"

    # ---------- P8: 高层自主音乐行为工具 ----------

    @Tool(
        "monika_music_act",
        description="莫妮卡的自主音乐行为入口。根据情境自主决定：弹琴/只聊音乐/只创作不发/练琴/找谱/复奏/变奏，"
                    "或判断此刻不该打扰。action=auto 时由插件结合真实作品状态与曲库二级路由。"
                    "仅当对话有音乐相关线索、用户提及曲目/心情、或有真实未完成创作时考虑调用；"
                    "普通闲聊、对方忙碌/难过求助、话题无关时不要调用（选择不弹也是一种回答）。",
        parameters=[
            _param("action", ToolParamType.STRING, "行为",
                   enum=["auto", "create", "vary", "replay", "search", "practice",
                         "talk", "ignore", "pref"]),
            _param("intent", ToolParamType.STRING, "简短意图，如 '想把雨天旋律结尾改一下'"),
            _param("referent", ToolParamType.STRING, "曲名/作曲家/piece_/ext_ ID（可选）"),
            _param("delivery", ToolParamType.STRING, "投递方式",
                   enum=["auto", "no_audio", "offer", "send"]),
        ],
    )
    async def handle_music_act(self, action: str = "auto", intent: str = "",
                               referent: str = "", delivery: str = "auto", **kwargs):
        if music_act is None or music_state is None:
            return f"音乐行为模块不可用: {_MUSIC_ERR[:60]}"
        stream_id = str(kwargs.get("stream_id", "") or "")
        group_id = str(kwargs.get("group_id", "") or "")
        user_id = str(kwargs.get("user_id", "") or "")
        chat_id = self._chat_id(group_id, user_id)
        action = (action or "auto").strip().lower()
        delivery = (delivery or "auto").strip().lower()

        # --- pref: 记录用户可验证的音乐偏好/禁令 ---
        if action == "pref":
            return self._music_pref(intent, chat_id, user_id)

        # --- ignore: 明确不弹 ---
        if action == "ignore":
            self._drain_signal(stream_id)
            return "决定: 不弹琴（继续正常聊天）。"

        # 组装路由状态（真实目录命中 + 词法信号 + 未完成项目）
        sig_pack = self._drain_signal(stream_id)
        sig = sig_pack.get("sig") or music_act.detect_music_signals(f"{intent} {referent}")
        piece_hit = score_hit = None
        if piano_engine is not None:
            try:
                metas = piano_engine.last_piece(str(self._cfg("output_dir")), chat_id)
                if referent and referent.startswith("piece_"):
                    piece_hit = referent
                elif sig.get("vary") or sig.get("replay"):
                    piece_hit = (metas or {}).get("piece_id")
            except Exception:  # noqa: BLE001
                pass
        if piano_external is not None and referent:
            try:
                hits = piano_external.list_scores(Path(self._music_db()), referent, 1)
                score_hit = hits[0]["score_id"] if hits else None
            except Exception:  # noqa: BLE001
                pass
        projects = music_state.active_projects(self._music_db(), chat_id, 3)

        if action == "auto":
            plan = music_act.route({
                "signals": sig, "referent": referent, "intent": intent,
                "explicit": delivery == "send" or sig.get("request", False),
                "piece_hit": piece_hit, "score_hit": score_hit,
                "projects": projects, "env": {"group": bool(group_id)},
            })
        else:
            src_map = {"create": "new_original", "vary": "variation",
                       "replay": "replay_original", "search": "search_external",
                       "practice": "practice_existing", "talk": ""}
            beh = {"create": "send_audio", "vary": "send_audio", "replay": "send_audio",
                   "search": "offer_audio", "practice": "practice",
                   "talk": "talk_music"}.get(action, "send_audio")
            plan = {"behavior": beh, "source": src_map.get(action, "new_original"),
                    "reason": f"模型显式 action={action}"}

        behavior, source = plan["behavior"], plan["source"]
        LOGGER.info("[MonikaPiano] music_act 路由 behavior=%s source=%s reason=%s "
                    "delivery=%s intent=%r", behavior, source, plan["reason"], delivery,
                    intent[:60])

        # --- 不产生音频的行为 ---
        if behavior == "ignore":
            return f"决定: 不弹琴（{plan['reason']}）。"
        if behavior == "talk_music":
            return f"决定: 只聊音乐不弹（{plan['reason']}）。"
        if behavior == "think_music":
            # 有灵感：可落库为未完成项目，不发送
            if delivery in ("auto", "no_audio", "offer") and source in ("new_original", "variation"):
                proj = music_state.add_project(
                    self._music_db(), chat_id=chat_id,
                    title=(referent or intent or "未命名动机")[:60],
                    motif=intent[:200], stage="idea", source_kind="original",
                    next_action="继续写完并试奏")
                if delivery == "offer":
                    return (f"决定: 有了灵感但先不发（{plan['reason']}）。"
                            f"记下了想法 {proj['project_id']}，想听的时候跟我说。")
                return (f"决定: 创作并保存为未完成作品 {proj['project_id']}，暂不发送"
                        f"（{plan['reason']}）。")
        if behavior == "practice":
            return await self._do_practice(chat_id, user_id, group_id, stream_id,
                                           source, piece_hit or score_hit or referent,
                                           projects)

        # --- send_audio / offer_audio：真实执行 + 门禁 ---
        explicit = delivery == "send" or bool(sig.get("request"))
        gate = self._gate(chat_id, user_id, explicit=explicit)
        if behavior == "offer_audio" or delivery == "offer":
            if not gate["allowed"] and gate["reason"] in ("opt_out", "governance_prohibit"):
                return "决定: 提都不提（对方已拒绝/有禁令）。"
            return (f"决定: 先问一句要不要听（{plan['reason']}），不直接发语音。"
                    f"可用 monika_music_act(action=replay/create, delivery=send) 继续。")
        if delivery == "no_audio":
            # 生成但不发送：落库为草稿项目
            proj = music_state.add_project(
                self._music_db(), chat_id=chat_id, title=(referent or intent or "草稿")[:60],
                motif=intent[:200], stage="draft", source_kind="original",
                next_action="择机试奏或分享")
            return f"决定: 只创作不发送（{plan['reason']}）。草稿项目 {proj['project_id']} 已记录。"
        if not gate["allowed"]:
            reason_cn = {"quiet_hours": "静默时段", "daily_quota": "今天主动分享额度用完了",
                         "group_not_enabled": "本群未开启主动分享", "group_interval": "群里分享间隔未到",
                         "opt_out": "对方不想听", "not_welcomed": "对方还没欢迎过主动分享"}.get(
                gate["reason"].split(":")[0], gate["reason"])
            LOGGER.info("[MonikaPiano] 投递门禁拦截: %s", gate["reason"])
            return f"决定: 这次不发音频（{reason_cn}）。{'可以改天再分享。' if gate.get('quota_limited') else ''}"

        # 真实生成/复奏
        if source == "replay_external" and score_hit:
            return await self._play_score_impl(score_hit, kwargs)
        if source in ("new_original", "variation", "replay_original", "practice_existing"):
            if source in ("replay_original",) and piece_hit:
                return await self._replay_piece(piece_hit, group_id, user_id, stream_id,
                                                chat_id, explicit=explicit)
            if source == "variation" and piece_hit:
                return await self._vary_piece(piece_hit, group_id, user_id, stream_id,
                                              chat_id, explicit=explicit)
            # new_original：按 intent 猜 mood
            mood = DEFAULTS["mood"]
            for alias, mv in MOOD_ALIAS.items():
                if alias in f"{intent} {referent}":
                    mood = mv
                    break
            result = await self._play(mood=mood, seed=int(time.time()) % 100000,
                                      bpm=int(DEFAULTS["bpm"]), bars=int(DEFAULTS["bars"]),
                                      variation=0, stream_id=stream_id or chat_id,
                                      group_id=group_id, user_id=user_id,
                                      explicit=explicit,
                                      source_kind="original")
            return ("决定: 即兴创作并发送。" + result["reply"][:80]) if result["ok"] else \
                   (f"决定: 想弹但没成功（{result['reply'][:40]}）。不能说已经弹过了。")
        if source == "search_external":
            return await self._find_midi_impl(referent or intent)
        return f"决定: {plan['reason']}（无可执行动作）"

    def _music_pref(self, intent: str, chat_id: str, user_id: str) -> str:
        """把用户可验证的音乐偏好写进 music_preferences / delivery_policy。"""
        t = (intent or "").lower()
        admin_ids = [str(u) for u in (self._cfg("admin_user_ids") or [])]
        is_admin = str(user_id) in admin_ids
        if any(w in t for w in ("别发", "不要发", "不想听", "闭麦", "停", "别弹", "不要弹")):
            music_state.set_policy(self._music_db(), chat_id, by=user_id, opt_out=1)
            music_state.add_preference(self._music_db(), chat_id=chat_id, user_id=user_id,
                                       key="opt_out", value=intent[:100], source="explicit")
            return "记下了：这个会话我不会再主动发音乐。"
        if any(w in t for w in ("可以主动", "欢迎", "偶尔发", "随便发", "主动发")):
            music_state.set_policy(self._music_db(), chat_id, by=user_id,
                                   allow_spontaneous=1, opt_out=0)
            music_state.add_preference(self._music_db(), chat_id=chat_id, user_id=user_id,
                                       key="welcome_spontaneous", value=intent[:100],
                                       source="explicit")
            return "记下了：之后有合适的机会我可以偶尔主动分享小曲。"
        if any(w in t for w in ("禁止弹", "禁止发", "禁音", "不准弹")) and is_admin:
            music_state.set_policy(self._music_db(), chat_id, by=user_id, prohibit_audio=1,
                                   prohibit_reason=intent[:100])
            return "管理员禁令已生效：本会话不再发任何音频。"
        if any(w in t for w in ("解除", "恢复", "允许弹")) and is_admin:
            music_state.set_policy(self._music_db(), chat_id, by=user_id, prohibit_audio=0,
                                   opt_out=0)
            return "禁令已解除。"
        music_state.add_preference(self._music_db(), chat_id=chat_id, user_id=user_id,
                                   key="note", value=intent[:100], source="explicit")
        return "记下你的音乐偏好了。"

    async def _do_practice(self, chat_id, user_id, group_id, stream_id, source,
                           target, projects) -> str:
        """练琴 = 真实执行分析/试奏流程并落 practice_sessions；无执行不称'练过'。"""
        analyzed = ""
        out_id = ""
        try:
            if piano_external is not None and target and str(target).startswith("ext_"):
                row = piano_external.get_score(Path(self._music_db()), str(target))
                if row:
                    analyzed = (f"分析《{row['title']}》: {row['note_count']}音符/"
                                f"{row['duration_seconds']:.0f}s/{row['track_count']}轨")
                    out_id = row["score_id"]
            elif piano_engine is not None:
                last = piano_engine.last_piece(str(self._cfg("output_dir")), chat_id)
                if last:
                    analyzed = f"复盘 {last['piece_id']}: {last.get('mood')} {last.get('bpm')}bpm"
                    out_id = last["piece_id"]
            if not analyzed and piano_engine is not None:
                # 真实生成一段练习用变奏（不发送）
                meta = await asyncio.to_thread(
                    piano_engine.generate, mood=DEFAULTS["mood"], bpm=72, bars=4,
                    seed=int(time.time()) % 100000, variation=0, chat_id=chat_id,
                    outdir=str(self._cfg("output_dir")),
                    soundfont=str(self._cfg("soundfont")))
                analyzed = f"练习片段 {meta['piece_id']} (4小节, 未发送)"
                out_id = meta["piece_id"]
        except Exception as exc:  # noqa: BLE001
            return f"练琴失败了（{type(exc).__name__}），今天先不标榜练习成果。"
        ps = music_state.log_practice(self._music_db(), chat_id=chat_id,
                                      input_kind="target", input_id=str(target or ""),
                                      output_kind="analysis", output_id=out_id,
                                      action=source or "practice_existing",
                                      result=analyzed)
        return (f"练琴完成（真实执行）：{analyzed}。记录 {ps['session_id']}。"
                f"未发送音频，想听的话跟我说。")

    async def _replay_piece(self, piece_id, group_id, user_id, stream_id, chat_id, explicit):
        meta = None
        if piano_engine is not None:
            allm = piano_engine.last_piece(str(self._cfg("output_dir")), chat_id)
            if allm and allm.get("piece_id") == piece_id:
                meta = allm
        if meta is None:
            return f"本地目录里没有 {piece_id} 的记录，不能假装记得。"
        wav = await asyncio.to_thread(piano_engine.ensure_wav, meta,
                                      str(self._cfg("soundfont") or ""))
        if not wav:
            return f"{piece_id} 的音频恢复失败，不说'再弹一遍'。"
        sent = await self._send_record(group_id, user_id, f"file://{wav}")
        self._log_play(chat_id, "original", piece_id, explicit=explicit,
                       send_status="sent" if sent else "failed")
        return (f"已复奏 {piece_id} 并发送。" if sent
                else f"复奏 {piece_id} 渲染完成但发送失败，不能说弹过了。")

    async def _vary_piece(self, piece_id, group_id, user_id, stream_id, chat_id, explicit):
        if piano_engine is None:
            return "引擎不可用"
        meta = piano_engine.last_piece(str(self._cfg("output_dir")), chat_id)
        if not meta or meta.get("piece_id") != piece_id:
            # 目录里找不回母曲参数 → 诚实失败
            return f"找不到 {piece_id} 的母曲参数，无法做有依据的变奏。"
        result = await self._play(
            mood=meta.get("mood", DEFAULTS["mood"]), seed=int(meta.get("seed", 0)),
            bpm=int(meta.get("bpm", 74)), bars=int(meta.get("bars", 8)),
            variation=int(meta.get("variation", 0)) + 1,
            stream_id=stream_id or chat_id, group_id=group_id, user_id=user_id,
            explicit=explicit, source_kind="variation", parent_id=piece_id)
        return ("变奏已发送: " + result["reply"][:80]) if result["ok"] else \
               (f"变奏失败: {result['reply'][:40]}")


def create_plugin() -> MonikaPianoPlugin:
    """创建插件实例 (MaiBot 插件加载器入口)。"""
    return MonikaPianoPlugin()
