# -*- coding: utf-8 -*-
"""Monika v5 知识库导入器：把 knowledge/ 下的文档写入 A-Memorix。

四个独立知识集合（方案 MEMORY_REPAIR_PLAN §4）：
    ddlc_canon        DDLC 与 DDLC+ 原作剧情、角色设定
    monika_persona    人格、文学部部长身份、表达风格
    literature_music  文学、诗歌、钢琴与乐理
    content_policy    内容治理规则（P0 不可变）

设计要点：
- 知识与个人经历分离：source_type="knowledge"，tags 带 collection/trust，
  不走 chat_summary / person_fact 通道，不进人物画像。
- 幂等：external_id = knowledge:<collection>:<文件名>，重复导入不产生重复段落。
- 每篇文档带 front-matter（collection / trust / source / updated），满足
  ACCEPTANCE G-41「每条知识有来源、更新时间和可信等级」。
- 入库前只做候选发现，不做自动升级（G-39/42：二创带 fanon 标记、不覆盖 canon）。

用法：
    python3 v5_knowledge_import.py            # dry-run，只列清单
    python3 v5_knowledge_import.py --apply    # 写入 A-Memorix（在 core 容器内跑）
"""
import json
import re
import sys
from pathlib import Path

APPLY = "--apply" in sys.argv
# 容器内与宿主路径不同，自动探测
_CANDIDATE_ROOTS = (
    "/MaiMBot/data/a-knowledge",
    "/opt/qq-fish/data/MaiMBot/a-knowledge",
)
ROOT = next((Path(p) for p in _CANDIDATE_ROOTS if Path(p).exists()), Path(_CANDIDATE_ROOTS[1]))

# collection -> (说明, 是否允许写入)
COLLECTIONS = {
    "ddlc_canon": ("DDLC 与 DDLC+ 原作剧情、角色设定、事件顺序"),
    "monika_persona": ("莫妮卡核心人格、文学部部长身份、表达风格、行为边界"),
    "literature_music": ("文学常识、诗歌形式、写作技巧、钢琴与乐理"),
    "content_policy": ("内容治理规则、黑名单分级、审核口径"),
}

TRUST_LEVELS = {"canon", "official", "fanon", "persona", "policy", "reference"}


def parse_front_matter(text: str) -> tuple[dict, str]:
    """解析文件开头的 YAML 风格 front-matter（简单 key: value）。"""
    meta = {}
    body = text
    if text.startswith("---"):
        parts = text.split("---", 2)
        if len(parts) >= 3:
            for line in parts[1].splitlines():
                if ":" in line:
                    k, v = line.split(":", 1)
                    v = v.split("#")[0].strip().strip('"').strip("'")
                    if v:
                        meta[k.strip()] = v
            body = parts[2].strip()
    return meta, body


def main() -> int:
    if not ROOT.exists():
        print("知识目录不存在：%s" % ROOT)
        return 1

    docs = []
    for path in sorted(ROOT.rglob("*.md")):
        rel = path.relative_to(ROOT)
        collection = rel.parts[0]
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        meta.setdefault("collection", collection)
        meta.setdefault("trust", "reference")
        meta.setdefault("source", "unspecified")
        meta.setdefault("updated", "")
        docs.append({
            "path": str(rel),
            "collection": meta["collection"],
            "trust": meta["trust"],
            "source": meta["source"],
            "updated": meta["updated"],
            "body": body,
            "chars": len(body),
        })

    print("=== 知识库清单 ===")
    print("%-26s %-22s %-10s %6s  %s" % ("collection", "trust", "updated", "chars", "文件"))
    print("-" * 100)
    by_col = {}
    for d in docs:
        by_col.setdefault(d["collection"], []).append(d)
        print("%-26s %-22s %-10s %6d  %s" % (
            d["collection"], d["trust"][:22], d["updated"] or "-", d["chars"], d["path"]))

    print("\n=== collection 覆盖 ===")
    for name, desc in COLLECTIONS.items():
        items = by_col.get(name, [])
        total = sum(i["chars"] for i in items)
        mark = "OK " if items else "空 "
        print("  %s %-18s %d 篇 / %d 字  %s" % (mark, name, len(items), total, desc))
    extra = set(by_col) - set(COLLECTIONS)
    if extra:
        print("  未登记的 collection:", extra)

    bad_trust = [d for d in docs if d["trust"] not in TRUST_LEVELS]
    if bad_trust:
        print("\n[警告] trust 取值不在允许集合 %s:" % sorted(TRUST_LEVELS))
        for d in bad_trust:
            print("   ", d["path"], d["trust"])

    if not APPLY:
        print("\n[dry-run] 未写入。加 --apply 生效（需在 core 容器内运行）。")
        return 0

    # ---- 写入 A-Memorix ----
    try:
        from src.services.memory_service import memory_service
    except Exception as exc:  # noqa: BLE001
        print("导入失败：必须在 core 容器内运行（%s）" % exc)
        return 1

    import asyncio

    async def run() -> int:
        ok = fail = 0
        for d in docs:
            external_id = "knowledge:%s:%s" % (d["collection"], Path(d["path"]).stem)
            tags = [
                "collection:%s" % d["collection"],
                "trust:%s" % d["trust"],
                "knowledge",
            ]
            result = await memory_service.ingest_text(
                external_id=external_id,
                source_type="knowledge",
                text=d["body"],
                tags=tags,
                metadata={
                    "collection": d["collection"],
                    "trust": d["trust"],
                    "source": d["source"],
                    "updated": d["updated"],
                    "doc_path": d["path"],
                    "ingested_by": "v5_knowledge_import",
                },
                respect_filter=True,
            )
            if getattr(result, "success", False):
                ok += 1
                print("  OK   %s" % d["path"])
            else:
                fail += 1
                print("  FAIL %s  %s" % (d["path"], getattr(result, "detail", "")))
        print("\n写入完成：成功 %d / 失败 %d" % (ok, fail))
        return 0 if fail == 0 else 1

    return asyncio.run(run())


if __name__ == "__main__":
    sys.exit(main())
