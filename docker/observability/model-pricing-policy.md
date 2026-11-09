# 模型 API 计价政策

核实日期：2026-11-10（Asia/Shanghai）。目录单位为 USD / 百万 Token。
当前内容版本：2026-11-10-peak-fx7-v1。

## 本地 Ollama

用户明确确认本地 Ollama API 免费。仅对实际配置的 bge-m3 embedding
登记 input 零价，显式别名 bge-m3:latest；不设置模型通配零价。
GPU、电费、机器和维护成本不在 API 费用范围内。零价仍要求响应提供合法用量。

## 商业模型

官方资料：

- DeepSeek：https://api-docs.deepseek.com/quick_start/pricing/
- 火山方舟：https://docs.volcengine.com/docs/ark/model-pricing?lang=zh&redirect=1
- Coding Plan：https://docs.volcengine.com/docs/ark/coding-plan-personal-plan-overview?lang=zh

DeepSeek 官方 Flash 当前按峰谷时段分别报价；旧模型名 deepseek-v4-flash
实际由 DeepSeek-V4.1-Flash-1017 服务。火山国内按量 API 使用人民币报价。
ark-code-latest 是在控制台切换实际模型的套餐入口，不能作为固定 Token 单价模型。
用户确认采用固定估算口径：人民币目录峰时标准 API 价 / 7，忽略套餐摊销、
谷时和折扣。不自动切换时段或更新汇率，不表示真实扣款。
Decimal 除法保留 28 位有效数字，目录中存储换算后的固定十进制字符串。

核实的商业报价如下（每百万 Token，依次为非缓存输入、缓存命中输入、输出）：

| 服务与模型 | 币种 | 高峰 | 空闲 |
|---|---|---|---|
| DeepSeek 官方 deepseek-flash（含 deepseek-v4-flash 旧别名） | USD | 0.30 / 0.006 / 1.20 | 0.15 / 0.003 / 0.60 |
| DeepSeek 官方 deepseek-flash（中文人民币价，本目录采用） | CNY | 2.00 / 0.04 / 8.00 | 1.00 / 0.02 / 4.00 |
| 火山常规在线推理 deepseek-v4-1-flash | CNY | 2.00 / 0.04 / 8.00 | 1.00 / 0.02 / 4.00 |

DeepSeek 高峰为周一至周五 UTC 01:00–04:00、06:00–10:00，中国法定节假日除外。
火山高峰为北京时间周一至周五 09:00–12:00、14:00–18:00，按请求完成时间计费。
火山还列出缓存存储 0.017 CNY / 百万 Token / 小时，该项不属于现有 Token 四桶线性费用。
人民币官方来源：https://api-docs.deepseek.com/zh-cn/quick_start/pricing/
本目录采用人民币价换算，因此不直接采用英文页面的美元挂牌价。
缓存存储费不在现有四桶 Token 费用范围内；此目录估算仅覆盖 Token 消耗。

## ark-code-latest

用户明确指定保留 ark-code-latest 展示名，将其标准 API Token 估算单价与
DeepSeek V4.1 Flash 对齐（CNY 2 / 0.04 / 8，按 7 换算为 USD）。
这是用户指定的替代估算价格，不声称该动态别名实际路由至 DeepSeek，
也不声称厂商为 ark-code-latest 发布了固定 Token 单价。
实际请求端点和套餐保持原配置，仅费用估算采用标准 API Token 模式。
本机 ark-code 与 ark-agent 两个 Provider 均显式映射至该条目；
Provider ID 属安装配置，仅保存在被忽略的 docker/.env 中。

目录更新必须发布新 content_version，并同步两仓文件及部署预期 JCS SHA-256。
