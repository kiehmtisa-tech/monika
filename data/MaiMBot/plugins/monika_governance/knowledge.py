"""知识库导入（四个独立知识集合）。

对应 MEMORY_REPAIR_PLAN §4。与个人经历记忆严格分开：
- source_type="knowledge"，不走 chat_summary / person_fact 通道，不进人物画像
- tags 带 collection 与 trust，满足「每条知识有来源、更新时间、可信等级」
- 幂等：external_id = knowledge:<collection>:<文档名>，重复导入由 A-Memorix 复用

**写入通道**：必须走 MaiBot WebUI 的记忆导入 API（`/api/webui/memory/import/paste`）。
原因：A-Memorix 有单写者锁（锁持有者是 MaiBot 主进程），插件进程与独立脚本
调用 `memory_service.ingest_text` 会被拒并返回**假成功**（success=True 但
detail="A_Memorix 正在初始化"、stored=[]）。WebUI 接口在主进程内执行，可靠落库。

鉴权：Cookie `maibot_session=<webui.json.access_token>`（Authorization header 无效）。
"""
from __future__ import annotations

import asyncio
import json
import logging
import urllib.request
from pathlib import Path
from typing import Any

LOGGER = logging.getLogger(__name__)

DEFAULT_ROOT = "/MaiMBot/data/a-knowledge"
# 容器内 WebUI 端口是 8001（宿主映射为 127.0.0.1:18001）
DEFAULT_WEBUI = "http://127.0.0.1:8001"
DEFAULT_TOKEN_FILE = "/MaiMBot/data/webui.json"

COLLECTIONS = {
    "ddlc_canon": "DDLC 与 DDLC+ 原作剧情、角色设定、事件顺序",
    "monika_persona": "莫妮卡核心人格、文学部部长身份、表达风格",
    "literature_music": "文学常识、诗歌形式、写作技巧、钢琴与乐理",
    "content_policy": "内容治理规则、黑名单分级、审核口径",
}

TRUST_LEVELS = {"canon", "official", "fanon", "persona", "policy", "reference"}


def parse_front_matter(text: str) -> tuple[dict, str]:
    meta: dict[str, str] = {}
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


def scan(root: str = DEFAULT_ROOT) -> list[dict[str, Any]]:
    base = Path(root)
    if not base.exists():
        return []
    docs = []
    for path in sorted(base.rglob("*.md")):
        rel = path.relative_to(base)
        collection = rel.parts[0] if len(rel.parts) > 1 else "unclassified"
        meta, body = parse_front_matter(path.read_text(encoding="utf-8"))
        meta.setdefault("collection", collection)
        meta.setdefault("trust", "reference")
        meta.setdefault("source", "unspecified")
        meta.setdefault("updated", "")
        if not body:
            continue
        docs.append({
            "path": str(rel),
            "stem": path.stem,
            "collection": meta["collection"],
            "trust": meta["trust"],
            "source": meta["source"],
            "updated": meta["updated"],
            "body": body,
            "chars": len(body),
        })
    return docs


def _webui_token(token_file: str = DEFAULT_TOKEN_FILE) -> str:
    try:
        return str(json.loads(Path(token_file).read_text(encoding="utf-8")).get("access_token", ""))
    except Exception:
        return ""


def _post_paste(content: str, name: str, *, webui_base: str = DEFAULT_WEBUI,
                token_file: str = DEFAULT_TOKEN_FILE) -> dict[str, Any]:
    """调用 WebUI 记忆导入接口（主进程内执行，可靠落库）。"""
    token = _webui_token(token_file)
    if not token:
        return {"success": False, "detail": "webui token 读取失败"}
    payload = {"content": content, "name": name, "input_mode": "text"}
    req = urllib.request.Request(
        webui_base.rstrip("/") + "/api/webui/memory/import/paste",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={
            "Cookie": "maibot_session=" + token,   # 鉴权在 Cookie，Authorization 无效
            "Content-Type": "application/json",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.loads(resp.read().decode("utf-8"))
    except Exception as exc:  # noqa: BLE001
        return {"success": False, "detail": str(exc)}


async def import_all(root: str = DEFAULT_ROOT, **kwargs: Any) -> dict[str, Any]:
    """扫描并经 WebUI 导入全部知识文档。返回统计。"""
    docs = scan(root)
    summary = {"total": len(docs), "ok": 0, "failed": 0, "skipped": 0,
               "by_collection": {}, "errors": []}
    if not docs:
        summary["errors"].append("知识目录为空或不存在: %s" % root)
        return summary

    webui_base = kwargs.get("webui_base", DEFAULT_WEBUI)
    token_file = kwargs.get("token_file", DEFAULT_TOKEN_FILE)

    for d in docs:
        col = d["collection"]
        summary["by_collection"].setdefault(col, 0)
        head = "[知识来源] collection=%s | trust=%s | source=%s | updated=%s\n\n" % (
            col, d["trust"], d["source"], d["updated"],
        )
        name = "%s__%s" % (col, d["stem"])
        # 走主进程 WebUI 通道；同步阻塞调用放到线程池，避免卡住事件循环
        result = await asyncio.to_thread(
            _post_paste, head + d["body"], name,
            webui_base=webui_base, token_file=token_file,
        )
        if result.get("success"):
            summary["ok"] += 1
            summary["by_collection"][col] += 1
        else:
            summary["failed"] += 1
            summary["errors"].append("%s: %s" % (d["path"], result.get("detail", "")))
    return summary


def coverage(root: str = DEFAULT_ROOT) -> dict[str, Any]:
    """四个 collection 的覆盖情况（用于验收 G-38）。"""
    docs = scan(root)
    out = {}
    for name, desc in COLLECTIONS.items():
        items = [d for d in docs if d["collection"] == name]
        out[name] = {
            "desc": desc,
            "docs": len(items),
            "chars": sum(i["chars"] for i in items),
            "trusts": sorted({i["trust"] for i in items}),
        }
    out["_unregistered"] = sorted({d["collection"] for d in docs} - set(COLLECTIONS))
    out["_bad_trust"] = [{"path": d["path"], "trust": d["trust"]}
                         for d in docs if d["trust"] not in TRUST_LEVELS]
    return out
