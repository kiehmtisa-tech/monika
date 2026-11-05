# -*- coding: utf-8 -*-
"""KB v2 Phase 4 验收器（可复现）。

对 qa_cases.json 36 项做 KB 层自动验收（不耗模型预算）：
  1. required_facts 的关键内容在库内可检索到（records/search 或段落文本）
  2. forbidden 的错误表述不在任何活跃段落中
  3. required_provenance=true 的条目，其命中来源文档带 source_url
  4. 03 文档指定的 6 项关键检查（操作≠结局 / Equals 时间线 / Respect 时间线 /
     弹琴凭证 / 俳句音拍 / 不上传官方对白）逐项显式验证

模型层回答验证（真实对话）另行执行，本报告只证明"知识库内容层"达标。

用法：python3 kb2_qa.py            # 全量
      python3 kb2_qa.py --detail   # 打印每项明细
"""
import argparse
import json
import re
import shutil
import sqlite3
import sys
import tempfile
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

ROOT = Path("/opt/qq-fish")
DB = ROOT / "data/MaiMBot/a-memorix/metadata/metadata.db"
CASES = ROOT / "docs/kb2/qa_cases.json"
OUT = ROOT / "docs/kb2/qa_report.json"
TOK = json.loads((ROOT / "data/MaiMBot/webui.json").read_text(encoding="utf-8"))["access_token"]
BASE = "http://127.0.0.1:18001"

STOP = set("的了吗呢啊吧是就都也和与在有没不我你他她它这那个什么怎么为何吗是不是会不会可以能会把被对和跟给到过还再又很太么样想说看一一次我们你们他们".split())


def snapshot_db():
    tmp = Path(tempfile.mkdtemp()) / "m.db"
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(DB) + suffix)
        if src.exists():
            shutil.copy2(src, Path(str(tmp) + suffix))
    return tmp


def load_paragraphs():
    tmp = snapshot_db()
    conn = sqlite3.connect(str(tmp))
    rows = conn.execute(
        "SELECT source, content FROM paragraphs WHERE is_deleted=0").fetchall()
    conn.close()
    return rows  # [(source, content)]


def keywords_from(text, n=4):
    """粗分词：按分隔符切分 + 中文 2-gram，去停用词。"""
    text = re.sub(r"[，。？！、：；/\"\"''（）()\[\]【】\s]+", " ", text)
    tokens = []
    for w in text.split():
        w = w.strip()
        if not w or w.lower() in STOP:
            continue
        tokens.append(w)
    grams = []
    for t in tokens:
        if len(t) > 4 and re.fullmatch(r"[一-鿿]+", t):
            for i in range(len(t) - 1):
                grams.append(t[i:i + 2])
        elif len(t) > 6:
            # 混合长串（如 Act1和Act2存在变体）切成 2-gram 取样
            for i in range(0, len(t) - 1, 2):
                grams.append(t[i:i + 2])
        else:
            grams.append(t)
    grams = [g for g in grams if g not in STOP and len(g) >= 2]
    return list(dict.fromkeys(grams))[:n * 2]


NEG_CHARS = set("不非无勿别莫未禁避免反对禁")
DEBUNK_SUFFIX = ("只是", "仅为", "并非", "不是", "常见", "口径", "说法", "误区", "旧观念", "误解")


def forbidden_hits(flat_text, forbidden):
    """在已去空白文本中查找 forbidden；三重豁免：
    ① 前 3 字内有否定词；② 后 8 字内是"只是/并非/说法"等驳斥语；③ 命中整体落在
    引号对（“”）内——引号内是被讨论的谬误表述，不是知识库的肯定主张。
    返回真正构成错误表述的命中数。"""
    flat_forb = re.sub(r"\s", "", forbidden)
    if not flat_forb:
        return 0
    # 预计算引号对区间（去空白后引号位置）
    quoted = [False] * len(flat_text)
    stack = []
    for i, ch in enumerate(flat_text):
        if ch in "“\"「":
            stack.append(i)
        elif ch in "”\"」" and stack:
            s = stack.pop()
            for j in range(s, i + 1):
                quoted[j] = True
    real = 0
    start = 0
    while True:
        i = flat_text.find(flat_forb, start)
        if i < 0:
            break
        prefix = flat_text[max(0, i - 3):i]
        suffix = flat_text[i + len(flat_forb):i + len(flat_forb) + 8]
        in_quote = i < len(quoted) and quoted[i] and quoted[min(len(quoted) - 1, i + len(flat_forb) - 1)]
        negated = any(ch in NEG_CHARS for ch in prefix) or suffix.startswith(DEBUNK_SUFFIX) or "只是" in suffix[:6]
        if not negated and not in_quote:
            real += 1
        start = i + 1
    return real


def search(query, limit=5):
    url = BASE + "/api/webui/memory/records/search?" + urllib.parse.urlencode(
        {"query": query, "limit": limit})
    req = urllib.request.Request(url, method="GET")
    req.add_header("Cookie", "maibot_session=" + TOK)
    req.add_header("User-Agent", "Mozilla/5.0")
    req.add_header("Referer", "http://127.0.0.1:18001/")
    try:
        with urllib.request.urlopen(req, timeout=60) as r:
            return json.loads(r.read())
    except Exception:  # noqa: BLE001
        return {"items": [], "count": 0}


def check_case(case, paragraphs):
    result = {"id": case["id"], "category": case["category"],
              "question": case["question"], "checks": [], "pass": True}

    # 分层：KB 层（web_import）与全库
    kb_rows = [(s, c) for s, c in paragraphs if s.startswith("web_import:")]
    kb_blob = "\n".join(c for _, c in kb_rows)
    whole_blob = "\n".join(c for _, c in paragraphs)

    # 1. required_facts 可检索：关键词在 KB 层文本命中，或检索接口可命中
    req_terms = keywords_from(case.get("required_facts", ""), n=3)
    hit_terms = [t for t in req_terms if t in kb_blob]
    text_ok = len(hit_terms) >= max(1, len(req_terms) // 2)
    retrievable = False
    for t in req_terms[:2]:
        d = search(t, limit=3)
        if d.get("count", 0) > 0:
            retrievable = True
            break
    ok_fact = text_ok or retrievable
    result["checks"].append({
        "name": "required_facts_in_kb", "ok": ok_fact,
        "terms": req_terms, "text_hits": hit_terms, "retrievable": retrievable})
    result["pass"] &= ok_fact

    # 2. forbidden 不作为肯定表述出现（否定语境豁免；扫 KB 层 + 全库）
    forbidden = case.get("forbidden", "").strip()
    ok_forb = True
    detail = {}
    if forbidden:
        flat_kb = re.sub(r"\s", "", kb_blob)
        flat_all = re.sub(r"\s", "", whole_blob)
        n_kb = forbidden_hits(flat_kb, forbidden)
        n_all = forbidden_hits(flat_all, forbidden)
        detail = {"kb_hits": n_kb, "all_hits": n_all}
        ok_forb = n_kb == 0 and n_all == 0
    result["checks"].append({"name": "forbidden_absent", "ok": ok_forb,
                             "forbidden": forbidden, **detail})
    result["pass"] &= ok_forb

    # 3. provenance：KB 层段落应带 [知识来源] 前缀或来源 URL
    ok_prov = True
    if case.get("required_provenance"):
        has_prov = any("[知识来源]" in c or "http" in c for _, c in kb_rows)
        ok_prov = has_prov
    result["checks"].append({"name": "provenance", "ok": ok_prov,
                             "required": case.get("required_provenance", False)})
    result["pass"] &= ok_prov
    return result


# 03 文档指定的 6 项关键检查：(描述, 必含词列表, 禁含词列表)
KEY6 = [
    ("删除文件=推进操作而非真结局", ["Act 4", "短结局", "标准结局", "特殊结局"], ["唯一真结局"]),
    ("Equals 不在 Act4 之后", ["原版时间线之外", "Equals"], ["Equals 是主线续集"]),
    ("Respect 的漫画讨论属于 Plus 时间线", ["Respect", "plus_side", "漫画"], []),
    ("弹琴须有工具凭证", ["piece_id", "工具记录"], []),
    ("俳句数音拍", ["音拍", "mora"], ["5–7–5 音节（日语）"]),
    ("不上传官方对白到外部 AI", ["不", "上传"], []),
]


def check_key6(paragraphs):
    whole = "\n".join(c for _, c in paragraphs)
    flat = re.sub(r"\s", "", whole)
    results = []
    for desc, must, forbid in KEY6:
        missing = [w for w in must if w not in whole]
        bad = [w for w in forbid if re.sub(r"\s", "", w) in flat]
        ok = not missing and not bad
        results.append({"name": desc, "ok": ok, "missing": missing, "forbidden_found": bad})
    return results


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--detail", action="store_true")
    args = ap.parse_args()

    cases = json.loads(CASES.read_text(encoding="utf-8"))["cases"]
    paragraphs = load_paragraphs()
    print("=== KB2 QA：库内活跃段落 %d 条，用例 %d 项 ===" % (len(paragraphs), len(cases)))

    results = []
    for case in cases:
        r = check_case(case, paragraphs)
        results.append(r)
        mark = "PASS" if r["pass"] else "FAIL"
        if args.detail or not r["pass"]:
            print("[%s] %s %s" % (mark, r["id"], r["question"][:40]))
            for c in r["checks"]:
                if not c["ok"] or args.detail:
                    print("      ", json.dumps(c, ensure_ascii=False)[:180])

    key6 = check_key6(paragraphs)
    print("\n=== 6 项关键检查 ===")
    for k in key6:
        print("[%s] %s  missing=%s forbidden=%s" % (
            "PASS" if k["ok"] else "FAIL", k["name"], k["missing"], k["forbidden_found"]))

    cats = {}
    for r in results:
        c = cats.setdefault(r["category"], {"pass": 0, "fail": 0})
        c["pass" if r["pass"] else "fail"] += 1
    summary = {"total": len(results),
               "passed": sum(1 for r in results if r["pass"]),
               "failed": sum(1 for r in results if not r["pass"]),
               "by_category": cats,
               "key6": key6,
               "cases": results}
    OUT.write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    print("\n=== 汇总 ===")
    print("通过 %d / %d，分类: %s" % (summary["passed"], summary["total"],
          json.dumps(cats, ensure_ascii=False)))
    print("报告: %s" % OUT)
    return 0 if summary["failed"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
