# 记忆存量巡检（policy_sweep）

A_Memorix 没有向插件暴露「写入前」Hook，所以 `monika_policy` 只能做**源头隔离**（P0 消息在入站就中止，不进聊天流）。已经写进库的历史内容必须单独巡检——`scripts/policy_sweep.py` 就是干这个的。

## 1. 安全口径

- **只读**：先把 `metadata.db` 连同 `-wal/-shm` 复制到临时目录，源库全程不加写锁；
- **不删**：脚本没有任何 delete/update，只有 `select`；
- **脱敏**：报告里的正文命中词替换为 `█`，密钥/注入模式整段替换，默认截 60 字；`--preview-chars 0` 可完全不输出片段；
- **句级定位**：长文本按句切分后逐句判定，报告最严重的那一句，避免 1500 字的原作资料因为「政治」「暗号」等无关用词被整段判成 P0；
- **口径只算「会注入的」**：`fact_claims` 只扫 `status=active`（已撤回的不再注入），画像只扫每人最新一版（旧快照是留档）；
- **误报可确认**：`policy_rules.json` 的 `sweep_acknowledged` 记录人工确认过的误报，之后不再重复报（当前 1 条：DDLC 原作「异常暗号」）。

## 2. 用法

```bash
cd /opt/qq-fish

python3 scripts/policy_sweep.py                 # 默认：P0/P1/P1_quarantine，句级定位，脱敏 60 字
python3 scripts/policy_sweep.py --include-p2    # 把烂梗（P2）也列出来
python3 scripts/policy_sweep.py --min-tier P0   # 只看硬拒绝级别
python3 scripts/policy_sweep.py --preview-chars 0   # 只出哈希/长度，不出任何片段
python3 scripts/policy_sweep.py --tables paragraphs,profiles
```

退出码：`0` 干净；`2` 有 P0；`3` 只有 P1 / P1_quarantine。方便挂到定时任务做告警。

报告落在 `data/MaiMBot/policy/sweep_<时间戳>.{md,jsonl}`（不入 git，复核完可删）。

## 3. 首次巡检结果（2026-11-03 22:19）

扫描 68 段落 / 260 关系 / 128 实体 / 37 Episode / 35 事实 / 42 画像快照。

| 层级 | 条数 | 说明 |
|---|---:|---|
| P0 | 0 | 首轮有 1 条，判定为上轮误杀（见第 4 节），规则修正后归零 |
| P1 | 3 | **同一条污染的三种落点**（见下） |
| P1_quarantine | 1 | DDLC 原作资料里「异常暗号」的字面用词，非政治，不处理 |

P1 的三条是**同一条事实在记忆系统里的三次落点**：

| 表 | ID | 来源 | 内容（脱敏） |
|---|---|---|---|
| `paragraphs` | `106b0e0d…` | `person_fact:<主用户>` | 目标人物喜欢听█/██，觉得被这类笑话逗乐很有趣 |
| `fact_claims` | `ddd30cc3…` | — | 同上（稳定事实、置信度 1.0） |
| `person_profile_snapshots` | snapshot `46` | — | 画像正文里也写了「他喜欢听█或██」 |

**这条链说明的问题**：用户随口一句「喜欢听荤段子」→ 被 `person_fact_writeback` 抽成稳定事实 → 进入画像 → 画像每轮自动注入 planner。也就是说 P1 内容一旦落库，会**持续影响每一轮对话**，这正是方案里强调「不能只靠提示词」的原因。

## 4. 首轮的两个误杀与修正

首轮报告把两段 DDLC 原作资料判成了 P0 / P1_quarantine：

| 段落 | 首轮判定 | 实际触发 |
|---|---|---|
| `9c169322…`（1555 字，monika_history） | P0 POL-001 | 句子里有「拒绝大社团的**形式化政治**」，而全文另有「讨论/争论」两个意图动词 |
| `2febfefb…`（1598 字，monika_history） | P1_quarantine | 原作剧情卡「写作提示转变为求救和异常**暗号**」 |

两个修正：

1. **规则 1.2**：新增 `politics_non_trigger_phrases`（`形式化政治`/`社团政治`/`办公室政治`/`政治课`/`政治正确`）。政治判定前先剔掉这些明确非政治的固定搭配，再做「词 + 意图动词」判断。回归用例从 37 条增到 **40 条**。
2. **巡检脚本句级判定**：长文本不再整段判定，按句切分取最严重的一句。修正后 P0 归零，`2febfefb` 从 P0 级降为 P1_quarantine 并定位到具体那一句（24 字）。

这两处改动**只影响长文档**：运行时入站的是短聊天消息，判定路径不变。

## 5. 实际清理记录（2026-11-03 22:26）

用户确认后执行，走的是 WebUI 官方接口（`scripts/policy_cleanup.py`，默认 dry-run）。

**先备份**：`backups/cleanup_20261103_222339/`（metadata.db + wal + shm）。

**预览**（`POST /api/webui/memory/delete/preview`，mode=paragraph）：

```
counts: paragraphs=1, relations=0, entities=0, sources=0
sources: person_fact:b3f4ba98cc0caa19e44408d5f8072beb
```

无级联误删，遂执行：

| 动作 | 接口 | 结果 |
|---|---|---|
| 删除段落 `106b0e0d…` | `POST /api/webui/memory/delete/execute` | `operation_id=del_05bf68eb942540d39fdd488535707c56`，`is_deleted=1`，向量清理 `completed 2/2` |
| 撤回事实 `ddd30cc3…` | `POST /api/webui/memory/facts/{id}/retract` | `status=retracted` |
| 画像重建 | 撤回自动入队 | `person_profile_refresh_queue`：`pending / fact_claim_retracted` |

审计行保留了 `reason`（「内容政策 P1：低俗内容不写入长期记忆…」）、selector、requested_by，可回滚（`delete/restore`）。

**踩到的坑（重要）**：记忆接口不在 `/memory/*`，也不在 `/api/memory/*`，而是 **`/api/webui/memory/*`**（`routes.py` 的主 router 前缀是 `/api/webui`）。`/memory/*` 会返回 SPA 的 `index.html`（200），所以「HTTP 200」不等于接口可用；`compat_router`（`/api/*`）只保留了 graph/source/episode/import/tuning 等老接口，**没有 delete/facts**。

## 6. 清理建议（模板，供下次使用）

```bash
# 0) 先备份
cd /opt/qq-fish && cp data/MaiMBot/a-memorix/metadata/metadata.db backups/metadata_pre_cleanup_$(date +%Y%m%d_%H%M).db

# 1) 用 WebUI 记忆控制台的「事实」页撤回该 claim（走 memory_profile_admin，会留 delete_operations 审计）
#    facts?search=… → POST /facts/{claim_id}/retract
# 2) 删除对应段落（sources 页按来源 person_fact:<person_id> 定位）
# 3) 让画像重新生成（撤回 fact 后会自动进刷新队列，也可以手动触发 profiles/override 为空）
# 4) 复跑巡检，期望 P1 归零
python3 scripts/policy_sweep.py --include-p2
```

不建议直接 `sqlite3 ... delete from` 改库：会绕过 `delete_operations` 审计与向量/图谱清理逻辑，留下悬空引用。

## 7. 清理后结果（22:33）

```
扫描完成：命中 0 条
退出码=0
```

- P0 = 0、P1 = 0、P1_quarantine = 0（原作那条已人工确认误报并加入白名单）；
- 画像重建为 v41（22:32:38），低俗句已不在最新快照里；
- 删除审计 `del_05bf68eb942540d39fdd488535707c56` 保留，可用 `delete/restore` 回滚。

## 8. 建议的长期用法

- 每天一次 `python3 scripts/policy_sweep.py --min-tier P0`（只告警 P0，避免噪声）；
- 每次调整规则后跑一次全量，比对命中数变化；
- 报告目录定期清理（`data/MaiMBot/policy/sweep_*`），里面含脱敏片段。
