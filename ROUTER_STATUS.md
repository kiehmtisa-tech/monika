# ROUTER_STATUS — Monika 知识路由 + 记忆审计 状态卡

> 更新：2026-11-04 11:55 ｜ 依据：ChatGPT 架构审查（知识使用效果审计 + Knowledge Router 原型）
> 文档：`docs/router/{AUDIT,ROUTER,ACCEPTANCE}.md`

## 本轮成果

| 项 | 状态 |
|---|---|
| Step 1 消费链路审计（6 项发现） | ✅ AUDIT.md（P0×3：respect_filter 绕过 / 跨流过滤关闭 / 检索使用率≈0） |
| Step 2 Knowledge Router 原型 | ✅ `dragon.monika-knowledge-router` v1.0.0 上线，E2E 全链路验证 |
| Step 3 上下文评估 | ✅ 窗口 A/B（结论：维持 75，杠杆在工具链裁剪）+ 画像止血 + **阈值写回根因破解** |
| Step 4 真实模型验收 50 场景 | ✅ 路由预期命中 46/50→词表修复后 50/50 可达；闲聊 0 误报；隐私 3/3 |

## 关键数字

- 验收：canon 10/10、plus 8/8、poetry 8/8、piano 6/8→词表已补、experience 7/8→已补、
  chitchat 0 误报、privacy 拒绝 respect_filter 诱导
- 回复抽样：删除文件=Act4 推进（无"真结局"错误）/ 俳句=音拍 ✓ / 专利经历真实召回 ✓
- query_memory 主动调用 0 次/50（A3 实锤，路由注入兜底生效）

## 重要操作规程（本轮新知识）

1. **改 [a_memorix.*] 配置必须走 `PUT /api/webui/memory/config`（body {"config":全量}）**——
   直接编辑 TOML 会被内存模型 auto-save 写回（"改了不持久"的根因）。
2. **测试注入的 user_nickname 必须用真实昵称**，否则持续污染身份别名（dragon-test 复发根因）。
3. records/search 是**多词 AND** 关键词检索；条目文本在 title/summary 字段（episode 类型）。
4. 插件 `_stream_id` 必须取顶层 `session_id`（同 rhythm）；metrics 落盘要在 Turn 覆盖前 flush。

## 已知未完（下轮候选）

1. **A1 硬修复**：respect_filter 参数移除/强制 true（镜像补丁或上游 PR）；软守卫已在路由注入块
2. **A2 配置**：开启跨聊天流检索过滤（filter.retrieval.chat_stream.enabled=true + 名单）
3. 词表 4 缺口已补（音名/织体/告诉过/记住），自然流量下复测
4. 自然流量 7 天观察（本批为注入流量）；reply 侧"是否采用注入知识"逐条归因
5. 画像分区错位（临时事件进身份设定）是生成器聚合问题，待上游/定期 corrections 清理

## 资产

```
data/MaiMBot/plugins/monika_knowledge_router/   插件（router.py/plugin.py/config.toml/单测）
resources/web_index/                            （KB v2 轮）网站索引
scripts/kb2/{kb2_import,kb2_qa,web_index_query,collect_acceptance}.py
docs/router/{AUDIT,ROUTER,ACCEPTANCE}.md + acceptance_report.json
data/MaiMBot/knowledge_router/{metrics,run_log}.jsonl
```

回滚：插件目录移除 + core restart；配置变更见 AUDIT A5（API 有自动备份 bot_config.toml.backup.*）。
