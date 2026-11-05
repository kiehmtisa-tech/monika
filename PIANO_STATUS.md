# Monika Piano 插件 — 实施进度

> 方案来源: MaiBot_Monika_低Token钢琴生成_实施方案.md (2026-11-02)
> 更新: 2026-11-03 18:30

## 已完成

### P0: 确定性钢琴生成 ✅ (2026-11-03 18:28)
- 脚本: /opt/qq-fish/scripts/piano_gen.py (mido 1.3.3 + FluidSynth 2.4.4)
- 结构: A-A\x27\x27-B-A\x27\x27\x27 8小节动机变奏, 左手根音-五音分解, 力度弧线+节奏微偏差+延音踏板
- 验证: seed=1842 两次生成 md5 一致 (d79a7dc4...), 28.85s, mean -36.7dB
- 输出: /opt/qq-fish/data/MaiMBot/piano/piece_{seed}_{mood}.{mid,wav,mp3,json}
- SoundFont: TimGM6mb.sf2 (6MB 轻量, 机器内存仅 1.6G 故未用 FluidR3 148MB)

### P1: QQ 语音发送 ✅ (2026-11-03 18:29)
- 链路: core 容器 python websockets → ws://maim-bot-napcat:3001 (OneBot 11, Bearer token)
  → send_private_msg + record 段 file:///MaiMBot/data/piano/xxx.mp3 (共享卷, 免 base64)
  → NapCat 自动转 silk (28.86s) → 发送成功, message_id=1776707250, retcode=0
- 脚本: /opt/qq-fish/scripts/send_piano_test.py
- 未改 NapCat 任何配置 (复用现有 WS 服务端, 与 MaiBot 适配器并存)

## 待做
- P2: /piano 命令接入 MaiBot 插件系统 (本地模板 0 LLM 调用) + 最小 Tool 参数接口
- P3: Monika 人设语言包装 / 作品记忆(piece_id+parent_piece_id) / 变奏续写 / 冷却与授权
- P4: 高级作曲与多乐器 (暂缓)
- 备注: SnowLuma 适配器 voice.py 出站能力已具备, P2 插件内发送 API 需对照其实现

### P2: MaiBot 插件接入 ✅ (2026-11-03 19:14 E2E 验证通过)
- 插件: /opt/qq-fish/data/MaiMBot/plugins/monika_piano/ (dragon.monika-piano)
  - plugin.py: @Command("piano") pattern ^/piano, 参数 [mood] [seed=N] [bpm=N] [bars=N], 支持中文情绪别名
  - piano_engine.py: 生成引擎 (与 scripts/piano_gen.py 同源, 容器内运行)
  - config.toml: 音色/输出目录/NapCat WS/token/冷却 60s
- 链路: QQ 发 "/piano" → MaiBot 命令链拦截 (0 LLM) → 线程池生成 MIDI
  → 容器内 fluidsynth 渲染 WAV → 共享卷 file:// URI → 插件内 OneBot WS 发 record
  → NapCat 转 silk → QQ 语音 + 人设化文本回复
- E2E 实测 19:14: 注入 "/piano" → 命令执行成功(拦截等级1) → piece_4476_melancholy
  → silk 28.86s → 发送私聊成功 + 文本回复成功
- 插件加载要求 (踩坑记录): _manifest.json 必须有 author.url(非空) 和 urls.repository;
  插件类必须实现 on_load/on_unload/on_config_update; 模块需提供 create_plugin() 工厂
- 容器依赖 (重建镜像后需重装): fluidsynth+timgm6mb-soundfont (--no-install-recommends,
  需补 libgomp1 libwayland-client0) + mido (拷贝宿主机包到 venv site-packages)
- 已知限制: 群聊/私聊都可用; 冷却 60s/会话; 暂无 Tool 参数接口 (P3); 无作品记忆续写 (P3)

### P3: 作品记忆 + 变奏 + Tool + 授权 ✅ (2026-11-03 19:26 E2E 验证通过)
- **变奏引擎**: 装饰层与动机层分离——动机仅由 seed 决定, 力度/节奏人味/B段移调/左手花样
  随 variation 变化; 同 seed 变奏保留主题。文件名 piece_{seed}_{mood}_v{n}
- **作品记忆**: /MaiMBot/data/piano/pieces_index.jsonl (piece_id/parent_piece_id/seed/
  variation/chat_id/created_at/md5); last_piece() 按会话回溯最近作品
- **/piano 变奏** (或 v2/改一下): 取本会话最近作品 → 同 seed variation+1, 人设化回复
- **play_piano Tool**: 枚举参数 mood(6选1)/bpm/bars/seed/variation, 工具结果极短(≤50字, Token控制);
  description 限定"仅用户明确想听钢琴时调用"防误触
- **授权**: config.toml allow_users/allow_groups 白名单 (空=不限制), 未授权人设化拒绝
- E2E 实测: ①/piano happy → piece_4873_playful ✅ ②/piano 变奏 → piece_4873_playful_v1
  (同seed不同md5, 动机保留) ✅ ③自然语言"莫妮卡弹一段欢快的钢琴曲" → Planner 调用
  play_piano → piece_5159_playful (模型自传 bpm=120 bars=16) ✅
- 预算联动: 全链路走 budget-gw 计费; 变奏/命令触发零 LLM, 仅自然语言触发有 Tool Call 开销
- 至此方案 P0-P3 全部完成; P4 (高级作曲/多乐器) 按方案暂缓

### P5: 作曲质量优化 v0.4 (2026-11-03 20:40，待人工听感验收)
- 诊断出旧版三处实现级缺陷：
  1. 节奏 tick 单位错：节奏值以 16 分音符为单位（1 拍 = 120 tick），代码按 30 tick/单位
     且乐句偏移 1 小节，导致右手只占每小节前 1.4 拍、全曲只到 3.34/8 小节，后 4 小节无主旋律
  2. mood 全部回退：按 mode 索引和声库而 mode 默认 minor，该库只有 melancholy/nostalgic，
     calm/playful/warm/hopeful 全部落到同一进行 + 同一音阶
  3. 逐音 clamp：_fit_to_chord 把不同级数压到同一音高，实测连续同音 9-12 次
- 重写引擎（275 行 -> 880 行）：动机结构 (onset,dur,degree,vel) + 候选评分 softmax 选音
  + A/A'/B/A'' 真正的变奏算子（rhythmic/sequence/neighbor/inversion/register/ending）
  + 左手 4 种织体按乐句切换 + 力度弧线 + 小节内 jitter + CC64 + 资源守护 + 并发锁
- A/B 结果（3 情绪 x 2 seed，同 mood/tempo/seed）：
  同音率 43-70% -> 3-13%；最长同音 9-12 -> 2-3；无旋律小节 4 -> 0；
  强拍和弦音 0-50% -> 75-100%；有声乐句 2/4 -> 4/4；B 段 6/6 组为全曲最高；
  同 seed 仍逐字节复现；生成耗时 0.32-0.53s、RSS 22MB 与旧版持平
- 版本控制：.gitignore 改为 data/* 逐级放行，插件源码纳入 git；tag piano-legacy-v0.3
  为旧版基线；scripts/piano_engine_legacy_v03.py 为旧引擎归档；真实 config.toml
  含 NapCat token 仍忽略，改为跟踪 config.toml.example
- 新增工具：scripts/ab_gen.py（批量生成对照组）、scripts/ab_metrics.py（诊断指标）
- 回归：/piano、/piano 变奏、冷却、索引写入、QQ 语音发送全部通过；资源无异常
  （可用内存 883MB、load 0.21、core 293MB/700MB、三容器 healthy）
- 详细报告：PIANO_AB_REPORT.md；试听对照：本地 piano_work/ab/AB_试听.html
- 文档落盘：`docs/piano/`（`WORKLOG.md` 全过程工作日志含 7 轮迭代与踩坑清单、
  `README.md` 索引、`listen/AB_试听.html` + 12 段对照 mp3、`metrics.json` 原始指标）
- E2E 补充：`play_piano` Tool 自然语言链路已验证（Planner 真实调用 →
  piece_12408_playful，模型自选 120bpm/16 小节）
### P6: 运维加固 — 依赖持久化 + 作品库清理 (2026-11-03 22:00)
- **依赖持久化**（优先级高于清理）：诊断确认 `.venv`(mido)、`/usr/bin/fluidsynth`、
  `TimGM6mb.sf2` 全在镜像 overlay 层，容器重建/换镜像即丢。已把运行时整体归档到
  持久化卷 `depends-data/piano-runtime/`（33MB：bin + 54 个 so + SoundFont + pylibs），
  并在 `env -i` 干净环境下验证可独立渲染。引擎优先用该副本，缺失回退系统安装
- **启动自检**：插件 on_load 检测 mido / fluidsynth / SoundFont，缺失时 ERROR 日志 +
  禁用作曲 + 提示 `bash scripts/piano_deps.sh restore`；mido 的 sys.path 引导置于
  `import piano_engine` 之前，保证 mido 缺失时插件仍能加载并优雅降级
- **作品库清理**（`piano_storage.py`）：MIDI/JSON/索引永不删；WAV 默认 30 天 TTL +
  300MB 容量上限；收藏永久保留；删前校验 MIDI 可解析，缺谱则保留 WAV；
  每天最多一次（懒触发在首次 /piano，不新增进程）
- **新命令**：`/piano 收藏` / `/piano 取消收藏` / `/piano 再听`（WAV 被清理则用 MIDI 重渲染）
- **验证**：干跑默认参数计划删 0；强制超期干跑计划删 9 首 44.5MB 且收藏受保护；
  手工删 WAV 后 `/piano 再听` 重建成功（字节数一致）；正式 exec 模式实删 0
- **工具**：`scripts/piano_deps.sh`(check/vendor/restore) + `piano_deps_inner.sh`、
  `requirements.txt`、`test_storage.py`
- 配置新增（`[piano]`）：`cleanup_enabled` / `cleanup_dry_run`(现为 false) /
  `wav_ttl_days` / `max_wav_mb`
- 详见 `docs/piano/WORKLOG.md` 第八节
### P7: 作品目录 SQLite + 双阈值修剪 (2026-11-03 22:10)
- **背景**：P6 定的"MIDI 永不删"只是把问题推迟。实测 MIDI 仅 ~0.9KB/首、元数据
  ~0.5KB/首 → 1GB 上限要 100 万首才触发，等于没设；真正先撞墙的是
  `pieces_index.jsonl` 只增不减且 `last_piece()` 每次全量解析（延迟问题）
- **做法**：新增 `piano_catalog.py`（只用标准库 sqlite3，零依赖）——作品目录落
  `pieces.db`，带 `(chat_id, created_at)` 与 `(created_at)` 索引；`generate()` 写 DB
  失败自动回退 jsonl；`last_piece()` 走索引查询不再扫全文件
- **迁移**：首次使用把 `pieces_index.jsonl` 导入 DB（幂等，标记文件 + mtime 判断），
  之后 jsonl 停止写入、降级只读归档（已实测冻结在 9 行）
- **双阈值修剪**：`max_pieces=5000`（实际护栏）、`midi_max_mb=1024`（安全网）、
  `min_keep_days=7`（新品保护）；收藏永不删；超限按 created_at 最旧整首移除
  （mid + json + wav + DB 行）；全被保护时不删只记日志；支持 dry-run
- **两级维护顺序**：作品目录硬阈值（整首移除）→ WAV 软阈值（只删音频留乐谱）
- **验证**：迁移 9 条幂等；生成后 DB 10→11 条且 parent_piece_id 正确；
  `/piano 变奏` 经 DB 回溯成功；真实数据 `max_pieces=3` 演练计划移除最旧 6 首；
  单测覆盖条数/容量/收藏/近期/dry-run/文件同步删除；容器内两个测试脚本均通过
- 配置新增：`max_pieces` / `midi_max_mb` / `min_keep_days`
- 详见 `docs/piano/WORKLOG.md` 第九节

### P8/P9: 在线曲谱库 + 自主音乐行为 ✅ (2026-11-05 10:15，已 E2E 验证)
- P9 `piano_external.py`：external_scores/external_render_cache 进 pieces.db；Mutopia 受控抓取
  （host+路径白名单、重定向复检、许可必须 PD/CC BY(-SA)）、MIDI 校验+消毒（剥 SysEx/文本 meta）、
  渲染缓存键=midi+sf2+渲染器版本+采样率；工具 find/import/play_piano_score + 命令
  /piano 找谱|导谱|弹谱；external_midis 永不自动删，external_wavs 独立 TTL
- P8 `music_state.py`+`music_act.py`：monika_music_act 高层工具（两层决策：行为×资源），
  词法信号候选探测（OBSERVE hook，零新增 LLM）；music_projects/practice_sessions/
  play_events/music_preferences/music_delivery_policy 五表；投递门禁（治理禁止令>opt-out>
  静默 23:00-08:00>配额：私聊 1/日需先被欢迎、群默认关 72h 间隔，明确点播绕配额不绕禁令）
- 真实验收：Mutopia id=472 肖邦前奏曲真实导入（PD/74.3s/幂等/pieces 不动）→ 渲染 13.6MB WAV →
  QQ 发送成功（缓存二次命中）；3 个可复现自主记录（闲聊不弹/找谱先提议/创作只落库不发）；
  群点播《卡农》未命中时诚实退回即兴不冒充原曲
- 数据一致性：补录 3 首迁移前孤儿进目录（18→21，与磁盘对齐），piece_1842_melancholy 的诗歌
  关联恢复；确认旧清理逻辑不碰 external 子目录
- 单测 48/48（本地+容器）：门禁矩阵/治理对接/路由/MIDI 消毒/URL 白名单/建表兼容
- 新工具：scripts/test_p8p9.py、verify_mutopia.py、backfill_orphans.py、state_dump.py
