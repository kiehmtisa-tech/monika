#!/usr/bin/env python3
"""Monika 内容政策周更维护任务（默认不联网、不改正式词库）。

职责边界（与 policy/MAINTAINER_JOB.md 一致）：
- 校验外部 Agent/人工提供的候选，写入 `pending_proposals.jsonl` 等待人工审阅；
- 绝不自动修改 `dynamic_terms.json`，也不自动把政治暗语变成永久硬规则；
- 没有真实来源（https）就直接拒绝，不允许编造「最新词库」。

用法：
    python3 scripts/policy_weekly.py --candidates new_candidates.json
    python3 scripts/policy_weekly.py --candidates-dir inbox/
    python3 scripts/policy_weekly.py --status
    python3 scripts/policy_weekly.py --approve CAND_ID     # 人工复核后执行
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date, datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
POLICY_DIR = ROOT / "data/MaiMBot/plugins/monika_policy/policy"
PENDING = ROOT / "data/MaiMBot/policy/pending_proposals.jsonl"
DYNAMIC = POLICY_DIR / "dynamic_terms.json"


def _load_validator():
    sys.path.insert(0, str(POLICY_DIR))
    import maintainer_proposals  # noqa: PLC0415

    return maintainer_proposals


def validate_file(path: Path) -> tuple[bool, str]:
    validator = _load_validator()
    try:
        item = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return False, f"读取失败: {exc}"
    try:
        validator.validate(item)
    except ValueError as exc:
        return False, str(exc)
    PENDING.parent.mkdir(parents=True, exist_ok=True)
    validator.add_pending(item, PENDING)
    return True, f"已入队待审: {item.get('candidate_id')} → {PENDING}"


def status() -> None:
    print(f"规则目录: {POLICY_DIR}")
    dynamic = json.loads(DYNAMIC.read_text(encoding="utf-8")) if DYNAMIC.exists() else {}
    print(f"动态词库版本: {dynamic.get('version')} 更新于 {dynamic.get('updated_at')}")
    print(f"  已批准: {len(dynamic.get('approved_entries') or [])} 条")
    print(f"  隔离中: {len(dynamic.get('quarantined_entries') or [])} 条")
    if PENDING.exists():
        lines = [ln for ln in PENDING.read_text(encoding="utf-8").splitlines() if ln.strip()]
        print(f"待人工审阅提案: {len(lines)} 条（{PENDING}）")
        for line in lines[-5:]:
            try:
                item = json.loads(line)
                print(f"  - {item.get('candidate_id')} [{item.get('proposed_tier')}] {item.get('category')}")
            except json.JSONDecodeError:
                continue
    else:
        print("待人工审阅提案: 0 条")


def approve(candidate_id: str) -> int:
    """人工确认后把提案写入 approved_entries（唯一允许改正式词库的入口）。"""

    if not PENDING.exists():
        print("没有待审提案")
        return 1
    items = [json.loads(ln) for ln in PENDING.read_text(encoding="utf-8").splitlines() if ln.strip()]
    target = next((i for i in items if i.get("candidate_id") == candidate_id), None)
    if target is None:
        print(f"未找到提案 {candidate_id}")
        return 1
    dynamic = json.loads(DYNAMIC.read_text(encoding="utf-8")) if DYNAMIC.exists() else {}
    approved = list(dynamic.get("approved_entries") or [])
    approved.append(
        {
            "alias": (target.get("aliases") or [""])[0],
            "aliases": target.get("aliases"),
            "category": target.get("category"),
            "tier": target.get("proposed_tier"),
            "context_required": [target.get("context_required")],
            "false_positive_examples": target.get("false_positive_examples"),
            "source": target.get("evidence_source"),
            "review_after": target.get("review_after"),
            "candidate_id": candidate_id,
            "approved_at": datetime.now().strftime("%Y-%m-%dT%H:%M:%S"),
            "approved_by": "human",
        }
    )
    dynamic["approved_entries"] = approved
    dynamic["version"] = f"{date.today().isoformat()}"
    dynamic["updated_at"] = date.today().isoformat()
    DYNAMIC.write_text(json.dumps(dynamic, ensure_ascii=False, indent=2), encoding="utf-8")
    remaining = [i for i in items if i.get("candidate_id") != candidate_id]
    PENDING.write_text("\n".join(json.dumps(i, ensure_ascii=False) for i in remaining) + ("\n" if remaining else ""), encoding="utf-8")
    print(f"已批准 {candidate_id}，动态词库版本 → {dynamic['version']}；插件会在 30 秒内自动重载规则")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--candidates", help="单个候选 JSON 文件")
    parser.add_argument("--candidates-dir", help="候选目录（逐文件校验）")
    parser.add_argument("--status", action="store_true")
    parser.add_argument("--approve", help="人工确认后批准某条候选")
    args = parser.parse_args()

    if args.status:
        status()
        return 0
    if args.approve:
        return approve(args.approve)
    if args.candidates:
        ok, message = validate_file(Path(args.candidates))
        print(message)
        return 0 if ok else 1
    if args.candidates_dir:
        files = sorted(Path(args.candidates_dir).glob("*.json"))
        if not files:
            print("目录内没有候选文件")
            return 1
        failures = 0
        for path in files:
            ok, message = validate_file(path)
            print(f"{'OK  ' if ok else 'FAIL'} {path.name}: {message}")
            failures += 0 if ok else 1
        return 0 if not failures else 1
    parser.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(main())
