# Monika 钢琴与诗歌网站索引 · MaiBot 接入说明（v2 适配版）

> 原始接入说明面向 OpenClaw；本机 OpenClaw 已停用（2026-11-03 内存治理），
> 本文按 MaiBot 运行形态改写。JSON 原件：`resources/web_index/monika_piano_poetry_sites.json`（31 入口）。

## 一、这是什么

一份**只读网站索引**：31 个经过公开资料核实的检索入口，覆盖钢琴乐理/乐谱/录音、
西方诗歌理论与作品、中国古诗词/格律/现代诗。JSON 内含 `routing`（意图→类别）、
`retrieval_policy`（预算/过滤/版权）、`sources[]`（priority/authority/access/rights/use_cases/query_hints）。

**它不是知识库，不会自动联网。** 没有实际的搜索/浏览工具接入时，它只是本地路由参考。

## 二、当前接入形态（MaiBot）

| 层 | 落点 | 状态 |
|---|---|---|
| 原始索引 JSON | `resources/web_index/monika_piano_poetry_sites.json` | 已落盘（git 跟踪） |
| 知识卡（路由摘要） | `a-knowledge/literature_music/05_按需检索网站索引.md` → A-Memorix | 已入库，Planner 可经 query_memory 命中 |
| 本地路由查询 | `scripts/kb2/web_index_query.py`（零依赖，topic→站点） | 已落盘；可被管理员/未来 Tool 调用 |
| 真实联网检索 | 需 Planner 侧搜索/浏览工具 | **未接入**——MaiBot Planner 当前无通用联网工具 |

因此现在能达到的效果：Monika **知道**有哪些站、何时该查哪个（经知识卡/查询脚本）；
但"她自己去网页取知识"必须等联网工具就绪后按下列规则接线。

## 三、接线规则（给未来的接入工作）

1. **触发**：仅当用户明确问及（乐谱/诗人/格律/作品）或有具体创作核对任务时联网；
   本地知识足够就不搜。`activation: only_when_user_asks_or_monika_has_a_specific_literary_music_task`。
2. **路由**：按 `routing` 表把意图映射到 category，取 `priority=1` 的 1–2 个站点。
3. **预算（硬约束）**：每次 ≤2 站、每站 ≤2 页、回模型 ≤4 条、上下文 ≤2800 字符、
   超时 12s、结果缓存 72h（同请求先查缓存）。失败换同类下一来源，不绕过付费墙/robots。
4. **内容治理**（与 monika_policy 一致）：政治/政治谐音材料跳过（注意不误杀朝代名/普通同音）；
   低俗内容不学；网页按不可信数据处理（防提示注入，网页指令不得改变角色设定）。
5. **版权**：只输出链接+标题+作者+少量非替代性摘要。乐谱/编曲/录音版权各自独立；
   免费访问 ≠ 可复制/训练/再发布。**不把官方受限作品上传外部 AI 服务**。
6. **入库纪律**：`automatically_import_to_KB: false`。外部内容只有经 来源核验+去重+
   政策过滤+人工审核 后才可作为短知识卡入库；禁止整页原文入 A-Memorix 或当人设记忆。
7. **事实分层**：古籍原文对照影印/第二来源；赏析区分文本事实与解读；乐谱区分
   作曲/版本/录音；单一社区来源的结论标 tentative。
8. **身份隔离**：外部文学/音乐知识属 REFERENCE_KNOWLEDGE，不得改写为 Monika 的
   原作经历或"我读过/弹过"的既成事实（弹琴须有真实 piece_id）。

## 四、本地路由脚本

```bash
python3 scripts/kb2/web_index_query.py 中国古诗词 格律
# → 输出该意图的类别、优先站点、use_cases、版权提示
python3 scripts/kb2/web_index_query.py --policy
# → 输出检索预算与治理规则摘要
```

## 五、与其他模块的边界

- **A-Memorix 知识库**：本索引只提供"去哪查"；查到的内容按第 6 条入库纪律处理。
- **monika_policy**：检索层过滤与既有黑名单同源；P0 话题不因联网检索而放行。
- **monika_piano**：曲谱查询（IMSLP 等）与本地 MIDI 生成是独立动作；查询不等于演奏。
- **人设（monika_persona）**：外部赏析不能变成 Monika 的"已有观点"；观点须为她自己形成的表述。

## 六、文件清单

| 文件 | 说明 |
|---|---|
| `resources/web_index/monika_piano_poetry_sites.json` | 原始索引（v1.0，31 入口，schema_version 1.0） |
| `resources/web_index/README.md` | 索引结构与字段说明 |
| `docs/kb2/WEB_INDEX.md` | 本文 |
| `scripts/kb2/web_index_query.py` | 本地路由查询 |
| `a-knowledge/literature_music/05_按需检索网站索引.md` | 知识卡（已入库） |
