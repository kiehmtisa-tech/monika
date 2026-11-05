# “养大肥鱼” → MaiBot Monika 拟人化适配 v3

**目标不是模仿鱼鱼的口头禅，而是借鉴其聊天节奏、独立记忆、角色连续性与可控主动性**。本包是供 OpenClaw 对用户现有 MaiBot 1.3.19 执行的配置/方案文件，并未部署到用户实际机器。

## 研究发现（2026-11-03）
| 公开项目/来源 | 可确认的做法 | 对 Monika 的改造 |
|---|---|---|
| SAKURAfan1023/dafeiyu-bot | `QQPersona.swift` 默认回复 60 字预算；表情情绪、触发与去重；每会话独立 persona | 一般闲聊短一些，但不是所有消息硬截成 60 字；表情另设触发，不机械三连 |
| ssamurias/dsh-fish | 有状态/成长事件流、可表达拒绝、独立外部经历、短回复预算等机制；README 明确仍有实验功能 | 固定 Monika 身份，只成长共同经历与模拟生活；不采用空白人格演化 |
| minedalang/dsh-dafeiyu-life | 模拟心情、兴趣、社交和自主待办、长期记忆 | 以写诗/练琴/文学部为活动主题，且仅在可追踪模拟事件或工具执行时更新日志 |
| FrigidCrow/dafeiyu | 研究架构设计，不是已验证运行的成品 | 采用“先单轮聊天→记忆→可控自主性”的验收路线 |
| MaiBot 官方 | 支持 `response_splitter.enable`、`max_split_num`、`chat.reply_timing.talk_value`、`maisaka.reply.before_post_process` Hook | 优先禁拆分、精简回复提示；仍需现场判断是否 Planner 连续 reply |

## 三步优先级
1. 修改 `config/bot_config.toml`：`response_splitter.enable=false`，`max_split_num=1`，关闭人为错别字；群聊 talk_value 以 0.5 起步，**这不是私聊回复条数设置**。只合并字段，别把 TOML 片段整个覆盖。
2. 在 WebUI 「身份与人格」→「导入」中粘贴 `MaiBot_Monika_自然聊天人设_v3.json`，创建后手动切换。
3. 用 `MONIKA_对话验收案例.md` 测试；若连续三气泡仍存在，再由 OpenClaw 检查 Planner reply 调用和真实日志。

## 后续可选
- `MONIKA_会话节奏与状态机.md`：设计意图分类与长期共同记忆；需要插件/调度器实现，不能只放在磁盘就说已经生效。
- 现有 `emoji_index.json` 不需要重做，只要给主动配图加冷却与上下文条件。
- 原作 82KB 记忆资料保持单独索引，不要每轮注入。
- `A_Memorix` 默认关闭；可单独启用，但必须配置有效 embedding 模型，谨慎处理跨会话记忆权限。

## 重要边界
- 截图仅能确认“三气泡”，无法单独判断是回复分割、Planner 重复 reply 还是插件造成。
- 官方资料中的配置项为近期版本；实际 MaiBot 1.3.19 是否具备全部 Hook 需启动后查看。
- 聊天风格与主动分享是可模拟的角色行为，不意味着模型真的有独立生命或已在现实里弹钢琴。

## 参考链接
- https://github.com/SAKURAfan1023/dafeiyu-bot
- https://github.com/SAKURAfan1023/dafeiyu-bot/blob/main/Sources/BotCore/QQPersona.swift
- https://github.com/ssamurias/dsh-fish
- https://github.com/minedalang/dsh-dafeiyu-life
- https://github.com/FrigidCrow/dafeiyu
- https://docs.maibot.chat/manual/configuration/bot-config
- https://docs.maibot.chat/faq/chat-and-reply
- https://docs.maibot.chat/plugin/hooks
- https://docs.maibot.chat/manual/configuration/amemorix-config
- https://docs.maibot.chat/manual/personality
