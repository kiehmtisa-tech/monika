#!/usr/bin/env python3
"""Monika 记忆定向清理（走 WebUI 官方删除/撤回接口，保留 delete_operations 审计）。

默认 dry-run，只调用 `delete/preview`；确认后加 `--execute` 才真正删除。
不直接改 sqlite：官方接口会同步清理向量、图谱与来源映射，并写入删除审计。

用法（在 core 容器内跑，因为要访问 127.0.0.1:8001）：
    # 1) 预览
    python3 policy_cleanup.py --paragraph <full_hash> --claim <claim_id>
    # 2) 执行
    python3 policy_cleanup.py --paragraph <full_hash> --claim <claim_id> --execute

    # 也支持按来源整源删除（谨慎，会删该来源全部段落）
    python3 policy_cleanup.py --source "person_fact:<person_id>" --execute
"""

from __future__ import annotations

import argparse
import json
import sys
import urllib.error
import urllib.request
from pathlib import Path

WEBUI_JSON = Path("/MaiMBot/data/webui.json")
BASE_CANDIDATES = ("http://127.0.0.1:8001/api/webui", "http://127.0.0.1:8001")
REASON_DEFAULT = "内容政策 P1：低俗内容不写入长期记忆（policy_rules v1.2 / MONIKA_POLICY.md）"


def token() -> str:
    data = json.loads(WEBUI_JSON.read_text(encoding="utf-8"))
    value = str(data.get("access_token") or "").strip()
    if not value:
        raise SystemExit("webui.json 里没有 access_token")
    return value


def call(base: str, tok: str, path: str, payload: dict | None = None, method: str = "POST") -> tuple[int, dict]:
    body = json.dumps(payload).encode("utf-8") if payload is not None else None
    request = urllib.request.Request(f"{base}{path}", data=body, method=method)
    request.add_header("Content-Type", "application/json")
    request.add_header("Cookie", f"maibot_session={tok}")
    request.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36")
    request.add_header("Referer", "http://127.0.0.1:8001/")
    request.add_header("Origin", "http://127.0.0.1:8001")
    try:
        with urllib.request.urlopen(request, timeout=120) as response:
            return response.status, json.loads(response.read().decode("utf-8") or "{}")
    except urllib.error.HTTPError as exc:
        raw = exc.read().decode("utf-8", "replace")
        try:
            return exc.code, json.loads(raw)
        except json.JSONDecodeError:
            return exc.code, {"raw": raw[:400]}
    except Exception as exc:  # noqa: BLE001
        return 0, {"error": str(exc)}


def resolve_base(tok: str) -> str:
    for base in BASE_CANDIDATES:
        status, _ = call(base, tok, "/memory/config", method="GET")
        if status == 200:
            return base
    raise SystemExit("无法连接 WebUI 记忆接口（尝试过 /api 与根路径）")


def preview(base: str, tok: str, mode: str, selector: dict, reason: str) -> dict:
    status, data = call(base, tok, "/memory/delete/preview", {"mode": mode, "selector": selector, "reason": reason})
    print(f"[preview] mode={mode} http={status}")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
    return data


def execute(base: str, tok: str, mode: str, selector: dict, reason: str) -> dict:
    status, data = call(base, tok, "/memory/delete/execute", {"mode": mode, "selector": selector, "reason": reason})
    print(f"[execute] mode={mode} http={status}")
    print(json.dumps(data, ensure_ascii=False, indent=2)[:2000])
    return data


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--paragraph", action="append", default=[], help="段落全哈希（可多次）")
    parser.add_argument("--claim", action="append", default=[], help="事实 claim_id（可多次）")
    parser.add_argument("--source", default="", help="按来源删除（如 person_fact:<person_id>）")
    parser.add_argument("--reason", default=REASON_DEFAULT)
    parser.add_argument("--execute", action="store_true", help="真正执行（默认只预览）")
    args = parser.parse_args()

    if not (args.paragraph or args.claim or args.source):
        parser.print_help()
        return 1

    tok = token()
    base = resolve_base(tok)
    print(f"接口根路径: {base}")

    ok = True
    if args.paragraph or args.source:
        selector = {"hashes": args.paragraph} if args.paragraph else {}
        if args.source:
            selector = {"source": args.source}
        mode = "source" if args.source else "paragraph"
        preview(base, tok, mode, selector, args.reason)
        if args.execute:
            result = execute(base, tok, mode, selector, args.reason)
            ok = ok and bool(result.get("success"))

    for claim_id in args.claim:
        if args.execute:
            status, data = call(base, tok, f"/memory/facts/{claim_id}/retract", {"reason": args.reason, "requested_by": "policy-cleanup"})
            print(f"[retract] claim={claim_id[:12]}… http={status}")
            print(json.dumps(data, ensure_ascii=False, indent=2)[:800])
            ok = ok and status == 200 and bool(data.get("success", True))

    print("完成" if args.execute else "（dry-run，未做任何修改）")
    return 0 if ok else 2


if __name__ == "__main__":
    sys.exit(main())
