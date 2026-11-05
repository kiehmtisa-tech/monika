# TACHIE_STATUS｜立绘「认得」改造（挂盘 + 自我认知 + 知识库）

日期：2026-11-04　关联提交：`05a0f22`（人设立绘认知文本）→ 本次（挂盘 + 知识库）

## 1. 目标

不给 bot 加发图插件，但让它**认得自己的立绘**：用户发来立绘图片说「这是你的立绘」时直接认出接住，不再说「tachie 够不着」，也不再用原作「棕绿发」和橙发立绘抬杠。

## 2. 三件事与落点

| 层 | 改动 | 位置 |
|---|---|---|
| 文件可达（挂盘） | `docker-compose.yml` core 服务新增 `./persona:/MaiMBot/persona:ro`，容器内可见 `/MaiMBot/persona/Tachie/` 四张 `.jfif` | `docker-compose.yml`（本次） |
| 自我认知（人设） | personality 追加立绘段：橙发二创设定 + normal/shine/nanimo/shock 四版情绪 + 识别指令 | `persona/active/monika_v3.json` → `bot_config.toml`（提交 `05a0f22`） |
| 可检索（知识库） | 新增 `monika_persona/03_形象立绘.md`，经 kb2_import 版本化导入为 6 段落 + 全量向量化 | `data/MaiMBot/a-knowledge/monika_persona/03_形象立绘.md`（本次） |

## 3. 知识库导入与验证

```
python3 scripts/kb2/kb2_import.py --doc "monika_persona/03_形象立绘.md"           # dry-run
python3 scripts/kb2/kb2_import.py --doc "monika_persona/03_形象立绘.md" --apply   # 落库 6 段落
POST /api/webui/memory/runtime/vectors/rebuild                                    # 补向量
```

- 检索命中（`/api/webui/memory/records/search`）：`立绘` 8 条、`tachie`/`Tachie` 5 条（全部来自新文档）、`橙发` 3/5、`形象立绘` 4/5、`nanimo` 2/2
- 向量覆盖：`paragraph:<hash>` **6/6** 进 embedding_inputs（209 段落全量向量化，0 failed）
- ⚠️ 注意：kb2 paste 导入的段落**不会自动进向量池**（embedding 工人只处理增量对话内容），导入后要手动调 `/runtime/vectors/rebuild`，否则只剩 BM25 关键词命中、语义检索查不到

## 4. 挂盘验证

- 容器内 `ls /MaiMBot/persona/Tachie` → `nanimo.jfif / normal.jfif / shine.jfif / shock.jfif`（只读挂载）
- 容器重建后插件运行时正常（8 个插件加载），QQ 快速登录重连
- compose 备份：`backups/docker-compose.pre_persona_mount.yml`

## 5. 回滚

```bash
# 挂盘回滚
cp backups/docker-compose.pre_persona_mount.yml docker-compose.yml && docker compose up -d core

# 知识库回滚（版本化删除，留 delete_operations 审计）
python3 scripts/kb2/kb2_import.py --doc "monika_persona/03_形象立绘.md" --delete-only --apply
```

人设文本回滚见 `PERSONA_STATUS.md` 第 6 节。

## 6. 边界

- 依旧**不发图**：没有按情绪自动发送立绘的插件；知识条目里也明确写了「不负责主动发图」。
- 挂载为只读（`:ro`），容器改不了 `persona/`；写入仍走宿主机的 `persona_apply.py`。
- 识别效果依赖人设段（常驻）+ 知识条目（检索兜底）；如需实测，发一张立绘图说「这是你的立绘」即可。

## 7. 图片本体入图像记忆索引（2026-11-04 11:40-12:05）

按「把明确的立绘图片提供给 bot 索引（有需要再索引）」执行。

### 7.1 已完成（零成本部分）

- **图像嵌入模型接通**：`model_config.toml` 注册 `Qwen/Qwen3-VL-Embedding-8B`（name=qwen3-vl-embedding，
  直连 SiliconFlow），绑定 `[model_task_config.image_embedding]`；热重载后 `/images/status` 报
  **ready ·「图片嵌入模型已通过真实图片探测」**（4096 维）。备份 `backups/model_config.pre_vl_embedding.toml`。
- **四张立绘入库**：写 `scripts/inject_tachie.py`，以「图片 + 情绪说明」消息走聊天管线自动 ingest
  （`message_segment=seglist: [image(base64), text]`，说明文本自动成为 `user_statement` 观察）。
  现状：image_assets 5（4 立绘 + 1 旧图）、occurrences 5、观察 4 条（normal/shine/nanimo/shock 各一条，
  原文如「这是你的立绘（tachie）：nanimo 无奈摊手版…」）。
- 入库即建向量任务（image_embedding_jobs 5 个）。

### 7.2 被卡住：硅基流动账户余额不足

`Qwen3-VL-Embedding-8B` 是**付费**模型，直连账户（sk-nin…）**余额为零**（probe + 1 张把余量用完后报
`账号余额不足`）。排查确认：

- 预算网关绕不开：网关与直连是**同一个账户**（gateway_secrets 的 provider_keys 就是从 model_config 提取的，
  bge-m3 免费所以文本嵌入能跑）；且 MaiBot 的 `validate_image_embedding_transport` 强制图片字节只走
  HTTPS 或本机回环，网关 `http://budget-gateway:8009` 直接被拒。
- **认图读路径同样被卡**：图像记忆搜索（含精确哈希匹配）内部先跑嵌入探测，探测失败整个搜索报错跳过
  （日志「图片记忆自然拉起失败，已跳过: 账号余额不足」，WebUI search 端点 502）。

### 7.3 充值后收尾（两条命令）

```
POST /api/webui/memory/image-jobs/retry     # 失败任务重排（现 4 个 failed）
POST /api/webui/memory/images/reindex       # 立即处理向量任务
```

预计成本：5 张图 × 每张约 4096 token 计量，按厘计，几元人民币可用很久（且「有需要再索引」用量极低）。
若不想充值 SiliconFlow，也可提供其他有额度的 key（百炼/豆包原生图片嵌入协议 MaiBot 均已内置支持）。

### 7.4 相关脚本

| 文件 | 用途 |
|---|---|
| `scripts/inject_tachie.py` | 四张立绘「图+说明」注入（seglist 协议） |
| `scripts/inject_tachie_ask.py` | 认图 E2E（重发 normal 并提问） |
| `scripts/register_vl_embedding.py` | 图像嵌入模型注册与任务绑定 |
