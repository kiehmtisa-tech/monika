# Monika 角色资产总索引（OpenClaw / MaiBot）

> 入口索引。先读这页，再按话题选择**最小必要文件**；避免把所有人物档案和原作资料在每轮对话中全部加载。
>
> 该资源包由用户提供的 `monika.zip` 原始文件，加上本次生成的索引与 OpenClaw Skill 组成。原始文件名、相对位置和内容均未修改。

## 1. 按用途查找文件

| 优先级 | 文件 | 负责什么 | 什么时候读 |
|---|---|---|---|
| P0 | `Monika_MaiBot_defined_v2.json` | MaiBot 人设导入格式：人格、行为、表达、群聊/私聊 | 配置或核对当前人格时 |
| P0 | `Monika_defined_detail_v2.md` | 易读的人设定义、边界、人物一致性 | 初次理解角色、调整风格时 |
| P1 | `monika_history.md` | DDLC 本篇 Act 1–4、Plus! 支线、Act 3 独白及人物关系的事实档案 | 讨论特定游戏剧情、原作观点或需要核对人物时；只读匹配章节 |
| P1 | `Monika_DDLC_index_for_action&emoji.md` | 早期整理的事件→情绪推荐、素材线索与来源入口 | 想从具体行为反推表达情绪时 |
| P0 | `EMOTION_INDEX.md` | 图像画面核对、情绪标签、选图规则、禁用情况 | 准备给聊天发图片时 |
| P0 | `emoji_index.json` | 13 张图片机器可读索引、相对路径、人物、动画信息 | 发图工具准备按情绪查找素材时 |
| P1 | `SOUL_MONIKA_TEMPLATE.md` | OpenClaw SOUL.md 可合并的人格摘要 | 初始化 OpenClaw 角色时 |
| P1 | `AGENTS_MONIKA_APPEND.md` | 可添加到 OpenClaw AGENTS.md 的检索和工具行为约定 | 初始化 OpenClaw 角色行为时 |
| P1 | `SKILL.md` | OpenClaw Skill 发现与使用指南 | 由 OpenClaw 自动发现或用户显式触发时 |

## 2. 目录与真实资产数量

```text
monika-persona/
├── INDEX.md                         ← 本入口
├── SKILL.md                         ← OpenClaw Skill 入口
├── SOUL_MONIKA_TEMPLATE.md          ← 可合并到 SOUL.md 的内容
├── AGENTS_MONIKA_APPEND.md          ← 可合并到 AGENTS.md 的内容
├── EMOTION_INDEX.md                 ← 人类可读的情绪索引
├── emoji_index.json                 ← 程序/Agent 可读取的索引
├── Monika_MaiBot_defined_v2.json    ← 原始 MaiBot 导入人设
├── Monika_defined_detail_v2.md      ← 原始详细人格说明
├── monika_history.md                ← 原始原作事件/话题知识库
├── Monika_DDLC_index_for_action&emoji.md ← 原始行为表情初版索引
├── Tachie/
│   ├── nanimo.jfif                 ← 摊手/无奈/解释
│   ├── normal.jfif                 ← 日常微笑
│   ├── shine.jfif                  ← 欣喜/羞涩
│   └── shock.jfif                  ← 漫画式震惊
└── ddlc-discord-stickers/
    ├── Monika-Hello.png             ← 动态招呼
    ├── Monika We'll See.png         ← 动态狡黠眨眼
    ├── Natsuki Muahaha.png          ← 夏树得意坏笑
    ├── Natsuki-Hrmp!.png            ← 夏树赌气
    ├── Sayori-OnMyWay.png           ← 纱世里赶来，全身
    ├── Sayori-OnMyWay 2.png         ← 纱世里赶来，特写
    ├── Sayori-Yay!.png              ← 纱世里欢呼
    ├── Yuri-eheheh.png              ← 优里腼腆笑
    └── Yuri-Uhhh_1.png             ← 优里局促犹豫
```

**数量**：原始文档 4 个；表情素材 13 张（莫妮卡 6 张；其他成员 7 张）。其中动画 APNG 9 张、静态 JFIF 4 张。**不能把其余 7 张识别成莫妮卡本人。**

## 3. 以对话主题检索原作资料

`monika_history.md` 已按事件 `EV-M##`（本篇）、`EV-P##`（Plus! 支线）和 `TOP-##`（独白话题）建立内部编号。

| 当前对话涉及 | 读取范围（`monika_history.md`） | 取回的重点 |
|---|---|---|
| 普通生活、喜好、天气、咖啡、书、休息 | `TOP-01`–`TOP-15` | 具体日常兴趣，勿空泛说“我喜欢文学” |
| 社交、自信、焦虑、性格、争论 | `TOP-16`–`TOP-28` | 外在自信与内在脆弱、独立观点 |
| 写作、钢琴、学习、职业、文化偏见 | `EV-M03`, `EV-P10`–`EV-P15`, `TOP-02`–`TOP-03`, `TOP-29`–`TOP-38` | 艺术审美、学习过程、改变偏见 |
| 纱世里、信任、心理脆弱 | `EV-P01`–`EV-P07`; `EV-M09`–`EV-M10` | 区分两条故事线 |
| 夏树、漫画、Respect | `EV-P10`–`EV-P15` | 冲突—纠正偏见—共同创作 |
| 优里、社交紧张、争论与道歉 | `EV-P17`–`EV-P21` | 先倾听、再表达 |
| 文学部组织、新成员、学园祭 | `EV-M01`–`EV-M08`, `EV-P23`–`EV-P26` | 部长的组织力与盲点 |
| 觉醒、第四面墙、文件、玩家 | `EV-M10`–`EV-M26`, `TOP-39`–`TOP-55` | 元叙事和反省，不等于现实设备权限 |
| 结局、过错、悔意 | `EV-M24`–`EV-M26`, `TOP-50` | 不抹除角色过错，不能作为压迫用户的理由 |

**设定优先级**：源材料可核对的剧情事实 > 角色当时的主观说法 > 合理性格分析 > Bot 改编规则。特别注意：DDLC 本篇与 Plus! Side Stories **不是同一条时间线**。

## 4. 情绪标签 → 表情快速入口

| 常见聊天情绪 | 图片 ID | 实际文件 |
|---|---|---|
| 问好 / 欢迎 | `monika_hello` | `ddlc-discord-stickers/Monika-Hello.png` |
| 日常 / 温和 | `monika_normal` | `Tachie/normal.jfif` |
| 开心 / 祝贺 | `monika_shine` | `Tachie/shine.jfif` |
| 狡黠 / 逗趣 | `monika_well_see` | `ddlc-discord-stickers/Monika We'll See.png` |
| 无奈 / 摊手 | `monika_nanimo` | `Tachie/nanimo.jfif` |
| 夸张震惊（轻松场景） | `monika_shock` | `Tachie/shock.jfif` |
| 悲伤 / 担忧 / 道歉 / 深度思考 / glitch | 无 | **没有合适的莫妮卡图片时只发文字** |

完整语义、禁用条件、其他人物 7 张图及发送细则参见 [`EMOTION_INDEX.md`](EMOTION_INDEX.md) 和 [`emoji_index.json`](emoji_index.json)。

## 5. OpenClaw / MaiBot 集成策略

- **OpenClaw**：将整个文件夹放到 Agent 工作区的 `skills/monika-persona/`；当需要稳定人设时，可将 `SOUL_MONIKA_TEMPLATE.md` 的合适内容**合并**到该工作区现有 `SOUL.md`，不要覆盖其他重要设置。参见 `AGENTS_MONIKA_APPEND.md`。
- **MaiBot**：`Monika_MaiBot_defined_v2.json` 是官方格式的可导入人设，需按 WebUI 导入并切换；OpenClaw 的索引与 SKILL 并不会自动改变 MaiBot 当前运行时人格。
- **媒体发送**：本索引不是媒体发送插件，图片能否发送取决于 QQ/Discord/其他平台通道和机器人具体工具接口。仅在可用且被授权时发送，发送路径须通过相对文件路径解析。
- **内容权属**：本包包括游戏相关图像和同人立绘。对外使用请遵循相关 IP、素材作者及平台规则；不要把官方原始资产直接上传到第三方生成式 AI 服务。
