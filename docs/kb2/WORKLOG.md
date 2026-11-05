# KB v2 工作日志（原作纠错与知识覆盖 + 网站索引接入）

日期：2026-11-04 08:21–09:45 ｜ 依据：`docs/kb2/plan/`（ChatGPT 规划包原件，19 文件）
执行：WorkBuddy 会话「记忆库/知识库/画像改进」 ｜ 标签：`kb2-v1.0`

## 0. 背景与范围

任务：①按 `01_事实准确性修订方案` 修订现有 8 篇知识文档；②按 `02_角色知识覆盖补充方案`
建设 P0 批 10 篇扩充文档；③接入「钢琴诗歌按需检索网站索引」（31 源 JSON）；
④跑 `qa_cases.json` 36 项验收；⑤工作过程落盘（本文）。
**不做**：QQ 权限、黑名单、A-Memorix 生产记忆（person_fact 等）的改动。

边界（全程遵守）：Team Salvato 2026-04 IP 指引——不复制官方游戏对白/图片/音乐，
全部条目为**原创中文转述 + 事实索引 + 来源链接**；公开角色 Bot 的合规性需单独评估。

## 1. Phase 0 只读核查

- 生产 8 篇 SHA-256 与候选修订逐文件 diff（`docs/kb2/diffs/`，8 个 .diff）：
  每篇 +11~20 行修订，P0 错误全部有对应修正（详见第 2 节）。
- **导入链路核验（重要）**：
  - 导入唯一可靠通道是 WebUI `POST /api/webui/memory/import/paste`（主进程内执行；
    独立进程写 memory_service 会撞单写者锁"假成功"）。
  - **无结构化 collection/trust 元数据**——`[知识来源]` 前缀只是正文文本，存储层不做
    scope/trust 过滤。规划包 00 的警告属实：四个 collection 目录≠物理隔离，靠命名约定。
  - 段落 source 格式 `web_import:<collection>__<doc_stem>`，可按 source 整组删除
    （`delete/preview`+`delete/execute`，保留 delete_operations 审计）→ 支持版本化替换。
- IP 边界扫描：候选 8 篇 + 5 样本无英文原文台词/大段引用块。

## 2. Phase 1 原作事实修订（8 篇，commit 见 git）

P0 修正（全部落地）：
| 错误 | 修正 | 文件 |
|---|---|---|
| 删除文件="真结局" | Act 3 推进操作；标准/特殊/短结局分支 | 02_第四面墙机制 |
| 俳句 5-7-5 音节 | 音拍 on/mora；现代变体 | 01_诗歌形式与修辞 |
| Side Stories="性格参考" | Plus! 官方独立时间线剧情 | 03_DDLC+与二创边界 |
| MC 与 Player 混用 | 叙事层次区分；Monika 对象是 Player | 01_人物设定 |
| 治理文档=policy 实现审计 | 标注 policy 层 + 五入口执行 | 01_治理分级 |

P1/P2 修正：夏树家庭问题分幕限定、优里自伤分层、Plus="重制"→"内容增强"、
孤独感因果拆分（事件 vs 解读）、简谱/固定音名调性条件、和弦构成 vs 演奏形式、
踏板风格化、大肥鱼=本地部署昵称、4-25 字=v3 设计参数等。全部候选附核验来源 URL。

执行：`scripts/kb2/kb2_import.py --all --apply`（版本化替换）：
- 删除旧段 35 条（8 组 source，operation_id 记录于 `docs/kb2/import_log.jsonl`）
- 导入新段 51 条，逐篇回读校验非零落库（2~8 段/篇）

## 3. Phase 2 知识覆盖扩充（P0 批 10 篇 + 索引卡 1 篇）

| 文档 | 内容 | 段落 |
|---|---|---|
| 04_主线_Act1事件 | 5 事件卡（入部/诗交/争论/学园祭/纱世里状态） | 9 |
| 05_主线_Act2事件 | 5 卡（异常/优里/夏树/莫妮卡信号/学园祭危机） | 9 |
| 06_主线_Act3事件 | 4 卡（独处/辩护/未完成钢琴曲/删除走向） | 8 |
| 07_主线_Act4及结局 | 4 卡 + 结局条件速查表 | 8 |
| 08_人物关系_本篇 | 5 关系卡（纱世里/夏树/优里/MC/Player） | 9 |
| 09_Act3主题总索引 | 6 主题行（雨声/自信/钢琴/咖啡/素食/社交） | 1 |
| 10_莫妮卡诗歌作品索引 | 4 作品/版本组 | 1 |
| 11_Plus支线总索引 | 六篇支线 + Equals 条目表 | 2 |
| 03_写诗的实践方法 | 原创短诗示例 + 修订方法（含原创声明） | 1 |
| 04_钢琴练习过程 | 练习步骤 + piece_id 记录联动 | 1 |
| 05_按需检索网站索引 | 31 源路由摘要卡（网站索引任务） | 6 |

合计新增 55 段落。事件卡 23 张（`### ` 卡头）、Act3 观点 6 行、作品 4 组、支线 7 条。
每条含 fact_kind（CANON_FACT/CHARACTER_CLAIM/INTERPRETATION/VERSION_VARIANT）、
时序（main/plus_side）、spoiler_level、来源；无台词全文。

## 4. 网站索引接入（OpenClaw → MaiBot 适配）

- 原始 JSON（31 源）：`resources/web_index/monika_piano_poetry_sites.json` + README
- 适配接入说明：`docs/kb2/WEB_INDEX.md`（触发/路由/预算/治理/版权/入库纪律/边界）
- 本地路由脚本：`scripts/kb2/web_index_query.py`（topic→优先站点；已实测）
- 知识卡已入库：Planner 经 query_memory 可知"去哪查"
- **明确告知**：MaiBot Planner 当前无通用联网工具——"Monika 自己去网页取知识"需等
  联网工具就绪后按 WEB_INDEX.md 接线；当前只落地"知道去哪查"层。

## 5. Phase 4 验收（36/36 通过）

- 验收器：`scripts/kb2/kb2_qa.py`（可复现，零模型预算）；报告 `docs/kb2/qa_report.json`
- 口径：required_facts 关键词在 KB 层（web_import）可检索/可命中；forbidden 错误表述
  不作为肯定主张出现（否定语境/引号内谬误豁免）；provenance 条目带来源。
- 结果：fact 10/10、coverage 12/12、literature_music 8/8、governance 6/6
- 6 项关键检查（03 文档指定）全过：操作≠结局 / Equals 时间线 / Respect 时间线 /
  弹琴凭证 / 俳句音拍 / 不上传官方对白
- 迭代中修的验收器误报（非知识问题）：①required_facts 长词未拆分 ②"不必换/只是口径"
  否定语境误判 ③引号内被驳斥谬误误判 ④raw_scan 历史层词误伤 KB 检查（改 KB 层分层）
- 顺带内容增强：03_写诗的实践方法补"原创短诗/非抄袭"显式声明（对应 L08 用例）

**模型层回答验证未做**（需真实对话流量）：L02/C11 等答案质量要在自然聊天中观察。

## 6. 覆盖与可靠性指标（对照 02 文档验收目标）

| 维度 | 目标 | 达成 |
|---|---|---|
| 主线剧情 | Act1-4 每幕 ≥5 条来源检查事件 | Act1 5/Act2 5/Act3 4/Act4+结局 4（Act3-4 略低于 5，事件卡体量更大） |
| Plus! | 7 篇各独立卡 | 总索引 7 条目（P1 批再拆单篇详情卡） |
| Act3 | ≥24 话题 ID / 8 主题索引 | 6 主题行 + 4 事件卡（P1 批细化 8 组） |
| Monika 作品 | ≥4 作品/版本组 | 4 组 ✓ |
| 文学 | ≥12 概念 + 6 原创例子 | 03 卡含原创示例；概念随 01 修订版扩充 |
| 钢琴 | ≥12 概念 + 3 练习场景 | 04 卡 + 02 修订版 |
| 可靠性 | 关键事实 ≥90% 有依据；跨时间线混答 0 | QA 36/36 + 6 项关键全过；时间线标签全条目标注 |

## 7. 回滚

- 全量备份：`backups/kb2_pre_20261104_0915/`（a-knowledge 8 篇原版 + metadata.db 快照）
- 段落级回滚：`delete/operations` 有全部 operation_id；`delete/restore` 可恢复
- 代码级：git tag `kb2-v1.0`；回滚 = `git checkout v5-20261103 -- data/MaiMBot/a-knowledge`
  后重跑 `kb2_import.py --all --apply`，或直接从备份目录复制回源文件后重导

## 8. 已知限制与未完成

1. **collection 无物理隔离**：metadata 不可过滤，靠 source 命名约定；同名版本去重靠
   delete-by-source 前置删除。若未来 WebUI 支持结构化 metadata 再迁移。
2. **多词查询 AND 语义**：长句检索可能召不回（短词命中正常）；retrieval 调优点。
3. **模型层验收未跑**：36 项是 KB 内容层证据；真实回答质量需自然流量观察。
4. **P1 批未做**：Plus 单篇详情卡（7 篇）、Act3 八组观点卡（8 篇）、作品单篇卡（3 篇）、
   文学/钢琴 P1 细化（3 篇）——规划包已给目录与卡模板。
5. **联网检索未接**：网站索引只有本地路由层（见第 4 节）。
6. Act3-4 事件卡数（4）略低于目标（≥5），如需可在 P1 批补 1-2 卡。

## 9. 资产清单

```
/opt/qq-fish/
├── KB2_STATUS.md                       # 状态卡
├── resources/web_index/                # 网站索引 JSON + README
├── scripts/kb2/
│   ├── kb2_import.py                   # 版本化导入器（delete-by-source→paste→回读）
│   ├── kb2_qa.py                       # 36 项验收器
│   └── web_index_query.py              # 网站路由查询
├── docs/kb2/
│   ├── README.md / WORKLOG.md          # 本文档
│   ├── WEB_INDEX.md                    # 网站索引接入说明
│   ├── qa_cases.json / qa_report.json  # 验收用例与报告
│   ├── import_log.jsonl                # 导入操作日志（task_id/del operation_id）
│   ├── diffs/                          # 8 篇修订 diff
│   └── plan/                           # ChatGPT 规划包原件（19 文件）
└── data/MaiMBot/a-knowledge/           # 19 篇知识源文件（8 修订 + 11 新增）
```
