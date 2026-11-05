# PERSONA_STATUS｜Monika 人格拟人化改造（v2 → v3）

> 本文档记录人格改造的完整过程、证据与回滚方式，按"再做一次能不能复现"的标准书写。
> 上一版人格基线见 `persona/active/v2_inline_baseline_20261103.json`，本版生效内容见 `persona/active/CURRENT.json`。
> 方案来源：`persona/plans_v3/`（ChatGPT 交付的 Monika 自然聊天拟人化改造 v3 包）。

## 1. 目标

用户反馈：**"每次都是我说一句她回三句"**，像客服三段式，不像真人。

v3 的判断：问题不只是"太长"，而是三件事叠在一起——
1. `response_splitter` 把一次回复按规则拆成最多 3 条气泡（机械三连的直接来源）；
2. 人设只写"她是谁"，没写"这一轮该说多少"，模型默认补齐关怀、理由、建议、追问；
3. `chinese_typo` 用人为错别字伪造真人感，反而显得刻意。

因此本轮改动分两层：**行为开关层（配置）** + **人设层（prompt 字段）**，都在 `bot_config.toml`，不碰 MaiBot 核心、不加模型调用、不改插件。

## 2. 时间线（2026-11-03）

| 时间 | 动作 | 结果 |
|---|---|---|
| 20:30 | 摸底：人设生效链路、git 跟踪范围、归档口径 | 发现 `docker-config/mmc/` 整体在 .gitignore 内，人格正文无版本历史 |
| 20:34 | 新增 `scripts/persona_apply.py` + `persona/active/` 受控目录 | 具备"快照 / 应用 / 校验 / 回滚"能力 |
| 20:34 | `persona_apply.py snapshot` | 导出 v2 基线 → `v2_inline_baseline_20261103.json` |
| 20:35:02 | 首次 apply（15 处改动） | 成功，但行尾注释被覆盖 |
| 20:35:17 | 从备份还原 + 改进脚本（保留行尾注释）后重跑 | 热重载命中 `personality / chat / chinese_typo / response_splitter` |
| 20:36–20:47 | E2E 注入 6 个验收场景 | 6/6 单气泡，详见第 5 节 |

## 3. 改了什么

### 3.1 人设字段（`[personality]` / `[chat.reply_style]`）

| 字段 | v2 字数 | v3 字数 | 变化要点 |
|---|---:|---:|---|
| personality | 400 | 220 | 只留身份、审美、矛盾面；删掉"文学部往事"式长铺垫 |
| behavior_style | 982 | 658 | 从"人格原则"改为"谈话不是答题"：先判断社交作用，做一件最自然的事 |
| reply_style | 257 | 286 | 明确单条消息、约 4–25 / 10–55 字的参考区间（非硬截断），禁止三段式 |
| group_chat_prompt | 185 | 131 | 群聊"先听、只对有关的话题开口、不一条群消息发三条" |
| private_chat_prompts | 197 | 168 | 报平安/确认/收尾时一句承接就够；不编造工具结果、不伪造记忆 |
| **合计** | **2020** | **1463** | **-27.6%（每轮 prompt 少约 560 字，≈370 tokens）** |

### 3.2 行为开关（白名单，只允许以下字段）

| 配置 | 旧值 | 新值 | 作用 |
|---|---|---|---|
| `[response_splitter].enable` | true | **false** | 停用自动拆气泡（三连的首要开关） |
| `[response_splitter].max_split_num` | 3 | **1** | 双保险 |
| `[chinese_typo].enable` | true | **false** | 不用人为错别字 |
| `[chat.reply_timing].talk_value` | 0.4 | 0.5 | 仅群聊发言倾向；`talk_value_rules` 白天档已同步为 0.5，夜间仍 0.1 |
| `[chat.reply_style].private_quote_frequency` | occasionally | never | 私聊不引用 |
| `[chat.reply_style].group_quote_frequency` | occasionally | occasionally | 保持 |

`mode`、`max_sentence_num`、`mentioned_bot_reply`、`inevitable_at_reply` 等未改。

## 4. 落盘清单

| 路径 | 作用 | 是否入 git |
|---|---|---|
| `PERSONA_STATUS.md` | 本文件：全过程、证据、回滚、边界 | ✅ |
| `scripts/persona_apply.py` | 人设应用器：`snapshot` / `apply <json> --knobs <toml> --sync-talk-rules`，profile 可省略；写前备份、写后解析校验、失败自动回滚 | ✅ |
| `scripts/test_rhythm.py` | 词法分类回归测试（9 组用例 42 断言） | ✅ |
| `scripts/e2e_probe.py` | 验收探针：注入一条私聊 → 统计真实 QQ 气泡数与回复内容 | ✅ |
| `scripts/inject_msg.py` | 往 core 容器注入模拟私聊消息（原 /tmp 临时脚本，已转正式） | ✅ |
| `data/MaiMBot/plugins/monika_rhythm/` | 阶段 2 插件（plugin.py / rhythm.py / config.toml / _manifest.json） | ✅ |
| `persona/active/v2_inline_baseline_20261103.json` | v2 基线（可一键回滚） | ✅ |
| `persona/active/monika_v3.json` | v3 人设源（唯一事实源） | ✅ |
| `persona/active/knobs_v3.toml` | v3 行为开关（白名单 + 注释说明） | ✅ |
| `persona/active/knobs_latency.toml` | 上下文窗口实验记录（无效已还原，留档） | ✅ |
| `persona/active/CURRENT.json` | 当前生效的快照（apply 自动写） | ✅ |
| `persona/active/APPLIED.jsonl` | 应用历史（时间、源文件、备份路径、改动字段） | ✅ |
| `persona/plans_v3/` | v3 方案包原件（README/验收案例/状态机/配置片段） | ✅ |
| `data/MaiMBot/rhythm/metrics.jsonl` | 每轮观测：stream/turn/intent/hint/replies/bubbles | ❌（运行时数据） |
| `backups/persona_*_bot_config.toml` | 每次 apply 前的整份配置备份 | ❌（.gitignore） |
| `docker-config/mmc/bot_config.toml` | 运行时真实配置 | ❌（含密钥目录，不入库） |

### 4.1 复现一次验收（三条命令）

```bash
cd /opt/qq-fish
python3 scripts/test_rhythm.py                                   # 词法回归，应全绿
docker cp scripts/inject_msg.py maim-bot-core:/tmp/inject_msg.py # 探针依赖容器内脚本
python3 scripts/e2e_probe.py "困了，先去睡了，明天聊"              # 注入 → 打印气泡数/回复/Token
tail -3 data/MaiMBot/rhythm/metrics.jsonl                        # 交叉核对 intent/replies/bubbles
```

本地镜像：`C:\Users\15438\Videos\nas\persona_work\`（脚本 + 人设源 + 插件源码 + 本文件副本）。

## 5. 验收证据（E2E，真实注入 → 真实 QQ 发送）

口径：`replyer` 输出 + `send_service` info 发送记录条数（真实气泡数）。模型 `mimo-v2.6-flash-gw` / 网关 `deepseek-flash-gw`。

| # | 注入 | 实际回复 | 气泡 | 判定 |
|---|---|---|---:|---|
| 1 | 换了的，回到宿舍马上就换了 😝 | 行，那我等你优化版 | 1 | ⚠️ 上下文被既有钢琴话题污染，不作为通过样本 |
| 2 | 到了 | 到了就好 | 1 | ✅ 承接即停，无重复提醒 |
| 3 | 晚安，我睡了 | 晚安，睡个好觉 | 1 | ✅ 不追问计划 |
| 4 | 今天写完专利了 | 刚说完晚安又回来报喜，写完就好，这下能睡踏实了。 | 1 | ✅ 记得上一轮，未复述未三段总结 |
| 5 | 今天有点烦 | 烦就烦吧，不用急着说清楚是哪儿烦。想说的时候我听着呢 | 1 | ✅ 接住情绪，不假装诊断 |
| 6 | 你觉得漫画算文学吗？ | 算，不过我承认早年有点"只有文字才算"的偏见。后来觉得分镜的节奏、停格留白，跟诗挺像的。关键看有没有真想表达的东西，载体倒其次。你问这个，是看到什么好作品了？ | 1 | ✅ 有观点、承认偏见、只问一个 |

- **单气泡率：6/6**（含被污染样本），字数区间 4–76 字，无一例机械三连。
- 热重载证据：20:35:17 `检测到配置文件变更，触发热重载` → `配置热重载完成` → 命中 `['personality', 'chat', 'chinese_typo', 'response_splitter']`；容器全程未重启（`restarts=0`）。
- 新 prompt 已生效：`replyer` 的 system prompt 首段为 v3 文本（已抽样核对）。
- 资源：改造期间 load 0.07–0.17，内存 used 804M/1.68G，三容器 healthy。

## 6. 回滚

```bash
# 方式 A：回滚到某次 apply 之前的整份配置（最快）
cd /opt/qq-fish
cp backups/persona_20261103_203502_bot_config.toml docker-config/mmc/bot_config.toml   # 该文件 = v2 基线
# 约 1–20 秒后 MaiBot 自动热重载，无需重启容器

# 方式 B：用受控源重新应用旧人设
python3 scripts/persona_apply.py apply persona/active/v2_inline_baseline_20261103.json

# 方式 C：仅回滚脚本/源文件本身
git -C /opt/qq-fish revert <commit>
```

回滚后核查：`/opt/qq-fish/data/MaiMBot/logs/app_*.log.jsonl` 里出现 `配置热重载完成`，且 `logs/maisaka_prompt/replyer/` 新文件的 system prompt 恢复旧文本。

## 7. 已知边界与未验证项

1. **案例 1 未取到干净样本**：注入进了正在进行的钢琴话题，回复偏离预期；不影响"气泡数"结论，但"报平安场景"待自然流量复核。
2. **群聊未实测**：`talk_value` 0.5 与"不一条群消息发三条"只做了配置与人设层改动，群聊行为待观察（成本也更高）。
3. **主动聊天链路未回归**：`idle_proactive_chat` 插件独立触发，本轮未改，其话术仍走新 prompt，需观察是否出现"主动一次+追问一次"的叠加。
4. **长回答场景（验收案例 10）未测**：`max_split_num=1` 不截断内容，但超长回复会变成一整条长消息，是否符合观感待自然出现时判断。
5. **一次 planner 硬超时**：20:35:04 出现 `触发硬超时 120.0s`（改造前窗口），19 分钟内仅此 1 条 error，后续 6 次请求正常；与配置重载的因果关系未确认，列入观察。
6. **配置注释仍描述旧行为**：`chinese_typo.enable = false # 让麦麦偶尔打错字…` 这类行尾注释是原生注释，值与注释语义相反，以 `persona/active/knobs_v3.toml` 为准。
7. **人设库未启用**：`[personality].profiles` 仍为空，切换/回滚靠 `persona_apply.py`，不用 WebUI 人设库。

## 8. 阶段 2：会话节奏插件 monika_rhythm（21:0x 上线）

v3 方案的 P2（词法判定"已照做/告别"等，只给提示）与 P1（观测每条入站对应的 reply 次数与最终气泡数）合并成一个插件，**零新增模型调用**。

### 8.1 为什么这样接

| 通道 | 结论 |
|---|---|
| `maisaka.replyer.before_request` + `extra_prompt` | ✅ 采用。官方 Hook 明确写着"可追加到本次 replyer 提示词中的额外回复要求"，天然是"只提示不覆盖"，且只影响本轮 |
| `maisaka.context.append`（idle 插件用的写法） | ❌ 会永久写进聊天流历史，提示词越攒越多 |
| `maisaka.reply.before_post_process` 改输出 | ❌ 等于改写模型输出，违背"不覆盖原意" |
| 回复扩展（ReplyExtension） | ❌ 需要 Planner 主动在 reply 工具参数里选，不能自动生效 |

### 8.2 判定表（本地词法，含糊一律不判）

| 状态 | 命中示例 | 注入的节奏参考 |
|---|---|---|
| PARTING | 晚安 / 我先撤了 / 困了先去睡 | 对方在收尾：只回一句自然道别，不要提问。 |
| ACK_DONE | 到了 / 衣服换了 / 那就好 | 回一句承接就收住，不要提问、不要重复提醒。 |
| SHARING | 专利写完了 / 跑通了 / 上线了 | 给一次具体认可或一个好奇，不要复述全篇、不要总结。 |
| EMOTION | 有点烦 / 心累 / 有点丧 | 先接住情绪，不要诊断，也不要连安慰三句。 |
| QUESTION | 带问号 / 吗 / 帮我 | 按内容完整回答，不要为了短而截断。 |
| HUMOR | 哈哈 / 笑死 / 离谱 | 接住梗，别说教。 |
| NOISE | 纯表情 / 纯图片 | 可以不回或只回极短一句。 |

判定顺序：PARTING → NOISE → QUESTION（短问句）→ SHARING → ACK_DONE → EMOTION → HUMOR → 其余返回 None（交给模型）。

### 8.3 文件与配置

| 路径 | 说明 | 入 git |
|---|---|---|
| `data/MaiMBot/plugins/monika_rhythm/{plugin.py,rhythm.py,config.toml,_manifest.json,__init__.py}` | 插件本体 | ✅ |
| `scripts/test_rhythm.py` | 词法回归测试，`python3 scripts/test_rhythm.py` | ✅ |
| `data/MaiMBot/rhythm/metrics.jsonl` | 每轮观测：stream/turn/intent/hint/replies/bubbles | ❌（运行时数据） |

配置项：`[rhythm].enabled`（关掉只留观测）、`[rhythm].states`（只对指定状态提示）、`hint_max_chars=60`、`[metrics].enabled/path/max_bytes`。

### 8.4 验证

- 单测：9 个用例 42 条断言全过（`scripts/test_rhythm.py`，本机与服务器均跑过）。
- 热加载：20:54:57 插件加载 → 改 rhythm.py 后自动重载，无 error/warning。
- 线上实测（真实 QQ 发送，`metrics.jsonl` 与 `send_service` 日志为准）：

| 时间 | 注入 | 判定 | 回复 | reply 次数 | 气泡 |
|---|---|---|---|---:|---:|
| 20:55 | 到了 | ACK_DONE | 今晚第二次说到了，这是到哪儿了 | 1 | 1 |
| 20:57 | 衣服换了，房间也收拾好了 | ACK_DONE | 动作挺全套，那今天就到此为止吧 | 1 | 1 |
| 20:58 | 报告个好消息，专利终于写完了 | SHARING | 这好消息今晚报第二遍了…我陪你多高兴一次 | 1 | 1 |
| 20:59 | 今天被领导说了一顿，有点丧 | EMOTION | 刚还高兴着呢…他说你什么了？ | 1 | 1 |
| 21:01 | 困了，先去睡了，明天聊 | PARTING | 行，明天聊，那事想说再说 | 1 | 1 |
| 21:16 | 我先去忙了，晚点聊（超时恢复后） | PARTING | 行，去忙吧，方案跑通的事我记着呢 | 1 | 1 |

- 第一行是唯一一次"提示了仍追问"：当时提示原文较软（"不追问"），模型因上下文里"到了"重复出现而反问。之后把 7 条提示改成更具体的指令式表述（"回一句承接就收住，不要提问"），21:03 之后的 ACK 用例不再追问。
- `metrics.jsonl` 已加双重去重：同一 `message_id` 只计一次气泡、同一轮计数不变不重复写行。

### 8.5 已知边界

1. **上游 planner 间歇超时**：19:23/19:25/19:51/20:35/21:06 各有 1 次 120s 硬超时（改造前就存在）；21:04–21:10 连续 3 次注入未得到回复，日志为 `APITimeoutError 60s`，与插件无关（插件只改 replyer 的 extra_prompt）。21:06 的失败请求上下文已达 106 items / 115KB。
2. **测试把私聊上下文撑大了**：今晚约 14 条注入消息留在真实私聊历史里，planner 输入从 12.6k 涨到 18.3k tokens、耗时 20–58s（同一模型、体量相近时波动很大）。会随正常聊天按 75 条窗口自然滑出。
3. **隔离测试流不可用**：用伪造 user_id 注入的消息在 MaiBot 侧被直接丢弃（日志无记录），因此 HUMOR / NOISE / QUESTION 三个状态只有单测覆盖，缺真实流量样本。
4. **只做提示，不保证遵守**：词法判定错误的代价是多一句无关的节奏参考，不会改写内容；`states=[]` 时可整体关闭提示层，只留观测。
5. **上下文窗口实验（已还原）**：21:14 试过把 `chat.max_private_context_size` 75→40，21:16 实测 planner 仍是 18.3k tokens / 109 items、55.8s——说明上下文规模由工具结果与系统块决定，不是消息条数，故已还原为 75。实验记录见 `persona/active/knobs_latency.toml`。

### 8.6 回滚

```bash
# 只关提示，保留观测
sed -i 's/^enabled = true$/enabled = false/' /opt/qq-fish/data/MaiMBot/plugins/monika_rhythm/config.toml  # [rhythm] 段
# 或整体下线插件
mv /opt/qq-fish/data/MaiMBot/plugins/monika_rhythm /opt/qq-fish/backups/monika_rhythm_off
# MaiBot 文件监视会在 ~30s 内卸载插件
```

## 9. 下一步（待决定）

- **阶段 2（方案已备）**：`persona/plans_v3/MONIKA_会话节奏与状态机.md` —— 意图分类 + 何时承接/追问/沉默/主动；需要插件/调度器实现，不能只写文档。
- **群聊侧观察**：`talk_value` 0.5 上线后看 1–2 天群聊活跃与 token 曲线，再决定是否回调到 0.4。
- **长期记忆**：`A_Memorix` 仍关闭；开启前必须确认 embedding 模型与跨会话隔离，先做小范围灰度。
- **人设库化（可选）**：把 `CURRENT.json` 导入 WebUI「身份与人格」成为 profile，获得一键切换 UI；代价是多一份可能漂移的副本。

---
更新于 2026-11-03 20:50（作者：WorkBuddy）
