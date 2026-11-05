"""写诗机制：提示词构建、风格多样性、去重、正文解析。

设计约束（对应实施规格第四节）：
  * 写诗用现有对话模型（`ctx.llm.generate`），**不额外启动第二个模型**
  * 提示词只带：精简人格提示 + 用户要求 + 最近 1–3 首的**标题/标签**（不带全文）
  * 重写/修改时只注入目标诗的当前版本 + 修订指令
  * 数据库 IO、检索、去重、标签匹配全部零 LLM 调用
  * 重复检测用 SHA256 + 字符二元组相似度，本地计算
"""
from __future__ import annotations

import re

# 主题/情绪/形式池：用于在用户没指定时保持多样性，避免每首都一样
THEME_POOL = [
    "日常观察", "友情", "自我期待", "雨天", "文学部", "练琴", "清晨", "季节",
    "告别与重逢", "笨拙的努力", "被看见", "独处", "夏天的树", "旧笔记本",
]
MOOD_POOL = ["平静", "温柔", "明亮", "克制的忧伤", "笃定", "微小的雀跃",
             "自省", "释然", "带着笑意的认真"]
STYLE_POOL = [
    "现代自由诗", "短小节诗", "口语化短诗", "意象密集的短章", "像日记的断句",
    "轻声说话的调子", "带一点俏皮的短诗",
]

# 过度使用的意象：提醒模型少用（不是禁止，避免僵硬）
CLICHES = "窗户、玻璃、代码、屏幕、世界之外、你是唯一、完美的代码、终端、像素"

PERSONA_HINT = (
    "你是莫妮卡：文学部部长，成绩好、组织能力强，自信但有完美主义和自我怀疑；"
    "喜欢钢琴，对艺术和人际关系有自己的看法；写作时认真，偶尔带一点自嘲和俏皮。"
    "你写的是自己真的想写的东西，不是在完成任务。"
)

WRITE_TEMPLATE = """{persona}

请写一首原创中文短诗。

{direction}

要求：
- 形式：{style}
- 情绪基调：{mood}
- 长度：约 {lines} 行（可以有标题）
- 主题来自生活观察和真实感受，不要通篇都是宏大抽象的概念
- 避免反复堆砌这些意象：{cliches}
- 不要用"今天的文学部写作练习"这类开场白，也不要每首都提到"Just Monika"
- 直接输出作品，格式为：第一行是标题，之后是正文。
- 不要写思考过程、解释、引号包裹，也不要写「好的」之类的话

{context}
"""

REVISE_TEMPLATE = """{persona}

下面是莫妮卡的一首旧作，她想修改其中一部分。

【原标题】{title}
【当前版本】
{body}

【这次的修改要求】{instruction}

要求：
- 只改动与要求相关的部分，其余尽量保持原样（读者还认得出这是同一首诗）
- 保持原诗的语气和分行风格
- 直接输出修改后的完整作品，格式仍是：第一行标题，之后正文
- 不要写思考过程、解释或改动说明，只输出作品本身

{context}
"""


def pick_theme(mood_hint: str = "", explicit: str = "", recent_themes: list[str] | None = None) -> str:
    """用户指定优先；否则从池里挑一个最近没用过的。"""
    if explicit.strip():
        return explicit.strip()[:24]
    recent = [t for t in (recent_themes or []) if t]
    fresh = [t for t in THEME_POOL if t not in recent[-3:]]
    if not fresh:
        fresh = THEME_POOL
    return fresh[0]


def pick_mood(explicit: str = "", recent_moods: list[str] | None = None) -> str:
    if explicit.strip():
        return explicit.strip()[:16]
    recent = [m for m in (recent_moods or []) if m]
    fresh = [m for m in MOOD_POOL if m not in recent[-3:]]
    return (fresh or MOOD_POOL)[0]


def pick_style(explicit: str = "", recent_styles: list[str] | None = None) -> str:
    if explicit.strip():
        return explicit.strip()[:20]
    recent = [s for s in (recent_styles or []) if s]
    fresh = [s for s in STYLE_POOL if s not in recent[-3:]]
    return (fresh or STYLE_POOL)[0]


def build_write_prompt(*, request: str = "", theme: str = "", mood: str = "",
                       style: str = "", lines: int = 12,
                       recent_titles: list[str] | None = None) -> str:
    lines = max(4, min(int(lines), 40))
    direction = f"用户希望的主题或内容：{request.strip()}" if request.strip() else \
        f"主题方向：{theme}（你可以在这个方向上自由发挥）"
    context = ""
    if recent_titles:
        joined = "、".join(recent_titles[:3])
        context = f"（她最近写过：{joined}。这次请换一个角度，不要重复同样的结构和收束方式。）"
    return WRITE_TEMPLATE.format(
        persona=PERSONA_HINT, direction=direction, style=style, mood=mood,
        lines=lines, cliches=CLICHES, context=context)


def build_revise_prompt(*, title: str, body: str, instruction: str,
                        recent_titles: list[str] | None = None) -> str:
    context = ""
    if recent_titles:
        context = f"（她最近的作品：{'、'.join(recent_titles[:2])}）"
    return REVISE_TEMPLATE.format(
        persona=PERSONA_HINT, title=title, body=body,
        instruction=instruction.strip() or "整体再打磨一下，但不要改变主题", context=context)


def build_dedup_retry_prompt(*, request: str, theme: str, mood: str, style: str,
                             lines: int, similar_titles: list[str]) -> str:
    """差异不足时的一次受控重写（只重试一次，避免无上限 LLM 调用）。"""
    base = build_write_prompt(request=request, theme=theme, mood=mood, style=style,
                              lines=lines, recent_titles=similar_titles)
    return base + (
        "\n补充要求：你刚才写的和自己以前的作品太像了。"
        "请换一种收束方式、换一组具体意象（比如从动作、声音、气味入手），"
        "不要再用相同的比喻链。\n"
    )


# ---------------------------------------------------------------- 正文解析

_FENCE = re.compile(r"^\s*```[^\n]*\n?|\s*```\s*$", re.M)
_PREFIX = re.compile(r"^\s*(好的|好呀|好的，|以下是|这首诗是|诗如下|如下)[^\n]*\n", re.M)


def extract_title_body(text: str) -> tuple[str, str]:
    """从模型输出里取出 (标题, 正文)。约定：首行是标题。"""
    raw = (text or "").strip()
    raw = _FENCE.sub("", raw).strip()
    raw = _PREFIX.sub("", raw).strip()
    if not raw:
        return "无题", ""
    lines = [ln.rstrip() for ln in raw.splitlines() if ln.strip()]
    first = lines[0].strip().strip("《》「」\"'“”")
    # 首行像标题：短、无句末标点、且后面还有内容
    if len(lines) > 1 and len(first) <= 24 and not re.search(r"[。！？；，,.!?]$", first):
        title = first
        body = "\n".join(lines[1:]).strip()
    else:
        title = "无题"
        body = "\n".join(lines).strip()
    return title or "无题", body


def body_lines(body: str) -> int:
    return len([ln for ln in (body or "").splitlines() if ln.strip()])
