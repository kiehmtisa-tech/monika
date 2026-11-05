# Monika Piano 全过程工作日志

> 记录"莫妮卡练琴"插件从零到 v0.4 的完整过程：需求、实施、诊断、迭代、验证。
> 最后更新：2026-11-03 21:30 · 引擎版本 0.4.0

本文件是**过程记录**；运行状态见仓库根 `PIANO_STATUS.md`，本轮技术报告见根 `PIANO_AB_REPORT.md`。

---

## 一、项目背景

Monika（QQ 个人号 100000002）是跑在 MaiBot + NapCat 上的角色机器人，人设是"在练习钢琴的少女"。
"练琴"能力要求：用户说一句"弹一段"，莫妮卡就能生成一首原创钢琴曲并以 QQ 语音发出来。

硬约束（决定了所有设计取舍）：

| 约束 | 数值 | 影响 |
|---|---|---|
| CPU / 内存 | 2 vCPU / 1.6 GB | 不能跑神经音乐模型；默认并发 1 |
| 同机其他服务 | NapCat、预算网关、frps、mihomo、rustdesk | 生成必须轻量、可拒绝 |
| Token 成本 | 预算网关强制计费 | 作曲必须纯本地，零 LLM 调用 |
| 音色 | TimGM6mb.sf2 (6 MB) | FluidR3(148MB) 只在内存富余时可选 |

## 二、历史阶段（P0–P3，2026-11-03 完成）

| 阶段 | 内容 | 关键实现 |
|---|---|---|
| P0 | 确定性钢琴生成 | `mido` 写 MIDI → `fluidsynth` 渲染 WAV；同 seed 逐字节一致 |
| P1 | QQ 语音发送 | 插件内 `websockets` 直连 NapCat OneBot WS 3001，`record` 段用 `file://` 共享卷路径，NapCat 自动转 silk |
| P2 | `/piano` 命令 | MaiBot `@Command` 拦截，0 LLM；参数 `[mood] [seed=N] [bpm=N] [bars=N]`，中文情绪别名，60s 冷却 |
| P3 | 记忆与变奏 | `pieces_index.jsonl` 存 `piece_id/parent_piece_id`；`/piano 变奏` 回溯本会话最近作品同 seed 续作；`play_piano` Tool 供自然语言触发 |
| P4 | 高级作曲/多乐器 | **按方案暂缓**，理由：本机内存极限、边际收益低、人设契合度差 |

详见 `PIANO_STATUS.md` 的 P0–P3 段落。

## 三、P5：作曲质量优化（2026-11-03 晚）

### 3.1 触发原因

用户听完第一版后反馈："好多都是重复音节"。角色自己也在对话里承认"后半段确实容易顺手重复"。
于是定下目标：**只改作曲算法，不换音色、不加模型、不加依赖、不加常驻进程**。
方案文档由外部提供（低资源作曲优化任务书），核心要求是"先诊断真实代码，再定向修改，
最后用同参数 A/B 对照 + 人耳验收"。

### 3.2 第一步：先建可回滚的基线

改之前发现一个硬问题：**插件源码根本不在 git 里**。
`.gitignore` 里有 `data/`，而插件在 `data/MaiMBot/plugins/monika_piano/`，改坏了没有回滚点。

处理：

1. `tar czf backups/plugins_monika_piano_pre_melody_*.tar.gz` 打物理备份；
2. `.gitignore` 由 `data/` 改为逐级放行（git 不会进入被忽略的父目录，必须
   `data/*` → `!data/MaiMBot` → `data/MaiMBot/*` → `!data/MaiMBot/plugins` → …）；
3. 发现 `config.toml` 里有明文 NapCat token —— **不能入库**。于是仍忽略真实
   config.toml，新增 `config.toml.example` 模板（token 留空）跟踪；
4. 旧引擎归档为 `scripts/piano_engine_legacy_v03.py`，打 tag `piano-legacy-v0.3`。
   这样任何时候都能重新生成旧版对照组。

### 3.3 第二步：冻结旧版 A/B 对照组

用旧引擎在容器内跑 3 情绪 × 2 seed，输出到 `/MaiMBot/data/piano/ab/legacy/`：

| mood | seed | bpm |
|---|---|---|
| calm | 1001 / 1002 | 96 / 84 |
| melancholy | 2001 / 2002 | 72 / 66 |
| playful | 3001 / 3002 | 116 / 128 |

工具：`scripts/ab_gen.py`（importlib 按路径加载引擎，记录耗时 / maxrss / MemAvailable / md5）。

### 3.4 第三步：诊断——三条实现级缺陷

任务书要求"不要凭架构图假定代码细节"。读完 275 行旧引擎 + 量化后，发现重复感来自
**三个底层实现错误**，而不是"变奏算法不够"：

**缺陷 1：节奏 tick 单位错 → 右手只占 3.34 小节**

`RHYTHMS` 里 4/2/6 是以 16 分音符为单位（1 拍 = 120 tick），但 `_section_notes` 用
`dur * 30 - 20` 当 tick、乐句偏移写 `v * 1920`（1 小节）。后果：

- 每乐句音符全挤在该小节前 1.4 拍，单音时值约 0.21 拍 → "哒哒哒哒"的机械敲击；
- 右手总跨度 6422 tick = beat 13.38 = **3.34 小节**（应为 8 小节），第 5–8 小节无主旋律；
- 指标"空小节"= 4，即一半篇幅只剩左手敲根音。

**缺陷 2：mood 全部回退 → 6 种情绪实为 2 种**

`prog_bank = PROGRESSIONS[mode]`，而 `mode` 默认 `"minor"`，该库只有
`melancholy / nostalgic` 两条进行。`prog_bank.get(mood) or 第一条` 使
calm / playful / warm / hopeful 全部落到 melancholy 的和声，音阶也永远 minor。
`key` 字段永远写 `C minor`。

**缺陷 3：逐音 clamp → 连续同音 9–12 次**

`_fit_to_chord` 把每个音就地夹到最近和弦音，音域限死 60–88、级数 clamp(-3,9)。
多个不同级数被压到同一音高。实测 seed=2001 第 3 小节是 10 个一模一样的 82。
这就是用户听到的"重复音节"。

### 3.5 第四步：新引擎设计

保留不动：`generate()` 签名与返回字段、`piece_id / parent_piece_id` 语义、
`pieces_index.jsonl`、输出路径、命令 / Tool / 白名单 / 冷却 / NapCat 链路 / 音色。

核心结构：

```
动机(A, 2小节)  ──派生──>  A' 变奏  ──>  B 对比  ──>  A'' 回归+终止
     │                        │            │             │
  只由 seed 决定         换第二小节cell   轮廓倒影+移区   主题前缀+级进终止
```

- 网格：`TPB=480`，1 拍 = 480 tick，1 小节 = 1920 tick；节奏模板以"拍"为单位，每小节严格合计 4 拍
- 动机：`(onset, dur, degree, vel_hint)` 列表；生成后先做一次跳进收敛，再派生所有乐句
- 选音：每音在 `prev_deg±5`（5–12 个）候选中按
  和弦匹配(强拍权重 3.4) / 轮廓贴合 / 级进优先 / 大跳后反向补偿 / 同音惩罚 / 音域惩罚
  加权 softmax 采样
- 半终止：A' 末音就近靠到当前和弦音（±2 级内找最近）
- B：以动机均值为轴做反射（不是围绕单点），整体移到目标音区，声部加密，强制为全曲最高点
- A''：复用主题前缀，末 2–3 音级进解决到主音；最后一小节左手给长终止和弦
- 左手：4 织体按乐句切换（柱式 / 分解八分 / 八度加密 / 收稀留白）
- 力度：每乐句一条弧线（A 中等、A' 稍强、B 高潮 +10~19、A'' 渐弱），叠加 ±3 随机
- 人性化：起始 tick 加 ±8~14ms 确定性抖动，**夹在本小节内**；同音重复靠 articulation 0.80 断开
- 踏板：CC64 每小节一踩一放（ch0/ch1 同步）
- 资源：读 `/proc/meminfo` 与 `/proc/loadavg`，不足抛 `ResourceBusyError`；`threading.Lock` 并发=1

### 3.6 第五步：迭代记录（真实踩坑，7 轮）

这一节是本项目最有复用价值的部分。

| # | 现象 | 根因 | 处理 |
|---|---|---|---|
| 1 | B 段 7 个音全是同一个音高 | 倒影围绕 `deg_a[len//2]` 单点做，再 clamp，负数全被压到 0 | 改为以动机**均值**反射，再整体平移到目标音区 |
| 2 | B 段音符比节奏槽少 | 倒影后 7 个级数 vs 对比节奏 9 个槽，`zip` 直接截断 | 新增 `_fit_length`：多了截断、少了按级进延伸 |
| 3 | A~A' 音程相似度 = 0 | 强拍**事后** clamp 到和弦音，把主题音改了 | 去掉事后 clamp，和声倾向移到**选音阶段**加权 |
| 4 | A~A' 相似度 = 1.0（逐字复制） | 节奏变化只改时值没改起音位置 | A' 保留第 1 小节 cell、**替换第 2 小节 cell** |
| 5 | 强拍和弦音比例掉到 46% | 去 clamp 后派生乐句换和弦就失配 | 仅对 A'/B 做 **±1 级**最小校正；A 与 A'' 不动（A'' 本身在主和弦） |
| 6 | 出现 7.99 拍的音符（跨小节） | jitter 把起始 tick 推到小节线之前 | 起始 tick 夹在 `[bar_start, bar_start+BAR-30]` |
| 7 | 变奏时 A 段被改掉 | 和声进行按 `variation` 随机选 | 和声改用独立 `random.Random(seed*7+11)`，与 variation 无关 |

另外修正了**两个指标口径错误**（否则会误判）：

- "强拍是否和弦音"原用当小节左手音高类，但左手有留白小节只有根音 → 改为按
  **2 小节窗口**合并左手音高类（和声本来就每 2 小节一换）；
- "节奏多样性"原本按时值算，而时值受 articulation／写入游标钳制影响 → 改为按
  **小节内起音位置**（16 分音符网格）算。改口径后旧版那个虚高的 1.0 才暴露出假象。

### 3.7 第六步：部署与 A/B

- 部署方式：直接覆盖 `data/MaiMBot/plugins/monika_piano/*`，MaiBot FileWatcher
  插件级热重载（实测约 30 秒，容器不重启）
- 容器内解释器是 `/MaiMBot/.venv/bin/python`（Python 3.13.16），系统 `python3` 没有 mido，
  排查时容易误判"缺依赖"
- 新版对照组：同参数重跑 6 组 → `/MaiMBot/data/piano/ab/new/`
- 指标：`scripts/ab_metrics.py`（120 行，从 MIDI 解析右手主旋律算 11 项指标 + 合法性校验）

结果见 `PIANO_AB_REPORT.md` 第三节，摘要：

| 指标 | 旧 v0.3 | 新 v0.4 |
|---|---|---|
| 连续同音率 | 43–70% | 3–13% |
| 最长同音连击 | 9–12 音 | 2–3 音 |
| 无旋律小节 | 4 / 8 | 0 / 8 |
| 强拍落在和弦音 | 0–50% | 75–100% |
| 有声乐句 | 2/4 | 4/4 |
| B 段为全曲最高 | 无 B 段 | 6/6 成立 |
| 同 seed 逐字节复现 | 是 | 是 |
| 耗时 / RSS | 0.32–0.50s / 22MB | 0.32–0.53s / 22MB |

### 3.8 第七步：回归验证

| 项目 | 方式 | 结果 |
|---|---|---|
| `/piano` 命令 | 注入 `/piano 开心 seed=8888` | `piece_8888_playful`，`key=C major`，send=True |
| 变奏续写 | 注入 `/piano 变奏` | `piece_8888_playful_v1`，`parent_piece_id` 正确，A 段逐音一致 |
| 冷却 | 60s 内连发 | 未生成新作品 |
| `play_piano` Tool | 注入"莫妮卡，弹一段欢快的钢琴曲给我听吧" | Planner 真实调用 `play_piano` → `piece_12408_playful`（模型自选 120bpm/16 小节），send=True |
| 资源 | 生成前后 free / loadavg / docker stats | 可用 866MB、load 0.04；core 293MB/700MB；三容器 healthy；无 OOM |
| 磁盘 | du | piano 目录 89MB（含 A/B 对照 50MB），根分区 27G/40G |

### 3.9 验收

- 用户人耳试听后确认本次改进成立（2026-11-03 21:25）
- 已知取舍：A' 会重新配和声（换和弦后只做 ±1 级校正），故 A~A' 相似度 0.2–0.78；
  A'' 与 A 共用节奏型是刻意的（回归主题），所以"节奏型数量"为 3 而非 4

## 四、当前资产清单

```
/opt/qq-fish/
├── PIANO_STATUS.md                     # 运行状态总览（P0–P5）
├── PIANO_AB_REPORT.md                  # 本轮技术报告
├── docs/piano/
│   ├── README.md                       # 本目录索引
│   ├── WORKLOG.md                      # 本文件：全过程
│   ├── metrics.json                    # A/B 指标原始数据
│   └── listen/                         # A/B 试听页（含 mp3）
│       ├── AB_试听.html
│       └── mp3/                        # 12 个 mp3（旧/新 × 3 情绪 × 2 seed）
├── scripts/
│   ├── piano_engine_legacy_v03.py      # 旧引擎归档（可复现旧版对照）
│   ├── piano_gen.py                    # P0 独立生成脚本
│   ├── ab_gen.py                       # 批量对照生成
│   ├── ab_metrics.py                   # 诊断指标
│   ├── send_piano_test.py              # P1 语音发送验证
│   └── inject_piano_msg.py             # E2E 消息注入
└── data/MaiMBot/
    ├── plugins/monika_piano/           # 插件源码（已入 git）
    │   ├── piano_engine.py             # 作曲引擎 v0.4
    │   ├── plugin.py                   # 命令 + Tool + 发送 + 冷却 + 授权
    │   ├── config.toml                 # 真实配置（含 token，被 git 忽略）
    │   └── config.toml.example         # 配置模板（入库）
    └── piano/                          # 作品库
        ├── pieces_index.jsonl          # 作品索引
        └── ab/{legacy,new}/            # A/B 对照音频与 MIDI
```

git：`e0eb214` (v0.4) ← `a728fa7` (纳入版本控制) ← `e8eee88` (P0–P3)
tag：`piano-legacy-v0.3`（改进前基线）、`piano-v0.4`（当前）

## 五、复现命令

```bash
cd /opt/qq-fish

# 生成对照组（容器内跑，输出 mid+wav+json）
docker exec maim-bot-core /MaiMBot/.venv/bin/python /tmp/ab_gen.py \
  /MaiMBot/plugins/monika_piano/piano_engine.py /MaiMBot/data/piano/ab/new \
  '[["calm",1001,96],["calm",1002,84],["melancholy",2001,72],["melancholy",2002,66],["playful",3001,116],["playful",3002,128]]'

# 指标对比（旧 vs 新）
docker exec maim-bot-core /MaiMBot/.venv/bin/python /tmp/ab_metrics.py \
  /MaiMBot/data/piano/ab/legacy /MaiMBot/data/piano/ab/new

# E2E：命令 / 变奏 / 自然语言
docker exec maim-bot-core /MaiMBot/.venv/bin/python /tmp/inject_piano_msg.py "/piano 忧伤 seed=42"
docker exec maim-bot-core /MaiMBot/.venv/bin/python /tmp/inject_piano_msg.py "/piano 变奏"
docker exec maim-bot-core /MaiMBot/.venv/bin/python /tmp/inject_piano_msg.py "莫妮卡，弹一段钢琴曲"
```

> 注：`ab_gen.py` / `ab_metrics.py` / `inject_piano_msg.py` 需先 `docker cp` 进容器。

## 六、踩坑清单（可复用）

1. **改代码前先确认它在不在 git 里**。本项目插件在 `data/` 下被整体忽略，
   等于没有回滚点。git 不会进入被忽略的父目录，un-ignore 必须逐级写。
2. **配置里的密钥不要顺手入库**。`config.toml` 含 NapCat token，用
   `config.toml.example` + 继续忽略真实文件的组合。
3. **容器内有两个 Python**。`/MaiMBot/.venv/bin/python` 才有 mido；
   用系统 `python3` 排查会误判缺依赖。
4. **MIDI 的 tick 单位是最容易错的地方**。节奏值一定要明确"单位是拍还是 16 分音符"，
   写完先 dump 每小节音符位置核对。
5. **"和声约束"要在选音阶段做，不要在生成后 clamp**。事后 clamp 会破坏主题，
   而且正是旧版产生同音连击的原因。
6. **人性化抖动不能越过小节线**。夹在 `[bar_start, bar_end)` 内。
7. **指标口径要先验证**。和弦音要按和声窗口（2 小节）合并；节奏型要按起音位置判定。
   口径错了会得出与听感相反的结论。
8. **热重载会触发两次**（多个文件同时改），日志里看到"卸载→加载→卸载→加载"是正常的。
9. **E2E 注入消息需要顶层 `account_id`**，`user_info.platform` 要在 `message_info` 内。

## 七、后续建议

| 优先级 | 事项 | 说明 |
|---|---|---|
| P1 | 作品库 TTL | piano 目录已 89MB，WAV 约 5MB/首；建议保留最近 N 首 + N 天，或 WAV 转 mp3 存 |
| P1 | 容器依赖自愈 | fluidsynth + TimGM6mb + mido 是运行时装的，镜像重建会丢；建议做进 Dockerfile 或启动脚本自检 |
| P2 | 音色 A/B | FluidR3_GM.sf2 已在容器里，改一行 config 即可对比；按方案应先过听感再切 |
| P2 | 更多情绪/进行 | 目前 6 情绪 × 3 条进行；可扩到每条情绪 5 条 |
| P3 | 多乐器/编曲 | 需服务器升内存（≥4G）后再评估 |
## 八、运维加固：依赖持久化 + 作品库清理（2026-11-03 22:00）

第七节列的两条建议已实施。二者都不碰作曲算法与 QQ 发送。

### 8.1 先诊断：为什么依赖会丢

查容器挂载表后确认：

| 组件 | 实际位置 | 是否持久化 |
|---|---|---|
| `/MaiMBot/.venv`（含 mido） | 镜像 overlay 层 | ❌ 镜像更新即丢 |
| `/usr/bin/fluidsynth`（apt 装的） | 镜像 overlay 层 | ❌ 同上 |
| `TimGM6mb.sf2`（apt 装的） | 镜像 overlay 层 | ❌ 同上 |
| `/MaiMBot/depends-data` | 宿主 `./depends-data`（bind） | ✅ |
| `/MaiMBot/data` | 宿主 `./data/MaiMBot`（bind） | ✅ |

所以"单纯重启容器"没事，"删除容器重建 / 换镜像"就全丢。旧文档只写了重装命令，
属于人工记忆。

### 8.2 做法：把运行时整体搬进持久化卷

不改 compose、不重建容器，直接利用已存在的 `depends-data` 挂载：

```
depends-data/piano-runtime/          (33 MB)
├── bin/fluidsynth                   可执行文件
├── lib/                             54 个动态库（排除 glibc 核心，避免与未来镜像混用）
├── soundfonts/TimGM6mb.sf2          6 MB 音色
└── pylibs/mido + typing_extensions  纯 Python 包
```

- `scripts/piano_deps.sh`（宿主）：`check` / `vendor` / `restore`
- `scripts/piano_deps_inner.sh`（容器内）：实际执行归档与自检
- 验证方式是在**干净环境**里跑：`env -i LD_LIBRARY_PATH=... bin/fluidsynth --version`
  → 报 FluidSynth 2.4.4；再用它真实渲染一首 → 产出 6.2 MB WAV。
  说明副本不依赖镜像内的任何库。
- 引擎 `resolve_runtime()`：优先 vendored 副本，缺失才回退系统 PATH；
  SoundFont 同理（配置路径不存在时自动用 vendored 的那份）。
- 插件启动自检 `_check_deps()`：只检测不安装，缺失时 **ERROR 日志 + 禁用作曲**，
  并提示 `bash scripts/piano_deps.sh restore`，避免运行到一半才炸。
  mido 的引导放在 `plugin.py` 顶部、**早于** `import piano_engine` 执行，
  否则 mido 缺失时整个插件连加载都会失败，就没机会优雅降级了。

### 8.3 作品库清理

`piano_storage.py`（约 200 行，无新依赖）：

| 规则 | 实现 |
|---|---|
| 保留名单 | `.mid` / `.json` / `pieces_index.jsonl` 永不删除 |
| TTL | 普通 WAV 超过 `wav_ttl_days`（默认 30）才考虑删除 |
| 容量上限 | WAV 总量超过 `max_wav_mb`（默认 300）时按最旧优先追加删除 |
| 收藏保护 | `favorites.json` 里的作品永不自动删除 |
| 缺谱保护 | 删前用 mido 解析 MIDI，必须存在且至少一个音符；否则保留 WAV |
| 频次 | 状态文件记录日期，每天最多执行一次（挂在首次 `/piano` 上，不额外起进程） |
| 干跑 | `cleanup_dry_run = true` 只记录计划；已演练后置为 false |

新增命令：`/piano 收藏`、`/piano 取消收藏`、`/piano 再听`。
`再听` 走 `ensure_wav()`：WAV 若已被清理，就用 MIDI 重新渲染再发。

### 8.4 验证记录

| 项目 | 方式 | 结果 |
|---|---|---|
| 依赖归档 | `piano_deps.sh vendor` | 33 MB / 54 库 / 自检 5 项全 OK |
| 脱离镜像渲染 | `env -i` 跑 vendored fluidsynth | 真实渲染出 6.2 MB WAV |
| 启动自检 | 重载插件 | 日志 `运行时自检通过: fluidsynth=vendored` |
| 生成回归 | `/piano 平静 seed=6060` | `piece_6060_calm` sent=True |
| 清理干跑（默认参数） | 首次 `/piano` | 扫描 10 首 49.3 MB，计划删除 0 |
| 清理干跑（强制超期） | `ttl=0, cap=10MB` 真实数据 | 计划删 9 首 44.5 MB，收藏项被保护，无缺谱项 |
| 收藏 | `/piano 收藏` | `favorites.json` 写入 `piece_6060_calm` |
| 断谱恢复 | 手工删掉该曲 WAV → `/piano 再听` | WAV 重建（字节数 5099052 与原来一致），重渲染=是 |
| 正式清理 | `cleanup_dry_run=false` 后 `/piano` | `清理(exec)` 实删 0（均未超期），状态落盘 |
| 单元测试 | `test_storage.py` | TTL / 收藏 / 缺谱 / 坏谱 / 容量上限 / 每日一次 全部符合预期 |

### 8.5 仍然存在的边界

- vendored 副本假设未来镜像的 glibc **不比现在旧**（glibc 向下兼容，正常升级没问题）。
  若哪天基座改到更老的发行版，`piano_deps.sh restore` 会重装并重新归档。
- 清理是"懒触发"：没人弹琴就不会扫描。这是刻意的——没有新作品就没有清理需求，
  也不需要多一个常驻定时器。
- `/piano 再听` 只重放本会话最近一首；按编号点播旧曲还没做（索引里有 piece_id，加一个
  参数即可，但当前没有需求）。
## 九、作品目录（SQLite）+ 双阈值修剪（2026-11-03 22:10）

### 9.1 问题：MIDI 永不删会不会越来越臃肿？

上一轮（P6）的规则是"MIDI 永不删"。这其实不是一个真正的策略，只是把问题推迟。

按实际体积算：一首 8 小节作品的 MIDI 约 **0.9 KB**，元数据 JSON 约 0.5 KB。
所以：

| 累计作品 | MIDI+JSON 占用 | 按 WAV 5 MB/首算 |
|---|---|---|
| 1,000 首 | ~1.4 MB | 5 GB（已由 300 MB WAV 上限挡住） |
| 100,000 首 | ~140 MB | — |
| 1,000,000 首 | ~1.4 GB | — |

结论：**"1 GB 上限"对 MIDI 来说等于永不触发**（要 100 万首才碰到）。
所以用户的"设个上限删最旧"方向是对的，但阈值不该只按容量设。

真正会先出问题的是另外两件事：

1. **索引文件的读取成本**。`pieces_index.jsonl` 只增不减，而
   `last_piece()` 每次变奏/再听都要**全量解析整份文件**。作品多了，
   变奏响应会越来越慢——这是延迟问题，不是磁盘问题。
2. **作品条数**。它是"作品库规模"最直观的度量，也是实际会先撞到的墙。

### 9.2 做法：SQLite 作品目录 + 双阈值

`piano_catalog.py`（约 280 行，**只用标准库 sqlite3，零新增依赖**）：

```sql
CREATE TABLE pieces (
  piece_id TEXT PRIMARY KEY, seed, mood, bpm, bars, variation,
  parent_piece_id, chat_id, created_at, engine_version,
  midi_path, wav_path, midi_bytes, meta_json
);
CREATE INDEX idx_pieces_chat ON pieces(chat_id, created_at DESC);
CREATE INDEX idx_pieces_created ON pieces(created_at ASC);
```

- **写入**：`generate()` 落盘后写 DB；写失败自动回退追加 jsonl（保证索引不丢）
- **读取**：`last_piece()` 走索引查询，不再扫全文件
- **迁移**：首次使用把 `pieces_index.jsonl` 导入 DB（幂等，靠标记文件 + mtime 判断），
  之后 **jsonl 停止写入**，降级为只读归档
- **双阈值修剪**：

| 阈值 | 默认 | 作用 |
|---|---|---|
| `max_pieces` | 5000 | **实际护栏**：超条数就按最旧整首移除 |
| `midi_max_mb` | 1024 | 安全网：极端情况下按容量兜底 |
| `min_keep_days` | 7 | 最近 N 天的作品不会被整首移除（防止刚生成就被删） |

- **保护规则**：收藏（`favorites.json`）永不删；`min_keep_days` 内不删；
  超限但全被保护时**不删任何东西**，只记日志
- **整首移除**：删 mid + json + wav + DB 行。之所以连 WAV 一起删——
  只删 0.9 KB 的乐谱对磁盘毫无意义，而留下的 WAV（5 MB）最终也会被 TTL 清掉
- **dry-run**：只出计划。本地单测 + 真实曲库演练通过后才启用

两级维护的先后关系：

```
作品目录硬阈值（整首移除） → WAV 软阈值（只删音频，保留乐谱）
```

### 9.3 验证记录

| 项目 | 方式 | 结果 |
|---|---|---|
| legacy 迁移 | 真实曲库首次 init | 导入 9 条；再次 init 返回 0（幂等） |
| DB 落盘 | 生成 `piece_8080_warm` | 表内 10→11 条，`parent_piece_id` 正确 |
| 变奏回溯 | `/piano 变奏` | 经 DB 查到母曲 → `piece_8080_warm_v1` sent=True |
| 双阈值 dry-run | 真实数据 `max_pieces=3` | 计划移除最旧 6 首，未动文件 |
| 默认阈值 | 真实数据 9 首 / 0.01 MB | `over_count=False, over_size=False`，不修剪 |
| 条数修剪（单测） | 10 首 / 上限 5 / 收藏 1 / 近期 2 | 删 5 首，收藏与近期项全部保留，mid+wav+json 同步消失 |
| 容量修剪（单测） | `max_mb=0.002` | 触发 `over_size`，收藏项未入选 |
| 两级串联（单测） | `storage.cleanup(max_pieces=3)` | catalog 段与 WAV 段分别按各自规则出计划 |
| jsonl 冻结 | 观察 mtime / 行数 | 停留在 21:55、9 行，不再增长 |
| 线上维护日志 | 首次 `/piano` | `WAV 维护(exec): 扫描=11 合计=54.2MB 计划删=0` + `作品目录 9 首 / 0.01MB，未达阈值` |

### 9.4 边界

- 修剪只在**每日维护**里跑，仍然是懒触发（首次 `/piano`），不新增常驻进程。
- 整首移除是**不可逆**的（乐谱没了就再也重渲染不出来）。所以默认
  `min_keep_days=7` + 收藏保护 + 每日只跑一次，三重缓冲。
  真要有作品永久保留，用 `/piano 收藏`。
- 570 首左右就会到 1 MB MIDI，5000 首约 9 MB——**容量阈值在有生之年不会触发**，
  这是刻意的：它是防止"某天批量导入"之类的意外，日常靠条数阈值。
- `pieces_index.jsonl` 现在是只读归档；若手工往里加行，下次 init 会自动补进 DB
  （靠 mtime 判断），但正常情况下不必再碰它。

## 十、P8/P9：在线曲谱库 + 自主音乐行为（2026-11-05）

依据用户提供的两份设计包（《Monika_Online_MIDI.md》+《Monika 自主钢琴行为系统.md》及配套 zip）实施。
P9（外部曲谱）是 P8（自主行为）的依赖，按 P9 → P8 顺序落地。

### 10.1 开工审计（规格要求"先审计不假设"）

| 核查项 | 结论 |
|---|---|
| pieces.db 行数 | SQL 实数 18（此前报告的"17"是汇总表笔误：playful 9/calm 3，不是 7/4；DB 无错） |
| 文件 vs 目录 | 磁盘 21 MIDI / 目录 18 行 → 3 首迁移前孤儿（piece_7_calm / piece_1842_melancholy / piece_4476_melancholy），有 JSON 元数据没进目录 |
| 清理模块影响面 | piano_storage.scan 只扫根目录 *.wav（非递归），external 子目录不受旧 TTL 影响 |
| Mutopia 连通性 | 服务器直连 OK（单曲页 5.8KB/4.5s）；搜索接口是 `make-table.cgi`（不是 advsearch.cgi，后者 404） |
| 治理禁止令 | monika_governance 的 `/MaiMBot/data/governance/rules.json`（rule_type=prohibition, scope, status=active），P8 门禁只读对接 |
| Hook 协议 | `chat.receive.after_process`（OBSERVE）+ `maisaka.planner.before_request`（改 Context Items，有锚点校验，风险高未采用） |

处置：3 首孤儿按各自 JSON 元数据补录进 pieces.db（backfill_orphans.py，幂等；created_at 取文件
mtime，来源标 `backfill:pre-catalog`）→ 目录 21 行与磁盘对齐。`piece_1842_melancholy` 的诗歌
关联（《雨落钢琴》）随之恢复可查。piece_7_calm 缺 WAV，可 ensure_wav 重渲染。

### 10.2 P9 在线曲谱库（piano_external.py，约 460 行）

- `external_scores` + `external_render_cache` 两表进 pieces.db（CREATE IF NOT EXISTS，pieces 表不动）
- 只自动抓取 Mutopia：HTTPS + host 白名单 + 路径规则（页面必须 piece-info.cgi?id=，MIDI 必须
  /ftp/*.mid），**重定向逐跳复检**；URL 带凭据/端口拒绝
- 页面解析：曲名 h1/h2、作曲家 h3/h4（真实页面是 h4！）、Copyright 正则只认 Public Domain /
  CC BY(-SA) x.y；**无许可文本直接拒绝导入**
- MIDI 校验+消毒：SMF 0/1、≤48 轨、≤18 万事件、时长 0–360s、音符数有界；剥除 SysEx/歌词/
  文本/标记/版权 meta（低信任输入，防提示注入），保留全部时值
- 渲染缓存键 = sha256(midi_sha256 + sf2_sha256 + 渲染器版本 + 采样率 + "v1")，音色/渲染器
  变更自动失效；复用 piano_engine.resolve_runtime（vendored fluidsynth 优先）
- 接口：工具 find_piano_midi（本地优先→Mutopia 搜索，只查不下载）/ import_piano_midi /
  play_piano_score；命令 `/piano 找谱|导谱|弹谱`
- 清理兼容：external_midis 与 external_scores 行**永不自动删**；external_wavs 独立 TTL
  （cleanup_external_wavs，默认 30 天）

### 10.3 P8 自主音乐行为（music_state.py + music_act.py）

两次独立决策（规格核心）：
1. 要不要做音乐：ignore / talk_music / think_music / practice / offer_audio / send_audio
2. 用什么资源：new_original / variation / replay_original / search_external / replay_external
   / practice_existing

- `monika_music_act(action, intent, referent, delivery)` 单一高层工具；action=auto 时由
  music_act.route() 二级路由（真实目录命中 + 词法信号 + 未完成项目），不按关键词硬映射、不随机
- 词法信号 `detect_music_signals`（点播/创作/变奏/复奏/练琴/话题/情绪/拒绝）只做低成本候选
  探测（chat.receive.after_process OBSERVE 存会话信号）；**普通聊天零新增 LLM 回合**
- 音乐生活状态 5 表：music_projects（未完成创作，stage=idea/draft/practicing/done）、
  practice_sessions（真实执行记录）、play_events（发送回执，failed 也记）、
  music_preferences（可验证偏好）、music_delivery_policy（会话级主动策略）
- **投递门禁**（顺序固定）：治理禁止令 > opt-out > 静默时段 23:00–08:00 > 配额（私聊 1 次/日
  需先被欢迎过；群默认关、开启后 72h 一次）。**明确点播绕过配额与静默，但不绕过禁令**；
  opt-out 只拦主动不拦明确点播（用户最新意愿优先）
- 自主创作与主动发送分离：delivery=no_audio → 只落 music_projects（stage=draft）不发 WAV
- practice 必须真实执行（分析曲谱/生成练习片段）才写 practice_sessions；无执行不称"练过"
- 偏好接口 `monika_music_act(action=pref)`：欢迎主动/别发语音/管理员禁令（admin_user_ids 校验）
- 外部作品永不冒充原创：import/play 返回都带"这是已有作品的曲谱版"与许可归因

### 10.4 验收记录

单测（本地 + 容器均 48/48）：门禁矩阵 12 项（明确点播绕配额、静默拦截、opt-out 双面、群默认关/
开启/72h 间隔、禁令拦一切）、governance 对接 3 项（音乐禁令拦、非音乐不误拦、群作用域隔离）、
路由 12 项、MIDI 消毒 4 项、页面解析 4 项、URL 白名单 5 项、建表不破坏 pieces。

真实抓取（验收门槛#2/#3）：
- id=472《Prelude: Op. 28, No. 20》Chopin / Public Domain / 74.3s / 286 音符 / solo_piano →
  ext_9cc6c417d4bb261644f6；重复导入同 ID（幂等）；pieces 21→21 不变
- 首次渲染 13.6MB WAV，二次渲染缓存命中；许可归因随发送文本附上
- 三个可复现的自主行为记录（真实 Planner 决策，日志留痕）：
  1. **主动不弹**：「刚下课，今天课好多」→ Planner 只调 reply，不碰音乐工具
  2. **找谱但先提议不直发**：「我想听肖邦的前奏曲」→ monika_music_act → search_external
     （路由日志 reason=点播真实曲名/作曲家，本地无谱）→ 回复"找到几首…想听哪首？挑一首
     我就给你导进来"（offer，不是直接塞语音）
  3. **创作但不发送**：「写一段雨天感觉的旋律，先存着别发」→ action=create delivery=no_audio
     → proj_1793844779_9661 落库 stage=draft，play_events 无新增
- 附加真实证据：群点播《卡农》本地未命中 → bot 诚实说"正经谱子还没入库呢，先拿即兴的顶上了"
  （不冒充原曲，ACCEPTANCE #16 的活样本）；play_events 记录了权限失败→修复→sent 的完整轨迹

### 10.5 踩坑（新增 4 条）

1. **参考 CLI 的正则不能信**：midi_catalog.py 的 `Instrument(s): ... | Style:` 带 `|` 分隔符，
   真实页面是两个独立 td（`Instrument(s):</b> Piano`）→ 解析出 'unknown' 卡审批。按真实 DOM 修。
2. **作曲家在 h4 不在 h3**：fixture 编的是 h3，真实页面是 h4——以真实页面为准，解析兼容 h2–h4。
3. **tempfile 权限 0600**：NamedTemporaryFile 创建的渲染产物 NapCat 容器读不到（Permission
   denied），replace 后必须 chmod 644。Windows 上 NamedTemporaryFile 还有独占句柄问题，
   delete=False + 手动 unlink 才跨平台。
4. **vendored mido 的 sys.path 引导要放进每个独立脚本**：插件里有引导，standalone 验证脚本
   没有就 ModuleNotFoundError（同 P6 的坑换了个马甲）。

另：`planner.before_request` hook 可改写 Context Items，但有 schema 锚点校验、风险高——
按规格"不给每句强塞提示"的本意改为工具描述引导 + OBSERVE 信号采集，零侵入。

### 10.6 边界与未做

- Mutopia 搜索是即时联网（每次找谱 2–5s），未做搜索结果缓存
- OpenScore Lieder / KernScores 等第二来源未接（规格也标为二阶段）
- 提取钢琴音轨的 derived 变换未做（首版只收 solo_piano，多轨标 needs_review 不自动演奏）
- 主动分享的"发 15–30 秒片段"策略未做（长曲按整首发）
- 跨作品：诗歌 related_piano_piece_id=piece_1842_melancholy 已随补录恢复可查，但
  "以旧作事实出发的回顾"由诗歌侧 get_poem 承担，本次未新增联动逻辑
