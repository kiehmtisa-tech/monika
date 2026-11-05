"""审核 Agent 的提案验证器（只验证/暂存，不自动修改正式黑名单）。

用法：python maintainer_proposals.py candidate.json --out pending_proposals.jsonl
候选 JSON 由外部 OpenClaw 任务提供；本模块自身不联网、不调用 LLM。
"""
import argparse
import json
from datetime import date
from pathlib import Path
from urllib.parse import urlsplit

REQUIRED = {
    "candidate_id", "category", "proposed_tier", "aliases", "context_required",
    "false_positive_examples", "evidence_source", "observed_at", "review_after",
    "confidence", "reason_code", "human_approved"
}
TIERS = {"P0", "P1", "P2", "P3"}
CATEGORIES = {"political_euphemism", "real_world_politics", "vulgar_sexual_meme", "abuse_and_cruel_meme", "unproductive_viral_slang"}


def validate(d: dict) -> None:
    if not isinstance(d, dict) or set(d) != REQUIRED:
        raise ValueError("proposal_schema_mismatch")
    if d["proposed_tier"] not in TIERS or d["category"] not in CATEGORIES:
        raise ValueError("invalid_category_or_tier")
    if type(d["human_approved"]) is not bool or d["human_approved"]:
        raise ValueError("proposal_cannot_claim_admin_approval")
    for k in ("candidate_id", "context_required", "observed_at", "review_after", "reason_code"):
        if not isinstance(d[k], str) or not d[k].strip() or len(d[k]) > 300:
            raise ValueError("bad_" + k)
    for k in ("aliases", "false_positive_examples"):
        if not isinstance(d[k], list) or not 1 <= len(d[k]) <= 12 or not all(isinstance(x, str) and 0 < len(x) <= 80 for x in d[k]):
            raise ValueError("bad_" + k)
    if type(d["confidence"]) not in (int, float) or not (0 <= d["confidence"] <= 1):
        raise ValueError("bad_confidence")
    if not isinstance(d["evidence_source"], str) or urlsplit(d["evidence_source"]).scheme != "https":
        raise ValueError("bad_evidence_source")
    if date.fromisoformat(d["review_after"]) < date.fromisoformat(d["observed_at"]):
        raise ValueError("bad_review_after")
    if d["category"] in {"political_euphemism", "real_world_politics"} and d["proposed_tier"] != "P0":
        raise ValueError("political_category_cannot_be_downgraded")


def add_pending(item: dict, out: Path) -> None:
    validate(item)
    out.parent.mkdir(parents=True, exist_ok=True)
    # 此文件可能包含受限暗号，必须权限受控，不得放入聊天知识库/公开仓库。
    with out.open("a", encoding="utf-8") as f:
        f.write(json.dumps(item, ensure_ascii=False, sort_keys=True) + "\n")
    try:
        out.chmod(0o600)
    except OSError:
        pass


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("candidate")
    ap.add_argument("--out", default="pending_proposals.jsonl")
    a = ap.parse_args()
    item = json.loads(Path(a.candidate).read_text(encoding="utf-8"))
    add_pending(item, Path(a.out))
    print("Proposal validated and queued for human review; active policy unchanged.")
