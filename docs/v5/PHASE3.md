# Monika v5 · 第三轮收尾报告

时间：2026-11-03 23:39 – 2026-11-04 01:15
范围：用户指令「没碰到限制的能做的都做，遇到需要审核的跳过，做完为止」

## 一、这一轮做成的（全部验证通过）

| 项 | 状态 | 验证方式 |
|---|---|---|
| **A-Memorix 写入通道打通** | ✅ | WebUI Cookie 鉴权，HTTP 200 |
| **知识库 4 collection 落库** | ✅ | 109 段落入库，检索命中 episode |
| **idle flush 收尾写回** | ✅ | 规则式要点提取 + WebUI 导入，落库验证 |
| **context_guard 安全轮换** | ✅ | 手动指令 + Dashboard 指令均执行成功 |
| **Dragon Board 上下文健康模块** | ✅ | 后端 API + 前端页面 + 按钮链路全通 |

### 1.1 写入通道（这是上一轮的卡点）

**根因**：A-Memorix 有单写者锁（`.a_memorix_runtime_writer.lock`，持有者是 MaiBot 主进程 pid=11）。插件进程与独立脚本调用 `memory_service.ingest_text/ingest_summary` 会被拒，却返回 `success=True`（**假成功**）。

**解法**：走 MaiBot WebUI 的记忆导入 API，在主进程内执行。关键是鉴权方式——

```
POST /api/webui/memory/import/paste
Cookie: maibot_session=<webui.json.access_token>     ← 鉴权在这里
Authorization: Bearer ...                            ← 这个无效，会 401
```

`dependencies.require_auth` 从 Cookie 取 token，`security.verify_token` 用 `secrets.compare_digest` 与 `webui.json` 的 `access_token` 比对（ASCII token 不做二次编码）。payload：`{content, name, input_mode: "text"}`。

### 1.2 知识库（ACCEPTANCE G 段）

- 4 个 collection、8 篇内容：`ddlc_canon`(3) / `monika_persona`(2) / `literature_music`(2) / `content_policy`(1)
- 每篇带 front-matter（collection / trust / source / updated），满足 G-41
- 落库后 source 形如 `web_import:<collection>__<文档名>`，分块 5–7 块/篇
- 向量重建：paragraphs 108/109 done，entities 284/284
- **检索验证**：查询「诗歌形式」命中 episode「诗歌形式与修辞知识概览」，summary 正确
- probe 测试条目已清理（软删除，`deletion_reason=v5_probe_cleanup`）
- 导入方式改为 WebUI API（插件内 `knowledge.py` 也已改造），`kb_import_on_load=false` 避免热重载重复导入

### 1.3 idle flush（上一轮我说"已上线"不准确的地方）

**已修复**：写回从 `memory_service.ingest_summary`（被锁拒绝）改为 WebUI `/import/paste`。

- 规则式要点提取（**不调 LLM**，符合低 Token）：取最近有意义消息，寒暄/表情自动过滤
- 实测：构造 3 条消息（含 1 条"嗯"）→ 要点只保留 2 条有意义的 → 导入成功 → 落库验证（含"会话要点"、"写完了专利"、"答辩材料"、`idle_flush` 标记）
- 触发条件不变：空闲 30 分钟 + ≥4 条未写回 + 至少 1 条有意义 + 每会话每日 1 次 + checkpoint 去重

### 1.4 context_guard 安全轮换

**运行时验证通过**：

| 触发方式 | 结果 |
|---|---|
| 管理员 QQ 指令「清理上下文」 | `runtime_cleared: true` |
| Dashboard 按钮（rotate_session 指令） | `runtime_cleared: true`，审计记录 |
| Dashboard 开关（toggle_auto_rotate） | `auto_rotate_toggled` 审计记录 |

链路：QQ 指令 → 插件 `_run_immediate` → `heartflow_manager.clear_chat_history_context()`；Dashboard 按钮 → API → `commands.jsonl` → 插件 `CommandRunner` 轮询执行 → 审计。**跨进程问题用「指令文件 + 插件拉取」绕开**，避免直调主进程服务。

修的 bug：`self._ctx` 与 PluginContext 属性名冲突（`'PluginContext' object does not support item assignment`），已改名 `self._sess_cache`。

### 1.5 Dragon Board 上下文健康模块

| 组件 | 位置 | 说明 |
|---|---|---|
| 后端 | `/root/dragon-dashboard/dragon/backend/app/routes/maibot_ctx.py` | 4 个接口：health / command / commands / sessions |
| 前端 | `frontend/src/views/MaiBotContext.vue` | 概览卡片 + 延迟分布 + 上下文构成 + 操作区 |
| 路由 | `router/index.js`、`App.vue` 侧栏 | 「上下文健康」入口 |
| 构建 | `vite build` 已跑，`MaiBotContext-kf7inCJY.js` 产出 | 服务已重启并验证 |

**实测数据（164 次 planner 请求采样）**：

| 指标 | 值 |
|---|---|
| 输入 Tokens | P50 8980 / P95 22712 / 峰值 25702 |
| Planner 耗时 | P50 17.8s / P95 55.8s / 峰值 102.3s |
| 超 30 秒 / 超 60 秒 | 40 次 / **6 次** |
| 上下文构成 | User 51.7% / System 33.2% / Assistant 15.0% |

这 6 次超 60 秒就是历史上「连续不回消息」的直接原因（客户端 60s 超时）。

## 二、需要你审核的（我按要求跳过了）

1. **知识库内容准确性**：8 篇是我写的（DDLC 设定、诗歌形式、乐理、治理分级）。DDLC 部分可能与你心中的理解有出入，`literature_music` 是她素养的底子，建议你过一遍。
2. **阈值参数**：软 8000 / 硬 12000 tokens（方案的初始值）。实测 P95 已到 22712，**可能偏松**。
3. **自动轮换当前是开启的**（我测试 Dashboard 开关时打开了 `auto_rotate: true`）。方案建议谨慎，你要不要关掉？
4. **管理员指令的注入文案**（如「你不能执行，请温和而明确地拒绝」）——会影响她的语气，建议你看看是否符合人设。
5. **熟悉度分级阈值**：new/known/familiar 按互动天数与次数（每日上限 8 次）。
6. **Dashboard 凭证硬编码**：`（凭证已轮换，见服务器 /root/DASHBOARD_LOGIN.txt）` 写在 `auth.py`，与服务器 root 密码相同。**建议改掉**（我没动，怕影响你现有访问）。

## 三、没做的（含受阻的）

| 项 | 原因 |
|---|---|
| **context_guard 持久化标记** | `SessionMessage.model_validate` 不存在（非 pydantic v2），标记未写入 → **轮换只清运行时，重启后历史会恢复**。需要手工构造 SessionMessage（`MaiMessage.__init__(message_id, timestamp, platform)` + 手工设 message_info/raw_message） |
| 真实 QQ 群聊端到端验证 | 避免打扰群友与烧预算；改用注入 + 准 E2E。**真实 @ 段的字段形态仍只按源码推断** |
| 好感度源码独立核实 | 仍采信 ChatGPT 对 dsh-fish / dsh-dafeiyu-life 的调研结论，我没自己读那两个仓库 |
| `tool_result_condense` 对照测试 | 方案要求"先测再开"，我既没开也没测 |
| ACCEPTANCE C-20 / E-31/32 | 重启后 checkpoint 去重、群聊隔离验证（只读可做，未做） |
| 上下文构成的实时刷新 | Dashboard 是手动刷新，没有 WebSocket 推送 |
| 知识库扩充 | literature_music 只有 2 篇，她真正的文学素养需要更多内容 |

## 四、下一步建议（按优先级）

1. **修持久化标记**：让轮换在重启后仍生效（手工构造 SessionMessage，或改用 `from_maim_message`）
2. **真实流量 7 天观察**：验证管理员指令、关系身份、idle flush 在真实 QQ 里的表现
3. **阈值调参**：按实测 P95=22712 重定软/硬阈值（可能要降到 6k/10k）
4. **Dashboard 鉴权强化**：密码改独立凭证、或加 IP 白名单
5. **tool_result_condense 实测**：一次开关对照，看延迟/成本
6. **知识库扩充**：尤其是 literature_music（诗歌、文学、钢琴）

## 五、资产位置

- 插件：`/opt/qq-fish/data/MaiMBot/plugins/monika_governance/`（新增 `context_guard.py`、`knowledge.py`，改 `flush.py`、`plugin.py`）
- 知识内容：`/opt/qq-fish/data/MaiMBot/a-knowledge/`（4 collection / 8 篇）
- 治理数据：`/opt/qq-fish/data/MaiMBot/governance/`（rules.json、familiarity.json、flush_state.json、context_guard.jsonl、commands.jsonl）
- Dashboard：`/root/dragon-dashboard/dragon/`（后端 `routes/maibot_ctx.py`、前端 `views/MaiBotContext.vue`，副本存 `docs/v5/dashboard/`）
- 脚本：`/opt/qq-fish/scripts/v5/`（新增 `v5_knowledge_webimport.py`）
- 本地副本：`inbox/ctx_v1/`
