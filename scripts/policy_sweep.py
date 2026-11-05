#!/usr/bin/env python3
"""Monika 记忆存量巡检（只读，绝不改动 A_Memorix 数据）。

背景：A_Memorix 是宿主内置子系统，没有向插件暴露「写入前」Hook，因此新内容靠
`monika_policy` 的入站中止做源头隔离；**存量数据**（历史段落、关系、画像、事实）
需要单独巡检。本脚本只做识别与清单，不做任何删除/改写。

用法：
    python3 scripts/policy_sweep.py                      # 扫描并输出报告（默认脱敏预览 60 字）
    python3 scripts/policy_sweep.py --preview-chars 0    # 只留哈希与长度，不输出任何片段
    python3 scripts/policy_sweep.py --tables paragraphs,relations
    python3 scripts/policy_sweep.py --min-tier P1        # 默认 P1（含 P0/P1/P1_quarantine）

安全口径：
- 先把 metadata.db（含 -wal/-shm）复制到临时目录再读，源库全程只读，不会加写锁；
- 报告里的正文一律经过掩码（命中词替换为 █，密钥整段替换），落盘在
  data/MaiMBot/policy/（不入 git），复核完即可删除；
- 退出码：0 = 未发现 P0/P1；2 = 发现 P0；3 = 仅发现 P1/P1_quarantine。
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
PLUGIN_DIR = ROOT / "data/MaiMBot/plugins/monika_policy"
DB_PATH = ROOT / "data/MaiMBot/a-memorix/metadata/metadata.db"
REPORT_DIR = ROOT / "data/MaiMBot/policy"

REPORTED_TIERS = {"P0", "P1", "P1_quarantine"}
TIER_ORDER = {"P0": 0, "P1": 1, "P1_quarantine": 2, "P2": 3, "P3": 4, "P4": 5}


@dataclass
class Hit:
    """一条命中记录。"""

    table: str
    record_id: str
    tier: str
    reason: str
    field: str
    length: int
    segment_len: int
    preview: str
    source: str
    extra: dict


def load_policy():
    sys.path.insert(0, str(PLUGIN_DIR))
    import importlib

    core = importlib.import_module("policy_core")
    importlib.reload(core)
    policy = core.Policy.load(PLUGIN_DIR / "policy/policy_rules.json", PLUGIN_DIR / "policy/dynamic_terms.json")
    return core, policy


def snapshot_db(source: Path, workdir: Path) -> Path:
    """把 db 与 wal/shm 复制到临时目录，返回副本路径（源库只读）。"""

    target = workdir / source.name
    shutil.copy2(source, target)
    for suffix in ("-wal", "-shm"):
        sidecar = source.with_name(source.name + suffix)
        if sidecar.exists():
            shutil.copy2(sidecar, target.with_name(target.name + suffix))
    return target


class Masker:
    """脱敏预览：命中词与密钥整段替换，限制长度。"""

    def __init__(self, policy, preview_chars: int) -> None:
        self.preview_chars = preview_chars
        triggers = policy.rules.get("lexical_triggers_for_review_only", {})
        terms: list[str] = []
        for group in triggers.values():
            terms.extend(str(t) for t in group)
        for entry in policy.dynamic.get("approved_entries") or []:
            terms.extend(str(a) for a in (entry.get("aliases") or [entry.get("alias")] or []) if a)
        for entry in policy.dynamic.get("quarantined_entries") or []:
            if entry.get("alias"):
                terms.append(str(entry["alias"]))
        self._terms = sorted({t for t in terms if t}, key=len, reverse=True)
        self._secret_patterns = [re.compile(p, re.IGNORECASE) for p in policy.rules.get("secret_patterns", [])]
        self._override_patterns = [re.compile(p, re.IGNORECASE) for p in policy.rules.get("persona_override_patterns", [])]

    def mask(self, text: str) -> str:
        if self.preview_chars <= 0:
            return ""
        out = str(text or "").replace("\n", " ")
        for pattern in self._secret_patterns + self._override_patterns:
            out = pattern.sub("██████", out)
        for term in self._terms:
            out = out.replace(term, "█" * min(len(term), 4))
        out = re.sub(r"\s+", " ", out).strip()
        if len(out) > self.preview_chars:
            out = out[: self.preview_chars] + "…"
        return out


_SENT_SPLIT = re.compile(r"(?<=[。！？!?；;\n])")


def segments(text: str, max_len: int = 200) -> list[str]:
    """短文本整条判定；长文本按句切分，避免长文档里无关用词互相『拼』出 P0。"""

    if len(text) <= max_len:
        return [text]
    parts = [part for part in _SENT_SPLIT.split(text) if part.strip()]
    return parts or [text]


def worst_segment(core, policy, text: str):
    """返回最严重的那一句及其判定（同严重度时取更短的一句，便于定位）。"""

    best_text, best_decision = text, policy.classify_local(text)
    for segment in segments(text):
        decision = policy.classify_local(segment)
        if core.severity(decision.tier) > core.severity(best_decision.tier):
            best_text, best_decision = segment, decision
        elif core.severity(decision.tier) == core.severity(best_decision.tier) and len(segment) < len(best_text):
            best_text, best_decision = segment, decision
    return best_text, best_decision


def acknowledged_ids(policy) -> set[str]:
    """人工已确认为误报的记录，巡检不再重复报告。"""

    items = policy.rules.get("sweep_acknowledged") or []
    return {str(item.get("record_id") or "")[:16] for item in items if isinstance(item, dict)}


def scan(conn: sqlite3.Connection, core, policy, masker: Masker, tables: set[str], limit: int, include_p2: bool) -> list[Hit]:
    """按表扫描并分类。"""

    hits: list[Hit] = []

    acked = acknowledged_ids(policy)

    def judge(table: str, record_id: str, field: str, text: str, extra: dict) -> None:
        if not text or not str(text).strip():
            return
        if str(record_id)[:16] in acked:
            return
        raw = str(text)
        segment, decision = worst_segment(core, policy, raw)
        if decision.tier in REPORTED_TIERS:
            pass
        elif decision.tier == "P2" and include_p2:
            pass
        else:
            return
        hits.append(
            Hit(
                table=table,
                record_id=str(record_id),
                tier=decision.tier,
                reason=decision.reason_code,
                field=field,
                length=len(raw),
                segment_len=len(segment),
                preview=masker.mask(segment),
                source=str(extra.get("source") or ""),
                extra=extra,
            )
        )

    if "paragraphs" in tables:
        query = (
            "select hash, content, source, is_permanent, is_deleted, created_at from paragraphs "
            "where coalesce(is_deleted,0)=0 order by created_at desc"
        )
        for row in conn.execute(query).fetchmany(limit):
            judge("paragraphs", row[0], "content", row[1], {"source": str(row[2])[:60], "permanent": row[3], "created": row[5]})

    if "relations" in tables:
        query = "select hash, subject, predicate, object, source_paragraph, is_permanent from relations"
        for row in conn.execute(query).fetchmany(limit):
            combined = f"{row[1]} {row[2]} {row[3]}"
            judge("relations", row[0], "subject+predicate+object", combined,
                  {"source_paragraph": str(row[4] or "")[:24], "permanent": row[5]})

    if "entities" in tables:
        query = "select hash, name from entities where coalesce(is_deleted,0)=0"
        for row in conn.execute(query).fetchmany(limit):
            judge("entities", row[0], "name", row[1], {})

    if "episodes" in tables:
        query = "select episode_id, title, summary from episodes"
        for row in conn.execute(query).fetchmany(limit):
            judge("episodes", row[0], "title", row[1], {})
            judge("episodes", row[0], "summary", row[2], {})

    if "fact_claims" in tables:
        # 已撤回/失效的 claim 不再参与注入，不计入命中
        query = "select claim_id, value_text, profile_section, status from fact_claims where coalesce(status, 'active') = 'active'"
        for row in conn.execute(query).fetchmany(limit):
            judge("fact_claims", row[0], "value_text", row[1], {"section": row[2], "status": row[3]})

    if "profiles" in tables:
        # 只扫每人最新一版：旧快照是留档，不会被注入
        query = (
            "select snapshot_id, person_id, profile_version, profile_text from person_profile_snapshots s "
            "where profile_version = (select max(profile_version) from person_profile_snapshots x "
            "where x.person_id = s.person_id) order by updated_at desc"
        )
        for row in conn.execute(query).fetchmany(limit):
            judge("profiles", f"{row[0]}", "profile_text", row[3], {"person": str(row[1])[:12], "version": row[2]})

    return hits


def write_report(hits: list[Hit], tables: set[str], preview_chars: int, outdir: Path) -> tuple[Path, Path]:
    """写 markdown 摘要 + JSONL 明细。"""

    outdir.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d_%H%M%S")
    md = outdir / f"sweep_{stamp}.md"
    jsonl = outdir / f"sweep_{stamp}.jsonl"

    by_tier: dict[str, list[Hit]] = {}
    for hit in hits:
        by_tier.setdefault(hit.tier, []).append(hit)

    lines = [
        "# Monika 记忆存量巡检报告",
        "",
        f"- 生成时间：{time.strftime('%Y-%m-%d %H:%M:%S')}",
        f"- 扫描范围：{', '.join(sorted(tables))}",
        f"- 预览长度：{preview_chars} 字（0 = 不输出片段）",
        f"- 命中合计：{len(hits)} 条",
        "- 本报告只用于人工复核，脚本未对 A_Memorix 做任何写入",
        "",
        "| 层级 | 数量 | 含义 | 建议处置 |",
        "|---|---:|---|---|",
    ]
    advice = {
        "P0": "硬拒绝级别：政治/密钥/人格改写。建议人工确认后从记忆库删除（走 WebUI 记忆控制台，不要直接改库）",
        "P1": "不模仿级别：低俗/恶意梗。建议删除或降权，避免被画像引用",
        "P1_quarantine": "语义不确定：先隔离观察，不写入新画像，不必立即删除",
        "P2": "低信息量热梗：保留但降低召回权重，可忽略",
    }
    for tier in ("P0", "P1", "P1_quarantine", "P2"):
        if tier in by_tier:
            lines.append(f"| {tier} | {len(by_tier[tier])} | {advice[tier].split('：')[0]} | {advice[tier]} |")
    lines.append("")

    for tier in ("P0", "P1", "P1_quarantine", "P2"):
        items = by_tier.get(tier)
        if not items:
            continue
        lines.append(f"## {tier}（{len(items)} 条）")
        lines.append("")
        lines.append("| 表 | 记录 ID | 字段 | 来源 | 触发原因 | 总长/句长 | 命中句（脱敏） |")
        lines.append("|---|---|---|---|---|---:|---|")
        for hit in items[:200]:
            preview = hit.preview.replace("|", "\\|") or "（未输出）"
            source = (hit.source or "-").replace("|", "\\|")
            source = source[-46:] if len(source) > 46 else source
            lines.append(
                f"| {hit.table} | `{hit.record_id[:16]}` | {hit.field} | {source} | {hit.reason} "
                f"| {hit.length}/{hit.segment_len} | {preview} |"
            )
        if len(items) > 200:
            lines.append(f"| … | 其余 {len(items) - 200} 条见 JSONL | | | | | |")
        lines.append("")

    lines.append("## 处置方式")
    lines.append("")
    lines.append("1. 本脚本**不会**删除任何数据。确认要清理时，走 WebUI 记忆控制台的删除/回收站，或 `memory_v5_operations` 接口，保留 `delete_operations` 审计。")
    lines.append("2. 删除前先备份：`cp data/MaiMBot/a-memorix/metadata/metadata.db backups/`。")
    lines.append("3. 清理后重跑本脚本，期望 P0/P1 归零。")
    lines.append("")
    md.write_text("\n".join(lines), encoding="utf-8")

    with jsonl.open("w", encoding="utf-8") as fh:
        for hit in hits:
            fh.write(json.dumps(hit.__dict__, ensure_ascii=False) + "\n")
    try:
        jsonl.chmod(0o600)
    except OSError:
        pass
    return md, jsonl


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--tables", default="paragraphs,relations,entities,episodes,fact_claims,profiles")
    parser.add_argument("--preview-chars", type=int, default=60)
    parser.add_argument("--limit", type=int, default=20000)
    parser.add_argument("--outdir", default=str(REPORT_DIR))
    parser.add_argument("--min-tier", default="P1", choices=["P0", "P1"], help="P0 = 只看硬拒绝；P1 = 含 P1/P1_quarantine")
    parser.add_argument("--include-p2", action="store_true", help="把 P2（烂梗）也列入报告")
    args = parser.parse_args()

    if not DB_PATH.exists():
        print(f"找不到记忆数据库: {DB_PATH}")
        return 1
    tables = {t.strip() for t in args.tables.split(",") if t.strip()}

    core, policy = load_policy()
    masker = Masker(policy, args.preview_chars)

    with tempfile.TemporaryDirectory(prefix="policy_sweep_") as tmp:
        copy = snapshot_db(DB_PATH, Path(tmp))
        conn = sqlite3.connect(f"file:{copy}?mode=ro", uri=True)
        try:
            hits = scan(conn, core, policy, masker, tables, args.limit, args.include_p2)
        finally:
            conn.close()

    if args.min_tier == "P0":
        hits = [h for h in hits if h.tier == "P0"]
    else:
        hits = [h for h in hits if h.tier in REPORTED_TIERS or (args.include_p2 and h.tier == "P2")]

    md, jsonl = write_report(hits, tables, args.preview_chars, Path(args.outdir))
    counts: dict[str, int] = {}
    for hit in hits:
        counts[hit.tier] = counts.get(hit.tier, 0) + 1

    print(f"扫描完成：命中 {len(hits)} 条 " + " ".join(f"{k}={v}" for k, v in sorted(counts.items(), key=lambda x: TIER_ORDER.get(x[0], 9))))
    print(f"报告: {md}")
    print(f"明细: {jsonl}")

    if counts.get("P0"):
        return 2
    if counts.get("P1") or counts.get("P1_quarantine"):
        return 3
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
