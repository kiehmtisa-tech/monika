# Monika v5 · 关系权限与记忆修复 · 部署报告

- 部署时间：2026-11-03 22:40 – 23:20
- 目标机器：阿里云 <SERVER_IP>，项目根 `/opt/qq-fish`
- 方案来源：`inbox/ctx_v1/`（ChatGPT_关系权限与修复.md、ChatGPT_滚动记忆.md、Monika_v5_关系权限与记忆修复.zip）
- 回滚基线：git tag `v5-baseline-20261103`（commit 4eb73d9）+ 备份 `backups/v5_20261103_224235/`

---

## 一、结果总览

| 需求 | 状态 | 说明 |
|---|---|---|
| 1. 好感度系统怎么实现、是否采用 | 已调研 | 不采用 0–100 数值；采用「固定关系身份 + 定性熟悉度 + 共同经历」三层 |
| 2. QQ 100000001 设为管理员 + 关系身份 | ✅ 已上线 | 插件 `dragon.monika-governance`，三重校验落在代码侧 |
| 3-1. 聊天经历几乎不进记忆 | ✅ 部分上线 | 阈值改写被运行时覆盖（见 KNOWN-ISSUE-1）；空闲收尾已上线兜底 |
| 3-2. 事实库无分类、画像被事件污染 | ✅ 已修复 | 39 条历史 claim 重分类 + 语义去重；画像已从「暂无」变为真实内容 |
| 3-3. 向量检索隐患 | ✅ 已澄清 | 旧 space 未被查询；空索引警告来自关系向量池（配置关闭的正常结果） |
| 3-4. 知识库零建设 | ⚠️ 未完成 | 仅完成方案与流水线设计，四个 collection 尚未建（见待办） |
| 3-5. 测试数据污染画像 | ✅ 已隔离 | 测试身份移出 + 别名清除 + 主体登记表 |
| 滚动记忆/上下文生命周期 | ✅ 配置已优化 | prompt token 25327 → 11534；会话轮换未做（无安全 SDK，见待办） |

---

## 二、已真正生效的改动

### 2.1 管理员权限与关系身份（插件 `dragon.monika-governance`）

位置：`data/MaiMBot/plugins/monika_governance/`（已入库）

授权三条件全部在代码侧校验，不依赖模型判断身份：

```
AdminCommand = is_owner(可信 user_id) AND is_real_at(适配器解析的@段) AND is_explicit(祈使句规则)
```

- 唯一管理员：QQ `100000001`（取自事件字段 `message_info.user_info.user_id`，绝不从昵称/正文推断）
- 真实 @ 判定：只认 `message_segments` 里的 `at` 段且 target 等于 bot QQ；正文手打名字不算
- 指令意图：规则式词法判定（explicit / suggestion / chat / blocked），零模型调用
- 作用域：默认当前群；出现「所有群/全局/以后所有」才设 `global`；私聊默认 `global`
- 红线：覆盖核心人格、隐私、安全、密钥的指令一律拒绝（`blocked`），并给出拒绝提示
- 规则持久化：`data/MaiMBot/data/governance/rules.json`，含 `rule_id / scope / type / status / expires_at / revoked_at` 与审计日志
- 撤销：`rule_revoke` 类指令只由管理员本人触发，普通用户无法撤销

关系身份（`relations.py`）：
- 管理员 → `special_person`：注入「你很熟悉、很在意的人」+ 明确写「不要公开宣告这是什么特殊关系」
- 其他用户 → `friend`：注入「朋友，不要把对在意之人的那种态度套给所有人」
- 熟悉度 `new / known / familiar`：由互动天数、次数、双向交流推导；每人每天最多计 8 次互动防刷分
- 明确不把数字写进 Prompt、不公开、不授予权限（`familiarity is not authorization`）
- 注入上限 420 字，防止挤占上下文

### 2.2 记忆修复

**历史 claim 重分类**（`scripts/v5/v5_claim_reclassify.py`，确定性映射表，零模型）：

| 项 | 改前 | 改后 |
|---|---|---|
| 全部 39 条的稳定性 | `stable` | `temporal` 24 条 / `stable` 14 条 / retracted 1 条 |
| 全部 39 条的画像分节 | `stable_facts` | 分布到 identity/relationship/preference/recent_interactions |
| confidence | 全部 1.00 | 0.60–0.95，与稳定性分开标注 |
| 语义重复 | 同一事件 6 种措辞并存 | 11 条标记 `superseded` 并留 `fact_transitions` 痕迹 |

分类口径（映射到 A-Memorix 现有枚举，不新增表字段）：

| 方案 claim_type | profile_section | stability | confidence |
|---|---|---|---|
| identity | identity_settings | stable | 0.85 |
| relationship | relationship_settings | stable | 0.85 |
| preference / interest | interaction_preferences | stable | 0.80 |
| episodic_event | recent_interactions | temporal | 0.90 |
| transient_state | recent_interactions | temporal | 0.70 |
| skill_state | stable_facts | temporal | 0.80 |
| project | recent_interactions | temporal | 0.75 |

**写回侧自动分类**（`scripts/v5/v5_claim_autoclassify.py`，已挂 cron `*/15`）：
- 只处理「仍在 ingest 默认态」（stable_facts/stable/confidence=1.0）的新增 claim，**不会回退人工判断**
- 保守默认：判定不明确的一律进 `recent_interactions/temporal`，绝不因为说不清就升级为长期稳定事实
- 期间曾短暂关闭 `person_fact_writeback`，分类器部署后已恢复为 true

**测试污染隔离**（`scripts/v5/v5_identity_fix.py`）：
- 新建 `v5_subject_registry` 登记 4 个主体：`qq:100000001`(owner_admin) / `bot:monika`(bot_self) / 两个测试号(test)
- 移出 `person_identities`：`100000003`（测试）、`900000111`（rhythm-test）—— 先登记再移出，可回溯
- 删除污染别名：主画像下的 `dragon-test`、`灰度测试`；bot 自身身份下的 `Monika`、`大肥鱼`（外号由人设文本承载，不需要别名表）
- bot 自身身份排除出画像刷新队列，避免给自己画人物画像
- 文本层规范化：claim 里的 `目标人物`→主称呼、`（ 丨丨）`单空格变体→canonical 双空格

**画像重建效果（验收关键）**：改前「身份设定/关系设定/相处偏好」永远显示「暂无」；改后 v45 快照已有真实内容（身份 1 条、关系 4 条、偏好 5 条、近期互动 3 条）。

### 2.3 上下文优化

| 配置 | 改前 | 改后 |
|---|---|---|
| `context_compaction_summary_max_chars` | 6000 | 2000 |
| `person_profile_injection_max_profiles` | 3 | 2 |
| `tool_result_condense` | false | 保持 false（会增加一次模型调用，方案要求先测再开） |

实测（planner prompt dump）：

| 指标 | 21:53 改造前 | 23:1x 改造后 |
|---|---|---|
| prompt_tokens | 25,327 | 11,534 |
| AssistantMessageItem（planner 自我推理）占比 | 61% | 10% |
| UserMessageItem（真实对话）占比 | 25% | 70% |
| SystemMessageItem（人设）占比 | 13% | 19% |

⚠️ 归因说明：改造后 core 重启、该私聊历史重新累积，token 下降中含「新会话」效应，不能全部归因于配置改动。**结构性结论可靠的是构成比**：Assistant 占比下降说明滚动摘要与画像注入确实在收窄；但更长观察窗口才能给出定量结论。

### 2.4 空闲收尾写回（`flush.py`，已随插件启动）

补「12 条阈值」之外的空档：只积了 4–11 条消息、但包含重要信息的私聊，在空闲 30 分钟后做一次收尾摘要。

- 每会话每天最多 1 次；只摘要尚未写回的消息（自维护 checkpoint）
- 纯寒暄/表情不生成记忆（规则式噪声过滤）
- 复用 A-Memorix 官方 `ingest_summary(generate_from_chat=True)` 通道，与图片描述补偿同一套写回路径

---

## 三、向量检索：一项纠正

原方案假设「旧空置 space 5e5b… 仍在被查询」。**现场核查不成立**：

- `vectors/active.json` 只指向新 space `424dc2…`，旧 space 是磁盘上的历史目录，不再被路由
- 新 space 健康：paragraph 69/69 done、entity 128/128 done、dimension 1024、embedding `BAAI/bge-m3`（经预算网关）
- relation 向量 0 个 —— 因为 `relation_vectorization.enabled = false`，关系走 BM25 + 图谱
- 日志里 6 小时 24 次 `Indices are empty. No data to search.`（`vector_store.py:1786`）**来自关系向量池为空**，是配置关闭的正常结果，不是旧 space 被查

处理：按方案「不为形式完整强行向量化关系」，保持 `relation_vectorization = false`；空索引警告属良性噪音，若要消除需改检索路径跳过空池（源码改动，列在待办）。

---

## 四、已知问题（未谎称成功）

### KNOWN-ISSUE-1：`chat_summary_writeback_message_threshold` 被运行时写回

现象与证据（完整复现）：
1. 22:43 用脚本改 72 → 12，写入成功、TOML 校验通过
2. 22:44 运行时保存配置，`bot_config.toml.bak_runtime` 记录了「写入前」的 12
3. 之后文件中该值变回 72
4. 重启 core 前 sed 成 12，重启后仍是 72
5. 盯盘实验：改后 30 秒保持 12，第 40 秒被写回 72（mtime 23:09:09.749，与 `bot_config.toml.tmp` 出现时间一致）

结论：`src/config/config.py` 的 `write_config_to_file()` 在配置加载/迁移路径被触发时，把该字段写回 72。不是升级钩子（`config_upgrade_hooks.py` 不涉及该字段），字段本身合法（`default=36, ge=1`）。

影响：方案要求的「阈值 72 → 12」未能持久生效，低流量私聊仍需 72 条消息才触发自动摘要。

已采取的补偿：空闲收尾写回（30 分钟空闲 + ≥4 条有意义消息 + 每日 1 次）已覆盖同一目标，且更省模型调用。

后续建议：定位 `from_dict`/`attribute_data` 对该字段的归一化逻辑，或改用 WebUI 配置接口写入（走 config_schema 白名单路径）。

### KNOWN-ISSUE-2：画像分桶仍有小瑕疵

`person_profile_service` 的分桶逻辑把「正在玩酒馆战棋」这类临时事件归入了「关系设定」，与本次 claim 分类的口径不完全一致。不影响画像可用性，属 A-Memorix 内部分桶实现，未改动。

---

## 五、未完成事项

1. **知识库四个 collection**（方案 P1）：`ddlc_canon` / `monika_persona` / `literature_music` / `content_policy` 尚未建立。A-Memorix 的 `knowledge_packages` 表为 0。方案要求的入库流水线为 `raw_scan → 来源核验 → 内容分类 → 去重 → 分块 → 向量化 → 审核 → 发布`，需在 WebUI 记忆控制台（SSH 隧道到 127.0.0.1:18001）或 `import_admin`/`bundle_admin` 接口完成，并准备内容素材。建议优先建 `literature_music`。
2. **安全会话轮换**（滚动记忆方案第三阶段）：需要能安全清理指定聊天流运行时上下文的 SDK 接口。MaiBot 现有 `context/clear_context.py` 可作起点，但不能直接改数据库或伪造 stream_id。本轮只做了原生配置优化（第一、二阶段）。
3. **Dragon Board 可视化**（滚动记忆方案第四阶段）：Planner 上下文健康模块、压缩/重置按钮，需要鉴权与确认机制。
4. **阈值字段的持久化**（KNOWN-ISSUE-1）。
5. **空索引警告消除**：需在检索路径跳过空的关系向量池（源码改动）。
6. **自然流量验证**：空闲收尾、管理员指令、关系身份的实际效果需 7 天观察；本轮验收用的是准 E2E（直接喂消息 dict 给 Hook 方法），未走真实 QQ 群（避免打扰群友）。

---

## 六、验收结果

见 `TESTS.md`。合计：单元测试 34 项、准 E2E 20 项、画像重建对比、上下文构成对比，全部通过（1 项文案调整后通过）。
