# Monika 记忆消费链路审计报告（Step 1）

日期：2026-11-04 10:30–11:10 ｜ 依据：ChatGPT 架构审查（2026-11-04）"知识使用效果审计"第一步
范围：只读审计 + 本轮 Router 原型附带修复；未改人设、未重导知识库。

## 审计结论速览

| # | 发现 | 级别 | 状态 |
|---|------|------|------|
| A1 | query_memory 的 respect_filter 参数对 LLM 开放，false 可绕过聊天流过滤 | **P0** | 软守卫已加 + 硬补丁待定 |
| A2 | 跨聊天流检索后置过滤全局关闭（filter.retrieval.chat_stream.enabled=false） | **P0** | 已记录，建议开启配黑名单 |
| A3 | 真实对话几乎从不触发记忆检索（query_memory 全量仅 5 次且全在测试会话） | **P0** | **本轮回 Router 原型解决** |
| A4 | 画像污染：临时事件进"身份设定/关系设定"、dragon-test 别名残留、跨节重复 | P1 | 污染源已止血 + 记录 |
| A5 | chat_summary 阈值被运行时写回（12→72→144，3 次复现） | P1 | **根因已定位并修复** |
| A6 | 启发式召回三开关全关（含 cross_chat） | OK | 符合审查建议，保持 |

## A1（P0）respect_filter 可被模型绕过 —— 访问控制非强制

**证据**：
- 工具 schema（`/MaiMBot/src/maisaka/builtin_tool/query_memory.py:63-68`）把 `respect_filter`
  声明为 boolean 工具参数，描述原文："是否遵守记忆过滤规则；默认true，模糊来源或整体印象可false"。
- 执行侧 `query_memory.py:238`：`respect_filter = bool(invocation.arguments.get("respect_filter", True))`
  → 直接透传 memory_service.search()。
- 过滤实现（`A_memorix/core/runtime/services/chat_filter_service.py:72-88`）：
  **`if not bool(respect_filter): return False`（= 不过滤）**，跳过全部 stream/group/user 范围检查。

**影响**：LLM 传 `respect_filter=false` 即可读取当前聊天流过滤范围之外的记忆（跨群/跨私聊）。
正常对话中模型按默认 true 行为良好，但这是"由 LLM 自觉遵守"而非服务器强制的访问控制；
prompt 注入或模型误判即可触达。

**本轮缓解**：
1. Knowledge Router 注入软守卫："调用 query_memory 时 respect_filter 必须保持 true"。
2. `privacy_guard_mode` 支持 always（每轮注入守卫行）。

**待定硬修复**（三选一，建议上游修复为主）：
- 镜像内补丁（docker cp，重建即失效）：删掉 schema 里的 respect_filter 参数并在执行侧硬编码 True；
- 插件替换法：`enable_memory_query_tool=false` 关内置工具，插件注册同名工具并强制 True（有切换风险）；
- 上游 PR：服务端强制 respect_filter=True 或收窄 false 的语义。

## A2（P0）跨聊天流检索过滤处于关闭状态

**证据**：`bot_config.toml` [a_memorix.filter.retrieval.chat_stream] `enabled=false, mode=blacklist, chats=[]`。
该配置是"普通 paragraph/relation 命中的跨聊天流检索后置过滤"——即检索默认跨流召回，
后置过滤是唯一范围限制，当前整体未启用。
（heuristic_memory_cross_chat / group↔private 三项为 false，是另一层，符合建议。）

**影响**：即使 respect_filter=True（无绕过），query_memory 的语义检索仍可能把其他聊天流的
段落召回进当前对话（如群聊内容进私聊）。隐私风险与 A1 叠加。

**建议**：按隐私边界配置 enabled=true + blacklist/whitelist（私聊流与群聊流互斥）；
开启后用 Step 4 的 privacy 组场景回归。

## A3（P0）知识检索使用率 ≈ 0 —— Router 的存在理由

**证据**（全量 app 日志工具调用统计，含历史重启）：
```
116 reply / 88 stop / 10 wait / 8 tool_search / 5 query_memory / 4 play_piano
2 send_emoji / 1 query_person_profile / 1 view_forward_message / 1 save_poem / 1 get_poem / 1 find_poem / 1 link_poem_piano
```
- `query_memory` 仅 5 次、`query_person_profile` 仅 1 次，且**全部发生在 dragon-test 测试会话**；
  真实自然流量中检索调用为 0。
- 即：KB v2 导入的知识能被检索（接口验证过），但 Planner 在真实对话中**想不起来查**。

**本轮解决**：Knowledge Router（`dragon.monika-knowledge-router`）以本地词法规则识别检索意图，
预取+来源过滤后注入 2-3 条可信参考进 planner 系统块，E2E 验证分类/预取/注入/到达模型全链路
（详见 ROUTER.md 与 ACCEPTANCE.md）。

## A4（P1）画像污染

当前画像 v62（主用户）实测问题：
1. **临时事件混入稳定区**："在公司还有专利相关事务要处理"出现在「身份设定」；
   "正在玩酒馆战棋"出现在「关系设定」——应属「近期互动」（该区也有，造成跨区重复）。
2. **测试别名残留**：别名仍含 `dragon-test`（v5 清过又回来——**注入测试用 nickname=dragon-test
   会持续重新污染身份层**）。
3. **跨区重复**："收藏了诗作 poem_4a57f34e287a"在「稳定了解」「相处偏好」各出现一次；
   "偏好诗的结尾写得更轻"同样两次。
4. **称谓混用**："他"与"目标人物"混用（v5 遗留问题复发/未清完）。

**本轮止血**：测试注入脚本 `scripts/inject_msg.py` 的 user_nickname 从 dragon-test 改为真实昵称，
停止测试污染源。**存量清理未做**（画像生成器的分区逻辑是上游问题；claims 层的分类 v5 已做过一轮，
生成器聚合到 profile_text 时仍有错位）——建议后续用 WebUI facts/corrections 或 fuzzy-modify
做一次分区级清理，并向上游反馈 profile 分区规则。

## A5（P1）chat_summary 阈值写回 bug —— 根因定位

**现象**（3 次复现）：文件改 12 → 运行时写回 72（11-03）/ 144（11-04）。
**根因**（本轮定位）：`[a_memorix.*]` 配置的**权威模型在内存**（memory runtime config），
它会 auto-save 自己的配置快照回 bot_config.toml。直接编辑 TOML 只改了文件，
内存模型仍持旧值 → 下次 auto-save 用旧值覆盖文件。表现就是"改了不持久"。
**验证**：改走官方 `PUT /api/webui/memory/config`（body `{"config": {...}}` 整体回写）后，
内存与文件同时变为 12/12，20s 后不再回写。
**操作规程（重要）**：改 [a_memorix.*] 必须走 memory config API；改 bot_config 其他 section
（chat/personality 等）仍可文件编辑 + 热重载。bak_runtime 现象由此解释。

## A6（OK）启发式召回

`heuristic_memory_recall_enabled=false`、cross_chat/group↔private 三项均 false。
符合审查"先做 Router 再评估启发式"的建议；如后续试开，严格按审查建议保持 cross_chat=false。

## 方法与可复现性

- 工具调用统计：`grep -oE "工具=[a-z_]+" data/MaiMBot/logs/app_*.log.jsonl | sort | uniq -c`
- respect_filter 链路：`docker exec maim-bot-core grep -rn respect_filter /MaiMBot/src`
- 画像检查：sqlite 读 person_profile_snapshots 最新版全文
- 阈值验证：memory config API GET/PUT + 文件 grep 20s 盯盘
