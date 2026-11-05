# 给 OpenClaw 的实施指令（直接粘贴）

你要针对我当前运行的 MaiBot 1.3.19、MiMo/DeepSeek 与 Monika 人格进行“自然聊天节奏”改造，优先在既有配置和 SDK Hook 层完成，不修改主程序源码，不推倒重建我的人格和原作记忆库。

## 输入文件
- `MaiBot_Monika_自然聊天人设_v3.json`：新版本可导入人设。
- `MaiBot_最小修改片段.toml`：配置调整参考，**逐项合并**到现有 TOML，不要整文件覆盖。
- `MONIKA_会话节奏与状态机.md`：意图分类、回复结束、主动说话与长期记忆规则。
- `MONIKA_对话验收案例.md`：固定回放测试样例与量化验收。
- 原有 `monika-persona/INDEX.md`、`monika_history.md`、`emoji_index.json` 等：作为事实与表情资源，只读取必要部分，不重建它们。

## 工作顺序
1. 只读检查运行中的 config/bot_config.toml、具体人设、模型任务分配、已启用插件、response_splitter、Replyer/Planner 调用和发送日志。备份配置与当前人设。记录 MaiBot 实际 commit/Host 版本。
2. 取用户提供的下雨截图对应一轮，区分：①一个 Replyer 输出被 splitter 拆为三条；②Planner 发起了三次独立 reply；③多个插件触发/循环。切勿只凭截图断定原因。
3. **先做最小配置改造**：禁用 response_splitter、关闭人为错别字；导入 v3 人设并手动切换。不要把 talk_value 当作私聊单句次数控制器。保留现有 QQ 账号、许可范围、模型凭证和记忆配置。
4. 测试 10 组对话：确认类默认一条气泡、无需重复提醒；长问题仍能完整回答；用户连续发两条能按上下文接续；结束话题能收住。保存原始 Replyer 输出、Planner reply 调用次数和最终 QQ 气泡数。
5. 只有第 4 步仍不合格，再查看 MaiBot SDK Hook：`maisaka.replyer.before_request` 可以注入简短风格要求；`maisaka.reply.before_post_process` 可以设置 `enable_splitter=false`。Hook 已在主程序注册与否必须运行时核实；不要直接按另一版本写插件。不要在 postprocess 粗暴按 60 字截断，保留严肃解答和用户请求的长内容。
6. 如果问题来自 Planner 对同一消息反复调用 reply：在 Planner 提示与按消息 ID 的回复去重上增加行为约束；不要在原始消息层全局硬拦，以免漏掉后续新消息。记录 msg_id、reply invocation ID、send message ID 和回执。
7. 其后才做可选的生活循环：练琴/写诗日程要以本地日志和真实工具结果为依据。首次只在私聊白名单和预览模式使用；对话结束后一段时间才考虑主动分享。
8. 对 `A_Memorix` 的启用和 embedding/记忆检索需要单独核对，模型列表里 MiMo/DeepSeek 纯聊天模型不能当作 embedding 服务。跨群、私聊隔离保持默认，不把用户的私聊用于群发。
9. 输出 `改动前后.diff`、回滚步骤、运行日志与十组回放的计数表。明确指出哪些步骤没有在实际 QQ 实测。禁止报告未执行的发送/通过。

## 成功标准
- 下雨截图场景：用户告知“已经换衣服”，自然承接一条消息，不再额外补两条提醒。
- 10 个普通闲聊案例中 ≥8 个为单次 1 条消息；这属于验收目标，不是对所有场景的绝对上限。
- 认真求助时不被固定短句限制；重复提醒和机械反问显著减少。
- 启动、配置热重载及已有表情/钢琴插件不被破坏。
- 默认不增加新的大模型调用；不要启用 LLM 断句或每轮自我评价模型。
