# Monika 内容政策（docs/policy）

内容治理与知识库防污染的文档入口。上线状态见根目录 `POLICY_STATUS.md`；规则正文在
`data/MaiMBot/plugins/monika_policy/policy/`（与插件同目录，改规则不需要重启）。

## 文件

| 文件 | 内容 |
|---|---|
| `../../POLICY_STATUS.md` | 目标、时间线、改动清单、E2E 证据、回滚、已知缺口 |
| `WORKLOG.md` | 全过程日志：调研 → 实现 → 5 例真实注入 → 3 轮迭代 → 存量巡检 → 踩坑清单 |
| `SWEEP.md` | 存量巡检：只读扫描口径、首轮结果（含一条污染链）、误杀修正、清理步骤 |
| `../../data/MaiMBot/plugins/monika_policy/policy/MONIKA_POLICY.md` | 政策正文（P0–P4、边界、判定方式） |
| `../../data/MaiMBot/plugins/monika_policy/policy/policy_rules.json` | 机器可读规则 v1.1 |
| `../../data/MaiMBot/plugins/monika_policy/policy/README.md` | 五个入口到 Hook 的映射表 + 与上游方案的偏差 |

## 三条常用命令

```bash
cd /opt/qq-fish

# 1) 回归（37 项，改任何规则前后都跑）
python3 scripts/policy_selftest.py

# 2) 看审计（只存哈希与枚举，不含原文）
tail -20 data/MaiMBot/policy/audit.jsonl

# 3) 存量巡检（只读，不删）
python3 scripts/policy_sweep.py --include-p2

# 4) 周更提案状态
python3 scripts/policy_weekly.py --status
```

## 口径提醒

- `audit.jsonl` 的 `event` 有四种：`inbound`（入站判定）、`p0_reply`（婉拒已发出）、`learning.*`（学习被拦）、`outgoing`（出站被拦）、`retrieval`（注入上下文审计命中）。
- `tier=P0` 且 `event=inbound` 才代表「这条消息被拦下、没进库」。只有 `p0_reply` 没有 `inbound` 说明发送失败。
- 判定「有没有误杀」看 P3 是否被误标为 P1_quarantine：搜索 `reason=CODED-CONTEXT-REVIEW` 的人工抽样即可。
- 插件目录里跑 python 会生成 `__pycache__`，会触发 MaiBot 文件监视反复 reload。测试请加
  `PYTHONDONTWRITEBYTECODE=1`，或跑完删除。
