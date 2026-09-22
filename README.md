# Research Agent Platform — phase 1

本仓库是独立 Git 仓库。当前只实现身份、迁移、权限、契约模型、双 API 健康检查和内部团队 Key 管理。`/v1/runs`、`/v1/evidence/*`、资料管理及 Worker 尚未注册，不能用于检索或生成报告。

## 环境

Python 3.12、PostgreSQL 16 加 pgvector。依赖版本固定在 `requirements.lock`。

```sh
python3.12 -m venv .venv
.venv/bin/pip install -r requirements.lock
```

环境变量：

| 变量 | 用途 |
| --- | --- |
| `MIGRATION_DATABASE_URL` | 数据库 owner / 迁移账号连接；仅用于迁移、角色配置和测试 |
| `RAG_DATABASE_URL` | `rag_runtime` 连接；RAG 运行及身份只读校验 |
| `AGENT_DATABASE_URL` | `agent_runtime` 连接；Agent 运行及身份只读校验 |
| `IDENTITY_ADMIN_DATABASE_URL` | `identity_admin` 连接；仅 RAG 内部管理 API 写身份数据 |
| `RAG_SERVICE_TOKEN` | Agent→RAG 独立 Bearer 秘密；至少 32 随机字节的 URL 安全编码，仅以后开放的证据路由使用 |

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
.venv/bin/python -m db.bootstrap
```

`db.migrate` 依次运行 `identity`、`rag`、`agent` 三条独立 Alembic 链，各有自己的 `alembic_version`。RAG 链安装 `vector` 扩展。`db.bootstrap` 创建或重置三个运行角色的密码、授权各自业务 schema，并只向 RAG/Agent 账号开放身份表的 `SELECT`；Agent 账号无 RAG schema 使用权，RAG 账号无 Agent schema 使用权。迁移账号须是数据库 owner，运行账号不执行迁移。数据库账号配置应在专用新数据库进行，脚本会收紧 `public` schema 权限。应用启动不会自动迁移。

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

签发响应中的 `key` 是唯一一次返回的团队 Key 明文。同一团队可同时持有多把有效 Key。管理 API 应仅在内部网络提供；此阶段没有文档删除端点。后续资料删除按逻辑删除处理，旧报告对原团队保持可读。

## 启动与验证

在两个终端分别执行：

```sh
.venv/bin/uvicorn rag_service.app:app --host 127.0.0.1 --port 8001
.venv/bin/uvicorn agent_service.app:app --host 127.0.0.1 --port 8002
```

```sh
curl http://127.0.0.1:8001/health
curl http://127.0.0.1:8002/health
```

使用专用测试数据库及上述四个 DSN 执行 `.venv/bin/pytest -q`。测试在数据库中创建随机命名团队与凭据；请勿指向生产库。无数据库变量时仅执行契约与健康检查测试，数据库测试会跳过。当前阶段没有实际证据和 Run API；Agent 的 RAG HTTP 适配器只通过 HTTP/JSON 契约交互，从持久 Run 读取可信团队 ID，供后续 Worker 接入。
