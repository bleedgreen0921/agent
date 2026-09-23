# Research Agent Platform

本仓库是独立 Git 仓库。Agent 是任务规划、工具选择和结果生成的执行主体，支持 ReAct 和固定 Plan-and-Execute 两种模式。RAG 作为当前默认工具提供者独立处理文档与证据检索，只返回证据；服务端工具注册表可以继续接入非 RAG 工具。身份、RAG 和 Agent 数据分别位于三个 schema。

## 环境

Python 3.12、PostgreSQL 16 加 pgvector。依赖版本固定在 `requirements.lock`。Docling 使用 CPU 版 PyTorch；安装时加入 PyTorch CPU wheel 索引：

```sh
python3.12 -m venv .venv
.venv/bin/pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.lock
```

环境变量：

| 变量 | 用途 |
| --- | --- |
| `MIGRATION_DATABASE_URL` | 数据库 owner / 迁移账号连接；仅用于迁移、角色配置和测试 |
| `RAG_DATABASE_URL` | `rag_runtime` 连接；RAG 运行及身份只读校验 |
| `AGENT_DATABASE_URL` | `agent_runtime` 连接；Agent 运行及身份只读校验 |
| `IDENTITY_ADMIN_DATABASE_URL` | `identity_admin` 连接；仅 RAG 内部管理 API 写身份数据 |
| `RAG_SERVICE_TOKEN` | Agent→RAG 证据路由的独立 Bearer 秘密；至少 32 随机字节的 URL 安全编码 |
| `RAG_FILES_DIR` | API 与文档 Worker 共享的本地持久文件目录，默认 `.data/rag`；只能部署在同一主机 |
| `RAG_TOKENIZER_PATH` | Qwen3-Embedding-0.6B tokenizer 的本地目录或 Hugging Face 模型标识；默认后者，首次可下载 tokenizer |
| `RAG_EMBEDDING_URL` / `RAG_EMBEDDING_KEY` | OpenAI 兼容 Embedding 服务根地址与可选 Bearer Key；不配置时文档处理失败 |
| `RAG_REWRITE_URL` / `RAG_REWRITE_MODEL` / `RAG_REWRITE_KEY` | OpenAI 兼容 Chat Completions 改写端点；缺失或临时失败时用原 query 并记录降级 |
| `RAG_RERANK_URL` / `RAG_RERANK_MODEL` / `RAG_RERANK_KEY` | 独立 `/rerank` 服务，默认模型名 `bge-reranker-v2-m3`；临时失败时保留 RRF 排序 |
| `RAG_MODEL_TIMEOUT_SECONDS` | 模型 HTTP 单次超时，默认 30 秒 |
| `RAG_BASE_URL` | Agent Worker 使用的 RAG API 根地址 |
| `AGENT_MODEL_URL` / `AGENT_MODEL` / `AGENT_MODEL_KEY` | OpenAI 兼容 Chat Completions 服务根地址、模型名和 Key；根地址可含或不含 `/v1` |
| `AGENT_QUEUE_TIMEOUT_SECONDS` | Run 排队期限，默认 60 秒 |
| `AGENT_EXECUTION_TIMEOUT_SECONDS` | 首次认领后的总墙钟期限，默认 300 秒；模型与工具等待均计入，单次外部调用不另设超时 |
| `AGENT_MAX_CONCURRENT_RUNS` | 所有父 Worker 共用的数据库全局并发上限，默认 2 |
| `AGENT_LEASE_SECONDS` / `AGENT_HEARTBEAT_SECONDS` | 执行租约与父 Worker 心跳周期，默认 120/30 秒 |
| `AGENT_TOOL_PROVIDERS` | Worker 启用的服务端工具提供者，逗号分隔的 `module:factory`；默认只加载 RAG 证据工具 |

DSN 例：`postgresql://role:password@127.0.0.1:5432/research_agent`。各运行角色需不同密码，且不能使用 owner DSN。把秘密放在进程环境或受控秘密管理系统中，不提交 `.env`。生成服务秘密可用：

```sh
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## 准备数据库与迁移

手工启动测试 PostgreSQL（没有 Compose 文件）：

```sh
docker run -d --name research-agent-test-pg -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=research_agent -p 127.0.0.1:5432:5432 pgvector/pgvector:pg16
```

设置上述四个 DSN 后，按顺序运行：

```sh
.venv/bin/python -m db.migrate
.venv/bin/python -m agent_service.checkpoints
.venv/bin/python -m db.bootstrap
```

`db.migrate` 依次运行 `identity`、`rag`、`agent` 三条独立 Alembic 链，各有自己的 `alembic_version`。RAG 链安装 `vector` 扩展。`agent_service.checkpoints` 由数据库 owner 显式建立 LangGraph 恢复表；API 和 Worker 都不会自动建表或迁移。`db.bootstrap` 创建或重置三个运行角色的密码、授权各自业务 schema，并只向 RAG/Agent 账号开放身份表的 `SELECT`；Agent 账号无 RAG schema 使用权，RAG 账号无 Agent schema 使用权。RAG 账号还需对放置 pgvector 的 `public` schema 有 `USAGE`，用于解析向量类型。数据库账号配置应在专用新数据库进行，脚本会收紧 `public` schema 权限。

首次生成管理凭据：

```sh
.venv/bin/python -m identity.bootstrap_admin
```

命令仅在 stdout 显示新 Key 一次，数据库只有摘要。可通过 `--expires-at 2027-01-01T00:00:00+00:00` 设置到期时间。管理 Key 和团队 Key 都用 `Authorization: Bearer key_id.secret`，但类型不可互用。管理调用示例：

```sh
curl -X POST http://127.0.0.1:8001/v1/admin/teams -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' -d '{"name":"team-a"}'
curl -X POST http://127.0.0.1:8001/v1/admin/teams/TEAM_UUID/keys -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' -d '{}'
curl -X PUT http://127.0.0.1:8001/v1/admin/keys/KEY_ID/expiry -H "Authorization: Bearer $ADMIN_KEY" -H 'Content-Type: application/json' -d '{"expires_at":"2027-01-01T00:00:00Z"}'
curl -X POST http://127.0.0.1:8001/v1/admin/keys/KEY_ID/revoke -H "Authorization: Bearer $ADMIN_KEY"
```

签发响应中的 `key` 是唯一一次返回的团队 Key 明文。同一团队可同时持有多把有效 Key。管理 API 应仅在内部网络提供。

## 资料处理与证据 API

使用管理 Key 上传四种支持格式（PDF、DOCX、Markdown、TXT），`team_ids` 是 JSON 数组字符串。上传仅创建待处理版本与任务，文档 Worker 完成解析、512 token/128 token overlap 正文切块、1024 维 Embedding 和全文索引后才发布 active 版本。PDF/DOCX 使用 Docling，Markdown 使用 markdown-it-py，TXT 保留行号。表格作为整表片段，原 HTML 结构在 RAG 内部保存。首次版本处理失败时不可检索；更新失败时旧版继续可用。

```sh
curl -X POST http://127.0.0.1:8001/v1/admin/documents -H "Authorization: Bearer $ADMIN_KEY" -F 'title=Policy' -F 'visibility=restricted' -F 'team_ids=["TEAM_UUID"]' -F 'file=@policy.pdf'
curl http://127.0.0.1:8001/v1/admin/documents/DOCUMENT_UUID/versions/VERSION_UUID -H "Authorization: Bearer $ADMIN_KEY"
```

新版本用 `POST /v1/admin/documents/{id}/versions` 上传，可选 `Idempotency-Key`；同键同内容重放返回已有版本，同键不同内容为 409。`PUT /v1/admin/documents/{id}/access` 更新逻辑文档 ACL；`DELETE /v1/admin/documents/{id}` **仅逻辑删除**，不物理清理原文件、旧片段或 Agent 报告。搜索与按 ID 读取都按当前 ACL 和删除状态过滤；仍获授权的团队可按 ID 读取已被新版本取代的旧片段。

证据端点仅接受独立服务 Bearer，并要求 Agent 从持久 Run 注入的 `X-Team-Id`、`X-Run-Id`、`X-Tool-Call-Id`。`POST /v1/evidence/search` 请求如 `{"query":"policy retention","top_k":5}`；`GET /v1/evidence/{evidence_id}` 只返回单片段。检索执行 dense 50、FTS 50、RRF 前 40、精排后 top_k；查询 SQL 在候选截断前应用 ACL。两路召回均不可用返回 503；单路或精排临时故障在响应 `degradations` 中标出。当前 pgvector 使用精确距离排序，尚无近似向量索引。

另启独立文档 Worker：

```sh
.venv/bin/python -m rag_service.worker
```

Embedding 影子 revision 可在持续上传期间构建。`start` 创建 building revision，Worker 为新发布版本同时准备 active 与 building 两套向量；`build` 补建当前 active 片段，`switch` 在锁内核对覆盖并一次切换全局指针。切换后只写新 revision，旧 revision 不保证可直接回滚。

```sh
.venv/bin/python -m rag_service.reindex start --model Qwen3-Embedding-0.6B --dimensions 1024
.venv/bin/python -m rag_service.reindex build REVISION_UUID
.venv/bin/python -m rag_service.reindex switch REVISION_UUID
```

## Agent Run API 与 Worker

团队 Key 可异步提交 Run。`mode` 为 `react` 或 `plan_execute`；可选 `Idempotency-Key` 在团队内防止重复提交。团队 ID、预算和工具集合都不接受客户端传入。查询 Run 会在终态结果中返回逐条 claim、实际引用的证据快照和通知。

```sh
curl -X POST http://127.0.0.1:8002/v1/runs \
  -H "Authorization: Bearer $TEAM_KEY" \
  -H 'Idempotency-Key: demo-1' \
  -H 'Content-Type: application/json' \
  -d '{"task":"查证资料中的保留期限并说明依据","mode":"plan_execute"}'
curl http://127.0.0.1:8002/v1/runs/RUN_UUID -H "Authorization: Bearer $TEAM_KEY"
curl -X POST http://127.0.0.1:8002/v1/runs/RUN_UUID/cancel -H "Authorization: Bearer $TEAM_KEY"
```

父 Worker 从 PostgreSQL 认领 Run，每个 Run 启动一个独立子进程。所有模型和工具调用在发出前持久预留额度；默认每个 Run 最多 16 次模型调用（保留最后一次用于最终生成）和 10 次工具调用。失租时若存在结果未知的外部调用，Run 以 `INTERRUPTED_UNKNOWN` 失败，避免重放。内部调用 trace 只保存安全摘要；LangGraph checkpoint 可能保存原始消息，首版不自动清理。

```sh
.venv/bin/python -m agent_service.worker
```

RAG 零命中和临时不可用会成为结果通知；身份、ACL 和配额错误会使 Run 失败。发布结果时仅保存最终 claim 实际引用的证据快照。报告发布后，原团队仍可通过 Run ID 读取快照，即使原资料后来更新、撤权或逻辑删除。

Agent 核心通过服务端工具提供者注册表加载工具，并对所有工具统一执行身份上下文注入、预算预留和调用 trace。RAG 是当前默认加载的证据工具提供者；新增工具不需要修改 Agent 图执行器。扩展约定和示例见 [Agent 工具扩展接口](docs/tool-providers.md)。

## 启动与验证

分别启动四个进程：

```sh
.venv/bin/uvicorn rag_service.app:app --host 127.0.0.1 --port 8001
.venv/bin/uvicorn agent_service.app:app --host 127.0.0.1 --port 8002
.venv/bin/python -m rag_service.worker
.venv/bin/python -m agent_service.worker
```

```sh
curl http://127.0.0.1:8001/health
curl http://127.0.0.1:8002/health
```

使用专用测试数据库及上述四个 DSN 执行 `.venv/bin/pytest -q`。测试在数据库中创建随机命名团队、凭据、Run 和合成文档；请勿指向生产库。无数据库变量时数据库测试会跳过。Agent 的 RAG 适配器只通过 HTTP/JSON 契约交互，从持久 Run 读取可信团队 ID。React、Plan-and-Execute、checkpoint、预算和结果发布使用脚本化模型验证；真实模型端点以及真实 Embedding、改写和精排服务的效果尚未验证。数据流见 [架构图](docs/architecture.md)。

可复现的本地 HTTP Mock 全链路步骤见 [合成资料演示](docs/synthetic-demo.md)。该流程已覆盖受限资料上传、Worker 索引、混合检索、两种 Agent 模式、工具调用和引用快照；Mock 固定输出只用于工程验收，不代表真实检索或模型效果。
