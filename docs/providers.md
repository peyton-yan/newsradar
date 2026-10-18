# 可选服务商（产品化配置）

价格与额度为 2026-10 参考，采购前以官方控制台为准。

## LLM

| 服务商 | Base URL | 默认模型 | 参考价（CNY/1M，输入→输出） | 备注 |
|---|---|---|---:|---|
| DeepSeek | `https://api.deepseek.com` | `deepseek-flash` | 1→4 空闲，2→8 高峰；缓存命中 0.02/0.04 | 默认；支持关 thinking；人民币计价 |
| OpenAI | `https://api.openai.com/v1` | `gpt-6-luna` | 约 0.72→3.6 | 需外币卡；JSON Schema 更强 |
| Moonshot / Kimi | `https://api.moonshot.cn/v1` | `kimi-k3` | 20→100 | 长上下文；缓存写入另计 |
| 智谱 GLM | `https://open.bigmodel.cn/api/paas/v4` | `glm-4.7-flash` | 0（免费档） | 免费兜底；5.3 系列强制思考 |
| 通义千问 | `https://dashscope.aliyuncs.com/compatible-mode/v1` | `qwen3.8-flash` | 0.8→2.4 | 国内延迟好；用 `enable_thinking` |
| OpenRouter | `https://openrouter.ai/api/v1` | `deepseek/deepseek-v4.1-flash-1017` | 上游价 +5.5% | 多模型聚合，适合备用路由 |
| 硅基流动 | `https://api.siliconflow.cn/v1` | `deepseek-ai/DeepSeek-V4.1-Flash` | 1→4（2–8 点） | 国内聚合，部分模型免费 |
| Ollama | `http://127.0.0.1:11434/v1` | `qwen3:8b` | 0（本地） | 仅开发/离线；2GB 板不要跑生产 |

## 新闻源

| 源 | 覆盖 | 免费额度 | 备注 |
|---|---|---|---|
| 天行数据 | 中国财经/综合 | 100 次/日；高级 29 元/月 1 万/日 | 默认主源；财经 API 64 |
| RSS | 任意 | 取决于源 | 官方源优先；自建 RSSHub 更稳 |
| NewsAPI | 全球英文 | 100/日，仅开发测试 | 免费档禁止生产；商业起价高 |
| GNews | 全球多语言 | 100/日，10 篇/次，非商业 | 12 小时延迟；生产需付费 |
| Finnhub | 全球市场/公司 | 60/分钟 | 英文、美股为主 |

## 微信推送

| 渠道 | 免费额度 | 付费 | 能否直达个人微信 |
|---|---|---|---|
| WxPusher | 永久免费 | 无 | 是，需启用微信 ClawBot/APP |
| PushPlus | 实名 200 条/日 | 10 元/月 2,000 条/日 | 是，关注服务号 |
| Server酱 | 5 条/日 | 会员制，价格登录可见 | 是，方糖服务号 |
| 企业微信机器人 | 免费 | 无 | 否，只到企业微信群 |
| 企业微信应用 | 免费 | 无 | 关注微信插件后可到个人微信 |

## 统一原则

- 不在请求里盲目塞 `thinking`：OpenAI/Qwen/OpenRouter/SiliconFlow 不认；代码按 provider 分支。
- 新闻源、推送渠道、模型都通过设置页切换，不写死在代码里。
- 任何渠道失败都不阻塞采集与分析；重试与历史在推送页可见。
