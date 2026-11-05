"""验证 poetry_db / poetry_engine：建库、CRUD、版本、权限、去重、原作索引、备份。

容器内运行：
  docker exec maim-bot-core /MaiMBot/.venv/bin/python /tmp/test_poetry.py
"""
import shutil
import sys
import time
from pathlib import Path

PLUGIN_DIRS = ["/MaiMBot/plugins/monika_poetry",
               "/opt/qq-fish/data/MaiMBot/plugins/monika_poetry",
               str(Path(__file__).resolve().parent.parent / "monika_poetry"),
               str(Path(__file__).resolve().parent)]
for _d in PLUGIN_DIRS:
    if Path(_d).is_dir() and _d not in sys.path:
        sys.path.insert(0, _d)

import poetry_db as db
import poetry_engine as pe

TMP = Path(__file__).parent / "t_poetry"
shutil.rmtree(TMP, ignore_errors=True)
TMP.mkdir()

print("=== 1) 建库 + 原作索引种子 ===")
print("  新增原作索引:", db.init(TMP), "| 重复 init:", db.init(TMP))
print("  统计:", db.stats(TMP))
canon = db.list_canon(TMP, keyword="隔阂")
print("  关键词『隔阂』命中:", [c["title"] for c in canon])

print("\n=== 2) 建作品 + 读回 ===")
a = db.create_poem(TMP, title="雨天的窗台", body="雨把窗台擦亮了\n我数着滴答声\n把心事也擦亮了",
                   origin="monika_original", visibility="public",
                   owner_or_chat_id="user:1", mood="平静", theme="雨天",
                   style="短小节诗", tags=["雨天", "日常"], chat_id="user:1")
print("  创建:", a)
got = db.get_poem(TMP, a["poem_id"], chat_id="user:1")
print("  读回标题:", got["title"], "| 正文首行:", got["body"].splitlines()[0])
print("  权限可读（其它会话）:", db.get_poem(TMP, a["poem_id"], chat_id="user:2") is not None)

print("\n=== 3) 版本：修改不覆盖旧稿 ===")
r = db.revise_poem(TMP, a["poem_id"], body="雨把窗台擦亮了\n我数着滴答声\n把心事也擦亮了\n直到天黑",
                   change_note="补一句", chat_id="user:1")
print("  新版本:", r)
print("  版本列表:", db.list_versions(TMP, a["poem_id"]))
v1 = db.get_version(TMP, a["poem_id"], 1)
print("  v1 原样保留:", v1["body"].endswith("擦亮了"))

print("\n=== 4) 私有作品的可见性 ===")
p = db.create_poem(TMP, title="给你的悄悄话", body="这句只说给你听",
                   origin="user_submitted", visibility="private",
                   owner_or_chat_id="user:1", chat_id="user:1")
print("  本人可读:", db.get_poem(TMP, p["poem_id"], chat_id="user:1") is not None)
print("  他人不可读:", db.get_poem(TMP, p["poem_id"], chat_id="user:2") is None)

print("\n=== 5) 检索 / 收藏 / 关联钢琴 ===")
print("  搜『雨天』:", [x["title"] for x in db.search(TMP, "雨天", chat_id="user:1")])
db.set_favorite(TMP, a["poem_id"], True, chat_id="user:1")
print("  只看收藏:", [x["title"] for x in db.list_recent(TMP, chat_id="user:1", favorite_only=True)])
print("  关联钢琴:", db.link_piano_piece(TMP, a["poem_id"], "piece_1842_melancholy", chat_id="user:1"))
print("  关联字段:", db.get_poem(TMP, a["poem_id"], chat_id="user:1")["related_piano_piece_id"])

print("\n=== 6) 去重（二元组相似度）===")
body_new = "雨把窗台擦亮了\n我数着滴答声\n把心事也擦亮了"
print("  完全重复的相似度:", db.similarity(body_new, body_new))
print("  与旧作命中:", [h["title"] for h in db.find_similar(TMP, body_new, chat_id="user:1", threshold=0.6)])

print("\n=== 7) 正文解析 ===")
t, b = pe.extract_title_body("《夏日的树》\n\n树影摇了一下\n像是跟我打招呼\n我抬头看了一眼")
print("  标题:", t, "| 行数:", pe.body_lines(b))
t2, b2 = pe.extract_title_body("```诗\n旧笔记本\n翻开是去年的字\n我笑了一下\n```")
print("  去代码块:", t2, "|", b2.splitlines()[0])

print("\n=== 8) 备份（VACUUM INTO）===")
out = db.backup(TMP, TMP / "bk")
print("  备份文件:", Path(out).name if out else "失败", "| 大小:",
      Path(out).stat().st_size if out else 0)

print("\n=== 9) 删除（连版本与事件）===")
print("  删除:", db.delete_poem(TMP, p["poem_id"], chat_id="user:1"))
print("  删后查:", db.get_poem(TMP, p["poem_id"], chat_id="user:1"))
print("  版本残留:", db.list_versions(TMP, p["poem_id"]))

print("\n=== 10) 确定性/多样性抽样 ===")
seen = set()
for _ in range(6):
    seen.add(pe.pick_theme(recent_themes=list(seen)))
print("  6 次主题去重后:", sorted(seen))

shutil.rmtree(TMP, ignore_errors=True)
print("\n全部断言人工核对完毕")
