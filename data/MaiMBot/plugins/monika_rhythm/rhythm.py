"""会话节奏词法判定（纯函数，可脱离 MaiBot 单测）。

设计约束（来自 v3 方案 P2）：
- 只做高置信度的词法判定，含糊消息一律返回 None，交给当前模型理解；
- 判定结果只用于给 replyer 追加一条极短的"节奏参考"，不覆盖、不裁剪模型输出；
- 不新增任何模型调用，纯本地字符串处理。
"""
from __future__ import annotations

import re

# 与 v3 方案的状态表一一对应
PARTING = "PARTING"
ACK_DONE = "ACK_DONE"
SHARING = "SHARING"
EMOTION = "EMOTION"
QUESTION = "QUESTION"
HUMOR = "HUMOR"
NOISE = "NOISE"

STATES = (PARTING, ACK_DONE, SHARING, EMOTION, QUESTION, HUMOR, NOISE)

# 给 replyer 的节奏参考，控制在 40 字以内，避免挤占上下文
HINTS = {
    PARTING: "对方在收尾：只回一句自然道别，不要提问。",
    ACK_DONE: "对方只是在确认已经照做：回一句承接就收住，不要提问、不要重复提醒。",
    SHARING: "对方在分享成果：给一次具体认可或一个好奇，不要复述全篇、不要总结。",
    EMOTION: "对方情绪不好：先接住情绪，不要诊断，也不要连安慰三句。",
    QUESTION: "对方在认真提问：按内容完整回答，不要为了短而截断。",
    HUMOR: "对方在开玩笑：接住梗，别说教。",
    NOISE: "这条不需要实质回应：可以不回或只回极短一句。",
}

_PARTING_RE = re.compile(
    r"(晚安|睡了|先睡|睡觉去|去睡了|拜拜|再见|下线|先走|先撤|撤了|回头聊|回头说|去忙|下了|我先忙|不用回)"
)
_SHARING_RE = re.compile(
    r"(写完|写好了|搞定|弄完|做完|跑通|上线|发版|部署好|通过|提交|交付|搞出来|做出来|修好|装好|完工)"
)
_ACK_RE = re.compile(
    r"(换了|穿好|穿了|吃了|喝了|洗了|到了|到家|回来了|回了|回宿舍|已经|关灯|关好|关了|开了|躺下|收拾好|弄好了|改好了|好了|那就好|好嘞)"
)
_EMOTION_RE = re.compile(
    r"(烦|难受|累死|累了|好累|郁闷|崩溃|emo|EMO|焦虑|委屈|不开心|压力|想哭|丧|失落|烦死|心累)"
)
_QUESTION_RE = re.compile(
    r"(吗[？?]?$|么[？?]?$|帮我|能不能|可不可以|要不要|怎么办|为什么|为啥|如何|是不是|有没有|多少|请问)"
)
_HUMOR_RE = re.compile(r"(哈哈|笑死|离谱|绷不住|笑不活|乐死|😂|🤣|😆)")
_NOISE_RE = re.compile(r"^(\[图片\]|\[表情包\]|\[语音\]|\[文件\]|\[表情\])$")
_NON_WORD_RE = re.compile(r"[\s\W_]+", re.UNICODE)


def _strip_noise(text: str) -> str:
    """去掉空白与纯标点/表情，用于判断"有没有实际文字"。"""
    return _NON_WORD_RE.sub("", text)


def classify(text: str, *, is_picture: bool = False) -> str | None:
    """判定一条入站消息的社交作用。

    Args:
        text: 入站消息的纯文本；可为空。
        is_picture: 是否纯图片消息。

    Returns:
        命中的状态名；含糊或无需判定时返回 None。
    """
    if is_picture:
        return NOISE
    raw = (text or "").strip()
    if not raw:
        return NOISE
    if _NOISE_RE.match(raw):
        return NOISE
    body = _strip_noise(raw)
    if len(body) <= 1:
        return NOISE

    if len(body) <= 30 and _PARTING_RE.search(body):
        return PARTING
    # 以问号/疑问词收尾的短句先判为求助，避免"写完了吗"被当成成果分享
    if len(body) <= 60 and (raw.endswith("?") or raw.endswith("？") or _QUESTION_RE.search(body)):
        return QUESTION
    if _SHARING_RE.search(body):
        return SHARING
    if len(body) <= 24 and _ACK_RE.search(body):
        return ACK_DONE
    if _EMOTION_RE.search(body):
        return EMOTION
    if _HUMOR_RE.search(body):
        return HUMOR
    return None


def build_hint(state: str | None, *, max_chars: int = 60) -> str:
    """把状态转成给 replyer 的一行节奏参考；未知状态返回空串。"""
    if not state:
        return ""
    hint = HINTS.get(state, "")
    if max_chars > 0 and len(hint) > max_chars:
        return hint[:max_chars]
    return hint
