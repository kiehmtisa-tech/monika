# docs/kb2 — Monika KB v2（原作纠错与知识覆盖）

2026-11-04 ｜ tag `kb2-v1.0` ｜ 详细过程见 [WORKLOG.md](WORKLOG.md)

## 一句话

按 ChatGPT 规划包完成：8 篇原作事实纠错（P0 修正 5 处）→ P0 批 10 篇知识扩充
（23 事件卡 + Act3/作品/支线索引）→ 31 源网站索引接入 → 36/36 验收通过。

## 文件导航

| 文件 | 内容 |
|---|---|
| [WORKLOG.md](WORKLOG.md) | 全过程日志：Phase 0-4、diff 摘要、覆盖指标、回滚、已知限制 |
| [WEB_INDEX.md](WEB_INDEX.md) | 网站索引（31 源）MaiBot 适配接入说明 |
| [qa_cases.json](qa_cases.json) | 36 项验收用例（fact/coverage/literature_music/governance） |
| [qa_report.json](qa_report.json) | 验收报告（36/36，6 项关键全过） |
| [import_log.jsonl](import_log.jsonl) | 版本化导入日志（task_id / delete operation_id） |
| diffs/ | 8 篇生产→候选修订的 unified diff |
| plan/ | ChatGPT 规划包原件（修订方案/覆盖方案/来源规范/执行任务/样本） |

## 知识库现状（本次后）

- `data/MaiMBot/a-knowledge/`：19 篇源文件（ddlc_canon 11 / literature_music 5 /
  monika_persona 2 / content_policy 1），全部已导入 A-Memorix（web_import 段落 ~106 条）。
- 每条目含 fact_kind / timeline / spoiler_level / 来源；无官方台词全文（IP 指引）。
- 工具：`scripts/kb2/kb2_import.py`（版本化替换）、`kb2_qa.py`（回归验收）、
  `web_index_query.py`（网站路由）。

## 复现命令

```bash
cd /opt/qq-fish
python3 scripts/kb2/kb2_import.py --all           # dry-run 查看导入计划
python3 scripts/kb2/kb2_qa.py --detail            # 跑 36 项验收
python3 scripts/kb2/web_index_query.py 钢琴 乐谱   # 网站路由查询
```

## 回滚

备份 `backups/kb2_pre_20261104_0915/`（8 篇原版 + metadata.db）；
段落级 `delete/restore`（operation_id 见 import_log.jsonl）；详见 WORKLOG 第 7 节。
