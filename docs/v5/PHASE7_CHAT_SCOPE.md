# 人物画像 chat 级隔离（收窄 person 级 → chat 级）

> 用户决定（2026-11-04）：把人物画像从 person 级收窄到 chat 级，私聊学到的个人信息不再注入群聊。
> 执行：2026-11-04 上午。回归 23/23 通过。

## 一、动机

复核发现：`get_person_profile(person_id)` 里 `del chat_id`，画像按 person_id 跨聊天流注入。于是私聊中形成的个人偏好、关系设定、近期互动会泄漏进群聊。用户要求收窄成 chat 级。

## 二、实现

**核心机制：claim 级证据溯源过滤。** 每条 person claim 通过 `fact_evidence → paragraph → metadata.chat_id` 溯源到具体聊天。注入时按当前 chat 过滤。

改 MaiBot 核心 4 个文件（镜像层 src/）：

| 文件 | 改动 |
|---|---|
| `A_memorix/core/storage/metadata_fact.py` | `list_person_profile_fact_claims` 增 `chat_id` 参数 + 溯源过滤 SQL |
| `A_memorix/core/utils/person_profile_service.py` | `query_person_profile` 增 `chat_id` 分支 → `_build_chat_scoped_profile`（不走全局缓存/LLM） |
| `A_memorix/core/runtime/services/profile_admin_service.py` | `get_person_profile`/`query` 透传 `chat_id` |
| `maisaka/memory/person_profile.py` | 注入时取 `anchor_message.session_id` 作为 `chat_id` 传入 |

**过滤规则（严格 chat 级）**：claim 的证据段落属于当前 chat 或全局（web_import/global）才可见；否则不可见。姓名/别名走 `get_person_aliases` 始终可用（头里显示），不依赖 claim。

**关键技术点**：
- 不读写全局快照（全局画像含跨聊天私有内容），chat 分支用预分类 claim 现建（无 LLM，廉价）
- 进程边界坑：改 src/ 后**必须重启 core**（Python 不热重载模块），否则 live 用旧代码

## 三、验证（三层）

### 1. 查询层（直接调 query_person_profile）
| 查询 | 泄漏词（喜欢/酒馆战棋/愿意听/不嘲笑/钢琴/专利） |
|---|---|
| 全局（无 chat_id） | 全部 6 个（未过滤） |
| **群聊 chat_id** | **0 个（清空）** |
| 私聊 chat_id | 全部 6 个（保留） |

群聊画像只剩姓名头 + 本群内容。判定 PASS。

### 2. 注入层（live，真实消息）
重启 core 后注入群聊，planner dump 的画像块：
- **群聊：泄漏词 = []**，只剩"身份设定-姓名" + "完成了音乐生成/表情包功能"（本群内容）
- 私聊：保留私聊内容

### 3. 来源核验
群聊里那 2 条（音乐生成/表情包功能）证据段落 `chat=cffa`（本群），非私聊漏的。私聊的"喜欢某表情图"是 temporal，画像查询本就不含。

## 四、回归

轮换验收套件 **23/23 通过**。注：T6-a 一度失败，是仓库副本 `test_rotation_acceptance.py` 用假 chat_id（scoped 导入校验聊天流存在，假 id 被拒），已改真实 chat_id，非本次回归。

## 五、⚠️ 镜像层持久性（重要）

本改动在 MaiBot 核心 `src/`，**容器重建/镜像更新后会丢失**。已把最终文件存到 `docs/v5/chat_scope/src/`，重建后重放：

```bash
bash /opt/qq-fish/docs/v5/chat_scope/apply_chat_scope.sh
```

回滚：`src/orig_backup.tar` 是改前原文件，解回容器即可。

## 六、行为边界（请确认）

- **姓名/别名跨聊天始终显示**（bot 在任何聊天都认得这个人是谁）——私有事实不漏，但身份标识在。
- **严格 chat 级**：某 chat 学到的偏好/关系/近期，只在该 chat 的画像出现。若某事实本群+私聊都聊过（证据含两处），则两边都可见。
- 全局（不带 chat_id）查询仍返回完整画像（管理/诊断用），只是聊天注入走 chat 级。

如需"身份类事实也跨聊天、只有敏感偏好隔离"，可改回 identity_settings 例外（已留改动点）。
