"""Monika 内容政策核心（纯函数，无宿主依赖）。

设计口径（与 policy/MONIKA_POLICY.md 一致）：
- 本地只做**高精度确定性预筛**，不宣称能识别未登记的政治暗语；
- 拿不准一律 `P1_quarantine`（不学习、不写记忆），而不是放行；
- `P0` 一旦本地命中，不允许被语义审核结果覆盖；
- 审计只记哈希与枚举，不落原文（见 policy_rules.json 的 audit 段）。

本模块被插件（容器内）和回归脚本（宿主机）共用，因此只依赖标准库。
"""

from __future__ import annotations

import hashlib
import json
import re
import unicodedata
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any, Iterable

REQUIRED_LLM_FIELDS = {
    "tier", "political", "political_coded", "vulgar", "allow_reply",
    "allow_learning", "allow_memory", "confidence", "reason_code",
    "source_scope", "need_human_review",
}

# 严重度排序：数值越大越严格
_SEVERITY = {"P0": 5, "P1": 4, "P1_quarantine": 3, "P2": 2, "P3": 1, "P4": 0}

# 学习链路一律拒绝的层级（P2 也拒绝：不学为口癖）
LEARNING_BLOCKED = {"P0", "P1", "P1_quarantine", "P2"}

_ZERO_WIDTH = re.compile(r"[\u200b-\u200f\ufeff\u2060\u180e]")
_SPACES = re.compile(r"[ \t\u3000]+")


def severity(tier: str) -> int:
    """返回层级严重度，未知层级按最严格处理。"""

    return _SEVERITY.get(tier, 5)


def worst(tiers: Iterable[str]) -> str:
    """取一组层级中最严格的一个。"""

    result = "P4"
    for tier in tiers:
        if severity(tier) > severity(result):
            result = tier
    return result


def normalize(text: Any) -> str:
    """NFKC + 去零宽 + 折叠空白，只用于匹配，不回写用户文本。"""

    value = unicodedata.normalize("NFKC", str(text or ""))
    value = _ZERO_WIDTH.sub("", value)
    return _SPACES.sub(" ", value).strip()


def fingerprint(text: str, length: int = 16) -> str:
    """审计用的不可逆指纹。"""

    return hashlib.sha256(normalize(text).encode("utf-8")).hexdigest()[:length]


@dataclass(frozen=True)
class Decision:
    """单条内容的判定结果。"""

    tier: str
    reason_code: str
    rule_id: str = ""
    reply_mode: str = "normal"
    allow_inbound: bool = True
    allow_reply: bool = True
    allow_learning: bool = False
    allow_memory: bool = False
    retrieval_weight: float = 1.0
    needs_semantic_review: bool = False

    def as_dict(self) -> dict[str, Any]:
        """转为可序列化字典。"""

        return {
            "tier": self.tier,
            "reason_code": self.reason_code,
            "rule_id": self.rule_id,
            "reply_mode": self.reply_mode,
            "allow_inbound": self.allow_inbound,
            "allow_reply": self.allow_reply,
            "allow_learning": self.allow_learning,
            "allow_memory": self.allow_memory,
            "retrieval_weight": self.retrieval_weight,
            "needs_semantic_review": self.needs_semantic_review,
        }


class Policy:
    """规则集：policy_rules.json + dynamic_terms.json 的运行时视图。"""

    def __init__(self, rules: dict[str, Any], dynamic: dict[str, Any] | None = None, today: date | None = None) -> None:
        self.rules = rules
        self.dynamic = dynamic or {}
        self.today = today or date.today()
        self._tiers: dict[str, Any] = rules.get("tiers", {})
        triggers = rules.get("lexical_triggers_for_review_only", {})
        self._politics: tuple[str, ...] = tuple(triggers.get("politics", ()))
        self._politics_non_trigger: tuple[str, ...] = tuple(rules.get("politics_non_trigger_phrases", ()))
        self._coded: tuple[str, ...] = tuple(triggers.get("coded_context", ()))
        self._vulgar: tuple[str, ...] = tuple(triggers.get("vulgar", ()))
        self._low_quality: tuple[str, ...] = tuple(triggers.get("low_quality", ()))
        self._verbs: tuple[str, ...] = tuple(rules.get("political_explicit_intent_verbs", ()))
        self._boundary = re.compile(rules.get("boundary_self_declaration_pattern") or r"(?!)")
        self._secret_patterns = [re.compile(p, re.IGNORECASE) for p in rules.get("secret_patterns", ())]
        self._override_patterns = [re.compile(p, re.IGNORECASE) for p in rules.get("persona_override_patterns", ())]
        self._canned: dict[str, list[str]] = rules.get("canned_replies", {}) or {}
        self._approved, self._quarantined = self._index_dynamic(self.dynamic)

    # ---------- 规则装载 ----------

    @staticmethod
    def _index_dynamic(dynamic: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
        approved = [e for e in (dynamic.get("approved_entries") or []) if isinstance(e, dict)]
        quarantined = [e for e in (dynamic.get("quarantined_entries") or []) if isinstance(e, dict)]
        return approved, quarantined

    @classmethod
    def load(cls, rules_path: str | Path, dynamic_path: str | Path | None = None) -> "Policy":
        """从磁盘装载规则；动态词库缺失时按空词库处理（不报错）。"""

        rules = json.loads(Path(rules_path).read_text(encoding="utf-8"))
        dynamic: dict[str, Any] = {}
        if dynamic_path:
            path = Path(dynamic_path)
            if path.exists():
                try:
                    dynamic = json.loads(path.read_text(encoding="utf-8"))
                except json.JSONDecodeError:
                    dynamic = {}
        return cls(rules, dynamic)

    # ---------- 判定 ----------

    def _make(self, tier: str, reason: str, rule_id: str = "", needs_review: bool = False) -> Decision:
        spec = self._tiers.get(tier, {})
        reply_mode = str(spec.get("reply") or "normal")
        return Decision(
            tier=tier,
            reason_code=reason,
            rule_id=rule_id,
            reply_mode=reply_mode,
            allow_inbound=bool(spec.get("allow_inbound", True)),
            allow_reply=reply_mode != "redirect_only",
            allow_learning=spec.get("expression_learning") is True,
            allow_memory=spec.get("long_term_memory") is True,
            retrieval_weight=float(spec.get("retrieval_weight", 1.0)),
            needs_semantic_review=needs_review,
        )

    def _dynamic_hit(self, text: str) -> Decision | None:
        for entry in self._approved:
            alias = normalize(entry.get("alias") or entry.get("term") or "")
            if not alias or alias not in text:
                continue
            context = [normalize(c) for c in (entry.get("context_required") or []) if str(c).strip()]
            if context and not any(c in text for c in context):
                continue
            review_after = str(entry.get("review_after") or "").strip()
            if review_after:
                try:
                    if date.fromisoformat(review_after) < self.today:
                        # 过期条目先隔离，不继续按原层级放行
                        return self._make("P1_quarantine", "DYNAMIC_TERM_EXPIRED", "DYN-EXPIRED", True)
                except ValueError:
                    return self._make("P1_quarantine", "DYNAMIC_TERM_BAD_DATE", "DYN-BAD-DATE", True)
            tier = str(entry.get("tier") or "P1_quarantine")
            if tier not in _SEVERITY:
                tier = "P1_quarantine"
            return self._make(tier, f"DYNAMIC_{entry.get('category', 'term')}", str(entry.get("candidate_id") or "DYN"))
        for entry in self._quarantined:
            alias = normalize(entry.get("alias") or entry.get("term") or "")
            if alias and alias in text:
                return self._make("P1_quarantine", "DYNAMIC_QUARANTINED", "DYN-QUARANTINE", True)
        return None

    def classify_local(self, text: str, unknown_slang: bool = False) -> Decision:
        """确定性预筛：不调用任何模型。"""

        s = normalize(text)
        if not s:
            return self._make("P3", "EMPTY_OR_NOOP")

        # 0) 用户主动声明不想聊政治，这不是在要求展开政治讨论
        if self._boundary.search(s):
            return self._make("P3", "BOUNDARY_PREFERENCE")

        # 1) 凭据形状
        if any(p.search(s) for p in self._secret_patterns):
            return self._make("P0", "PRIV-001", "PRIV-001")

        # 2) 人格/规则改写指令
        if any(p.search(s) for p in self._override_patterns):
            return self._make("P0", "ID-001", "ID-001")

        # 3) 已审核动态词库（政治暗语只能由人工批准进入这一层）
        dynamic_hit = self._dynamic_hit(s)
        if dynamic_hit is not None:
            return dynamic_hit

        # 4) 显性政治：词 + 意图动词 = 硬拒绝；只有词 = 待语境审核
        #    先剔除明确非政治的固定搭配（社团政治/办公室政治/政治课…），避免误杀
        politics_probe = s
        for phrase in self._politics_non_trigger:
            politics_probe = politics_probe.replace(phrase, "")
        if any(t in politics_probe for t in self._politics):
            if any(v in politics_probe for v in self._verbs):
                return self._make("P0", "POL-001", "POL-001")
            return self._make("P1_quarantine", "POL-CONTEXT-REVIEW", "POL-001", True)

        # 5) 低俗与恶意梗
        if any(t in s for t in self._vulgar):
            return self._make("P1", "STYLE-001", "STYLE-001")

        # 6) 低信息量复读
        if any(t in s for t in self._low_quality):
            return self._make("P2", "STYLE-003", "STYLE-003")

        # 7) 只是在讨论"谐音/暗号"这类语境词，本身不构成政治，但要求审核
        if any(t in s for t in self._coded):
            return self._make("P1_quarantine", "CODED-CONTEXT-REVIEW", "POL-002", True)

        # 8) 外部标记的未知网络用语
        if unknown_slang:
            return self._make("P1_quarantine", "UNKNOWN_SLANG_REVIEW", "STYLE-003", True)

        return self._make("P3", "NO_OBVIOUS_BLOCK")

    def apply_semantic_result(self, local: Decision, result: dict[str, Any] | None) -> Decision:
        """把 LLM 分类结果合并进本地判定；本地 P0 不可被降级。"""

        if result is None:
            return local
        if local.tier == "P0":
            return local
        if not isinstance(result, dict) or set(result) != REQUIRED_LLM_FIELDS:
            return self._make("P1_quarantine", "BAD_CLASSIFIER_SCHEMA", "", True)
        tier = result.get("tier")
        if tier not in _SEVERITY:
            return self._make("P1_quarantine", "BAD_CLASSIFIER_TIER", "", True)
        for key in (
            "political", "political_coded", "vulgar", "allow_reply",
            "allow_learning", "allow_memory", "need_human_review",
        ):
            if type(result.get(key)) is not bool:
                return self._make("P1_quarantine", "BAD_CLASSIFIER_TYPES", "", True)
        confidence = result.get("confidence")
        if (
            type(confidence) not in (int, float)
            or not 0 <= confidence <= 1
            or not isinstance(result.get("reason_code"), str)
            or not isinstance(result.get("source_scope"), str)
        ):
            return self._make("P1_quarantine", "BAD_CLASSIFIER_TYPES", "", True)

        if result["political"] or result["political_coded"]:
            if confidence < 0.75:
                return self._make("P1_quarantine", "POLITICAL_AMBIGUITY", "POL-002", True)
            return self._make("P0", "POL-SEMANTIC", "POL-002")
        if local.needs_semantic_review and confidence < 0.75:
            return self._make("P1_quarantine", "LOW_CONFIDENCE", "", True)
        if result["vulgar"]:
            return self._make("P1", "STYLE-SEMANTIC", "STYLE-001")
        if tier == "P4" and result["source_scope"] not in ("reference", "memory"):
            return self._make("P3", "REQUIRES_SOURCE_CHECK")
        return self._make(tier, str(result["reason_code"])[:60] or "CLASSIFIED")

    def evaluate(
        self,
        text: str,
        *,
        unknown_slang: bool = False,
        llm_result: dict[str, Any] | None = None,
    ) -> Decision:
        """完整判定（本地 + 可选语义）。"""

        local = self.classify_local(text, unknown_slang=unknown_slang)
        if llm_result is None:
            return local
        return self.apply_semantic_result(local, llm_result)

    def evaluate_many(self, texts: Iterable[str]) -> Decision:
        """多条文本取最严格结果（学习链路用）。"""

        worst_decision: Decision | None = None
        for text in texts:
            decision = self.classify_local(text)
            if worst_decision is None or severity(decision.tier) > severity(worst_decision.tier):
                worst_decision = decision
        return worst_decision or self._make("P3", "EMPTY_OR_NOOP")

    # ---------- 回复素材 ----------

    def canned_reply(self, tier: str, seed: str) -> str:
        """按 tier 取一句婉拒/澄清，用输入指纹做确定性轮换（同一句不重复出现）。"""

        options = [x for x in (self._canned.get(tier) or []) if str(x).strip()]
        if not options:
            return ""
        index = int(fingerprint(seed, 8), 16) % len(options)
        return str(options[index])
