# Monika Poetry 文档索引

莫妮卡诗歌系统（写诗 / 读诗 / 长期作品库）的留档。

| 文件 | 内容 |
|---|---|
| [`../../POETRY_STATUS.md`](../../POETRY_STATUS.md) | **完整实施记录**：现场审计、数据分层、两条写诗路径、命令与工具、三个坑、逐项验证、未做项 |

## 代码

```
plugins/monika_poetry/
├── plugin.py         # 命令 + 工具 + 权限 + 冷却 + LLM 调用
├── poetry_db.py      # SQLite：poems / poem_versions / poem_events / canon_poem_references
├── poetry_engine.py  # 提示词构建、主题/情绪/形式多样性、正文解析
├── config.toml       # 参数（无密钥，可入库）
└── _manifest.json    # 插件清单
```

## 一句话设计

**两条写诗路径**：`/poem` 斜杠命令由插件调模型（1 次调用）；自然语言由对话模型自己写完诗
再调 `save_poem` 落库（**0 次额外调用**）。数据库增删改查、检索、去重全在本地，不消耗模型 Token。

## 版权边界

原作诗歌只在 `canon_poem_references` 存元信息 + 自行撰写的中文概述，不保存官方诗歌全文、
官方台词或任何游戏资产（Team Salvato IP Guidelines）。

## 与钢琴系统的关系

两个插件独立、互不覆盖。诗歌可通过 `link_poem_piano` 关联 `piece_id`，实现跨作品记忆。
