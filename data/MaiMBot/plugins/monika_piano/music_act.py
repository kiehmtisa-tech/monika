"""Monika 自主音乐行为路由（P8）：两次独立决策的本地实现。

第一层（要不要做音乐）: ignore | talk_music | think_music | practice | offer_audio | send_audio
第二层（用什么资源）:   new_original | variation | replay_original
                        | search_external | replay_external | practice_existing

Planner 只需调 monika_music_act(action=auto|..., intent=..., referent=...,
delivery=auto|...)；本模块用真实作品状态 + 词法信号 + 目录命中做二级路由，
不随机、不按固定频率、不为"活人感"编造练习经历。

词法信号仅用于低成本候选探测（提示 Planner 可以考虑音乐，也可以不弹），
不参与人格判定，不从单次表情推断心理。
"""
from __future__ import annotations

import re

# ------------------------------------------------------------------ 信号探测

# 真实曲名/作曲家线索（外部曲谱路由用）
_COMPOSER_WORDS = ("肖邦", "贝多芬", "莫扎特", "巴赫", "德彪西", "李斯特", "舒伯特",
                   "拉赫玛尼诺夫", "柴可夫斯基", "门德尔松", "勃拉姆斯", "斯卡拉蒂",
                   "chopin", "beethoven", "mozart", "bach", "debussy", "liszt")

_TITLE_MARK = re.compile(r"[《【]([^》】]{1,40})[》】]")

# 创作/灵感线索
_CREATE_WORDS = ("写首", "写一首", "作曲", "创作", "灵感", "旋律", "动机", "写成曲",
                 "谱个", "写段", "新曲", "写歌")
# 修改/续作线索
_VARY_WORDS = ("改一", "改改", "修改", "变奏", "结尾", "续写", "续作", "接着写",
               "重写", "润色", "改一下", "不满意", "换个结尾")
# 复奏/回忆线索
_REPLAY_WORDS = ("再听", "再弹", "重听", "上次那", "之前那", "那首", "旧版", "原版",
                 "再放", "重温", "还记得", "你写的那")
# 显式点播（想听/弹一首）
_REQUEST_WORDS = ("弹一首", "弹个", "弹段", "想听", "给我弹", "来一首", "来段",
                  "放首", "唱首", "听首", "弹琴", "play ")
# 练琴线索
_PRACTICE_WORDS = ("练琴", "练习", "指法", "音阶", "爬音", "基本功", "练一练")
# 音乐话题（只谈不弹）
_TOPIC_WORDS = ("钢琴", "音乐", "曲子", "乐谱", "五线谱", "和弦", "作曲家", "听歌",
                "歌单", "演奏", "弹奏")
# 心情/情境（谨慎：不单独触发）
_EMOTION_WORDS = ("难过", "伤心", "低落", "累", "疲惫", "开心", "高兴", "睡不着",
                  "失眠", "下雨", "雨天", "下雪", "夜晚", "安静")
# 明确拒绝
_REFUSE_WORDS = ("别弹", "不要弹", "别放", "不要放", "别发语音", "不要发语音",
                 "闭麦", "静音", "别唱", "不想听")


def detect_music_signals(text: str) -> dict:
    """词法扫描入站文本 → 候选信号（低成本，无 LLM）。"""
    t = (text or "").lower()
    if not t:
        return {"candidate": False}
    if any(w in t for w in _REFUSE_WORDS):
        return {"candidate": False, "refuse": True}
    sig = {
        "request": any(w in t for w in _REQUEST_WORDS),
        "create": any(w in t for w in _CREATE_WORDS),
        "vary": any(w in t for w in _VARY_WORDS),
        "replay": any(w in t for w in _REPLAY_WORDS),
        "practice": any(w in t for w in _PRACTICE_WORDS),
        "topic": any(w in t for w in _TOPIC_WORDS),
        "emotion": any(w in t for w in _EMOTION_WORDS),
        "composer": next((w for w in _COMPOSER_WORDS if w in t), ""),
        "title": "",
    }
    m = _TITLE_MARK.search(text or "")
    if m:
        sig["title"] = m.group(1).strip()
    sig["candidate"] = any(sig[k] for k in
                           ("request", "create", "vary", "replay", "practice",
                            "composer", "title")) or sig["topic"]
    return sig


# ------------------------------------------------------------------ 路由

def _hit_local_piece(referent: str, pieces_lookup) -> str | None:
    """referent 命中本地原创作品 → piece_id。pieces_lookup(query)->list[dict]。"""
    if not referent:
        return None
    if referent.startswith("piece_"):
        return referent
    hits = pieces_lookup(referent)
    return hits[0]["piece_id"] if hits else None


def _hit_local_score(referent: str, scores_lookup) -> str | None:
    if not referent:
        return None
    if referent.startswith("ext_"):
        return referent
    hits = scores_lookup(referent)
    return hits[0]["score_id"] if hits else None


def route(state: dict) -> dict:
    """二级路由。state 提供真实状态，返回 {behavior, source, reason}。

    state 键：
      signals      : detect_music_signals() 的结果（可以为空）
      referent     : 模型给的曲名/作曲家/ID
      intent       : 模型给的简短意图
      explicit     : 是否用户明确要求弹/听
      piece_hit    : 本地原创命中 (piece_id or None)
      score_hit    : 本地外部曲谱命中 (score_id or None)
      projects     : music_state.active_projects() 结果
      env          : {"group": bool, "busy_topic": bool}
    """
    sig = state.get("signals") or {}
    referent = str(state.get("referent") or "").strip()
    intent = str(state.get("intent") or "").strip().lower()
    explicit = bool(state.get("explicit"))
    piece_hit = state.get("piece_hit")
    score_hit = state.get("score_hit")
    projects = state.get("projects") or []
    env = state.get("env") or {}
    busy = bool(env.get("busy_topic"))
    text_all = f"{referent} {intent}"

    # --- 拒绝 / 无关：不弹 ---
    if sig.get("refuse"):
        return {"behavior": "ignore", "source": "", "reason": "用户明确拒绝音乐"}
    if busy and not explicit:
        # 群里正在热烈讨论别的主题且非明确请求 → 不插音频
        if not sig.get("request"):
            return {"behavior": "ignore", "source": "",
                    "reason": "话题繁忙且无明确请求，不打扰"}

    # --- 明确点播：直接走资源路由并发送 ---
    if explicit or sig.get("request"):
        if score_hit:
            return {"behavior": "send_audio", "source": "replay_external",
                    "reason": f"点播命中外部曲谱 {score_hit}"}
        if piece_hit:
            if sig.get("vary") or any(w in text_all for w in _VARY_WORDS):
                return {"behavior": "send_audio", "source": "variation",
                        "reason": f"要求修改旧作 {piece_hit}"}
            return {"behavior": "send_audio", "source": "replay_original",
                    "reason": f"点播命中本地作品 {piece_hit}"}
        if referent or sig.get("composer") or sig.get("title"):
            return {"behavior": "send_audio", "source": "search_external",
                    "reason": "点播真实曲名/作曲家，本地无谱，查合规来源"}
        return {"behavior": "send_audio", "source": "new_original",
                "reason": "明确要求弹琴，即兴原创"}

    # --- 修改/续作 ---
    if sig.get("vary"):
        if piece_hit:
            return {"behavior": "send_audio", "source": "variation",
                    "reason": f"续作/修改 {piece_hit}"}
        if projects:
            p = projects[0]
            return {"behavior": "send_audio", "source": "variation",
                    "reason": f"续作未完成项目 {p['project_id']}"}
        return {"behavior": "send_audio", "source": "new_original",
                "reason": "想改旧曲但无旧作，重新创作"}

    # --- 复奏/回忆 ---
    if sig.get("replay"):
        if piece_hit:
            return {"behavior": "send_audio", "source": "replay_original",
                    "reason": f"回忆到 {piece_hit}"}
        if score_hit:
            return {"behavior": "send_audio", "source": "replay_external",
                    "reason": f"回忆到 {score_hit}"}
        return {"behavior": "talk_music", "source": "",
                "reason": "提到旧旋律但目录无命中，只谈不发"}

    # --- 练琴（真实执行分析/练习流程，不必然发送） ---
    if sig.get("practice"):
        target = score_hit or piece_hit or (projects[0]["source_id"] if projects else "")
        return {"behavior": "practice", "source": "practice_existing",
                "reason": f"练琴请求，目标={target or '自选'}"}

    # --- 创作灵感（可只谈可创作；不强制发送） ---
    if sig.get("create"):
        if sig.get("emotion") or sig.get("topic"):
            # 文学/意象语境 → think_music：可以只谈，或落库不发
            return {"behavior": "think_music", "source": "new_original",
                    "reason": "创作意象语境，倾向谈/写而不主动发"}
        return {"behavior": "offer_audio", "source": "new_original",
                "reason": "创作动机明确，提出分享"}

    # --- 提到真实作曲家/曲名但未点播：查谱分享或只谈 ---
    if sig.get("composer") or sig.get("title"):
        if score_hit:
            return {"behavior": "offer_audio", "source": "replay_external",
                    "reason": f"提到作品且本地已有 {score_hit}"}
        return {"behavior": "offer_audio", "source": "search_external",
                "reason": "提到真实作品，可找合规曲谱"}

    # --- 有真实未完成项目且话题相关：续作候选 ---
    if projects and (sig.get("topic") or sig.get("emotion")):
        return {"behavior": "think_music", "source": "variation",
                "reason": f"有未完成项目 {projects[0]['project_id']}，话题相关"}

    # --- 纯音乐话题：只谈 ---
    if sig.get("topic"):
        return {"behavior": "talk_music", "source": "",
                "reason": "音乐话题闲聊，不弹"}

    # --- 情绪语境：谨慎，不自动塞音乐 ---
    if sig.get("emotion"):
        return {"behavior": "ignore", "source": "",
                "reason": "情绪语境不自动触发音乐"}

    return {"behavior": "ignore", "source": "", "reason": "无音乐候选"}
