"""P8 状态一览：音乐项目/练习/播放事件/偏好/策略。"""
import json
import sqlite3
import sys

sys.path.insert(0, "/MaiMBot/plugins/monika_piano")
sys.path.insert(0, "/MaiMBot/depends-data/piano-runtime/pylibs")
import music_state as ms  # noqa: E402

DB = "/MaiMBot/data/piano/pieces.db"
print("projects:", json.dumps(ms.active_projects(DB, "", 5), ensure_ascii=False))
print("practice:", json.dumps(ms.recent_practice(DB, 5), ensure_ascii=False))
c = sqlite3.connect(DB)
print("play_events:")
for r in c.execute("select event_id,chat_id,source_kind,source_id,explicit,send_status,"
                   "datetime(created_at,'unixepoch','localtime') from play_events "
                   "order by event_id desc limit 8"):
    print(" ", r)
print("prefs:", json.dumps(ms.preferences(DB, "", 5), ensure_ascii=False))
print("policies:")
for r in c.execute("select * from music_delivery_policy"):
    print(" ", r)
print("external_scores:")
for r in c.execute("select score_id,title,composer,license_name,rights_status "
                   "from external_scores"):
    print(" ", r)
print("pieces:", c.execute("select count(*) from pieces").fetchone()[0])
