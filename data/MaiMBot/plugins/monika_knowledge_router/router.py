# -*- coding: utf-8 -*-
"""Monika 知识路由：纯函数分类与来源过滤（零依赖，可独立测试）。

设计目标（对应 ChatGPT 审查 Step 2）：
  1. 本地词法规则识别"该查知识"的意图，不加任何 LLM 调用；
  2. 识别范围：DDLC 原作 / Plus! 支线 / 诗歌文学 / 钢琴乐理 / 共同经历；
  3. 检索结果按来源前缀过滤 + 时间线关键词重排，2-4 条进上下文；
  4. 不替代 Planner 的自由检索，只降低"该查却没查"的概率。
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field

# ---- 范围定义 ----------------------------------------------------------------

SCOPE_CANON = "canon"
SCOPE_PLUS = "plus"
SCOPE_POETRY = "poetry"
SCOPE_PIANO = "piano"
SCOPE_EXPERIENCE = "experience"

SCOPE_LABELS = {
    SCOPE_CANON: "DDLC 本篇剧情/设定",
    SCOPE_PLUS: "DDLC Plus! 支线（独立时间线）",
    SCOPE_POETRY: "诗歌与文学",
    SCOPE_PIANO: "钢琴与乐理",
    SCOPE_EXPERIENCE: "与用户的共同经历",
}

# 触发词表：命中任一词即进入该范围（纯词法，区分大小写不敏感）
TRIGGERS: dict[str, tuple[str, ...]] = {
    SCOPE_PLUS: (
        "plus", "支线", "trust", "understanding", "respect", "balance",
        "reflection", "self-love", "equals", "独立时间线",
    ),
    SCOPE_CANON: (
        "纱世里", "夏树", "优里", "文学部", "monika.chr", "角色文件",
        "第四面墙", "学园祭", "act 1", "act 2", "act 3", "act 4", "act1", "act2",
        "act3", "act4", "结局", "真结局", "删除文件", "游戏主角", "mc",
        "诗社", "部员", "青梅竹马",
    ),
    SCOPE_POETRY: (
        "诗", "写诗", "俳句", "十四行", "押韵", "格律", "意象", "断行",
        "诗体", "赏析", "朗诵", "文学", "修辞", "散文诗", "律诗", "绝句",
    ),
    SCOPE_PIANO: (
        "钢琴", "乐理", "和弦", "踏板", "音阶", "练琴", "弹琴", "旋律",
        "动机", "midi", "谱", "曲子", "音符", "节拍", "作曲", "变奏",
        "音名", "织体", "伴奏", "调性", "唱名",
    ),
    SCOPE_EXPERIENCE: (
        "记得", "还记得", "上次", "昨天", "之前", "说过", "聊过", "回忆",
        "我们当时", "那时候", "我跟你讲过", "记不记得", "告诉过", "告诉你过", "记住",
    ),
}

# 可用于预取检索的实体/作品词（命中即加入 query）
ENTITY_LEXICON = (
    "纱世里", "夏树", "优里", "莫妮卡", "Monika", "MC",
    "Hole in Wall", "Save Me", "Happy End",
    "文学部", "学园祭", "monika.chr",
    "俳句", "十四行诗", "格律", "和弦", "踏板",
)

# ---- 来源映射（预取结果的允许来源前缀，按优先级） ----------------------------

SOURCE_MAP: dict[str, tuple[str, ...]] = {
    SCOPE_CANON: (
        "web_import:ddlc_canon__01_", "web_import:ddlc_canon__02_",
        "web_import:ddlc_canon__04_", "web_import:ddlc_canon__05_",
        "web_import:ddlc_canon__06_", "web_import:ddlc_canon__07_",
        "web_import:ddlc_canon__08_", "web_import:ddlc_canon__09_",
        "web_import:ddlc_canon__10_",
    ),
    SCOPE_PLUS: (
        "web_import:ddlc_canon__11_", "web_import:ddlc_canon__03_",
    ),
    SCOPE_POETRY: (
        "web_import:literature_music__01_", "web_import:literature_music__03_",
    ),
    SCOPE_PIANO: (
        "web_import:literature_music__02_", "web_import:literature_music__04_",
    ),
    SCOPE_EXPERIENCE: (
        "person_fact:", "chat_summary:", "episode:", "raw_scan:",
    ),
}

# 时间线/归属词：出现则在该范围内加权（防止主线/Plus 混淆）
TIMELINE_HINTS: dict[str, tuple[str, ...]] = {
    SCOPE_PLUS: ("plus", "支线", "时间线之外", "独立时间线"),
    SCOPE_CANON: ("act", "本篇", "主线"),
    SCOPE_EXPERIENCE: ("用户", "共同", "上次", "说过"),
}

_PUNCT_RE = re.compile(r"[\s，。！？、：；\"'“”‘’（）()【】\[\]<>《》,.!?;:]+")

# ---- 数据结构 ----------------------------------------------------------------


@dataclass
class Route:
    """一次路由判定结果。"""

    scopes: list[str] = field(default_factory=list)
    triggers: list[str] = field(default_factory=list)
    query_terms: list[str] = field(default_factory=list)


@dataclass
class PrefetchItem:
    """一条预取候选（来自 records/search）。"""

    source: str
    content: str
    score: float = 0.0


# ---- 分类 --------------------------------------------------------------------


def classify(text: str) -> Route | None:
    """词法分类；无命中返回 None（普通闲聊，不检索）。"""
    if not text or not text.strip():
        return None
    lower = text.lower()
    hits: dict[str, list[str]] = {}
    for scope, words in TRIGGERS.items():
        matched = [w for w in words if w.lower() in lower]
        if matched:
            hits[scope] = matched

    if not hits:
        return None

    # experience 优先单列（共同经历问题不去预取原作知识）；plus 与 canon 可共存
    scopes: list[str] = []
    if SCOPE_EXPERIENCE in hits and len(hits) == 1:
        scopes = [SCOPE_EXPERIENCE]
    else:
        ordered = [SCOPE_PLUS, SCOPE_CANON, SCOPE_PIANO, SCOPE_POETRY]
        scopes = [s for s in ordered if s in hits]
        if SCOPE_EXPERIENCE in hits:
            scopes.append(SCOPE_EXPERIENCE)
        scopes = scopes[:2]  # 最多两个范围，控制成本

    triggers = [t for s in scopes for t in hits.get(s, [])]
    query_terms = _query_terms(text, triggers)
    return Route(scopes=scopes, triggers=triggers, query_terms=query_terms)


def _query_terms(text: str, triggers: list[str]) -> list[str]:
    """构建预取 query 词：命中触发词 + 词典实体，去重限量。"""
    terms: list[str] = []
    for w in triggers:
        if w not in terms:
            terms.append(w)
    for ent in ENTITY_LEXICON:
        if ent.lower() in text.lower() and ent not in terms:
            terms.append(ent)
    return terms[:6]


# ---- 过滤与重排 --------------------------------------------------------------


def filter_rerank(items: list[dict], scopes: list[str], max_items: int = 4) -> list[PrefetchItem]:
    """按来源前缀过滤 → 时间线加权 → 去重 → 截断。

    items: records/search 返回的条目（含 source/content 字段）。
    """
    allowed: list[str] = []
    for s in scopes:
        allowed.extend(SOURCE_MAP.get(s, ()))
    if not allowed:
        return []

    seen: set[str] = set()
    scored: list[PrefetchItem] = []
    for raw in items:
        source = str(raw.get("source") or raw.get("label") or "")
        # records/search 条目按类型携带不同文本字段：paragraph→content/preview，
        # episode→title+summary，entity/relation→name/description
        content = str(
            raw.get("content") or raw.get("summary") or raw.get("preview")
            or raw.get("description") or raw.get("text") or "")
        title = str(raw.get("title") or raw.get("name") or "")
        if title and title not in content:
            content = (title + "：" + content) if content else title
        if not source or not content:
            continue
        if not any(source.startswith(p) for p in allowed):
            continue
        key = content[:80]
        if key in seen:
            continue
        seen.add(key)
        score = 1.0
        low = content.lower()
        for s in scopes:
            for hint in TIMELINE_HINTS.get(s, ()):
                if hint.lower() in low:
                    score += 0.5
        # 每个允许前缀按顺序给小幅优先级
        for rank, prefix in enumerate(allowed):
            if source.startswith(prefix):
                score += max(0.0, 0.3 - rank * 0.02)
                break
        scored.append(PrefetchItem(source=source, content=content, score=score))

    scored.sort(key=lambda x: -x.score)
    return scored[:max_items]


# ---- 注入文本 ----------------------------------------------------------------


def build_injection(route: Route, picked: list[PrefetchItem], max_chars: int = 900) -> str:
    """构造注入 planner extra_prompt 的文本块。"""
    labels = "、".join(SCOPE_LABELS.get(s, s) for s in route.scopes)
    lines = [f"【知识路由】本条消息涉及：{labels}。以下为内部参考（可信来源），可采用要点但不要照读原文："]
    total = sum(len(lines[0]) for _ in [0])
    for i, item in enumerate(picked, 1):
        excerpt = item.content.strip().replace("\n", " ")
        if len(excerpt) > 160:
            excerpt = excerpt[:160] + "…"
        line = f"{i}. ({item.source}) {excerpt}"
        if total + len(line) > max_chars:
            break
        lines.append(line)
        total += len(line)
    lines.append("提示：需要更深入背景可调用 query_memory；调用时 respect_filter 必须保持 true（false 会绕过聊天隔离，禁止使用）。")
    return "\n".join(lines)
