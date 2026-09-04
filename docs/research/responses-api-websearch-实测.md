# Responses API web_search 实测（2026-08-13）

## 原始响应结构（截断）

以下为 `POST {{base_url}}/v1/responses`（model=deepseek-v4-flash，tools=[web_search]，tool_choice=web_search，temperature=0.3，json_format）真实返回体，按脱敏截断规则（dict 前 30 键 / list 前 3 项 / 长字符串截 500 字符）落盘：

```json
{
  "id": "0da6bb3e-dbdb-4524-9e9f-f748c08a632e",
  "object": "response",
  "created_at": 1786598019,
  "status": "completed",
  "background": false,
  "completed_at": 1786598024,
  "content_filters": null,
  "error": null,
  "frequency_penalty": 0.0,
  "incomplete_details": null,
  "instructions": "题目：明天北京最高气温会超过 35°C 吗\n截止时间：2026-08-13T09:00:00\n输出 JSON：{\"outcome\": true/false, \"confidence\": 0.0-1.0, \"citations\": [\"url\"]}",
  "max_output_tokens": 4096,
  "max_tool_calls": null,
  "model": "deepseek-v4-flash",
  "moderation": null,
  "output": [
    {
      "type": "reasoning",
      "id": "1868bf4b-a7ef-495b-a0b5-c2b9d6aa8d8c",
      "status": "completed",
      "content": [
        {
          "type": "reasoning_text",
          "text": "We need answer about whether Beijing max temp tomorrow (2026-08-13) will exceed 35°C. We need forecast for Beijing for August 13, 2026. But we are in a simulated context? Current date unknown. Let's search web.\n\nWe need JSON output. We should search for Beijing weather forecast August 13, 2026. However, actual data may not exist. Let's use tools."
        }
      ],
      "summary": []
    },
    {
      "type": "web_search_call",
      "id": "call_00_DPq3JNORVEbTJGp3WUtv4091",
      "status": "completed",
      "action": {
        "type": "search",
        "queries": [
          "北京 2026年8月13日 最高气温 预报",
          "Beijing weather August 13 2026 forecast",
          "北京天气预报 8月13日"
        ]
      }
    },
    {
      "type": "reasoning",
      "id": "707ce73d-92d8-4336-bcc9-a6be32d27f54",
      "status": "completed",
      "content": [
        {
          "type": "reasoning_text",
          "text": "The forecasts clearly indicate Beijing's max temp on August 13, 2026 is around 29-30°C, well below 35°C. So outcome = false. Confidence high. Let's verify with NMC page."
        }
      ],
      "summary": []
    }
  ],
  "parallel_tool_calls": true,
  "presence_penalty": 0.0,
  "previous_response_id": null,
  "prompt_cache_key": null,
  "prompt_cache_retention": null,
  "reasoning": {
    "effort": null,
    "summary": null
  },
  "safety_identifier": null,
  "service_tier": "default",
  "store": false,
  "temperature": 0.3,
  "text": {
    "format": {
      "type": "json_object"
    },
    "verbosity": null
  },
  "tool_choice": {
    "type": "web_search"
  },
  "tools": [
    {
      "type": "web_search",
      "search_context_size": null,
      "user_location": null
    }
  ],
  "top_logprobs": 0
}
```

## 关键观察

- web_search_call 条目字段：`type/id/status/action` 四键，**无 `results` 字段**。`action.type` 为 `search` 时携带 `queries`（搜索词数组，末项为内部 `ws_call_id=` 标记）；为 `open_page` 时携带 `url`（模型点开的具体页面，URL 尾部拼接 `#ws_call_id=` 片段）。本次响应共 3 个 web_search_call：1 次 search + 2 次 open_page（nmc.cn 官方预报页、bjmy.gov.cn 政府网天气预报页）。
- 结果是否带时间戳/recency：**不带**。搜索结果的正文/标题/发布时间等 per-result 元数据完全不回传——只有模型内部可见，响应体里仅能拿到"搜了什么词"和"点开了哪个 URL"，无任何时间戳或 recency 字段（预测侧 spec 若依赖结果时间戳做时效性校验将无数据可用）。
- 中文官方数据覆盖（北京气温）：**好**。模型点开了中央气象台官方页（nmc.cn/publish/forecast/ABJ/beijing.html，7 天预报含 08/13）与政府网天气预报栏目（bjmy.gov.cn 密云区政府），citations 自报 3 条（nmc.cn / bjmy.gov.cn / news.sina.cn 新京报转引北京市气象台）。最终判定 JSON：`{"outcome": false, "confidence": 0.98, "citations": ["https://www.nmc.cn/publish/forecast/ABJ/beijing.html", "https://www.bjmy.gov.cn/sy/tqyb/202608/t20260813_550051.html", "https://news.sina.cn/2026-08-13/detail-ininctay0182262.d.html?vt=4"]}`。外部交叉验证：中央气象台页预报 8-13 北京最高 29°C、北京市气象台 13 日 9 时发布 30°C（新京报/新浪转引）、房山区气象局 31°C、中国天气网 ~30°C——与 outcome=false（未超 35°C）一致。

## 响应结构关键差异（对 Task 1/2 代码的影响）

1. **最终答案条目类型是 `message`，不是顶层 `output_text`**：`output` 末项为 `{"type": "message", "phase": "final_answer", "role": "assistant", "content": [{"type": "output_text", "text": "…JSON…"}]}`。Task 1 `LLMClient._aresponses` 的截断护栏检查的是顶层 `output_text` item，因此对真实 DeepSeek 响应必然判"无 output_text"，重试 3 次（4096→8192→16384 max_output_tokens）后抛 `LLMError: responses: no output_text item (reasoning truncated?): retrying`。
2. **引用提取路径不成立**：Task 2 `LLMResolver._judge` 从 `web_search_call.results` 取 URL，但真实条目只有 `action`（无 results）→ citations 恒为空 → no_evidence 护栏必然触发。可用的引用来源只有两条：`open_page` action 的 `url`，以及 final message 中模型自报的 citations JSON（本次自报 3 条均为真实存在的页面）。
3. 响应体还含 `reasoning` 条目（DeepSeek 侧推理轨迹，含 `summary` 字段）与 `usage`（本次 input 8988 / cached 4096 / output 485 含 reasoning 189）。

## 试点运行记录（#68，只打印不入库）

- 第 1 次（2026-08-13T13:08:23）：`verdict=null`（约 1.5 分钟，3 次重试均失败）
- 第 2 次（2026-08-13T13:10:15）：`verdict=null`（同上）
- 根因：不是 no_evidence/护栏，而是上述差异 1 → `responses_create` 抛 LLMError → `LLMResolver.resolve` 走 api_error 路径返回 None（storage=None 时日志静默跳过）。两次结果一致。
- 判定质量旁证：绕过代码护栏直接看同题原生响应，模型输出 outcome=false、confidence=0.98、citations 3 条真实 URL；官方交叉验证（中央气象台 29°C / 北京市气象台 30°C / 房山区气象局 31°C）一致支持 false。

## 定价（2026-08-13 核实 api-docs.deepseek.com/zh-cn/quick_start/pricing）

- 定价页**未单列 web_search 独立计费**——搜索按普通 token 计费（搜索注入的上下文计入 input tokens，本次单次 judge 调用 input 8988 含缓存 4096）。
- 模型价（每百万 tokens）：deepseek-v4-flash 输入缓存命中 0.02 元 / 未命中 1 元 / 输出 2 元；deepseek-v4-pro 0.025 / 3 / 6 元。
- 实测单次判定（双采样）约 18–19k input + ~1k output ≈ 0.02 元量级（flash，无缓存时）。
- ⚠️ 定价页提示"计划近期整体上调 DeepSeek API 服务的定价，预计涨幅较大，请合理安排您的使用"。
