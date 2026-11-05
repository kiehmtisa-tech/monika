#!/usr/bin/env bash
# 人物画像 chat 级隔离 —— 重建后重放脚本
#
# 背景：这套改动在 MaiBot 核心 src/（镜像层），docker compose down/up 或镜像重建后会丢失。
# 本脚本把 docs/v5/chat_scope/src/ 里的最终文件拷回容器并重启 core。
#
# 用法（服务器上）：
#   bash /opt/qq-fish/docs/v5/chat_scope/apply_chat_scope.sh
#
# 回滚：把 src/orig_backup.tar 解回容器（见 README.md）。
set -e
REPO=/opt/qq-fish/docs/v5/chat_scope/src
CONT=maim-bot-core

echo "== 1) 拷回 4 个核心文件 =="
docker cp "$REPO/A_memorix/core/storage/metadata_fact.py"        "$CONT:/MaiMBot/src/A_memorix/core/storage/metadata_fact.py"
docker cp "$REPO/A_memorix/core/utils/person_profile_service.py" "$CONT:/MaiMBot/src/A_memorix/core/utils/person_profile_service.py"
docker cp "$REPO/A_memorix/core/runtime/services/profile_admin_service.py" "$CONT:/MaiMBot/src/A_memorix/core/runtime/services/profile_admin_service.py"
docker cp "$REPO/maisaka/memory/person_profile.py"               "$CONT:/MaiMBot/src/maisaka/memory/person_profile.py"

echo "== 2) 语法校验 =="
docker exec "$CONT" /MaiMBot/.venv/bin/python -c "
import py_compile
for f in ['A_memorix/core/storage/metadata_fact.py','A_memorix/core/utils/person_profile_service.py','A_memorix/core/runtime/services/profile_admin_service.py','maisaka/memory/person_profile.py']:
    py_compile.compile('/MaiMBot/src/'+f, doraise=True)
print('语法 OK')
"

echo "== 3) 清 __pycache__ 并重启 core =="
docker exec "$CONT" sh -lc "find /MaiMBot/src -name __pycache__ -type d -exec rm -rf {} + 2>/dev/null; true"
cd /opt/qq-fish && docker compose restart core
echo "完成。等 core 起来后（约 80s）验证："
echo "  docker exec -w /MaiMBot -e PYTHONPATH=/MaiMBot $CONT /MaiMBot/.venv/bin/python /opt/qq-fish/scripts/v5/test_chat_scope_profile.py"
