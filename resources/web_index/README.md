# Monika 文学与钢琴按需检索网站索引

- `monika_piano_poetry_sites.json` — v1.0（2026-11-04），31 个检索入口，schema_version 1.0。
- 用途：Monika 需要文学/钢琴/古诗词资料时的**本地路由参考**；不是知识库、不含网页内容。
- 结构：`routing`（意图→类别）、`retrieval_policy`（预算/过滤/版权/入库纪律）、`sources[]`
  （id/category/name/url/languages/priority/authority/access/use_cases/query_hints/rights/notes/verification）。
- 接入说明（MaiBot 适配版）：`docs/kb2/WEB_INDEX.md`。
- 查询脚本：`scripts/kb2/web_index_query.py`（topic→优先站点，零依赖）。
- 边界：不授权自动爬取/登录/下载/再发布；免费浏览 ≠ 可复制；外部内容入库需人工审核。
