# -*- coding: utf-8 -*-
"""KB v2 版本化导入器：delete-by-source → import/paste → 回读校验。

背景：
  A-Memorix 段落 source 格式为 web_import:<collection>__<doc_stem>。
  修订知识文档时不能只追加导入（会产生新旧两版并存），必须先按 source
  删除旧段落，再导入新版本，最后回读段落数确认落库。

用法（宿主机 /opt/qq-fish 下）：
    python3 scripts/kb2/kb2_import.py                     # 列出可导入文档
    python3 scripts/kb2/kb2_import.py --doc ddlc_canon/01_人物设定.md           # dry-run 单篇
    python3 scripts/kb2/kb2_import.py --doc ... --apply  # 版本化替换单篇
    python3 scripts/kb2/kb2_import.py --all --apply      # 全部
    python3 scripts/kb2/kb2_import.py --all --delete-only --apply  # 只删不导

约束：
  - 导入走 WebUI paste（主进程内执行），不走 memory_service（单写者锁假成功）。
  - 鉴权是 Cookie maibot_session=<webui.json.access_token>。
  - 删除走 delete/preview + delete/execute（保留 delete_operations 审计）。
  - 每步失败立即停止，不留下"删了没导"的中间态而不报告。
"""
import argparse
import json
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

BASE = "http://127.0.0.1:18001"
ROOT = Path("/opt/qq-fish/data/MaiMBot/a-knowledge")
WEBUI_JSON = Path("/opt/qq-fish/data/MaiMBot/webui.json")
WORKLOG = Path("/opt/qq-fish/docs/kb2/import_log.jsonl")

REASON = "kb_v2 versioned replace (原作纠错与知识覆盖)"


def token():
    return json.loads(WEBUI_JSON.read_text(encoding="utf-8"))["access_token"]


def call(path, payload=None, method="POST"):
    tok = token()
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8") if payload is not None else None
    req = urllib.request.Request(BASE + path, data=body, method=method)
    req.add_header("Content-Type", "application/json")
    req.add_header("Cookie", "maibot_session=" + tok)
    req.add_header("User-Agent", "Mozilla/5.0 kb2-import/1.0")
    req.add_header("Referer", "http://127.0.0.1:18001/")
    req.add_header("Origin", "http://127.0.0.1:18001")
    try:
        with urllib.request.urlopen(req, timeout=120) as resp:
            return resp.status, json.loads(resp.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:400]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def parse_front_matter(text):
    meta, body = {}, text
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


def doc_source_name(rel):
    rel_path = Path(rel)
    return "%s__%s" % (rel_path.parts[0], rel_path.stem)


def log(entry):
    WORKLOG.parent.mkdir(parents=True, exist_ok=True)
    with WORKLOG.open("a", encoding="utf-8") as f:
        f.write(json.dumps(entry, ensure_ascii=False) + "\n")


def delete_by_source(source_name, apply):
    """按 source 删除旧段落。source_name 形如 collection__doc（不含 web_import: 前缀）。
    返回 (ok, preview_count). 无匹配（已删过/新文档）视为通过。"""
    full_source = "web_import:" + source_name
    status, data = call("/api/webui/memory/delete/preview",
                        {"mode": "source", "selector": {"source": full_source}, "reason": REASON})
    if status != 200:
        print("  [delete/preview] HTTP %s %s" % (status, json.dumps(data, ensure_ascii=False)[:200]))
        return False, None
    # 0 匹配：返回 {"success": false, "error": "未命中可删除内容"}，属正常
    if data.get("success") is False and "未命中" in str(data.get("error", "")):
        print("  [delete/preview] 无旧段落（新文档或已替换），跳过删除")
        return True, 0
    counts = data.get("counts", {})
    affected = counts.get("paragraphs", "?")
    rel_n = counts.get("relations", "?")
    print("  [delete/preview] source=%s → paragraphs=%s relations=%s" % (full_source, affected, rel_n))
    if not apply:
        return True, affected
    if affected == 0:
        print("  [delete/execute] 跳过（无可删段落）")
        return True, affected
    status, data = call("/api/webui/memory/delete/execute",
                        {"mode": "source", "selector": {"source": full_source}, "reason": REASON})
    ok = status == 200 and data.get("success") is True
    print("  [delete/execute] HTTP %s success=%s op=%s deleted_p=%s" % (
        status, data.get("success"), data.get("operation_id", "-"),
        data.get("deleted_paragraph_count", "?")))
    return ok, affected


def import_doc(rel, meta, body, apply):
    """导入一篇（含元数据前缀）。返回 (ok, result)."""
    head = "[知识来源] collection=%s | trust=%s | source=%s | updated=%s\n\n" % (
        meta.get("collection", ""), meta.get("trust", ""),
        meta.get("source", ""), meta.get("updated", ""),
    )
    content = head + body
    payload = {
        "content": content,
        "name": doc_source_name(rel),
        "input_mode": "text",
    }
    if not apply:
        print("  [import/dry] name=%s content=%d 字" % (payload["name"], len(content)))
        return True, {"dry": True}
    status, data = call("/api/webui/memory/import/paste", payload)
    ok = status == 200
    print("  [import] HTTP %s %s" % (status, json.dumps(data, ensure_ascii=False)[:220]))
    return ok, data


def verify_count(source_name, wait=8):
    """回读校验：等待异步分块后按 source 统计段落数。"""
    time.sleep(wait)
    # 直接查 sqlite（先复制避免锁）
    import shutil
    import sqlite3
    import tempfile
    db = ROOT.parent / "a-memorix" / "metadata" / "metadata.db"
    tmp = Path(tempfile.mkdtemp()) / "m.db"
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(db) + suffix)
        if src.exists():
            shutil.copy2(src, Path(str(tmp) + suffix))
    conn = sqlite3.connect(str(tmp))
    try:
        cur = conn.execute(
            "SELECT COUNT(*) FROM paragraphs WHERE source=? AND is_deleted=0",
            ("web_import:" + source_name,))
        n = cur.fetchone()[0]
    finally:
        conn.close()
    print("  [verify] web_import:%s → %d 段落" % (source_name, n))
    return n


def list_docs():
    docs = []
    for path in sorted(ROOT.rglob("*.md")):
        rel = path.relative_to(ROOT)
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        docs.append((str(rel).replace("\\", "/"), meta, body))
    return docs


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--doc", action="append", default=[], help="相对 a-knowledge 的文档路径（可多次）")
    ap.add_argument("--all", action="store_true", help="全部文档")
    ap.add_argument("--apply", action="store_true", help="真正执行（默认 dry-run）")
    ap.add_argument("--delete-only", action="store_true", help="只删除旧版不导入")
    ap.add_argument("--skip-verify", action="store_true", help="跳过回读校验")
    args = ap.parse_args()

    docs = list_docs()
    if args.all:
        selected = docs
    elif args.doc:
        wanted = set(args.doc)
        selected = [d for d in docs if d[0] in wanted]
        missing = wanted - {d[0] for d in selected}
        if missing:
            print("未找到文档: %s" % ", ".join(sorted(missing)))
            return 1
    else:
        for rel, meta, body in docs:
            print("  %-44s %-16s %5d 字" % (rel, meta.get("collection", ""), len(body)))
        print("合计 %d 篇。用 --doc/--all 选择，--apply 执行。" % len(docs))
        return 0

    mode = "APPLY" if args.apply else "dry-run"
    print("=== KB2 版本化导入 [%s] 共 %d 篇 ===" % (mode, len(selected)))
    results = []
    for rel, meta, body in selected:
        src = doc_source_name(rel)
        print("── %s (source=web_import:%s)" % (rel, src))
        ok, affected = delete_by_source(src, args.apply)
        if not ok:
            print("  !! 删除失败，中止（避免删了没导）")
            results.append({"doc": rel, "stage": "delete", "ok": False})
            return 1
        if args.delete_only:
            results.append({"doc": rel, "stage": "delete", "ok": True, "affected": affected})
            continue
        ok, res = import_doc(rel, meta, body, args.apply)
        results.append({"doc": rel, "stage": "import", "ok": ok, "result": str(res)[:200]})
        if not ok:
            print("  !! 导入失败，中止")
            return 1
        if args.apply and not args.skip_verify:
            n = verify_count(src)
            results[-1]["verified_paragraphs"] = n
            if n == 0:
                print("  !! 回读 0 段落，导入可能未落库")
                results[-1]["verify_failed"] = True

    log({"ts": time.strftime("%Y-%m-%d %H:%M:%S"), "mode": mode, "results": results})
    print("=== 完成，日志: %s ===" % WORKLOG)
    return 0


if __name__ == "__main__":
    sys.exit(main())
