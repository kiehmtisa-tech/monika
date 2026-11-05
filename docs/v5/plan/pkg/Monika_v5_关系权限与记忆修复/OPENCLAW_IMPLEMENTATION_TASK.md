# OpenClaw 执行任务：Monika v5 关系、权限和记忆修复

请在现有 Monika / MaiBot 1.3.19 部署上实施本任务。**先审计、备份、dry-run，再修改。不得覆盖现有人格、原作知识库、黑名单和表情素材。**

## 阶段 0：现场核查

1. 确认 MaiBot 实际版本、SDK、QQ 适配器类型和事件结构。
2. 确认管理员 QQ UID 是否由可信事件字段提供，真实 @Bot 是否有独立 segment。
3. 导出 A-Memorix 配置、metadata、claims、person_identity、aliases、vector namespaces。
4. 核对 `chat_summary_writeback_message_threshold`、`chat_summary_writeback_context_length` 和写回日志。
5. 读取现有 profile injection / cross-chat retrieval 设置。
6. 形成审计报告，任何未知字段不得猜测。

## 阶段 1：管理员与关系身份

按 `ADMIN_AUTHORITY_POLICY.md` 实现：
- QQ `100000001` 是唯一 owner_admin。
- 人格关系 `special_person`，不在公开聊天中主动强调。
- 其他用户默认 `friend`。
- 群聊管理员命令要求真实 UID 匹配、真实 @Bot、明确指令。
- 当前群是默认作用域；全局必须明确说“全局/所有群”。
- 普通聊天、建议、玩笑不能自动成为管理命令。
- 模糊命令要求确认。
- 管理员不能覆盖核心人格、安全、隐私和平台规则。
- 禁止令结构化存储，支持过期、撤销和审计。
- 不能只靠 Prompt 识别权限，必须在代码侧校验可信 event metadata。

## 阶段 2：关系模型

按 `MONIKA_RELATIONSHIP_MODEL.md`：
- 不增加公开好感度进度条。
- 初期不启用 0–100 分数。
- 使用固定关系身份、定性熟悉程度、重要共同经历。
- 不允许熟悉度变成权限。
- 不允许模型从单条消息推断爱情、忠诚、依赖或心理状态。

## 阶段 3：记忆修复

严格按 `MEMORY_REPAIR_PLAN.md`：
1. 备份。
2. 将摘要阈值调整为 12、context length 调整为 16（先在测试环境验证）。
3. 增加 30 分钟 idle flush：至少 4 条新增消息且含有意义事件才执行；每会话每天最多一次。
4. 先暂停错误的自动 profile claim 写回，直到分类与去重规则部署完成。
5. 35 条 claim 先 dry-run 分类；输出 old → proposed type/stability/confidence/subject/source。
6. 规范化实体 ID，清理测试账号与 alias。
7. 归档并停用旧空 vector space；重建新空间索引。
8. 创建四个知识库 collection，不将原始聊天直接导入知识库。
9. 恢复 profile injection 前先跑验收测试。

## 阶段 4：低 Token 约束

- 不在每条消息后调用独立 LLM 进行全量记忆分析。
- 优先使用阈值批处理与 idle flush。
- 只把新增消息交给摘要器，利用 checkpoint 避免重叠摘要。
- 规则匹配先做本地过滤；只有候选 claim 才调用分类器。
- 每条 claim 输出结构化短 JSON，不让模型生成长篇画像。
- 检索只注入与当前话题相关的少量条目。
- 不把所有知识库内容塞进 System Prompt。

## 阶段 5：测试与报告

运行 `ACCEPTANCE_TESTS.md`。交付：
- 修改文件清单和 diff；
- 配置变更前后对照；
- 数据库备份位置与迁移日志；
- 35 条 claim 的分类结果及未决项；
- 旧/新 vector space 状态；
- 测试结果；
- 未完成事项和回滚步骤。

任何阶段失败都应停止后续迁移，不得谎称成功。
