# 供应商限流诊断记录（2026-10-20）

本步补齐未来失败请求的诊断证据：启用 `RECIPE_USAGE_LEDGER` 时，urllib 与 LangChain/httpx 两条请求链路会在原有用量账本中增加 `provider_diagnostics`。既有评测脚本已经启用该账本；普通服务需要显式设置账本路径。本步不调用真实供应商制造限流，不调整账号配额、生产模型或历史评测结论。

## 原因与实现

原账本只有 HTTP 状态码，无法从429判断是请求频率、Token配额还是其他供应商限制。新字段保留可识别的错误码和受限响应头，为后续与供应商控制台核对提供证据。错误码本身不等于根因，仍需按供应商含义解释。

- 错误码读取顶层 `code` 或 `error.code`，只接受最多12位非负整数/数字字符串，以及明确列出的限流标识，例如 `rate_limit_exceeded`、`insufficient_quota`。其他字符串省略。
- 响应头只接受 `Retry-After` 和 `x-ratelimit-{limit,remaining,reset}-{requests,tokens}`，只记录受限数字、时间单位或规范化HTTP日期。授权头、Cookie、请求ID和其他任意头均不写入。
- 错误响应体最多解析8192字节；超长、非JSON、过深嵌套、缺失或不可读的响应体跳过。消息、正文、提示词和密钥不写入诊断字段。
- urllib在启用账本时有限读取并关闭错误响应流；抛出的原异常及其状态码、响应头保留。未启用账本时不读取错误流。httpx复用原链路已读取的响应内容。
- 每次实际HTTP尝试单独记账；诊断不会增加探测请求，不改动重试、共享冷却和熔断策略。成功请求仍按原有用量字段记录。

代码：`scripts/usage_ledger.py`、`scripts/service_runtime.py`。测试：`tests/test_provider_diagnostics.py`。

## 验证与边界

修复前专项测试已复现缺少诊断字段。新增7项测试覆盖两条请求链路、敏感字段过滤、日期响应头、过长/过深/不可读响应体及关闭账本；本地真实HTTP注入429后重试200，确认仅发生两次请求且分别记账。

完整回归数量、最终代码哈希和日志见 `data/processed/prd-acceptance/provider-diagnostics-20261020/summary.json`。没有请求真实供应商，不能据此确认其当前容量或历史429原因；没有该字段也不表示供应商没有限流。账号RPM/TPM与成本上限仍为“未定”，完整PRD仍按封存报告判定为部分通过。
