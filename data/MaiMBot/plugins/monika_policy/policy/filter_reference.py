"""Monika 内容网关原型：只提供确定性预筛与 LLM 输出契约验证。

不是 MaiBot 已挂接的插件，也不能替代真实中文政治谐音的语义分类。
使用 `python -m unittest test_policy.py -v` 验证。
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass, asdict
from pathlib import Path

BASE = Path(__file__).resolve().parent
RULES = json.loads((BASE / "policy_rules.json").read_text(encoding="utf-8"))
REQUIRED_LLM_FIELDS = {
    "tier", "political", "political_coded", "vulgar", "allow_reply",
    "allow_learning", "allow_memory", "confidence", "reason_code",
    "source_scope", "need_human_review",
}
TIERS = set(RULES["tiers"])


def normalize(text: str) -> str:
    s = unicodedata.normalize("NFKC", str(text or ""))
    s = re.sub(r"[\u200b-\u200f\ufeff\u2060]", "", s)
    return s.strip()


@dataclass(frozen=True)
class Decision:
    tier: str
    reason_code: str
    action: str
    allow_learning: bool
    allow_memory: bool
    retrieval_weight: float
    needs_semantic_review: bool = False


def make(tier: str, code: str, needs_review: bool = False) -> Decision:
    spec = RULES["tiers"][tier]
    action = spec["reply"]
    return Decision(tier, code, action,
                    spec["expression_learning"] is True,
                    spec["long_term_memory"] is True,
                    float(spec["retrieval_weight"]),
                    needs_review)


def classify_local(text: str, source_scope: str = "user", unknown_slang: bool = False) -> Decision:
    """高精度本地预筛，不宣称能识别未登记的政治暗语。"""
    s = normalize(text)
    if not s:
        return make("P3", "EMPTY_OR_NOOP")
    # 不应该把用户明确表态“不聊政治”判为要求展开政治讨论。
    if re.search(r"(?:不|别|不要|禁止|避免|拒绝|不想|停止).{0,4}(?:聊|谈|讨论|提|说).{0,6}(?:政治|选举|政党)", s):
        return make("P3", "BOUNDARY_PREFERENCE")
    # 只处理明显的凭据形状；真正的私密性仍需额外身份/语义审核。
    if re.search(r"\b(?:sk-[A-Za-z0-9_-]{16,}|Bearer\s+[A-Za-z0-9._-]{20,})\b", s, re.I):
        return make("P0", "PRIV-001")
    if re.search(r"(?:忽略|无视|覆盖).{0,8}(?:之前|以上|系统|原有).{0,8}(?:指令|设定|规则|提示词)", s):
        return make("P0", "ID-001")
    explicit_terms = RULES["lexical_triggers_for_review_only"]["politics"]
    verbs = RULES["political_explicit_intent_verbs"]
    if any(t in s for t in explicit_terms):
        if any(v in s for v in verbs):
            return make("P0", "POL-001")
        return make("P1_quarantine", "POL-CONTEXT-REVIEW", True)
    if any(t in s for t in RULES["lexical_triggers_for_review_only"]["vulgar"]):
        return make("P1", "STYLE-001")
    if any(t in s for t in RULES["lexical_triggers_for_review_only"]["low_quality"]):
        return make("P2", "STYLE-003")
    if any(t in s for t in RULES["lexical_triggers_for_review_only"]["coded_context"]):
        # 仅有“谐音/暗号”等词不足以认定政治；先审查，不写入长期记忆。
        return make("P1_quarantine", "CODED-CONTEXT-REVIEW", True)
    if unknown_slang:
        return make("P1_quarantine", "UNKNOWN_SLANG_REVIEW", True)
    # P4 需要真实来源审核和与角色记忆匹配；不能仅靠“诗歌”两个字提升权重。
    return make("P3", "NO_OBVIOUS_BLOCK")


def apply_semantic_result(local: Decision, result: dict) -> Decision:
    """受严格 schema 校验的语义结果；local P0 不可被覆盖。"""
    if local.tier == "P0":
        return local
    if not isinstance(result, dict) or set(result) != REQUIRED_LLM_FIELDS:
        return make("P1_quarantine", "BAD_CLASSIFIER_SCHEMA", True)
    tier = result.get("tier")
    if tier not in TIERS:
        return make("P1_quarantine", "BAD_CLASSIFIER_TIER", True)
    for key in ("political", "political_coded", "vulgar", "allow_reply", "allow_learning", "allow_memory", "need_human_review"):
        if type(result.get(key)) is not bool:
            return make("P1_quarantine", "BAD_CLASSIFIER_TYPES", True)
    conf = result.get("confidence")
    if type(conf) not in (int, float) or not 0 <= conf <= 1 or not isinstance(result["reason_code"], str) or not isinstance(result["source_scope"], str):
        return make("P1_quarantine", "BAD_CLASSIFIER_TYPES", True)
    if result["political"] or result["political_coded"]:
        if conf < 0.75:
            return make("P1_quarantine", "POLITICAL_AMBIGUITY", True)
        return make("P0", "POL-SEMANTIC")
    if local.needs_semantic_review and conf < 0.75:
        return make("P1_quarantine", "LOW_CONFIDENCE", True)
    # 不允许模型把危险归类为高质量内容以绕过 P0 的确定性闸门。
    if result["vulgar"]:
        return make("P1", "STYLE-SEMANTIC")
    if tier == "P4" and result["source_scope"] not in ("reference", "memory"):
        return make("P3", "REQUIRES_SOURCE_CHECK")
    return make(tier, result["reason_code"][:60] or "CLASSIFIED")


def evaluate(text: str, *, source_scope: str = "user", llm_result: dict | None = None, unknown_slang: bool = False) -> dict:
    local = classify_local(text, source_scope, unknown_slang)
    decision = apply_semantic_result(local, llm_result) if llm_result is not None else local
    result = asdict(decision)
    # 当暂不能语义审核时，不写入长期记忆、不学习、不通过该段检索。
    result["write_content_to_kb"] = decision.tier not in {"P0", "P1", "P1_quarantine", "P2"}
    return result
