# v1 契约

`v1.py` 固定首版请求、响应与错误码模型；业务语义依据工作区的 `API_CONTRACT_DRAFT.md`。身份管理端点位于 RAG 内部管理面：`POST /v1/admin/teams`、`POST /v1/admin/teams/{team_id}/keys`、`PUT /v1/admin/keys/{key_id}/expiry`、`POST /v1/admin/keys/{key_id}/revoke`。

当前只开放这些管理端点与两个 `/health`。Run、证据及资料端点将在相应模块完成后开放；未实现路径返回 404。错误形状统一为 `{"error":{"code":"...","message":"..."},"request_id":"..."}`。身份验证失败统一为 `401 UNAUTHENTICATED`，避免泄漏 Key 是否存在或已过期。
