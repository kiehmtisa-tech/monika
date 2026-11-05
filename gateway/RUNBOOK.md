# 预算网关 RUNBOOK（阶段4）

> 网关: `qq-fish-budget-gw`（FastAPI, :8009, 仅 Docker 内网 `qq-fish_maim_bot`）
> 配置: `/opt/qq-fish/docker-config/mmc/model_config.toml`
> 备份: `/opt/qq-fish/backups/mmc_pre_gateway_20261103_111619/`（切换前完整原始配置）

## 灰度顺序（每步功能验证通过才进下一步）

Utils → Planner → Memory（含 fast_model/mid_memory/learner/emoji）→ Replyer → Embedding

切换方法：把对应 `[model_task_config.<task>].model_list` 中的模型名改为 `*-gw` 版本
（`mimo-v2.6-flash` → `mimo-v2.6-flash-gw`，`deepseek-flash` → `deepseek-flash-gw`）。
MaiBot FileWatcher 约 1-60s 热重载（日志: `[配置] 配置热重载完成` + `已清空LLM客户端实例缓存`）。

验证三点：
1. core 日志出现热重载完成、无报错；
2. `llm_usage` 中该任务的 `model_api_provider_name` 变为 `*-GW`；
3. `/opt/qq-fish/budget/gateway.db` 出现对应结算记录（provider=`*-GW`）。

## 快速回滚（需人工授权）

网关故障 Fail-Closed：MaiBot 请求失败，**不会**绕过网关直连（除非人工回滚）。

```bash
# 1. 恢复切换前完整配置（覆盖当前 model_config.toml）
cp -a /opt/qq-fish/backups/mmc_pre_gateway_20261103_111619/model_config.toml \
      /opt/qq-fish/docker-config/mmc/model_config.toml

# 2. 等待热重载（日志确认），若不生效再重启 core（需人工确认）
docker logs maim-bot-core --since 2m 2>&1 | grep 热重载
# docker compose -f /opt/qq-fish/docker-compose.yml restart maim-bot-core   # 最后手段

# 3. 验证回滚生效
docker exec maim-bot-core python3 -c "
import sqlite3; c=sqlite3.connect('/MaiMBot/data/MaiBot.db')
print(c.execute(\"SELECT task_name,model_api_provider_name,datetime(timestamp) FROM llm_usage ORDER BY id DESC LIMIT 5\").fetchall())"
```

回滚后硬限额立即失效（直连上游），须人工决定何时重新灰度。

## 网关运维

```bash
docker logs qq-fish-budget-gw --since 10m          # 日志
docker compose -f /opt/qq-fish/docker-compose.yml restart budget-gateway   # 重启网关（MaiBot 不受影响）
curl -s http://127.0.0.1:8009/healthz              # 宿主机不可达（未发布端口）→ 用 docker exec 测:
docker exec qq-fish-budget-gw python3 -c "import httpx;print(httpx.get('http://127.0.0.1:8009/healthz').json())"
```

- 预算文件: `/opt/qq-fish/budget/budget.json`（Dragon Board 可写，网关按 mtime 热加载）
- 状态快照: `/opt/qq-fish/budget/gateway_status.json`（网关每 5s 更新，Dragon Board 读）
- 台账: `/opt/qq-fish/budget/gateway.db`（SQLite WAL，只读查询安全）
- 真实厂商 Key: `/opt/qq-fish/secrets/gateway_secrets.json`（600，仅网关读取）

## 验证结果（2026-11-03 完成）

- 单元测试 54/54、真实上游 E2E 19/19（Chat/流式/ToolCalls/VLM/Embeddings，结算与官方价逐位一致）
- 灰度四主任务全部验证：utils(Xiaomi-GW) → planner(Xiaomi-GW) → memory(DeepSeek-GW) → replyer(DeepSeek-GW)；
  辅助任务(learner/fast_model/mid_memory/emoji)与 embedding(bge-m3-gw)已切换，embedding 有台账证据，
  其余四个近 24h 无自然流量（配置级受控，llm_usage 会自动补证据）
- 覆盖终检：Dragon Board `hard_limit_complete=true`，网关 healthy、未发布端口
- 耗尽测试：预算设为已用值 → 402 单次请求不可重试、无循环、无回复、无积压（后台 embedding 正常）
- 宕机测试：Connection error 重试 1 次共 2 次后失败，llm_usage 无新行（未直连）；拉起后自动恢复
- 注入测试消息方法：容器内 ws://127.0.0.1:8000/ws 发 MessageBase 字典
  （message_info 需扁平 user_info；sender 需在 adapter_policy allow_ids 内）

## 关键语义

- `402 + BUDGET_EXCEEDED`：预算耗尽，MaiBot 4xx 不重试、立即失败（区别于普通 API 错误）
- 网关不可达/配置缺失/密钥缺失 → 503 fail-closed，不绕过
- 预留 TTL 180s；上游超时/客户端断开 → 释放不计费；重启 → 在途预留作废（outcome=restart）
- 结算日 = 预留创建日（北京时间），跨日请求不串账

---

## 阶段5：自适应行为（2026-11-03 15:5x 上线）

**链路：** Dragon（60s 定时 + 行为/预算变更即时）计算 λ 策略 → `/opt/qq-fish/data/MaiMBot/budget_policy.json`
（原子写；容器内路径 `/MaiMBot/data/budget_policy.json`），三个消费方：
1. **idle_proactive_chat 插件**（`plugins/idle_proactive_chat/budget_gate.py`）：每 300s 检查时按
   `proactive_rate_per_hour` 换算触发概率；策略缺失/损坏/过期 → 保守 3 次/时；降频立即、恢复 EMA 渐进；
   插件文件被 MaiBot 文件监视自动热加载（实测生效，日志 `主动发言预算闸门`）
2. **bot_config 热重载**（Dragon `apply_runtime_config()`，仅 auto 模式、仅变化时写入 + `.bak_runtime` 备份）：
   - 群聊发言频率：`chat.reply_timing.talk_value` + talk_value_rules 两个全局规则值 × group_talk_factor
   - 记忆节流：`a_memorix.integration.chat_summary_writeback_message_threshold`、
     `a_memorix.person_profile.refresh_debounce_seconds` × memory_delay_factor
   - ⚠️ 这两个键在**嵌套表**里，不是 [a_memorix] 根表（曾误写根表，15:52 已修复并自动清理）
3. **多模态**：`multimodal_allowed=false` 时插件对图片消息写人设化拒看提示（append_context，不调模型，30min/流防刷）

**分级（状态滞回：恶化立即、改善需超阈值 10%）：**
| 状态 | λ 阈值 | group_talk_factor | memory_delay |
|---|---|---|---|
| normal | ≥42 | 1.0 | ×1 |
| tight | ≥9 | 0.5 | ×2 |
| critical | >0 | 0.15 | ×4 |
| exhausted | 余额0 | 0 | ×8 |

**网关预留（阶段5-A，与策略独立）：** tier=high(planner/replyer) / low(memory/learner/utils/fast_model/mid_memory/emoji)，
`behavior.json.mention_reserve_ratio=0.3` → low 上限 budget×0.7，超限 402 PRIORITY_RESERVE_EXHAUSTED（与 BUDGET_EXCEEDED 区分）。

**新增接口：** `GET /api/ai-budget/policy`（策略文件+年龄+runtime写入审计）、`GET /api/ai-budget/forecast`
（耗尽ETA/燃烧率/剩余可调用次数/置信度/状态）；`POST /behavior` 新增 `proactive_share`（0~1，默认0.5）。

**测试：**
- `python3 /opt/qq-fish/gateway/test_phase5.py` → **60/60**（含子进程跑 test_gateway 63 项）
- `python3 /opt/qq-fish/gateway/test_gateway.py` → **63/63**

**关键文件：**
- 策略 / `budget/policy` 消费: `/opt/qq-fish/data/MaiMBot/budget_policy.json`
- 基准值（首次运行捕获，勿手改）: `budget/runtime_talk_base.json`、`budget/runtime_memory_base.json`
- 写入审计: `budget/runtime_apply.json`
- 插件: `data/MaiMBot/plugins/idle_proactive_chat/{plugin.py,budget_gate.py}`
- 宿主机依赖: `pip3 install --break-system-packages tomlkit`（dragon 写 bot_config 用）

**线上实测（15:53-15:54）：** mimo 预算 2.0→0.10 即时生效：policy state=exhausted、proactive_rate=0、
talk_value 0.4→0.0（含两条规则）、multimodal off、MaiBot 热重载完成；恢复 2.0 → normal、talk 回 0.4、
`used` 全程不重置；恢复后 rate=23.1（≤基准30，EMA 渐进回升）。

**回滚：** ① behavior.json `mode=manual` → Dragon 停止接管 bot_config（⚠️ 若当时处于节流态，talk_value 保持最后值，需手工调回）；
② `proactive_enabled=false` → 只关主动发言；③ bot_config 可从 `bot_config.toml.bak_runtime` 恢复；
④ 策略文件整体无害：缺失只导致插件保守降频，网关硬限额不依赖它。

**已知边界：**
- 网关只有 2 档优先级：普通群聊回复与主人私聊/@ 同为 high（共享保留份额）；主动发言靠插件侧
  `proactive_share=0.5` 限速防止挤占，未做按会话类型的第三档
- talk_value 规则的改写对 MaiBot WebUI 手工编辑存在 last-writer-wins 竞争（低概率，写前有备份）
- manual 模式不接管 bot_config（按 global_activity/throttle 只体现在策略与主动速率上）
