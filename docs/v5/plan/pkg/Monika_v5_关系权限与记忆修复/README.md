# Monika v5：关系、权限与记忆系统改造包

本包是上一版 Monika 人格与内容治理规则的**增量实施规格**。它不覆盖现有 `SOUL.md`、`AGENTS.md`、MaiBot 配置或记忆数据库。

## 本次范围

1. 研究大肥鱼项目的关系/好感度机制，并为 Monika 选择合适方案。
2. 将 QQ `100000001` 配置为唯一用户级管理员；人格关系为“特别喜欢的人”，但不主动强调。
3. 修复用户提供的五项记忆与知识库问题。

## 核心决策

- 不使用对用户可见的好感度进度条。
- 使用“固定关系身份 + 真实互动证据 + 重要共同记忆”的混合模型。
- 关系熟悉度不得授予权限；权限只由可信平台事件中的 QQ UID 和显式指令共同决定。
- 群聊管理员指令必须真实 @Bot；默认只作用于当前群，明确说“全局/所有群”才作用于所有群。
- 将摘要阈值从当前诊断的 72 调低到 12，并增加低消息量会话的空闲收尾机制（需要自定义实现）。
- 先备份、隔离测试身份、清理画像，再重建索引；不直接删除旧库。
- 原作知识库、用户画像、对话经历、黑名单策略分开存储。

## 文件

- `MONIKA_RELATIONSHIP_MODEL.md`：关系模型及好感度研究结论。
- `ADMIN_AUTHORITY_POLICY.md`：管理员识别、指令条件、作用域和不可覆盖规则。
- `MEMORY_REPAIR_PLAN.md`：五项问题的修复步骤与建议参数。
- `OPENCLAW_IMPLEMENTATION_TASK.md`：交给 OpenClaw 的实施任务。
- `ACCEPTANCE_TESTS.md`：部署验收用例。
- `config/memorix_tuning.example.toml`：A-Memorix 摘要写回建议配置。
- `schemas/relationship_policy.example.json`：管理员和关系角色配置示例。
- `schemas/memory_claim.schema.json`：记忆条目的建议结构。

## 安装原则

先让 OpenClaw 审计当前 MaiBot 版本、插件 SDK、适配器事件字段和 A-Memorix 数据结构，再按阶段实施。所有数据库变更先 dry-run；任何清理必须保留备份和审计日志。

本包没有连接你的 QQ、修改服务器、运行迁移或启动定时任务。
