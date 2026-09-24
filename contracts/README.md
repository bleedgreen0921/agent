# v1 契约

`v1.py` 固定首版请求、响应与错误码模型；业务语义依据工作区的 `API_CONTRACT_DRAFT.md`。身份管理端点位于 RAG 内部管理面：`POST /v1/admin/teams`、`POST /v1/admin/teams/{team_id}/keys`、`PUT /v1/admin/keys/{key_id}/expiry`、`POST /v1/admin/keys/{key_id}/revoke`。

当前开放 RAG 的资料管理与证据路由，以及 Agent Run 的提交、查询和取消路由。`POST /v1/admin/documents` 接收 multipart 的 `file`、`title`、`visibility`、JSON 数组形式的 `team_ids`；`POST /v1/admin/documents/{id}/versions` 接收文件与可选 `Idempotency-Key`；状态、ACL 更新与逻辑删除路径遵循工作区的 `API_CONTRACT_DRAFT.md`。证据搜索与按 ID 读取只接受独立服务 Bearer 与 `X-Team-Id`、`X-Run-Id`、`X-Tool-Call-Id`。团队 Key 使用 `POST /v1/runs`、`GET /v1/runs/{run_id}` 和 `POST /v1/runs/{run_id}/cancel`；管理 Key 可使用 `/v1/admin/runs/{run_id}/trace` 与 `/v1/admin/runs/{run_id}/timeline`。Timeline 公共模型使用 `extra="forbid"`，且不包含 task、answer、原始 prompt、工具原始参数或证据正文。

错误形状统一为 `{"error":{"code":"...","message":"..."},"request_id":"..."}`。未归类服务端异常返回 `500 INTERNAL_ERROR` 且不暴露异常正文。身份验证失败统一为 `401 UNAUTHENTICATED`，避免泄漏 Key 是否存在或已过期。搜索 `status=no_hits` 时可同时有 `degradations`，表示另一召回路故障，不能解读为完整检索后的零命中。检索只返回当前 active 版本；按 ID 读取旧版本仍须满足当前文档 ACL 且文档未逻辑删除。
