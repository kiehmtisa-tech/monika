# KB2_STATUS — Monika 知识库 v2 状态卡

> 更新：2026-11-04 09:45 ｜ tag `kb2-v1.0` ｜ 文档：`docs/kb2/`

## 当前状态：P0 阶段全部完成 ✅

| 阶段 | 内容 | 状态 |
|---|---|---|
| Phase 0 | 只读核查（diff / 导入链路 / IP 边界） | ✅ |
| Phase 1 | 8 篇原作事实修订 + 版本化导入 | ✅ P0 修正 5 处全落地 |
| Phase 2 | P0 批 10 篇知识扩充 | ✅ 23 事件卡 + 索引卡，55 段落 |
| 网站索引 | 31 源 JSON + MaiBot 适配 + 路由脚本 + 知识卡 | ✅ |
| Phase 4 | qa_cases 36 项验收 | ✅ 36/36 + 6 项关键全过 |
| 落盘 | 文档/diff/日志入库 + git tag | ✅ |

## 知识库规模

- 源文件 19 篇：ddlc_canon 11（Act1-4 事件、人物关系、Act3 主题、诗歌、Plus 索引、
  原 3 篇修订）/ literature_music 5（诗歌形式、乐理、写诗、钢琴练习、网站索引）/
  monika_persona 2 / content_policy 1
- A-Memorix web_import 段落 ~106 条（8 修订 51 + 10 新增 49 + 网站卡 6）
- 全部条目带 fact_kind（CANON_FACT/CHARACTER_CLAIM/INTERPRETATION/…）与时序标签

## 关键约束（改动前必读）

1. **导入必须走 WebUI paste**（`scripts/kb2/kb2_import.py`），独立进程直写会"假成功"
2. **无结构化 collection 过滤**——目录命名≠物理隔离，检索可能跨 collection
3. **IP 指引**：不复制官方对白/图片/音乐；只用原创转述 + 来源链接
4. **版本化替换**：改文档先 delete-by-source 再导入（kb2_import.py 已封装）
5. 改后必跑 `kb2_qa.py` 回归

## 未完成（P1/P2 候选）

- Plus 单篇详情卡 ×7、Act3 八组观点卡 ×8、作品单篇卡 ×3（规划包 02 有目录与模板）
- 网站索引联网检索接线（当前只有本地路由层；Planner 无联网工具）
- 模型层回答验证（真实流量观察）；多词查询 AND 语义调优

## 回滚

`backups/kb2_pre_20261104_0915/` + git tag `kb2-v1.0`；详见 `docs/kb2/WORKLOG.md` 第 7 节。
