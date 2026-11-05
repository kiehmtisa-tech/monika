# Monika Knowledge Router（知识路由插件）v1.0.0

id: `dragon.monika-knowledge-router` ｜ 位置：`data/MaiMBot/plugins/monika_knowledge_router/`
解决审计 A3：知识已入库但真实对话几乎从不触发 query_memory（全量日志仅 5 次且全在测试会话）。

## 设计（对应 ChatGPT 审查 Step 2）

```
入站消息 → 词法分类（零 LLM）→ 命中范围？ ──否──> 跳过（闲聊零开销）
                │是
                v
        按单词预取 records/search（WebUI 8001 只读）
                v
        来源前缀过滤 + 时间线加权 + 去重（可审计）
                v
        2-3 条 + 隐私守卫 → 追加进 planner 系统块
                v
        Planner 仍可自由调用 query_memory（不替代）
```

- **分类范围**：canon（本篇剧情）/ plus（支线，独立时间线）/ poetry / piano / experience（共同经历）。
  触发词表见 `router.py` TRIGGERS；experience 独占时不去预取原作知识；最多 2 个范围。
- **来源过滤**：SOURCE_MAP 把范围映射到 `web_import:ddlc_canon__*` 等前缀；
  person_fact/chat_summary 只在 experience 范围出现——**结构化来源过滤在工具层强制**，
  不依赖 LLM 自觉（审查第 2 点）。
- **预取口径**：records/search 是关键词匹配（**多词 AND 语义会 0 命中**），因此按单词
  逐个查询（≤3 词）合并去重，总候选 ≤8，采纳 ≤3，注入 ≤900 字。
- **隐私守卫**：注入块尾部固定一行"respect_filter 必须保持 true"（软提示；
  硬修复见 AUDIT.md A1）。`privacy_guard_mode=routed|always|off`。
- **零新增模型调用**：分类与过滤全本地；预取是只读 HTTP（100ms 级）。

## E2E 实测（2026-11-04）

输入："莫妮卡，Plus 支线 Respect 里你对夏树漫画的偏见是怎么回事？"
1. 分类：scopes=['plus','canon'] triggers=['plus','支线','respect','夏树'] ✓
2. 预取：候选 2 → 采纳 2：`web_import:ddlc_canon__08_人物关系_本篇` + `web_import:ddlc_canon__11_Plus支线总索引` ✓（正是该问题的正确来源）
3. 注入 623 字节到 planner 系统块 ✓
4. planner dump（1793760991891.json）含「知识路由」「隐私守卫」「人物关系_本篇」「Plus支线总索引」✓（确认到达模型）
5. 观测落盘 `data/MaiMBot/knowledge_router/metrics.jsonl` ✓

单测：`test_router.py` 23 项（分类/优先级/过滤/时间线加权/注入构造）本机与服务器全绿。

## 开发中踩的坑（复用价值高）

1. **`_stream_id` 必须取顶层 `session_id`**（rhythm 同款）——只查 message_info/顶层 stream_id
   会拿不到流 ID 而提前返回，表现为"Hook 不报错但计数全零"。
2. **records/search 多词 AND**：`"plus 支线 respect"` 直接 0 命中；单词逐查合并才有效。
3. **records/search 条目字段**：episode 类型内容在 `title`+`summary`（不是 content/preview）；
   取文本要覆盖 content/summary/preview/description/title/name。
4. **新插件目录必须 `docker compose restart core`**；已有目录改文件走 FileWatcher 热重载（~30s）。
5. 插件目录内跑 python 会生成 __pycache__ 触发反复 reload → 测试用 PYTHONDONTWRITEBYTECODE=1。
6. 并行会话会重启 core（本轮期间发生 4+ 次重启风暴），验证要看**全部** app 日志而非仅最新。

## 配置（config.toml）

```toml
[plugin]  enabled = true
[router]  inject_enabled / prefetch_limit=8 / max_inject_items=3 / max_inject_chars=900
          webui_base="http://127.0.0.1:8001"（容器内）/ webui_json="/MaiMBot/data/webui.json"
[privacy] privacy_guard_mode="routed"
[metrics] metrics_path="/MaiMBot/data/knowledge_router/metrics.jsonl"
```

## 边界（如实）

- Router 解决"该查没查"的**概率**问题，不保证 100% 召回；Planner 仍可能不采用注入参考。
- 词法分类有意保守（闲聊不触发）；漏路由的代价小于误路由的 token 浪费。
- 观测指标 `metrics.jsonl` 记录 scopes/triggers/picked_sources/injected，验收统计见 ACCEPTANCE.md。
