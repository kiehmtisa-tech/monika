# Monika 内容治理黑名单 v1.1（MaiBot 适配版）

> 上游交付：`Monika_内容治理黑名单_v1`（ChatGPT，2026-11-03）+ `ChatGPT_hmd.md` 方案说明。
> 本目录是**实际部署的那一份**，已按当前宿主（MaiBot 1.3.19 + 自研插件，OpenClaw 已停用）做了适配，偏差见第 4、5 节。

## 1. 目录内文件

| 文件 | 作用 | 是否被运行时读取 |
|---|---|---|
| `policy_rules.json` | 机器可读五级策略 + 触发词 + 婉拒素材 + 审计口径（v1.2） | ✅ 插件每轮使用 |
| `dynamic_terms.json` | 经人工批准/临时隔离的动态词库，初始为空 | ✅ 插件每轮使用 |
| `MONIKA_POLICY.md` | 政策正文（P0–P4 定义、边界、判定方式） | ❌ 文档 |
| `CLASSIFIER_PROMPT.md` | 歧义内容的语义审核提示词（默认未启用） | ❌ 文档 + 预留接口 |
| `MAINTENANCE.md` | 周更流程、预算、误杀回归、回滚 | ❌ 文档 |
| `MAINTAINER_JOB.md` | 周更任务规范（谁做、做什么、不许做什么） | ❌ 文档 |
| `MONIKA_AGENTS_APPEND.md` | 供 Agent 行为层的固定条款（已核对与人设 v3 不冲突） | ❌ 文档 |
| `filter_reference.py` / `test_policy.py` | 上游参考实现与 20 条单测（保留作为对照基线） | ❌ 仅测试 |
| `maintainer_proposals.py` | 提案校验器（字段/schema/政治不可降级） | ✅ 周更脚本调用 |

## 2. 五级策略（与上游一致）

`P0` 硬拒绝（政治、密钥、人格改写、依赖操控）→ `P1` 不模仿（低俗、恶意、能力幻觉）→ `P1_quarantine` 语义不确定（不学习不写记忆）→ `P2` 降权（烂梗复读）→ `P3` 正常 → `P4` 优先保留（原作、文学、钢琴、真实共同经历）。

## 3. 五个审查入口的实际落点

上游要求「不能只写在提示词里」。当前宿主可用的官方 Hook 如下，插件 `monika_policy` 已逐一挂接：

| 上游入口 | 实际 Hook | 行为 |
|---|---|---|
| `incoming` | `chat.receive.after_process`（BLOCKING） | P0 → `abort`，消息不入库、不进上下文、不触发回复；随后插件发一句自然婉拒 |
| `learning` | `expression.learn.after_extract` / `expression.learn.before_upsert` / `jargon.extract.before_persist` | 过滤候选集、拦住写入；P0–P2 一律不学 |
| `memory_write` | **无写入 Hook（缺口）** | 见第 5 节；靠源头隔离 + 入库巡检 |
| `retrieval` | `maisaka.planner.before_request`（BLOCKING） | 审计系统块与工具输出中的 P0/P1，命中记审计；同时把本轮「表达边界」追加进系统块 |
| `outgoing` | `send_service.before_send`（BLOCKING） | P0 → `abort` 取消发送（防模型越界、防密钥外泄） |

补充说明：`replyer.before_request` 也注入了同一句表达边界，作为二次强化。**实测发现只注入 replyer 不够**——planner 先决定「要不要接梗、往哪个方向回」，replyer 只是执行方向；因此提示必须先进 planner 的系统块（见 WORKLOG 第 3 轮迭代）。

## 4. 与上游的偏差（有意为之）

1. **OpenClaw → MaiBot 原生插件**：上游按 OpenClaw skill 设计（`~/.openclaw/workspace/skills/monika-persona/policy/`）。本机 OpenClaw 已停用（内存治理），改为 MaiBot 插件 `monika_policy`，规则目录随插件走。
2. **`policy_rules.json` 升到 1.2**：1.1 加入边界自述/密钥/双语注入/婉拒素材/审计口径；1.2 再加入 `politics_non_trigger_phrases`（社团政治·办公室政治·政治课等非政治固定搭配），避免把「拒绝大社团的形式化政治」这类正常表达判成 P0。新增 `boundary_self_declaration_pattern`（用户声明「不聊政治」不算要求展开）、`secret_patterns`（含身份证）、`persona_override_patterns`（中英双语）、`canned_replies`（婉拒素材外置，改文案不用改代码）、`audit`（只存哈希不存原文）。
3. **不写 `[message_receive].ban_words`**：上游明确反对——它会整条吞掉用户消息。本实现只对 P0 生效，且给一句回应。
4. **`MONIKA_AGENTS_APPEND.md` 未写进运行的 `AGENTS.md`**：`/MaiMBot/AGENTS.md` 在镜像内未挂载，硬改会被镜像重建冲掉。经核对，人设 v3 已覆盖其中「社交边界 / 第四面墙 / 连续性 / 不伪造工具结果」条款；剩余条款的强制力由插件执行，不依赖提示词。
5. **语义审核默认关闭**：`CLASSIFIER_PROMPT.md` 的接口已留（`Policy.apply_semantic_result` 做严格 schema 校验、本地 P0 不可被降级），但默认不调用模型——理由是当前 1.6G 内存与预算闸门都在紧约束下，且确定性预筛的「拿不准就隔离」已能覆盖主要风险。

## 5. 已知缺口（不含未经验证的承诺）

1. **A_Memorix 写入无法在写入瞬间拦截**：它是宿主内置子系统，未向插件暴露 `memory_write` Hook。当前补偿是「源头隔离」——P0 消息在入站即 `abort`，不会进入聊天流，因此不会被 `chat_summary_writeback` / `person_fact_writeback` 写回。但如果 P0 内容以其他途径进入（例如资料导入、群聊摘要的历史存量），现有机制无法在写入时拦住。
2. **检索侧只审计不改写**：`planner.before_request` 能改写 Context Items，但直接删改记忆片段存在破坏上下文结构的风险，v1 只做审计。若审计里出现持续命中，再单独评估是否开改写开关。
3. **周更任务未启用**：`MAINTAINER_JOB.md` 的定时器需要真实联网与真实来源，默认不启动（避免自作主张抓取）。脚本已就绪，人工确认后再开。

## 6. 存量巡检

已有数据用 `scripts/policy_sweep.py` 只读扫描（句级定位、脱敏、不删），详见 `docs/policy/SWEEP.md`。

## 7. 验收

```bash
python3 scripts/policy_selftest.py        # 40 项，覆盖政治/谐音/误杀/注入/密钥/绕过/动态词库
cd policy && python3 -m unittest test_policy   # 上游 20 项基线
```

注意：在插件目录内跑测试会生成 `__pycache__`，请加 `PYTHONDONTWRITEBYTECODE=1`，否则 MaiBot 文件监视会反复触发无效变更。
