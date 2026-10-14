# 个人 API v1

请求、响应和错误模型见 [v1.py](v1.py)。业务接口无需 Key，仅允许 loopback 实际来源、Host 和 Origin。不存在身份管理、ACL 或旧管理路径。

| 功能 | 接口 |
| --- | --- |
| Run 提交与分页列表 | `POST/GET /v1/runs` |
| 队列概览 | `GET /v1/runs/summary` |
| Run 状态、结果、取消 | `GET /v1/runs/{id}`、`POST /v1/runs/{id}/cancel` |
| Run 轨迹与时间线 | `GET /v1/runs/{id}/trace`、`timeline` |
| 会话创建、列表、读取 | `/v1/conversations` 及 `/{id}` |
| 提交、读取、取消轮次 | `/v1/conversations/{id}/turns` 及 `/{turn_id}`、`/{turn_id}/cancel` |
| 个人历史事实 | `GET /v1/memories` |
| 文档上传、逻辑删除 | `POST /v1/documents`、`DELETE /v1/documents/{id}` |
| 上传版本、读取状态 | `POST /v1/documents/{id}/versions`、`GET /v1/documents/{id}/versions/{version_id}` |
| 两个服务各自的 Worker | `GET /v1/workers` |
| 服务证据调用 | `POST /v1/evidence/search`、`GET /v1/evidence/{id}` |
| 健康与就绪 | `/health`、`/health/ready` |

Run 的 `Idempotency-Key` 在实例内唯一；轮次必须提供该头，作用域为会话；文档创建与新增版本按操作分别去重。同键输入冲突返回 409。Run 列表支持状态、带时区的创建时间、游标，默认 30、最多 100 项。游标只携带位置及业务过滤条件。

文档上传接收文件、标题和可选幂等键；新增版本只接收文件和可选幂等键。检索只读未删除文档的 active 版本；按证据 ID 读取仍可返回其历史版本。逻辑删除后不能再次检索或读取原证据，已发布的引用快照仍可读取。

证据接口要求 `Authorization: Bearer RAG_SERVICE_TOKEN`、`X-Run-Id`、`X-Tool-Call-Id`。它们关联持久调用与检索审计。错误 Token 返回 401，缺少关联头返回 422。

业务状态映射见 [执行契约](../docs/execution-contract.md)。Run 取消竞争遵循已完成终态、旧 token 拒绝、已提交取消、总期限、普通失败的顺序。


第三阶段：Worker 状态字段使用 `memory_consumer_alive`；时间线调用 details 增加 `checkpoint_receipts`，Run 创建事件的 details 增加 `recovery_protocol`。Agent→RAG 可传 `X-Request-Budget-Seconds` 缩短搜索期限；必须保持服务认证和 Run／调用关联头，预算头不构成执行权。业务路径和状态映射保持不变。
