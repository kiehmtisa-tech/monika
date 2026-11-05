# MaiBot / A-Memorix 五项记忆问题修复方案

> 现状依据：用户提供的部署诊断。MaiBot 源码依据：`src/services/memory_flow_service.py` 与 `src/config/official_configs.py`。实施前必须对照实际安装版本。

## 0. 先备份

迁移前导出：
- A-Memorix metadata / claims / profiles；
- 各向量空间的 namespace、索引状态和配置；
- person_identity、alias、关系索引；
- 聊天摘要和知识库原始文档；
- 当前 bot_config.toml 与插件配置。

备份后先 dry-run，输出“将修改/合并/隔离/删除”的报告。未经确认不做不可逆删除。

## 1. 聊天经历几乎不进入记忆

### 根因

用户诊断中的 `chat_summary_writeback_message_threshold=72` 对低消息量私聊过高。

检查的 MaiBot 源码中：
- `chat_summary_writeback_message_threshold` 是自上次成功写回后新增消息数的触发阈值；
- `chat_summary_writeback_context_length` 是摘要回看的消息条数；
- 实际摘要窗口取 `min(configured_context_length, pending_message_count)`；
- 该自动流程按消息阈值触发，不等同于“会话结束自动写回”。

因此降低阈值和补充空闲收尾是两件不同的事。

### 建议配置

建议先用：
- `chat_summary_writeback_enabled = true`
- `chat_summary_writeback_message_threshold = 12`
- `chat_summary_writeback_context_length = 16`

示例见 `config/memorix_tuning.example.toml`。

增加自定义低消息量空闲收尾：
- 会话空闲 30 分钟；
- 至少有 4 条尚未摘要的新消息；
- 只在出现有意义的候选事件/偏好/承诺时写回；
- 每个会话每天最多额外触发 1 次空闲收尾；
- 只摘要自上次 checkpoint 后的新消息；
- 不为“你好/哈哈/收到”等低信息量消息生成记忆。

这样不必每条消息都调用模型，也不会要求私聊必须积累 72 条消息。

## 2. 事实库没有分类，画像被单次事件污染

### 新分类

| 类型 | 示例 | 默认稳定性 |
|---|---|---|
| `identity` | 长期身份、教育背景 | candidate_stable / stable |
| `preference` | 明确喜欢的表达方式 | candidate_stable |
| `interest` | 长期兴趣、反复参与的活动 | candidate_stable |
| `episodic_event` | 今天被领导批评、某天打完酒馆战棋 | episodic |
| `transient_state` | 今天很累、正在感冒 | transient，设置复核/过期 |
| `skill_state` | 当前不会弹钢琴、正在学习钢琴 | time-bound skill |
| `relationship` | 与某人的关系、共同经历 | 有来源证据，按关系实体存储 |
| `boundary` | 不希望在群里讨论某类话题 | 按适用范围持久保存 |

### 核心规则

- `confidence` 与 `stability` 分开：一句明确自述可以有较高置信度，但仍然只是一次性事件。
- 不允许所有 claim 默认 `stability=stable`、`confidence=1.0`。
- 稳定事实必须有明确来源；可用“用户明确要求记住”或跨时间重复证据提升状态。
- “今天被批评”不能升级为稳定画像。
- “已打完酒馆战棋”属于一次事件；同一事件的不同措辞应语义去重。
- “不会弹钢琴”属于技能状态，记录观察时间和证据，允许未来更新。
- “喜欢某游戏”可以是兴趣候选；多次独立提及或用户明确确认后再升级。
- 每条 claim 保留 source message / session / timestamp，允许回溯。

### 主体规范化

使用平台 ID 作为 canonical identity：
- 管理员：`qq:100000001`
- Bot：独立 `bot:monika`
- 其他 QQ 用户：`qq:<真实UID>`

“他”“目标人物”“大肥鱼”等自然语言称谓不能作为唯一实体键。别名必须有来源、所属实体和审核状态，不能靠相似字符串自动合并。

## 3. 向量检索空间异常

### 处理顺序

1. 对旧 space `5e5b…` 导出 metadata、配置和索引信息。
2. 在运行时禁用该空置 space 的检索路由，停止反复调用空索引。
3. 保留快照，不要直接删除。
4. 检查新 space 的 paragraph / entity embedding 数量、维度、模型标识和最后更新时间。
5. 对缺失向量重建；对关系查询继续使用图谱与 BM25，不必为了“所有东西都向量化”而强行嵌入关系。
6. 采用 hybrid retrieval：向量召回段落/实体，BM25 精确召回名称与关键词，图谱处理关系路径。
7. 建立 index health check：active namespace 非空、embedding model 一致、查询命中率可观察；空索引不得每轮反复搜索。

## 4. 知识库尚未建设

不要把所有聊天记录直接导入知识库。知识库和个人经历记忆必须分开。

建议建立四个独立 collection：

### `ddlc_canon`
- DDLC 与 DDLC+ 原作剧情、角色设定、事件顺序。
- 每条内容记录来源、版本、原作/官方/二创标记。
- 原作事实优先；Wiki 和同人资料不得自动升级为官方设定。

### `monika_persona`
- 莫妮卡核心人格、文学部部长身份、表达风格、价值判断、行为边界。
- 作为稳定人格资料，只有管理员审核才能修改。

### `literature_music`
- 文学常识、写作技巧、诗歌形式、钢琴基础乐理、练习方法、作曲知识。
- 来源优先采用可靠教材、权威机构和可追溯出版资料。
- 低俗 Meme、政治梗和无来源网络碎片不进入该库。

### `content_policy`
- 黑名单规则、低权重表达、分类定义、例外和审核记录。
- 由定期维护任务提出候选，不允许自动覆盖 P0 不可变规则。

用户画像和共同经历继续放在 A-Memorix / episode memory，不放进 `ddlc_canon` 或 `monika_persona`。

### 入库流水线

`raw_scan → source_check → semantic_classification → dedup → chunk → embed → review → publish`

`raw_scan` 只代表发现候选，不代表已入库。LPmm 导入、调优中心应在文档分类和来源审查后逐步启用，不要一次性导入整个互联网。

## 5. 测试数据污染画像

- 将 `rhythm-test` 与 `100000003` 标记为 `environment=test`，从生产 person profile injection、关系索引和长期记忆召回中排除。
- 从主用户画像 alias 中移除 `dragon-test`；先保留在测试数据审计记录，不要直接物理删除。
- 主用户唯一 canonical identity 使用 `qq:100000001`。
- 重建 `identity / preferences / relationship / episodes` 分类后再恢复 profile injection。
- 测试环境与生产环境使用独立数据目录、索引 namespace 和 person_identity 表。
- prompt 注入前执行最终过滤：`environment != test`、主体 ID 匹配、scope 允许、状态有效。

## 6. 建议执行顺序

P0：备份、隔离测试 ID、禁用空旧向量空间、暂停错误的 profile injection。

P1：调整摘要阈值至 12；实现 30 分钟空闲收尾；确认私聊摘要成功写入。

P2：对 35 条现有 claim 做 dry-run 分类、实体规范化、语义去重；人工审核后提交。

P3：重建 active vector space 与 hybrid retrieval；建立索引健康检查。

P4：建立 DDLC 原作、Monika 人格、文学音乐、内容政策四个知识库 collection。

P5：恢复 profile injection，开启长期回归测试；观察至少 7 天。

## 7. 成功标准

- 私聊不再需要累积 72 条消息才可能写回。
- 一次性事件不再进入 stable_facts。
- 重复措辞合并，claim 有可追溯来源。
- profile 中 identity / preferences / relationship 不再因分类错误长期为空。
- 日志不再持续查询已停用的空 vector space。
- 私聊内容不进入其他群聊上下文。
- 测试账号与 `dragon-test` 不再出现在生产 Prompt。
- DDLC canon、Monika persona、文学音乐和个人经历有清晰边界。
