# -*- coding: utf-8 -*-
"""通过 MaiBot WebUI 记忆导入 API 写入知识库（主进程内执行，可靠落库）。

为什么不用 memory_service.ingest_text：
  A-Memorix 有单写者锁（.a_memorix_runtime_writer.lock），插件进程与独立脚本
  进程调用 ingest_text 时会 initialize() 失败，返回 success 但 detail=chat_filtered
  或 "A_Memorix 正在初始化"，实际不落库。WebUI 的 /import/paste 在主进程内执行，
  走官方导入流水线（含分块、向量化），是唯一可靠通道。

用法（宿主机上跑）：
    python3 v5_knowledge_webimport.py            # dry-run
    python3 v5_knowledge_webimport.py --apply    # 导入
"""
import json
import sys
import urllib.request
from pathlib import Path

APPLY = "--apply" in sys.argv
BASE = "http://127.0.0.1:18001"
ROOT = Path("/opt/qq-fish/data/MaiMBot/a-knowledge")
WEBUI_JSON = Path("/opt/qq-fish/data/MaiMBot/webui.json")


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


def api(path, payload):
    token = json.loads(WEBUI_JSON.read_text(encoding="utf-8"))["access_token"]
    req = urllib.request.Request(
        BASE + path,
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            # 鉴权走 Cookie（dependencies.require_auth 从 maibot_session 取），
            # Authorization header 不被接受（会 401）
            "Cookie": "maibot_session=" + token,
            "Content-Type": "application/json",
        },
        method="POST",
    )
    with urllib.request.urlopen(req, timeout=30) as resp:
        return json.loads(resp.read().decode("utf-8"))


def main():
    docs = []
    for path in sorted(ROOT.rglob("*.md")):
        rel = path.relative_to(ROOT)
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        meta.setdefault("collection", rel.parts[0])
        docs.append({"rel": str(rel), "meta": meta, "body": body})

    print("=== 待导入知识文档 ===")
    for d in docs:
        print("  %-40s %-16s %5d 字" % (d["rel"], d["meta"].get("collection", ""), len(d["body"])))
    print("合计 %d 篇" % len(docs))

    if not APPLY:
        print("\n[dry-run] 未导入。加 --apply 生效。")
        return 0

    ok = fail = 0
    for d in docs:
        # 把元数据写进正文开头，保证来源/可信等级随内容入库
        head = "[知识来源] collection=%s | trust=%s | source=%s | updated=%s\n\n" % (
            d["meta"].get("collection", ""), d["meta"].get("trust", ""),
            d["meta"].get("source", ""), d["meta"].get("updated", ""),
        )
        content = head + d["body"]
        payload = {
            "content": content,
            "name": "%s__%s" % (d["meta"].get("collection", "kb"), Path(d["rel"]).stem),
            "input_mode": "text",
        }
        try:
            result = api("/api/webui/memory/import/paste", payload)
            task_id = result.get("task_id") or result.get("id") or ""
            status = result.get("status") or "queued"
            ok += 1
            print("  OK   %-40s task=%s status=%s" % (d["rel"], task_id[:12], status))
        except Exception as exc:
            fail += 1
            print("  FAIL %-40s %s" % (d["rel"], exc))
    print("\n导入完成：成功 %d / 失败 %d（任务在主进程异步处理，可查 /api/webui/memory/import/tasks）" % (ok, fail))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
