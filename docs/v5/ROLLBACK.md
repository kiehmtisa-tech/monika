# 回滚步骤（Monika v5）

## 全量回滚到改造前

```bash
cd /opt/qq-fish

# 1. 停止治理插件（热重载即可，无需重启容器）
rm -rf data/MaiMBot/plugins/monika_governance
# FileWatcher 约 30 秒内卸载

# 2. 恢复配置
cp backups/v5_20261103_224235/bot_config.toml docker-config/mmc/bot_config.toml
docker compose restart core          # 让配置彻底生效

# 3. 恢复记忆数据库
cp backups/v5_20261103_224235/metadata.db data/MaiMBot/a-memorix/metadata/metadata.db
# 恢复前先停容器避免写冲突
docker compose stop core
cp backups/v5_20261103_224235/metadata.db data/MaiMBot/a-memorix/metadata/metadata.db
docker compose start core

# 4. 恢复插件目录（若连其他插件也要回滚）
rm -rf data/MaiMBot/plugins/monika_governance
# 其余插件未改动

# 5. 去掉定时任务
crontab -l | grep -v "v5_claim_autoclassify" | crontab -

# 6. 代码回滚
git reset --hard v5-baseline-20261103
```

## 仅回滚某一部分

| 要回滚的 | 操作 |
|---|---|
| 管理员权限 + 关系身份 | 删 `data/MaiMBot/plugins/monika_governance/`，热重载 |
| 空闲收尾 | 在 `monika_governance/config.toml` 设 `[flush] enabled = false`，热重载 |
| claim 重分类 | 用 `backups/v5_20261103_224235/metadata.db` 覆盖 `data/MaiMBot/a-memorix/metadata/metadata.db`（需停 core） |
| 测试身份隔离 | 反向执行 `v5_identity_fix.py` 的删除语句，或用备份 db 覆盖 |
| 上下文配置 | `context_compaction_summary_max_chars` 改回 6000、`person_profile_injection_max_profiles` 改回 3 |
| 定时分类 | `crontab -l \| grep -v v5_claim_autoclassify \| crontab -` |

## 备份清单（`backups/v5_20261103_224235/`，共 7.0 MB）

| 文件 | 内容 |
|---|---|
| `bot_config.toml` | 改造前配置 |
| `metadata.db` | 改造前 A-Memorix 全库（sqlite backup API 一致性快照） |
| `a-memorix.tar.gz` | 改造前整个 a-memorix 目录（含向量、图谱、画像快照） |
| `plugins/` | 改造前插件目录 |
| `docker-compose.yml` | 改造前编排文件 |

另有：`backups/v5_20261103_224310_bot_config.toml`（配置应用器首次写入前的备份）、`backups/crontab_pre_v5.txt`（定时任务原始快照）。

## 回滚后需要确认

1. `docker ps` 三个容器 healthy
2. `grep -n "person_fact_writeback" docker-config/mmc/bot_config.toml` 为 `true`
3. `git log --oneline -3` 回到 `4eb73d9`
4. 画像快照回到改造前版本（`person_profile_snapshots` 的 profile_version 下降）
