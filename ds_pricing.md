---
created: 2026-11-02T22:06:07 (UTC +08:00)
tags: [pricing]
source: https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
author: 
---

# 模型 & 价格 | DeepSeek API Docs

> ## Excerpt
> 下表所列模型价格以“百万 tokens”为单位。Token 是模型用来表示自然语言文本的的最小单位，可以是一个词、一个数字或一个标点符号等。我们将根据模型输入和输出的总 token 数进行计量计费。

---
## 模型 & 价格

下表所列模型价格以“百万 tokens”为单位。Token 是模型用来表示自然语言文本的的最小单位，可以是一个词、一个数字或一个标点符号等。我们将根据模型输入和输出的总 token 数进行计量计费。

___

## 模型细节[](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/#%E6%A8%A1%E5%9E%8B%E7%BB%86%E8%8A%82 "模型细节的直接链接")

**

|模型|deepseek-flash<sup>(1)</sup>|deepseek-v4-pro|
|---|---|---|
|BASE URL (OpenAI 格式)|[https://api.deepseek.com](https://api.deepseek.com/)|
|BASE URL (Anthropic 格式)|[https://api.deepseek.com/anthropic](https://api.deepseek.com/anthropic)|
|模型版本|DeepSeek-V4.1-Flash-1017|DeepSeek-V4-Pro-0813|
|思考模式|支持非思考与思考模式（默认）
切换方式详见[思考模式](https://api-docs.deepseek.com/zh-cn/guides/thinking_mode)|
|上下文长度|1M|
|输出长度|最大 384K|
|功能|[Json Output](https://api-docs.deepseek.com/zh-cn/guides/json_mode)|支持|支持|
|[Tool Calls](https://api-docs.deepseek.com/zh-cn/guides/tool_calls)|支持|支持|
|[Responses API](https://api-docs.deepseek.com/zh-cn/guides/responses_api)|支持|支持|
|[Anthropic API](https://api-docs.deepseek.com/zh-cn/guides/anthropic_api)|支持|支持|
|[对话前缀续写（Beta）](https://api-docs.deepseek.com/zh-cn/guides/chat_prefix_completion)|支持|支持|
|[FIM 补全（Beta）](https://api-docs.deepseek.com/zh-cn/guides/fim_completion)|仅非思考模式支持|仅非思考模式支持|
|[图像理解](https://api-docs.deepseek.com/zh-cn/guides/vision)|支持|不支持|
|价格<sup>(2)</sup>|百万tokens输入
（缓存命中）|空闲时段|0.02元|0.15元|
|高峰时段|0.04元|0.30元|
|百万tokens输入
（缓存未命中）|空闲时段|1元|4.5元|
|高峰时段|2元|9.0元|
|百万tokens输出|空闲时段|4元|13.5元|
|高峰时段|8元|27.0元|
|并发限制<sup>(3)</sup>|2500|500|

**

(1) 模型名请使用 `deepseek-flash`。旧模型名 `deepseek-v4-flash`、`deepseek-v4-flash-vision-exp` 仍可调用，但对应模型已下线，请求将由 DeepSeek-V4.1-Flash-1017 模型提供服务，并按 Flash 价格计费。

(2) 空闲时段价格为高峰时段价格的一半。北京时间周一至周五（不含中国法定节假日）9:00 - 12:00、14:00 - 18:00 为高峰时段；其余时段，包括周末及中国法定节假日全天均为空闲时段。

(3) 更多并发限制细节，请参考[限速与隔离](https://api-docs.deepseek.com/zh-cn/quick_start/rate_limit)。

___

## 网页搜索与抓取价格[](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/#%E7%BD%91%E9%A1%B5%E6%90%9C%E7%B4%A2%E4%B8%8E%E6%8A%93%E5%8F%96%E4%BB%B7%E6%A0%BC "网页搜索与抓取价格的直接链接")

Search 和 Fetch 是独立的 API，按请求次数计费。

|API|计费单位|价格|
|---|---|---|
|Search|每 1,000 次成功请求|5元|
|Fetch|每 1,000 次成功请求|5元|

请注意，当前用户在 DeepSeek Harness/Claude Code/Codex 中使用 DeepSeek API 时，已经会通过 Server 端工具调用访问到 Search/Fetch 接口，这部分 Search/Fetch 接口调用目前未计费，并将于北京时间 2026 年 11 月 1 日 00:00 起按次收取调用费用。

接口用法与完整计费规则详见[网页搜索与抓取](https://api-docs.deepseek.com/zh-cn/guides/search)。

___

## 扣费规则[](https://api-docs.deepseek.com/zh-cn/quick_start/pricing/#%E6%89%A3%E8%B4%B9%E8%A7%84%E5%88%99 "扣费规则的直接链接")

模型调用费用 = token 消耗量 × 模型单价；Search 和 Fetch 按上表中的成功请求次数计费。对应的费用将直接从充值余额或赠送余额中进行扣减。 当充值余额与赠送余额同时存在时，优先扣减赠送余额。

产品价格可能发生变动，DeepSeek 保留修改价格的权利。请您依据实际用量按需充值，定期查看此页面以获知最新价格信息。
