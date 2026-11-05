# Monika Poetry — 实施进度

> 依据《Monika_诗歌创作与阅读数据库_OpenClaw实施Prompt》实施
> 更新：2026-11-04 08:55 · 插件 `dragon.monika-poetry` v0.1.0

## 一、现场审计（先查后写，不假设 API）

| 检查项 | 结论 |
|---|---|
| 插件 SDK 能否调用 LLM | **能**。`maibot_sdk/capabilities/llm.py` 的 `LLMCapability.generate(prompt, temperature, max_tokens, task_name, model_name)`，挂载在 `self.ctx.llm` |
| 返回结构 | `{"success", "response", "reasoning", "model_name", "prompt_tokens", "completion_tokens", "total_tokens", "model"}` |
| RPC 超时 | `call_capability` 默认 **30 秒**；可传 `timeout_ms` 覆盖（会绑定到 `call_capability` 的同名参数） |
| 插件持久化目录 | `self.ctx.paths.data_dir` = `/MaiMBot/data/plugins/<id>` → 宿主 `./data/MaiMBot-plugin-data/<id>`（bind 挂载，容器重建不丢） |
| 人格与原作档案 | `/opt/qq-fish/persona/{monika_history.md, Monika_defined_detail_v2.md, Monika_MaiBot_defined_v2.json}` 实测存在 |
| 钢琴作品结构 | `piece_id`（如 `piece_1842_melancholy`）、`pieces.db`、`pieces_index.jsonl`，可直接作为关联外键 |
| 插件清单要求 | `_manifest.json` 必须有 `author.url` 非空 + `urls.repository`；类需实现 `on_load/on_unload/on_config_update`；模块需 `create_plugin()` |
| 是否已有诗歌能力 | 无，需新建 |

**改动清单**：新增 `plugins/monika_poetry/`（6 个文件）+ `scripts/test_poetry.py`；不改 MaiBot 核心，不覆盖钢琴/人设/表情。
**回滚**：`rm -rf data/MaiMBot/plugins/monika_poetry` 即可完全移除；数据库在 `data/MaiMBot-plugin-data/dragon.monika-poetry/`，可单独备份或删除。

## 二、数据分层（三类不混）

| 层 | 表 | 存什么 | 版权边界 |
|---|---|---|---|
| 原作研究索引 | `canon_poem_references` | 作品名、时间线、章节、主题标签、**自行撰写的中文概述**、关联事件 | **不存官方诗歌全文/台词/游戏资产** |
| 莫妮卡原创 | `poems` + `poem_versions` | 全文、主题、情绪、形式、版本、关联钢琴曲 | 标 `origin=monika_original`，不冒充原作 |
| 用户提供 | 同上 | 全文 + 作者 + 授权范围 + 隐私级别 | 默认 `visibility=private`，可删除 |

`poem_events` 只记 `created/revised/read/favorited/shared/linked_piano` + 时间 + 会话，不存聊天内容。

## 三、两条写诗路径（Token 成本不同，都实现了）

| 路径 | 触发 | 模型调用 | 说明 |
|---|---|---|---|
| `/poem` 斜杠命令 | 明确指令 | **1 次** | 命令本身没有对话轮次可搭，必须插件调 `ctx.llm.generate` |
| 自然语言 | "写首诗吧" | **0 次额外** | **对话模型自己写出诗**，再调 `save_poem` 工具落库；数据库 IO/检索/去重全在本地 |

其余工具：`find_poem`（只返标题编号）、`get_poem`（返正文）、`revise_poem`（新版本）、`link_poem_piano`。
`find_poem` 只返标题是刻意的——避免模型为找一首诗把整个库读进上下文。

## 四、命令与工具

```
/poem [主题] [lines=N] [mood=xx] [style=xx]   写诗并入库
/poems [收藏]                                  列出最近作品
/readpoem <编号|标题>                          读一首
/revise <编号> <修改要求>                      产生新版本，旧稿保留
/poemfav <编号>                                收藏/取消（收藏不会被清理）
/poemcanon [关键词]                            查原作研究索引
```

工具：`save_poem` / `find_poem` / `get_poem` / `revise_poem` / `link_poem_piano`

## 五、实施中踩到的三个坑

1. **`_sha256` 返回的是摘要字符串却再调 `.hexdigest()`** → `AttributeError`。建库测试第一轮就暴露了。
2. **代码围栏解析只认英文语言标记**：```` ```诗 ```` 里的"诗"没被剥掉，被当成标题。改成 `^\s*```[^\n]*\n?`。
3. **模型把预算花在 reasoning 上，`response` 返回空**：
   - 先是 `max_tokens=520` 不够 → 提到 1200；
   - 然后 `cap.call` **30 秒 RPC 超时** → 传 `timeout_ms=90000` 覆盖；
   - 最后 1200 仍不够（`completion_tokens=1118` 已接近上限）→ 定 **2500**。
   - 教训：**`max_tokens` 是上限不是成本**，思考型模型的 reasoning 也要算进去；prompt 越长思考越久。

另外补了两点可观测性：每个命令都有日志，`_llm` 记录 `keys / success / 文本长度 / prompt_tokens / completion_tokens`，空结果会打出原始输出前 200 字。

## 六、验证记录

| 阶段 | 项目 | 方式 | 结果 |
|---|---|---|---|
| P0 | 建库 + 原作索引 | `poetry_db.init` | 新增 12 条；重复 init 返回 0（幂等） |
| P0 | CRUD / 版本 | 单测 | 创建→读回→修订 v2，v1 原样保留 |
| P0 | 权限 | 单测 | `visibility=private` 的作品他人读不到 |
| P0 | 备份 | `VACUUM INTO` | 产出 `poetry_20261104_085501.db`（57 KB） |
| P0 | 删除 | 单测 | 连版本与事件一起删，无残留 |
| P1 | `/poem` | E2E 注入 `/poem 雨天和练琴` | 《雨落钢琴》入库；LLM `success=True` |
| P1 | 多样性 | `/poem 友情` | 自动选中「日常观察/平静/现代自由诗」，不再为空 |
| P1 | `/poems` `/readpoem` `/poemfav` `/poemcanon` | E2E | 全部生效，事件流 `created/shared/read/favorited` |
| P1 | `/revise` | E2E | v2 存入，`change_note` 记录修改要求，v1 保留 |
| P1 | 自然语言 | "写一首…保存下来" | `Planner 1/1: 工具=save_poem` →《夏天的树》入库，**0 额外调用** |
| P2 | 真实回忆 | "你之前写的那首关于下雨的诗是哪首" | `get_poem` 取回真实作品《雨落钢琴》，未编造 |
| P2 | 跨作品关联 | 同上 + 关联指令 | `link_poem_piano` → `related_piano_piece_id=piece_1842_melancholy` |
| P2 | 去重/多样性 | 三首真诗 | 主题全不同；两两相似度 **0.000 / 0.015 / 0.015**（阈值 0.72） |
| 性能 | 空闲扫描 | — | 只在命令/工具调用时查库，无后台任务、无常驻进程 |

## 七、实际 API 与部署路径

- 插件目录：`/opt/qq-fish/data/MaiMBot/plugins/monika_poetry/`（git 已跟踪）
- 数据库：`/MaiMBot/data/plugins/dragon.monika-poetry/poetry.db`（宿主 `./data/MaiMBot-plugin-data/`）
- 备份：`poetry_db.backup(db_dir, dest_dir)` 走 SQLite `VACUUM INTO` 热备份
- 模型调用：`self.ctx.llm.generate(prompt, temperature=0.95, max_tokens=2500, timeout_ms=90000)`
- 发送：`self.ctx.send.text(text, stream_id)`

## 八、未做 / 已知边界

- **P3（TTS 朗读）未做**，符合规格："只有 TTS 与 QQ 出站语音接口已验证可用时再加"。
  当前环境有 NapCat 的语音出站能力（钢琴插件在用），但**诗歌朗读需要 TTS**，本机没有 TTS 能力，
  所以按规格只提供文字阅读与鉴赏，不假称发出语音。
- 主动分享：未接主动聊天策略，不主动发诗（规格要求"避免刷屏"，且现有主动策略是另一个插件的职责）。
- 重复检测的 n-gram 阈值 0.72 是经验值，可能需要按实际作品调。
- 用户诗歌的"授权范围"目前只有 `visibility` 三级，没有做按用户粒度的 ACL。

## 九、当前状态

数据库内 3 首原创作品、4 个版本、12 条原作索引；命令与工具全部 E2E 通过。
