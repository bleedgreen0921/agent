# v1 契约

[`v1.py`](v1.py) 定义当前请求、响应、证据与错误码模型，路由和权限以仓库中的 [RAG API](../rag_service/api.py)、[身份 API](../identity/api.py)、[Agent API](../agent_service/api.py) 和[会话 API](../agent_service/conversations.py) 为准。平台面向个人科研工作流，团队仍是底层授权和隔离边界；定位和功能边界见[项目能力现状](../docs/platform-capability-assessment.md)。

## 身份、任务与会话

身份管理端点位于 RAG 内部管理面：`POST /v1/admin/teams`、`POST /v1/admin/teams/{team_id}/keys`、`PUT /v1/admin/keys/{key_id}/expiry`、`POST /v1/admin/keys/{key_id}/revoke`。

团队 Key 在身份服务使用 `POST /v1/team/users`、`GET /v1/team/users`、`POST /v1/team/users/{user_id}/keys` 和 `POST /v1/team/users/{user_id}/keys/{key_id}/revoke` 管理团队内用户及其独立 Key。用户 Key 在 Agent 服务使用 `/v1/conversations` 创建、列出和读取会话，使用 `/v1/conversations/{conversation_id}/turns` 提交轮次，使用 `/{turn_id}` 读取、`/{turn_id}/cancel` 取消轮次，使用 `GET /v1/memories` 只读查询个人历史陈述。提交轮次必须带 `Idempotency-Key`；同一会话存在活动轮次时返回 `409 CONVERSATION_BUSY`，跨用户访问返回 404。

团队 Key 使用 `POST /v1/runs`、`GET /v1/runs/{run_id}`、`POST /v1/runs/{run_id}/cancel` 和 `GET /v1/runs`。列表只包含所属团队的 Run；管理 Key 使用 `GET /v1/admin/runs` 查询全平台并可按 `team_id` 筛选。两个列表支持状态、创建时间及游标分页，默认 30 项、最多 100 项，返回 `items` 与 `next_cursor`；时间参数必须带时区，下一页须保持筛选条件不变。

管理 Key 还可读取 `GET /v1/admin/runs/summary`、`/v1/admin/runs/{run_id}/trace` 和 `/v1/admin/runs/{run_id}/timeline`。Timeline 公共模型使用 `extra="forbid"`，且不包含 task、answer、原始 prompt、工具原始参数或证据正文。RAG 与 Agent 各自提供 `/health`、`/health/ready`，以及管理 Key 可访问的 `GET /v1/admin/workers`；readiness 不调用模型，Worker 心跳也不表示模型服务可用。

## 文档与实验证据

`POST /v1/admin/documents` 接收 multipart 的 `file`、`title`、`visibility`、JSON 数组形式的 `team_ids`；`POST /v1/admin/documents/{id}/versions` 接收文件与可选 `Idempotency-Key`。版本状态查询使用 `GET /v1/admin/documents/{id}/versions/{version_id}`，ACL 更新使用 `PUT /v1/admin/documents/{id}/access`，逻辑删除使用 `DELETE /v1/admin/documents/{id}`。`POST /v1/evidence/search` 与 `GET /v1/evidence/{evidence_id}` 只接受独立服务 Bearer，并要求 `X-Team-Id`、`X-Run-Id`、`X-Tool-Call-Id`。

公共 `Evidence` 按 `source_locator.kind` 区分来源：

| 来源 | 标识与定位 | 约束 |
| --- | --- | --- |
| 文档：`pdf`、`docx`、`markdown`、`txt` | `document_id`、`document_version_id`，页码、标题、块或行号 | 文档和版本 ID 必填 |
| 实验：`experiment` | 项目与实验 ID、文件相对路径与 SHA-256、输入快照 SHA-256、计算方法及版本、合成标志 | 文档和版本 ID 必须为空，使用实验来源身份 |

两类依据共用 Agent 最终 `answer/claims/citations/notices` 结构。claim 只能引用本 Run 实际获得的 evidence ID，发布时保存被引用的证据快照；这保证引用身份和来源可追溯，语义支持性仍需评估。个人记忆作为历史上下文，不属于这两类证据。

实验分析通过 Agent 工具 Provider 执行，当前没有独立实验 CRUD API。首版只接收约定的合成 YAML/CSV/JSON 格式，保存每 Run 输入快照；在线响应为 JSON，Markdown/CSV 导出由离线脚本完成。在线文件产物生成与下载尚待实现。详见[实验工具](../docs/experiment-tools.md)。

## 错误与检索语义

错误形状统一为 `{"error":{"code":"...","message":"..."},"request_id":"..."}`。未归类服务端异常返回 `500 INTERNAL_ERROR` 且不暴露异常正文。身份验证失败统一为 `401 UNAUTHENTICATED`，避免泄漏 Key 是否存在或已过期。搜索 `status=no_hits` 时可同时有 `degradations`，表示另一召回路故障，不能解读为完整检索后的零命中。检索只返回当前 active 版本；按 ID 读取旧版本仍须满足当前文档 ACL 且文档未逻辑删除。
