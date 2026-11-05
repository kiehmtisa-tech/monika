# v5 阶段五：轮换验收 + 工具链优化 + Schema 精简（2026-11-04 上午）

按"上下文next.md"任务书执行。**任务书里点名要做的六节全部完成**：
C-20/E-31/E-32 验收、历史工具调用链优化、工具 Schema 精简、A/B/C/D 对照、
后续记录、Dragon Board 扩展。

## 一、C-20 / E-31 / E-32 验收（按 ACCEPTANCE_TESTS 原文定义）

| 编号 | 定义（原文） | 结果 |
|---|---|---|
| C-20 | 重启后恢复 checkpoint，不重复写回同一窗口 | ✅ 重启前后同窗口重复 flush 零写入 |
| E-31 | A 群私聊内容不能出现在 B 群 Prompt | ✅ 作用域 SQL 验证：A 独有 46 条对 B 不可见 |
| E-32 | 仅允许明确标记为跨群共享的低敏偏好跨群召回 | ✅ 仅 shared_memory_groups / scope_type=global 才跨会话 |

**8 项特别验证**（任务书第一节）全部通过：

| # | 验证项 | 方式 | 结果 |
|---|---|---|---|
| 1 | 连续轮换不产生重复边界 | 注入式：2 次轮换 = 恰好 2 条边界 | ✅ 23/23 套件 T1 |
| 2 | 异常时状态一致恢复 | 故障注入（标记写失败/运行时清理失败） | ✅ T5：单侧失败另一侧照常、如实上报 |
| 3 | 轮换与新消息并发不丢 | 真实注入 + 同时轮换 | ✅ 消息完整入库 |
| 4 | 重启只恢复最新边界之后 | 3 边界 + 3 条消息 + 重启 | ✅ 只留边界后 1 条；原始消息全部保留在 DB |
| 5 | 不同会话互不影响 | 双会话边界计数 | ✅ T3 |
| 6 | A-Memorix 不受影响 | 来源归因（轮换产生 0 条记忆记录） | ✅ |
| 7 | idle flush 不重复写入 | 同窗口二次触发 | ✅ T6-b + C-20 |
| 8 | 工具调用中安全延迟 | **新实现**（见下） | ✅ T2 共 11 项子检查 |

### 验收 #8 的新实现（此前缺失，已补）
`ContextGuard` 增加忙碌跟踪（`maisaka.planner.before_request` 标记、
`after_response` 按是否含工具调用决定续期/解除），轮换遇到忙碌**只排队不执行**，
`drain_pending` 在轮次结束后补执行；Dashboard 指令同语义（延迟=不标完成）。
超 600 秒的僵死轮次自动解除保护。回归：23/23 通过。

## 二、E-31 的真实缺口与修复（本轮最有价值的发现）

验收中发现 idle flush 的会话要点经 WebUI paste 落库后是
`scope_type=global`（**任何会话的 Prompt 都能检索到**），违反 E-31。
根因是粘贴导入的默认作用域是 global。修复：

1. 发现导入 API 原生支持 `scope_type=chat` + `chat_id` 参数（web_import_manager:1140）
2. `flush_one` 改为带 `chat_id` 导入 → 落库 `{"chat_id": ..., "scope_type": "chat"}`
3. 旧的全局作用域 flush 段落补打 chat 作用域（含真实私聊内容那条）
4. 附带发现两条通道事实：**插件运行在独立进程**（RPC runner），
   `memory_service.ingest_summary` 在插件内会进 A-Memorix 启动队列（延迟到下次重启才回放，
   返回"假成功"）；paste 通道即时落库且校验聊天流必须存在（假 chat_id 会被 400 拒绝）

终验：作用域 SQL 重跑，A 独有内容对 B 不可见、flush 要点只在本会话可见。

## 三、历史工具调用链优化（tool_calls ~22% 膨胀源）

实现 `tool_chain.py`「完整工具轮次归档」，经 `maisaka.planner.before_request`
Hook 改写请求 items（该 Hook 的返回会经官方 `deserialize_prompt_items` +
`_validate_function_call_context_anchors` 校验，实测通过）：

- 进行中/配对不完整的轮次：**原样保留**（不截断未完成链）
- 最近 2 个完整轮次：保留（Planner 继续推理需要）
- 更早完整轮次：压缩为一条结论（`[工具轮次归档] 调用 xxx(args摘要) → 结果摘要`）
- 只改发给模型的请求，不动数据库/原始日志
- 结构校验兜底：保留的 call 必须有 output、无孤儿 output，失败则放弃整轮归档

单测（真实 dump）9/9：配对完好、输入不被就地修改、少量轮次不误伤。
live 验证：3 个归档条目进入请求、calls=7 outs=7 无缺口、协议零告警。

## 四、工具 Schema 精简（tool_schema ~25% 膨胀源）

统计（225 次请求）：常驻 10 个工具、Schema 合计 ~4.2k 字符（~2.1k tokens）；
其中 `query_image_memory`/`open_private_chat`/`get_qq_by_msg_id` 在非图片、
非管理场景 0 调用。实现 `tool_policy.py` 场景过滤（同一 Hook 的 tool_definitions）：

- 固定保留 reply/stop/wait/tool_search/query_memory/query_person_profile/send_emoji
- query_image_memory 仅图片轮次；open_private_chat/get_qq_by_msg_id 仅管理员流
- 历史调用过的工具当轮不裁（防历史失去定义）；不动注册表、不影响并发流

live 验证：10→9（无图轮次裁掉 query_image_memory，省 402 字符/次）。

## 五、A/B/C/D 对照（40 个 hook 上线前的真实请求重放）

| 配置 | items 字符/次 | 工具字符/次 | 合计 | 估算 tokens | 相对基线 |
|---|---|---|---|---|---|
| A 基线 | 32556 | 4693 | 37249 | 18625 | — |
| B 只归档 | 27955 | 4693 | 32648 | 16324 | **-12.4%** |
| C 只精简 | 32556 | 4452 | 37008 | 18504 | -0.6% |
| D 两者 | 27955 | 4452 | 32407 | 16203 | **-13.0%** |

live 抽查：同会话启用后 prompt_tokens 12408 → 11117（-10.4%），与离线口径一致。
B 是主要收益；C 在管理员私聊场景小（只裁 1 个），群聊场景会多裁 2 个（~+584 字符）。
延迟/超时率/回复正确性按任务书要求留给 7 天自然流量观察——已验 proxy：
工具选择与回复正常、协议配对零缺口、零"items 无效"告警。

**分类口径修正**（审核意见指出的加总不符）：固定=38.3%（schema 24.7 + system 13.6），
可裁剪=53.9%（历史 27.3 + tool_calls 22.1 + tool_results 4.5），其他=7.9%。
此前"固定 41/可裁剪 59"是把「其他」并入两边的约数，本表为准。

## 六、Dragon Board 扩展（任务书第六节 7 项）

上下文健康页新增两张卡 + API 字段（`tool_opt` 与 `planner.tool_tokens`）：

- ✅ 历史工具调用 Tokens（均值/总量）
- ✅ 工具定义 Tokens（均值/总量）
- ✅ 已归档工具轮次数（含移除条数、节省字符）
- ✅ 每次裁剪前后（last_run 明细 + 累计）
- ✅ 工具调用协议错误次数（protocol_aborted，当前 0）
- ✅ 最近轮换时间与持久化状态（marker_written / rotate_count 比例）
- ✅ 自动轮换开关及不可启用原因（当前：未开启，按序待 tool_result_condense 与调参后评估）

插件每 5 秒把统计落盘 `governance/tool_opt_stats.json` 供 Dashboard 跨进程读取。

## 七、知识库检索记录（任务书第五节）

- 8/9 单关键词命中（KB_INDEX.md 已录）；失败 1 例与多词查询已记录：
  **多词查询是 AND 语义**，长句如「十四行诗 诗歌形式」召回 0（两词必须同段命中）。
  按任务书要求：不扩大知识库、不换检索算法，留给 retrieval 调优轮（并行会话正在做知识库）。
- ⚠️ 协同提示：另一会话今晨在做知识库扩充（09:14 改了 a-knowledge 文档、09:18 重导入，
  新增 ddic_canon 04-08、literature_music 03-05 等）。本次未动其内容。

## 八、需要审核的事项

1. **工具归档结论文案**（`[工具轮次归档] 此前的工具步骤已压缩：...`）——影响 Planner 的
   行为语义，建议抽样看几次真实请求的 dump 确认语气自然
2. **Schema 场景策略**（管理工具仅 owner 流可见）——如果群聊里也需要开私聊动作，把
   ADMIN_ONLY 的判定放宽即可
3. **自动轮换开启时机**——任务书定位它是"最后一道保护"，目前保持关闭是按计划执行；
   要提前开的话 Dashboard 按钮随时可以
4. keep_recent=2（保留最近 2 个完整工具轮次）——调大更保守、调小更省

## 九、未做（按任务书顺序）

1. **tool_result_condense A/B**——任务书第四节列为工具链/Schema 之后的下一项；
   其定位是"需要额外模型调用，测量收益是否超过延迟与费用"，留作独立一轮
2. **延迟 P50/P95 的 7 天观察**——本轮只做 token 与正确性 proxy（任务书允许）
3. 多词 AND 召回改造（明确要求"暂时不要大规模替换检索算法"）

## 十、资产

- 插件新增/更新：`tool_chain.py`、`tool_policy.py`、`plugin.py`（before_request 改写）、
  `context_guard.py`（忙碌保护）、`flush.py`（scoped 写回 + checkpoint）
- Dashboard：`maibot_ctx.py`（tool_opt/tool_tokens/guard 持久化字段）、`MaiBotContext.vue`（两张新卡）
- 测试：`test_rotation_acceptance.py`（23 项）、`test_restart_acceptance.py`（#3/#4/C-20）、
  `test_tool_chain.py`（9 项）、`test_e31_scope.py`（作用域 SQL）、`ab_tool_optimization.py`
- 关键机制备忘：**Hook handler 必须返回 `{"modified_kwargs": {...}}` 信封**，
  裸字典会被 dispatcher 丢弃（本次踩过，已修正并写进代码注释）
