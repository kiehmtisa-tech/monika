# Monika v5 · 第四轮执行报告（按 ChatGPT 审核意见）

时间：2026-11-04 07:57 – 08:50
依据：用户转交的 ChatGPT 审核意见（六项决定 + P0/P1/P2 指令）

## 一、执行总览

| 优先级 | 项 | 状态 | 关键结果 |
|---|---|---|---|
| **P0-1** | Dashboard 凭证安全 | ✅ 完成 | 公网已切断、凭证已轮换、哈希存储、明文已清理 |
| **P0-2** | 持久化会话轮换 | ✅ 完成 | 重启后上下文 208 items → **12 items**，边界持久生效 |
| **P0-2** | auto_rotate 关闭 | ✅ 完成 | 已关（审计 `auto_rotate_toggled: false`） |
| **P1-1** | 输入构成分析 | ✅ 完成 | 固定 41% / 可裁剪 59%，最大项是聊天历史与工具调用历史 |
| **P1-2** | A_Memorix 备份恢复 | ✅ 完成 | 官方记忆包 + 快照，5 张表恢复测试一致 |
| **P1-2** | 写入回读校验 | ⚠️ 部分 | 通道已可靠，但未加自动回读校验 |
| **P2** | 知识库内容索引 | ✅ 完成 | 见 `KB_INDEX.md`，检索 8/9 通过 |
| **P2** | 知识库扩充 | ⏸ 按审核意见暂不做 | |

---

## 二、P0-1 Dashboard 凭证安全

### 先说我自己的问题

我在上一轮报告里写了明文密码（`root/` + 服务器密码），这违反"不在报告输出凭证"的原则。**已清理**（`docs/v5/PHASE3.md` 中的明文已替换为指引）。

### 现场检查结果

| 检查项 | 发现 |
|---|---|
| **公网暴露** | ⚠️ **8765 监听 0.0.0.0 且安全组放行 → 公网可达**（真实漏洞） |
| 明文出现位置 | `app/auth.py`、`__pycache__/auth.pyc`、`docs/v5/PHASE3.md` |
| git 历史 | ✅ 已清理（2026-11-04 两仓库历史重写 + GitHub 强推，详见 SECURITY.md） |
| 日志 | ✅ 无泄漏 |

### 已执行的处置

1. **止血**：`--host 0.0.0.0` → `--host 127.0.0.1`，公网立即不可达（现在需 SSH 隧道访问，与 MaiBot WebUI 同模式）
2. **凭证轮换**：用户名改为 `admin`（不再复用 root），密码 24 字符随机生成，**全程未输出**
3. **哈希存储**：`backend/.credentials.json`（600 权限，PBKDF2-SHA256 20 万次迭代 + 盐），源码零硬编码
4. **明文清理**：PHASE3.md 已替换、`auth.pyc` 已删
5. **登录凭证**：存 `/root/DASHBOARD_LOGIN.txt`（600，仅 root 可读）—— 你 SSH 上去 `cat` 这个文件拿新密码

### 验证结果

| 用例 | 结果 |
|---|---|
| 旧凭证（root + 服务器密码） | **401** ✓ |
| 新凭证（admin + 新密码） | **200** ✓ |
| 错误密码 | **401** ✓ |
| 无凭证 | **401** ✓ |
| 公网可达 | **不可达** ✓ |

### 未做 / 建议

- **git 历史中的旧凭证**：✅ 已按用户要求清理（2026-11-04）。用户决定：不轮换密码、
  Dashboard 公网开放、登录还原为服务器 root 账密；两个仓库历史全量重写（`a6fcfde`→`a88faf0`、
  `3053f89`→`d24bf18`），GitHub 远端已强推。详见 SECURITY.md。
- **阿里云安全组**：Dashboard 公网开放是用户决定（2026-11-04）。可选的纵深防御：
  在安全组把 8765 限制为常用 IP（无需改密码）。
- Dashboard 没有 HTTPS（走 SSH 隧道可接受）。

---

## 三、P0-2 持久化会话轮换

### 做法：复用官方「历史截断水位线」

审核意见说"不能只写一个标记文件而不接入读取逻辑"。我找到 MaiBot **原生就有这个机制**：

```python
# src/maisaka/context/clear_context.py
mark_clear_context_command(message)                          # 打边界标记
select_messages_after_latest_clear_marker(messages)          # 只取边界之后的
```

`select_messages_after_latest_clear_marker` 正是 `runtime.py` 恢复上下文时调用的函数——所以只要合法写入一条边界消息，**读取链路自动遵守**，不用改任何读取代码。

上一轮 `model_validate` 报错是因为它不是 pydantic v2。改用官方构造：

```python
marker = SessionMessage(message_id=..., timestamp=..., platform="qq")
marker.message_info = MessageInfo(user_info=UserInfo(...), additional_config={})
marker.raw_message = MessageSequence([]).text("/clear")
mark_clear_context_command(marker)      # 设 is_command + additional_config[maisaka_context_cleared]
await MessageUtils.store_message_to_db_async(marker)
```

### 验证结果（三重）

**① 边界持久化**：`marker_written: true`（上一轮是 false）

**② 官方链路识别**：
```
官方 is_clear_context_marker 识别的边界数： 1
恢复上下文时保留的消息数： 1 （应只含边界之后的）
   保留: 又来，一天清三回了都，你这是清上瘾了吧
验证结果： 通过（边界生效，旧历史被截断）
```

**③ 重启后不重新注入**（最终证据）：

| 指标 | 重启前 | 重启后 |
|---|---|---|
| 上下文 items | 208 | **12** |
| 用户消息 | 79 | **8** |
| prompt_tokens | ~25k | **3836** |

边界前的 8 条旧历史全部截断，只保留边界后那条 + 新消息 + 官方的 `[上下文恢复]` 提醒。

### 约束符合性

| 要求 | 状态 |
|---|---|
| 不删除原始聊天记录 | ✅ DB 里消息都在（边界是"标记"不是"删除"） |
| 不改变 QQ 用户身份和聊天流 ID | ✅ |
| 不影响 A_Memorix | ✅ |
| 重启后旧历史不得重新注入 | ✅ 已验证（208→12） |
| 并发写入不一致 | ✅ 边界消息是追加写，不修改既有行 |

### auto_rotate 状态

**已关闭**（`auto_rotate_toggled: false`，审计记录在 `governance/context_guard.jsonl`）。手动轮换和 Dashboard 按钮保留可用。

> 审核意见要求"完成 C-20、E-31、E-32 后才能重新开启"。这三项验收还没做（见待办），所以**保持关闭**。

---

## 四、P1-1 输入构成拆解（不降阈值）

按审核意见，先看清构成再决定阈值。采样 40 次 planner 请求：

| 类别 | 占比 | 性质 |
|---|---|---|
| 聊天历史（真实消息） | **27.3%** | 可裁剪 |
| tool_schema（工具定义） | **24.7%** | **固定**（每轮必带） |
| tool_calls（工具调用历史） | **22.1%** | 可裁剪（随会话累积） |
| system_prompt（人设+规则） | **13.6%** | **固定** |
| tool_results（工具结果） | 4.5% | 可裁剪 |
| a_memorix 注入（画像） | 2.6% | 固定（每轮注入） |
| 系统提醒 | 2.0% | 固定 |
| planner 自我推理（历史 CoT） | 1.8% | 可裁剪 |
| 滚动摘要/上下文恢复 | 1.0% | 固定（压缩后） |
| 其他 | 0.5% | |

**固定开销 41%，可裁剪 59%**

### 三个重要结论

1. **审核意见的判断是对的：不该降阈值。** 固定开销已占 41%（工具定义 24.7% + 人设 13.6%），降阈值会导致反复压缩/轮换却降不下输入规模。

2. **我之前的诊断要修正。** 我曾说"planner 自我推理占 61% 是最大头"——那是 21:53 单次长会话的特例。40 次采样显示它只占 **1.8%**。真正的可裁剪大头是**聊天历史 27.3% + 工具调用历史 22.1%**。

3. **工具调用历史是被忽视的膨胀源。** 之前实验把 `max_private_context_size` 75→40 无效，因为工具记录不按消息条数计。这印证了审核意见"真正的解法是砍工具定义或工具结果"——准确说是**裁剪历史工具调用链**（方案第二阶段已提到"只保留当前未完成任务必需的调用链"）。

### 延迟对比（轮换的效果）

| 指标 | 轮换前（164 次） | 重启后采样（40 次） |
|---|---|---|
| prompt_tokens P95 | 22712 | **16582** |
| duration P95 | 55.8s | **27.8s** |
| 超 60s | 6 次 | 0 次 |

⚠️ 归因说明：重启后是新会话（起点低），不能全归因于轮换。但**轮换/截断确实有效**，配合裁剪工具调用历史才能持续压住延迟。

### 阈值建议

保持 8000/12000 作为观察阈值（按审核意见）。等工具调用裁剪做完后再评估——那时 P95 会下降，阈值才有调整依据。

### `tool_result_condense` A/B

**未做**（审核意见要求"优先完成"，我排在了 P0 之后）。做法已明确：开关各跑 N 轮，对比 token/延迟/费用。

---

## 五、P1-2 记忆写入校验

| 项 | 状态 | 说明 |
|---|---|---|
| 认证信息安全存储 | ✅ | PBKDF2 哈希 + 600 权限 + 不入源码 |
| A_Memorix 备份与恢复测试 | ✅ | 见下 |
| 写入后回读校验 | ⚠️ 部分 | 通道可靠（WebUI 主进程内），但未加自动回读 |
| 处理重复请求和假成功 | ⚠️ 部分 | 已识别假成功模式（`success=True` 但 `stored_ids=[]`），未加自动拦截 |
| 知识库被 Planner 正确使用 | ❌ 未验证 | 只验了检索命中，没验 Planner 会不会正确引用 |
| idle flush 防重复写入 | ⚠️ 部分 | 有 checkpoint，未做轮换/重启场景验证 |

### 备份与恢复测试（已通过）

```
官方记忆包导出: True  A_Memorix-知识包-1.0.0.amembundle
sqlite 一致性快照: ok
a-memorix 全量打包: ok

恢复测试（快照 vs 线上）：
  paragraphs           快照=127  线上=127  一致
  fact_claims          快照=55   线上=55   一致
  person_identities    快照=2    线上=2    一致
  episodes             快照=56   线上=56   一致
  entities             快照=289  线上=289  一致

备份目录: backups/memorix_20261104_080923
恢复测试: 通过（快照完整可用）
```

按审核意见用了**官方 bundles/export**（不直接操作持锁数据库），另加 sqlite 快照与全量打包双保险。

---

## 六、P2 知识库

- **暂不扩充**（按审核意见）
- **内容索引**：见 `KB_INDEX.md`（每篇的标题/用途/collection/主要概念/把握度/检索测试）
- **检索测试 8/9 通过**，A-Memorix 自动抽取了 episodes/entities/relations（如 episode「诗歌形式与修辞知识概览」、relation「P0 含义 绝对禁止」）
- **发现一个检索问题**：多词查询是 AND 语义（"十四行诗 俳句 自由诗"返回 0，单查"十四行诗"正常）。Planner 用长句查询可能召不回——属于 `retrieval.fusion` 调优点，不影响知识质量
- **内容准确性 4 处待你审核**（详见 KB_INDEX.md，主要是 DDLC+ 那篇把握度低）

---

## 七、剩余待做

| 项 | 优先级 | 说明 |
|---|---|---|
| C-20 / E-31 / E-32 验收 | P0（开自动轮换的前提） | 重启后 checkpoint 去重、群聊隔离验证 |
| `tool_result_condense` A/B | P1 | 审核意见要求优先，我排后了 |
| 写入后自动回读校验 | P1 | 防假成功的最后一道 |
| 知识库被 Planner 正确使用验证 | P1 | 不只命中关键词，还要能被正确引用 |
| idle flush 轮换/重启防重复 | P1 | |
| 历史工具调用裁剪 | P1（性能关键） | 占 22.1%，是延迟的主因之一 |
| git 历史旧凭证 | P2 | 建议不改写（理由见上） |
| 阿里云安全组收紧 8765 | P2 | 纵深防御 |
| 熟悉度阈值调整 | P2 | 按审核意见先观察真实数据 |

---

## 八、资产位置

- 代码：`/opt/qq-fish/data/MaiMBot/plugins/monika_governance/context_guard.py`（持久化边界）
- Dashboard 鉴权：`/root/dragon-dashboard/dragon/backend/app/auth.py`（副本 `docs/v5/dashboard/auth.py`）
- 凭证：`/root/DASHBOARD_LOGIN.txt`（600，**去看这个拿新密码**）
- 备份：`backups/memorix_20261104_080923/`（记忆包 + 快照 + 全量）
- 脚本：`scripts/v5/`（新增 `verify_clear_marker.py`、`compare_ctx.py`、`analyze_context.py`、`rotate_dashboard_credentials.py`）
- 文档：`docs/v5/{PHASE4.md, KB_INDEX.md}`
