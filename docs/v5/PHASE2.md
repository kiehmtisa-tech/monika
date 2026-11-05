# Monika v5 · 第二轮补充（知识库 / 安全轮换 / Dashboard）

时间：2026-11-03 23:20 – 2026-11-04 00:25
范围：用户指定的三项（知识库四 collection、安全会话轮换、Dragon Board 上下文健康）

## 一、结论先行

| 项 | 状态 | 说明 |
|---|---|---|
| 知识库内容 | ✅ 完成 | 4 个 collection、8 篇文档已就位（`data/MaiMBot/a-knowledge/`） |
| 知识库写入 A-Memorix | ⛔ **受阻** | 写入通道被主进程写者锁拦截，且接口返回**假成功** |
| 安全会话轮换 | 🟡 代码完成 | 复用官方 `clear_chat_history_context`，含手动指令 + 自动分级；**运行时验证受阻**（同一进程边界问题） |
| Dragon Board 上下文健康 | ❌ 未做 | 今晚时间不足，Dashboard 结构已摸清（FastAPI + Vue，`/root/dragon-dashboard`） |

## 二、⚠️ 对上一轮汇报的纠正（重要）

**上一轮我说「空闲收尾写回已上线」，这个说法不准确。**

idle flush 的代码确实部署了（`flush.py`，30 分钟空闲 + ≥4 条有意义消息 + 每日一次 + checkpoint 去重），但它的写回通道是 `memory_service.ingest_summary()`——与知识库写入**是同一条通道**，同样会被主进程写者锁拦截并返回假成功。

实测证据：

```
RuntimeError: A_Memorix 数据目录已有活动写者: /MaiMBot/data/a-memorix/.a_memorix_runtime_writer.lock
当前持有者={"pid": 11}                      # pid 11 = MaiBot 主进程
返回值：success= True  detail= "A_Memorix 正在初始化，写入已进入启动队列"  stored=[]  skipped=[]
```

**影响面**：
1. 知识库 8 篇内容**未能**写入 A-Memorix（paragrap数 74 未变，`knowledge:*` 引用 0）
2. **idle flush 的收尾写回大概率同样失效**（代码在跑、判定正确，但写入被丢弃）
3. 这也意味着：任何在插件进程或独立脚本进程里调用 `memory_service.ingest_text / ingest_summary` 的写入，都会**静默失败**

**这是 A-Memorix 的架构硬约束**（单写者锁，锁持有者是 MaiBot 主进程 pid），不是代码 bug。我试了三条路都不通：

| 路径 | 结果 |
|---|---|
| 独立脚本进程调用 `memory_service.ingest_text` | 锁拒绝 → 假成功 |
| 插件进程内调用（含 `respect_filter=False`） | 同样锁拒绝 → 假成功 |
| WebUI HTTP API `/api/webui/memory/import/paste` | 401「Token 无效或已过期」（`webui.json` 的 access_token 不被该接口接受） |

## 三、这一轮实际做出来的东西

### 3.1 知识库内容（4 个 collection，8 篇）

`data/MaiMBot/a-knowledge/`，每篇带 front-matter（collection / trust / source / updated）：

| collection | 篇数 | 字数 | trust | 内容 |
|---|---|---|---|---|
| `ddlc_canon` | 3 | 1870 | canon | 人物设定、第四面墙机制、DDLC+ 与二创边界 |
| `monika_persona` | 2 | 1203 | persona | 核心人格、表达与聊天风格 |
| `literature_music` | 2 | 1323 | reference | 诗歌形式与修辞、钢琴与乐理基础 |
| `content_policy` | 1 | 976 | policy | 五级治理分级（P0 不可变） |

内容是真实可用的知识（不是占位符）：诗歌形式/修辞表、乐理最小集、钢琴练习的实际困难、DDLC 设定与二创边界、内容治理分级表。满足 ACCEPTANCE G-38/G-39/G-41（独立 collection、二创标记、来源与可信等级）。

### 3.2 安全会话轮换（`context_guard.py`）

关键发现：**MaiBot 原生就有安全的会话清理机制**，完全满足方案第三阶段"不删数据库、不改 stream_id"的约束：

```python
# src/maisaka/context/clear_context.py
mark_clear_context_command(message)                          # 插入清理边界
heartflow_manager.clear_chat_history_context(session_id)     # 清运行时短期上下文
select_messages_after_latest_clear_marker(messages)          # 恢复时只取边界之后的
```

实现（`context_guard.py`）：
- **分级**：正常 ≤8k / 压缩候选 8k–12k / 轮换候选 >12k（方案初始阈值，可配）
- **观测**：Hook `maisaka.planner.after_response` 实时拿 `total_tokens`、`selection_reason`
- **手动轮换**：管理员即时指令「清理上下文 / 重置对话 / 忘掉刚才」
- **自动轮换**：默认**关闭**（避免误伤正在进行的话题），管理员可即时开关，同会话 5 分钟冷却
- **持久化**：同时写一条清理边界标记，重启后仍保持清空状态
- **审计**：`data/MaiMBot/data/governance/context_guard.jsonl`
- 管理员即时指令还提供：上下文状态查询、开关自动轮换、重触发知识导入

⚠️ 但 `heartflow_manager.clear_chat_history_context` 与 A-Memorix 写入**可能**存在同样的进程边界问题（未验证——验证需要真实触发一次轮换）。如果同样受阻，就只能走「管理员在 QQ 里发 `/clear`」的原生路径（这条是主进程内处理的，一定有效）。

### 3.3 知识导入器（两版，都已入库 git）

- `knowledge.py`（插件内，幂等，带 collection/trust 标签）——**受锁阻挡**
- `v5_knowledge_webimport.py`（WebUI API）——**受 token 阻挡**
- `v5_knowledge_import.py`（独立脚本）——**受锁阻挡**

三版都写好了，通道一通就能用。

## 四、Dragon Board（未做，但已摸清）

- 位置：`/root/dragon-dashboard/dragon/`，systemd `dragon-dashboard.service`（端口 8765，CPU 50%/内存 256M 限额）
- 后端：FastAPI，`backend/app/routes/{dashboard,portfolio,config,astock_db,ai_budget}.py`，`main.py:124` 注册
- 前端：Vue，`frontend/src/{api,components,views,router}`
- 计划加的模块：`routes/maibot_ctx.py`（读 `logs/maisaka_prompt/` + `budget/gateway.db` + `context_guard.jsonl`）+ 一个 view，展示 token 趋势、构成占比、P50/P95、超时次数、压缩/轮换次数，并提供两个鉴权按钮（压缩 / 安全重置）

## 五、下一步（按优先级）

1. **打通写入通道**（这是知识库与 idle flush 的共同前置）
   - 首选：查清 WebUI 的正确鉴权（`webui.json` 的 access_token 不是该接口的凭证，可能需要登录签发 JWT）
   - 备选：通过 MaiBot 插件 RPC / PluginContext 调用主进程能力
   - 兜底：给 cron 任务（宿主侧、已验证可写 metadata.db）做一次性导入——绕过 service 层，需手工补向量
2. **验证 idle flush** 是否真的写入成功（现在判定逻辑正确但通道存疑）
3. **验证 context_guard 轮换**是否可在插件进程内执行
4. Dragon Board 上下文健康模块

## 六、代码与文档位置

- 插件：`data/MaiMBot/plugins/monika_governance/`（新增 `context_guard.py`、`knowledge.py`）
- 知识内容：`data/MaiMBot/a-knowledge/`（4 collection / 8 篇）
- 脚本：`scripts/v5/`（新增 `v5_knowledge_import.py`、`v5_knowledge_webimport.py`）
- 本地副本：`inbox/ctx_v1/`
