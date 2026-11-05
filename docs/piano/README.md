# Monika Piano 文档索引

莫妮卡"练琴"插件的完整留档。按这个顺序读最快：

| 顺序 | 文件 | 内容 | 适合谁 |
|---|---|---|---|
| 1 | [`../../PIANO_STATUS.md`](../../PIANO_STATUS.md) | 运行状态总览，P0–P5 各阶段结论与 E2E 证据 | 想知道"现在能做什么" |
| 2 | [`WORKLOG.md`](WORKLOG.md) | 全过程工作日志：需求、诊断、设计、7 轮迭代、踩坑清单 | 想知道"为什么这么做" |
| 3 | [`../../PIANO_AB_REPORT.md`](../../PIANO_AB_REPORT.md) | 本轮（v0.4）技术报告：根因、改动清单、指标、验收 | 想知道"改了什么、效果如何" |
| 4 | [`listen/AB_试听.html`](listen/AB_试听.html) | 新旧对照试听页（12 段音频 + 完整指标表） | 想直接听 |
| 5 | [`metrics.json`](metrics.json) | A/B 指标原始数据（脚本输出） | 想自己算/画图 |

## 代码

```
plugins/monika_piano/
├── piano_engine.py    # 作曲引擎 v0.4.1：动机、变奏算子、候选评分、左手织体、资源守护、运行时解析
├── plugin.py          # /piano 命令 + play_piano/monika_music_act/find|import|play_piano_score 工具
├── piano_catalog.py   # 作品目录（SQLite）：落库、查询、双阈值修剪
├── piano_storage.py   # 文件层维护：WAV TTL/容量、收藏、缺谱保护、每日一次
├── piano_external.py  # P9 在线曲谱库：Mutopia 受控抓取/许可校验/MIDI 消毒/渲染缓存
├── music_state.py     # P8 音乐生活状态：项目/练习/播放事件/偏好/投递门禁
├── music_act.py       # P8 两层决策路由：行为 × 资源（词法信号 + 真实目录命中）
├── config.toml        # 真实配置（含 NapCat token，被 .gitignore 忽略，不入库）
└── config.toml.example# 配置模板（入库）
```

引擎的关键设计只有一句话：**先由 seed 生成一条 2 小节动机，再用变奏算子派生出
A′/B/A″，而不是四次独立随机**。旧版失败的地方在于把四个乐句都当成独立随机来生成，
且节奏单位算错、情绪全部回退到同一条和声。

## 口径提醒

- `同音率`、`最长同音`、`空小节`、`强拍和弦音` 是本项目最有效的四个判据。
  旧版同音率 43–70%、最长连续 9–12 个音、8 小节里 4 小节没有主旋律。
- `节奏多样性` 必须按**小节内起音位置**算。按时值算会被 articulation 和写入游标干扰，
  旧版会虚高到 1.0（假象）。
- `A~A'` 相似度中等（0.2–0.78）是**预期**：A′ 会重新配和声，只保留节奏和前半句音高。
- 指标只能证明"不再机械重复"，不能证明"更好听"。最终判定必须人耳听 `listen/AB_试听.html`。

## 运维

**运行时依赖已固化到持久化卷** `depends-data/piano-runtime/`（33 MB），
镜像重建后仍可用，不依赖镜像里 apt 装的 fluidsynth 或 venv 里的 mido。

```bash
bash scripts/piano_deps.sh check     # 自检：bin / so / SoundFont / mido 是否齐全可用
bash scripts/piano_deps.sh vendor    # 重新归档（系统依赖还在时）
bash scripts/piano_deps.sh restore   # 容器重建后：缺什么补什么，然后归档
```

插件启动时会自动自检并打日志 `运行时自检通过: fluidsynth=vendored`。
若日志出现 `运行时依赖缺失，钢琴生成已禁用`，跑一次 `piano_deps.sh restore` 即可，
**不需要重建容器**。

**作品库维护**：每天最多跑一次（挂在首次 `/piano` 上），分两级。

作品目录 `piano_catalog.py` 把作品记在 `piano/pieces.db`（SQLite，标准库，零依赖）。
`pieces_index.jsonl` 已**冻结为只读归档**（首次启动时导入 DB），不再增长。

| 配置（`[piano]`） | 默认 | 说明 |
|---|---|---|
| `cleanup_enabled` | true | 总开关 |
| `cleanup_dry_run` | false | true = 只记录计划不真删 |
| `max_pieces` | 5000 | **作品条数上限**（实际护栏）：超了按最旧整首移除 |
| `midi_max_mb` | 1024 | 乐谱+元数据容量安全网（正常用不到） |
| `min_keep_days` | 7 | 最近 N 天的作品不会被整首移除 |
| `wav_ttl_days` | 30 | 普通 WAV 保留天数（只删音频，留乐谱） |
| `max_wav_mb` | 300 | WAV 总容量上限 |

顺序是：**目录硬阈值（整首删：mid+json+wav+行）→ WAV 软阈值（只删音频）**。

- 收藏的作品（`/piano 收藏`）**永不删**；`min_keep_days` 内的新作品也不删
- 删 WAV 前会校验 MIDI 可解析，缺谱就保留
- WAV 被清理后再 `/piano 再听` 会用 MIDI 重渲染
- 超限但全部被保护时，不删任何东西，只记日志

> 为什么容量阈值是 1 GB 却几乎不会触发：MIDI 只有 ~0.9 KB/首，1 GB 约等于 100 万首。
> 真正兜底的是 `max_pieces`。容量阈值只防"某天批量导入"这类意外。

## 版本

- `piano-legacy-v0.3`：改进前基线（旧引擎归档在 `scripts/piano_engine_legacy_v03.py`）
- `piano-v0.4`：当前版本
