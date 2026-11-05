"""Monika Poetry 插件 —— 诗歌创作、阅读、修改与长期作品库。

两条写诗路径（Token 成本不同，都保留）：
  * `/poem` 斜杠命令：插件用 `ctx.llm.generate` 生成（1 次模型调用，命令本身没有对话轮次可搭）
  * 自然语言："写首诗吧" → **对话模型自己写出诗**，再调 `save_poem` 工具落库
    （**0 次额外模型调用**，数据库 IO/检索/去重全在本地）

本插件不修改 MaiBot 核心，不覆盖钢琴、人设、表情等已有功能。
原作诗歌只做元信息研究索引，**不保存官方诗歌全文**。
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from pathlib import Path

from maibot_sdk import MaiBotPlugin
from maibot_sdk.components import Command, Tool
from maibot_sdk.types import ToolParameterInfo, ToolParamType

try:
    from . import poetry_db
    from . import poetry_engine
except Exception:  # noqa: BLE001
    try:
        import poetry_db          # type: ignore
        import poetry_engine     # type: ignore
    except Exception:  # noqa: BLE001
        poetry_db = None         # type: ignore[assignment]
        poetry_engine = None     # type: ignore[assignment]

LOGGER = logging.getLogger(__name__)

DEFAULTS = {
    "default_mood": "平静",
    "default_lines": 12,
    "max_lines": 40,
    "temperature": 0.95,
    "max_tokens": 2500,
    "llm_timeout_ms": 90000,
    "cooldown_seconds": 45,
    "auto_save": True,
    "similarity_threshold": 0.72,
    "monika_default_visibility": "public",
    "user_default_visibility": "private",
    "allow_users": [],
    "allow_groups": [],
}

ALIASES = {
    "poem": "/poem", "写诗": "/poem", "写首诗": "/poem", "作诗": "/poem",
    "poems": "/poems", "诗集": "/poems", "最近的诗": "/poems",
    "readpoem": "/readpoem", "读诗": "/readpoem", "看诗": "/readpoem",
    "revise": "/revise", "改诗": "/revise", "改一下": "/revise",
    "poemfav": "/poemfav", "收藏这首诗": "/poemfav",
    "poemcanon": "/poemcanon", "原作诗": "/poemcanon",
}

POEM_ID_RE = re.compile(r"^poem_[0-9a-f]{6,12}$")


def _param(name: str, ptype, desc: str, enum=None, required: bool = False):
    return ToolParameterInfo(name=name, param_type=ptype, description=desc,
                             required=required, enum_values=enum)


class MonikaPoetryPlugin(MaiBotPlugin):
    def __init__(self):
        super().__init__()
        self._conf: dict = {}
        self._last_write: dict[str, float] = {}

    # ---------- 生命周期 ----------

    async def on_load(self) -> None:
        self._reload_conf()
        if poetry_db is None:
            LOGGER.error("[MonikaPoetry] poetry_db/poetry_engine 导入失败，插件已禁用")
            return
        try:
            added = poetry_db.init(self._db_dir())
            st = poetry_db.stats(self._db_dir())
            LOGGER.info("[MonikaPoetry] 数据库就绪 %s：作品 %s 首 / 版本 %s / 原作索引 %s 条（本次新增 %s）",
                        self._db_dir(), st["poems"], st["versions"], st["canon_refs"], added)
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[MonikaPoetry] 数据库初始化失败: %s", exc)
        LOGGER.info("[MonikaPoetry] 插件已加载：/poem /poems /readpoem /revise /poemfav /poemcanon")

    async def on_unload(self) -> None:
        LOGGER.info("[MonikaPoetry] 插件已卸载")

    async def on_config_update(self, scope: str, config_data: dict, version: str) -> None:
        try:
            if scope == "poetry" and isinstance(config_data, dict):
                self._conf = config_data
                LOGGER.info("[MonikaPoetry] 配置已热更新")
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaPoetry] 配置热更新失败: %s", exc)

    # ---------- 配置 / 权限 ----------

    def _reload_conf(self) -> None:
        try:
            import tomllib
            cfg = Path(__file__).resolve().parent / "config.toml"
            if cfg.exists():
                with open(cfg, "rb") as f:
                    self._conf = (tomllib.load(f) or {}).get("poetry", {}) or {}
        except Exception as exc:  # noqa: BLE001
            LOGGER.warning("[MonikaPoetry] 配置读取失败，用默认值: %s", exc)

    def _cfg(self, key: str):
        val = self._conf.get(key)
        return val if val is not None else DEFAULTS.get(key)

    def _db_dir(self) -> str:
        try:
            return str(self.ctx.paths.data_dir)
        except Exception:  # noqa: BLE001
            return "/MaiMBot/data/plugins/dragon.monika-poetry"

    def _allowed(self, user_id: str, group_id: str) -> bool:
        users = [str(u) for u in (self._cfg("allow_users") or [])]
        groups = [str(g) for g in (self._cfg("allow_groups") or [])]
        if users and str(user_id) not in users:
            return False
        if group_id and groups and str(group_id) not in groups:
            return False
        return True

    @staticmethod
    def _chat_id(group_id: str, user_id: str) -> str:
        return f"group:{group_id}" if group_id else f"user:{user_id}"

    def _cooldown_ok(self, stream_id: str) -> tuple[bool, float]:
        cd = float(self._cfg("cooldown_seconds") or 0)
        left = cd - (time.monotonic() - self._last_write.get(stream_id, 0.0))
        return left <= 0, max(0.0, left)

    # ---------- 生成 ----------

    async def _llm(self, prompt: str) -> str:
        # 传 timeout_ms 覆盖 RPC 默认的 30s：改诗的提示词更长，30s 不够
        try:
            res = await self.ctx.llm.generate(
                prompt, temperature=float(self._cfg("temperature") or 0.95),
                max_tokens=int(self._cfg("max_tokens") or 1200),
                timeout_ms=int(self._cfg("llm_timeout_ms") or 90000))
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[MonikaPoetry] llm.generate 异常: %s", exc, exc_info=True)
            return ""
        if not isinstance(res, dict):
            LOGGER.error("[MonikaPoetry] llm 返回类型异常: %s", type(res).__name__)
            return ""
        text = (res.get("response") or res.get("content") or "").strip()
        LOGGER.info("[MonikaPoetry] llm 返回: keys=%s success=%s 文本长度=%s "
                    "prompt_tokens=%s completion_tokens=%s%s",
                    list(res.keys()), res.get("success"), len(text),
                    res.get("prompt_tokens"), res.get("completion_tokens"),
                    (" reason=" + str(res.get("reason") or res.get("reasoning"))[:120]
                     if not text else ""))
        return text

    async def _compose(self, *, request: str, theme: str, mood: str, style: str,
                       lines: int, chat_id: str) -> dict:
        """写一首新诗。返回 {title, body, theme, mood, style, similar}。"""
        recent = poetry_db.list_recent(self._db_dir(), chat_id=chat_id, limit=6)
        titles = [r["title"] for r in recent]
        # 用户没指定时从池里挑最近没用过的，保证风格多样
        theme = theme or poetry_engine.pick_theme(request, recent_themes=[r["theme"] for r in recent])
        mood = mood or poetry_engine.pick_mood(recent_moods=[r["mood"] for r in recent])
        style = style or poetry_engine.pick_style(recent_styles=[r["style"] for r in recent])
        prompt = poetry_engine.build_write_prompt(
            request=request, theme=theme, mood=mood, style=style,
            lines=lines, recent_titles=titles)
        raw = await self._llm(prompt)
        title, body = poetry_engine.extract_title_body(raw)

        # 重复检测：本地计算，不额外调模型；最多重试一次
        similar = poetry_db.find_similar(
            self._db_dir(), body, chat_id=chat_id,
            threshold=float(self._cfg("similarity_threshold") or 0.72))
        if similar:
            LOGGER.info("[MonikaPoetry] 与旧作相似度偏高 %s，重写一次", similar)
            retry = poetry_engine.build_dedup_retry_prompt(
                request=request, theme=theme, mood=mood, style=style,
                lines=lines, similar_titles=[s["title"] for s in similar])
            raw2 = await self._llm(retry)
            t2, b2 = poetry_engine.extract_title_body(raw2)
            if b2 and poetry_engine.body_lines(b2) > 0:
                title, body = t2, b2
        return {"title": title, "body": body, "theme": theme,
                "mood": mood, "style": style, "similar": similar, "raw": raw}

    # ---------- 命令 ----------

    @Command(
        "poem",
        description="让莫妮卡写一首原创中文短诗并存入作品库",
        pattern=r"^/poem(?:\s+(?P<args>.+))?\s*$",
        aliases=["写诗", "写首诗", "作诗"],
    )
    async def cmd_poem(self, stream_id: str = "", group_id: str = "", user_id: str = "",
                       matched_groups: dict | None = None, **kwargs):
        if poetry_db is None:
            await self.ctx.send.text("诗稿本还没准备好…等我修一下再写好不好", stream_id)
            return False, "依赖缺失", True
        if not self._allowed(user_id, group_id):
            await self.ctx.send.text("这首诗我现在只想写给特定的人看哦, 抱抱~", stream_id)
            return False, "未授权", True
        ok, left = self._cooldown_ok(stream_id)
        if not ok:
            await self.ctx.send.text(f"刚写完一首, 让我喘口气…过 {int(left)} 秒再写给你", stream_id)
            return False, "冷却中", True

        args = str((matched_groups or {}).get("args", "") or "").strip()
        request, theme, mood, style, lines = self._parse_write_args(args)
        self._last_write[stream_id] = time.monotonic()
        try:
            got = await self._compose(
                request=request, theme=theme, mood=mood, style=style,
                lines=lines, chat_id=self._chat_id(group_id, user_id))
        except Exception as exc:  # noqa: BLE001
            LOGGER.error("[MonikaPoetry] 写诗失败: %s", exc, exc_info=True)
            await self.ctx.send.text("呜…刚才卡住了, 没写出来。等下再试一次好不好", stream_id)
            return False, "生成失败", True

        title, body = got["title"], got["body"]
        if not body:
            LOGGER.warning("[MonikaPoetry] 生成结果为空，未入库。原始输出前 200 字: %r",
                           (got.get("raw") or "")[:200])
            await self.ctx.send.text("我写了个开头, 但不太满意, 先不给你看了…", stream_id)
            return False, "空作品", True

        chat = self._chat_id(group_id, user_id)
        info = poetry_db.create_poem(
            self._db_dir(), title=title, body=body,
            origin="monika_original", author="Monika",
            visibility=str(self._cfg("monika_default_visibility") or "public"),
            owner_or_chat_id=chat, theme=got["theme"] or request[:12],
            mood=got["mood"], style=got["style"],
            change_note="初稿", chat_id=chat)
        poetry_db.log_event(self._db_dir(), info["poem_id"], "shared", chat)
        LOGGER.info("[MonikaPoetry] 已写入 %s《%s》 %s行 主题=%s 情绪=%s 形式=%s 相似重写=%s",
                    info["poem_id"], title, poetry_engine.body_lines(body),
                    got["theme"], got["mood"], got["style"], bool(got["similar"]))
        note = f"\n\n（{info['poem_id']}，已存进我的诗稿本）"
        if got["similar"]:
            note += f"　提醒：和之前那首《{got['similar'][0]['title']}》有点像，我下次换个写法。"
        await self.ctx.send.text(f"{title}\n\n{body}{note}", stream_id)
        return False, f"已写入 {info['poem_id']}", True

    @Command("poems", description="列出莫妮卡最近的作品",
             pattern=r"^/poems(?:\s+(?P<args>.+))?\s*$",
             aliases=["诗集", "最近的诗"])
    async def cmd_poems(self, stream_id: str = "", group_id: str = "", user_id: str = "",
                        matched_groups: dict | None = None, **kwargs):
        if poetry_db is None:
            await self.ctx.send.text("诗稿本还没准备好", stream_id)
            return False, "依赖缺失", True
        args = str((matched_groups or {}).get("args", "") or "").strip()
        fav_only = "收藏" in args or "fav" in args.lower()
        rows = poetry_db.list_recent(self._db_dir(), chat_id=self._chat_id(group_id, user_id),
                                     limit=10, favorite_only=fav_only)
        if not rows:
            await self.ctx.send.text("我还没写过什么诗呢…要不现在写一首? 试试 /poem", stream_id)
            return False, "无作品", True
        head = "这是我最近写的：" + ("（只看收藏）" if fav_only else "")
        lines = [f"{i + 1}. 《{r['title']}》 {r['poem_id']}"
                 + (f"　[{r['theme']}]" if r["theme"] else "")
                 + (" ★" if r["favorite"] else "")
                 for i, r in enumerate(rows)]
        st = poetry_db.stats(self._db_dir())
        LOGGER.info("[MonikaPoetry] /poems 返回 %s 首（收藏筛选=%s）", len(rows), fav_only)
        await self.ctx.send.text(f"{head}\n" + "\n".join(lines) +
                                 f"\n（共 {st['poems']} 首 / {st['versions']} 个版本）", stream_id)
        return False, "列出作品", True

    @Command("readpoem", description="读一首诗（按编号或标题找）",
             pattern=r"^/readpoem(?:\s+(?P<args>.+))?\s*$",
             aliases=["读诗", "看诗"])
    async def cmd_readpoem(self, stream_id: str = "", group_id: str = "", user_id: str = "",
                           matched_groups: dict | None = None, **kwargs):
        if poetry_db is None:
            await self.ctx.send.text("诗稿本还没准备好", stream_id)
            return False, "依赖缺失", True
        args = str((matched_groups or {}).get("args", "") or "").strip()
        if not args:
            await self.ctx.send.text("要读哪一首? 可以给编号或标题，比如 /readpoem poem_xxx 或 /readpoem 雨天",
                                     stream_id)
            return False, "缺参数", True
        poem = self._resolve_poem(args, group_id, user_id)
        if poem is None:
            await self.ctx.send.text("这首诗我找不到…是不是记错了，或者已经删掉了?", stream_id)
            return False, "未找到", True
        poetry_db.log_event(self._db_dir(), poem["poem_id"], "read",
                            self._chat_id(group_id, user_id))
        LOGGER.info("[MonikaPoetry] /readpoem 命中 %s《%s》 v%s", poem["poem_id"],
                    poem["title"], poem.get("current_version", 1))
        meta = f"《{poem['title']}》"
        if poem.get("theme"):
            meta += f"　主题：{poem['theme']}"
        if poem.get("current_version", 1) > 1:
            meta += f"　v{poem['current_version']}"
        await self.ctx.send.text(f"{meta}\n\n{poem.get('body', '')}", stream_id)
        return False, "已读出", True

    @Command("revise", description="修改一首诗（会产生新版本，不覆盖旧稿）",
             pattern=r"^/revise(?:\s+(?P<args>.+))?\s*$",
             aliases=["改诗", "改一下"])
    async def cmd_revise(self, stream_id: str = "", group_id: str = "", user_id: str = "",
                         matched_groups: dict | None = None, **kwargs):
        if poetry_db is None:
            await self.ctx.send.text("诗稿本还没准备好", stream_id)
            return False, "依赖缺失", True
        if not self._allowed(user_id, group_id):
            await self.ctx.send.text("这首诗我现在只想写给特定的人听哦, 抱抱~", stream_id)
            return False, "未授权", True
        args = str((matched_groups or {}).get("args", "") or "").strip()
        parts = args.split(None, 1)
        if not parts:
            await self.ctx.send.text("用法：/revise <诗歌编号> <修改要求>", stream_id)
            return False, "缺参数", True
        poem = self._resolve_poem(parts[0], group_id, user_id)
        if poem is None:
            await self.ctx.send.text("这首诗我找不到…是不是记错了?", stream_id)
            return False, "未找到", True
        instruction = parts[1].strip() if len(parts) > 1 else "整体再打磨一下"
        ok, left = self._cooldown_ok(stream_id)
        if not ok:
            await self.ctx.send.text(f"刚写完一首, 让我喘口气…过 {int(left)} 秒再改", stream_id)
            return False, "冷却中", True

        self._last_write[stream_id] = time.monotonic()
        recent = poetry_db.list_recent(self._db_dir(),
                                       chat_id=self._chat_id(group_id, user_id), limit=3)
        prompt = poetry_engine.build_revise_prompt(
            title=poem["title"], body=poem.get("body", ""), instruction=instruction,
            recent_titles=[r["title"] for r in recent])
        raw = await self._llm(prompt)
        title, body = poetry_engine.extract_title_body(raw)
        if not body:
            await self.ctx.send.text("我试着改了改, 但改得不满意…要不要换个说法告诉我你想改哪里?",
                                     stream_id)
            return False, "修改失败", True

        info = poetry_db.revise_poem(self._db_dir(), poem["poem_id"], body=body,
                                     change_note=instruction[:40],
                                     chat_id=self._chat_id(group_id, user_id))
        if info is None:
            await self.ctx.send.text("保存修改的时候出了点问题…旧版本没被动过，放心", stream_id)
            return False, "保存失败", True
        await self.ctx.send.text(
            f"{title}\n\n{body}\n\n（已存为 v{info['version']}，旧版本还在，随时可以回看）", stream_id)
        return False, f"已存为 v{info['version']}", True

    @Command("poemfav", description="收藏/取消收藏一首诗（收藏的不会被自动清理）",
             pattern=r"^/poemfav(?:\s+(?P<args>.+))?\s*$",
             aliases=["收藏这首诗"])
    async def cmd_poemfav(self, stream_id: str = "", group_id: str = "", user_id: str = "",
                          matched_groups: dict | None = None, **kwargs):
        if poetry_db is None:
            await self.ctx.send.text("诗稿本还没准备好", stream_id)
            return False, "依赖缺失", True
        args = str((matched_groups or {}).get("args", "") or "").strip()
        poem = self._resolve_poem(args, group_id, user_id) if args else None
        if poem is None:
            await self.ctx.send.text("要收藏哪一首? 给我编号或标题，比如 /poemfav poem_xxx", stream_id)
            return False, "未找到", True
        new_state = poetry_db.set_favorite(
            self._db_dir(), poem["poem_id"], not poem.get("favorite", False),
            chat_id=self._chat_id(group_id, user_id))
        LOGGER.info("[MonikaPoetry] /poemfav %s → %s", poem["poem_id"],
                    "收藏" if new_state else "取消收藏")
        msg = f"好, 《{poem['title']}》我特别留着了 ★" if new_state else \
              f"《{poem['title']}》不再特别标记了"
        await self.ctx.send.text(msg, stream_id)
        return False, msg, True

    @Command("poemcanon", description="查原作诗歌的研究索引（只含概述，不含原文）",
             pattern=r"^/poemcanon(?:\s+(?P<args>.+))?\s*$",
             aliases=["原作诗"])
    async def cmd_poemcanon(self, stream_id: str = "", group_id: str = "", user_id: str = "",
                            matched_groups: dict | None = None, **kwargs):
        if poetry_db is None:
            await self.ctx.send.text("诗稿本还没准备好", stream_id)
            return False, "依赖缺失", True
        args = str((matched_groups or {}).get("args", "") or "").strip()
        rows = poetry_db.list_canon(self._db_dir(), keyword=args, limit=8)
        if not rows:
            await self.ctx.send.text("这个方向我没查到相关的原作诗…换个词试试?", stream_id)
            return False, "无结果", True
        out = []
        for r in rows:
            tags = "、".join(r.get("theme_tags") or [])
            out.append(f"《{r['title']}》（{r['timeline']}·{r['chapter']}）[{tags}]\n"
                       f"　{r['summary_zh']}")
        LOGGER.info("[MonikaPoetry] /poemcanon 关键词=%r 命中 %s 条", args, len(rows))
        await self.ctx.send.text(
            "这是我整理的原作诗歌索引，只有我自己的概括，原文我不会拿来复述：\n\n" +
            "\n\n".join(out), stream_id)
        return False, "查到原作索引", True

    # ---------- Tool（自然语言触发，零额外模型调用）----------

    @Tool(
        "save_poem",
        description="把已经写好的诗存进莫妮卡的诗稿本。只在用户明确要求写诗或保存时调用；"
                    "正文必须是模型自己刚写出来的完整作品，用户只让看一眼的文字不要存。",
        parameters=[
            _param("title", ToolParamType.STRING, "标题（不含书名号）", required=True),
            _param("body", ToolParamType.STRING, "诗的正文，保留原有分行", required=True),
            _param("theme", ToolParamType.STRING, "主题，如 雨天/友情/练琴"),
            _param("mood", ToolParamType.STRING, "情绪基调"),
            _param("style", ToolParamType.STRING, "形式，如 现代自由诗"),
            _param("origin", ToolParamType.STRING, "来源",
                   enum=["monika_original", "user_submitted"]),
            _param("tags", ToolParamType.STRING, "标签，逗号分隔"),
        ],
    )
    async def tool_save_poem(self, title: str = "", body: str = "", theme: str = "",
                             mood: str = "", style: str = "", origin: str = "",
                             tags: str = "", **kwargs):
        if poetry_db is None:
            return "诗稿本不可用"
        if not (body or "").strip():
            return "正文是空的，没有保存"
        user_id = str(kwargs.get("user_id", "") or "")
        group_id = str(kwargs.get("group_id", "") or "")
        chat = self._chat_id(group_id, user_id)
        org = origin if origin in ("monika_original", "user_submitted") else "monika_original"
        vis = str(self._cfg("user_default_visibility") if org == "user_submitted"
                  else self._cfg("monika_default_visibility") or "public")
        tag_list = [t.strip() for t in (tags or "").replace("，", ",").split(",") if t.strip()]
        info = poetry_db.create_poem(
            self._db_dir(), title=title, body=body, origin=org,
            visibility=vis, owner_or_chat_id=chat, theme=theme, mood=mood,
            style=style, tags=tag_list, change_note="初稿", chat_id=chat)
        poetry_db.log_event(self._db_dir(), info["poem_id"], "shared", chat)
        return f"已保存：{info['poem_id']}《{info['title']}》v{info['version']}"

    @Tool(
        "find_poem",
        description="按标题/主题/标签/情绪查找莫妮卡诗稿本里的作品，只返回标题和编号，不返回正文。",
        parameters=[_param("query", ToolParamType.STRING, "关键词，如 雨天", required=True)],
    )
    async def tool_find_poem(self, query: str = "", **kwargs):
        if poetry_db is None:
            return "诗稿本不可用"
        user_id = str(kwargs.get("user_id", "") or "")
        group_id = str(kwargs.get("group_id", "") or "")
        rows = poetry_db.search(self._db_dir(), query,
                                chat_id=self._chat_id(group_id, user_id), limit=6)
        if not rows:
            return "没找到相关作品"
        return "；".join(f"{r['poem_id']}《{r['title']}》" for r in rows)

    @Tool(
        "get_poem",
        description="读取一首诗的正文（按编号）。用于讨论、鉴赏或修改前取原文。",
        parameters=[_param("poem_id", ToolParamType.STRING, "作品编号，如 poem_a1b2c3", required=True)],
    )
    async def tool_get_poem(self, poem_id: str = "", **kwargs):
        if poetry_db is None:
            return "诗稿本不可用"
        user_id = str(kwargs.get("user_id", "") or "")
        group_id = str(kwargs.get("group_id", "") or "")
        poem = poetry_db.get_poem(self._db_dir(), poem_id.strip(),
                                  chat_id=self._chat_id(group_id, user_id))
        if poem is None:
            return "没有找到这首诗，或者它不在你可见范围内"
        poetry_db.log_event(self._db_dir(), poem["poem_id"], "read",
                            self._chat_id(group_id, user_id))
        return f"《{poem['title']}》（v{poem['current_version']}）\n{poem.get('body', '')}"

    @Tool(
        "revise_poem",
        description="把修改后的完整诗稿存为新版本（不会覆盖旧稿）。"
                    "new_body 必须是修改后的完整作品。",
        parameters=[
            _param("poem_id", ToolParamType.STRING, "作品编号", required=True),
            _param("new_body", ToolParamType.STRING, "修改后的完整正文", required=True),
            _param("change_note", ToolParamType.STRING, "这次改了什么，一句话"),
        ],
    )
    async def tool_revise_poem(self, poem_id: str = "", new_body: str = "",
                               change_note: str = "", **kwargs):
        if poetry_db is None:
            return "诗稿本不可用"
        user_id = str(kwargs.get("user_id", "") or "")
        group_id = str(kwargs.get("group_id", "") or "")
        if not (new_body or "").strip():
            return "新正文是空的，没有保存"
        info = poetry_db.revise_poem(self._db_dir(), poem_id.strip(), body=new_body,
                                     change_note=change_note[:40],
                                     chat_id=self._chat_id(group_id, user_id))
        if info is None:
            return "找不到这首诗，旧稿没有被动过"
        return f"已存为 v{info['version']}"

    @Tool(
        "link_poem_piano",
        description="把一首诗和一首钢琴曲关联起来（双向可选引用）。只在用户明确要求时调用。",
        parameters=[
            _param("poem_id", ToolParamType.STRING, "作品编号", required=True),
            _param("piece_id", ToolParamType.STRING, "钢琴作品编号，如 piece_1842_melancholy",
                   required=True),
        ],
    )
    async def tool_link_poem_piano(self, poem_id: str = "", piece_id: str = "", **kwargs):
        if poetry_db is None:
            return "诗稿本不可用"
        user_id = str(kwargs.get("user_id", "") or "")
        group_id = str(kwargs.get("group_id", "") or "")
        ok = poetry_db.link_piano_piece(self._db_dir(), poem_id.strip(), piece_id.strip(),
                                        chat_id=self._chat_id(group_id, user_id))
        return "已关联" if ok else "找不到这首诗"

    # ---------- 辅助 ----------

    def _resolve_poem(self, key: str, group_id: str, user_id: str):
        key = (key or "").strip()
        chat = self._chat_id(group_id, user_id)
        if POEM_ID_RE.match(key):
            return poetry_db.get_poem(self._db_dir(), key, chat_id=chat)
        rows = poetry_db.search(self._db_dir(), key, chat_id=chat, limit=1)
        return poetry_db.get_poem(self._db_dir(), rows[0]["poem_id"], chat_id=chat) if rows else None

    @staticmethod
    def _parse_write_args(args: str) -> tuple[str, str, str, str, int]:
        """'/poem [主题/要求] [lines=N] [mood=xx] [style=xx]'"""
        request, theme, mood, style, lines = "", "", "", "", 12
        words = []
        for tok in (args.split() if args else []):
            low = tok.lower()
            if low.startswith("lines="):
                try:
                    lines = max(4, min(40, int(low[6:])))
                except ValueError:
                    pass
            elif low.startswith("mood="):
                mood = tok[5:]
            elif low.startswith("style="):
                style = tok[6:]
            elif low.startswith("theme="):
                theme = tok[6:]
            else:
                words.append(tok)
        request = " ".join(words).strip()
        return request, theme, mood, style, lines


def create_plugin() -> MonikaPoetryPlugin:
    """创建插件实例（MaiBot 插件加载器入口）。"""
    return MonikaPoetryPlugin()
