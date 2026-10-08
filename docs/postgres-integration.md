# 本机 Docker 数据库集成测试

使用 `pgvector/pgvector:pg16` 创建专用 PostgreSQL 16 实例，初始化 identity、rag、agent 的完整迁移链及 LangGraph checkpoint，按现有隔离矩阵配置运行角色，再运行合成数据测试。默认绑定 `127.0.0.1:55432`。

2026-10-08 已完成租约竞态与失败汇总修复后的本机全量验收：299 项通过，0 失败、0 错误、0 跳过；七个核心数据库场景与 doctor 的 15 项检查均通过，容器和网络已清理。版本、原始报告标识及验证边界见[验收记录](validation-status.md)。这是科研平台在合成资料与脚本化模型下的工程验证。

在仓库根目录、能够访问 Docker daemon 的本机终端执行：

```sh
.venv/bin/python -m scripts.postgres_integration
```

默认运行三个实验数据库用例：ReAct 与 Plan-and-Execute 的完整执行与引用发布，以及快照并发、原文件变化后复用、租约和团队校验。实验由固定生成器临时生成，模型由 `FakeMessagesListChatModel` 和固定结构化响应替代，不启动模型服务。

运行科研专项（原实验数据库场景、绘图计算与文件发布、联动和异常场景），再运行整个仓库测试集：

```sh
.venv/bin/python -m scripts.postgres_integration --suite research
.venv/bin/python -m scripts.postgres_integration --suite full
```

联动验收实际上传合成 Markdown、运行 RAG Worker、经进程内 HTTP transport 调用实际 RAG API 和检索 SQL，然后从 checkpoint 恢复两类证据与图片元数据并持久发布。仅 tokenizer、Embedding、改写、精排与 Agent 模型使用脚本化替身或 Mock，预期答案不进入检索或计算。[详细链路与人工核对](experiment-plots.md)。

测试入口额外阻止 HTTPX 的真实同步/异步 HTTP transport，只允许 MockTransport 和 TestClient 的内存 ASGI transport；任何真实 HTTP 请求尝试均在发出前阻止，并让测试命令返回失败，即使业务代码捕获了异常。数据库连接仍使用真实 PostgreSQL。继承的数据库 DSN、工具配置和模型 URL 会在子进程环境中覆盖，OpenAI/Agent 模型密钥会移除；模型地址指向本机未使用的端口，RAG 改写与精排关闭。不会读取仓库 `.env`，不会修改用户已有数据库配置。

每次运行生成唯一 Compose project，创建新的临时数据库。数据库文件位于容器 tmpfs，默认在成功或失败后移除本次容器；不操作已有项目的容器或数据。默认 `experiments` 套件要求三个原实验数据库场景全部通过；`research` 与 `full` 另要求两个绘图、两个联动场景在 ReAct/Plan-and-Execute 中实际通过，总计七个核心场景。所有套件都要求所选测试集没有失败、错误或跳过。日志保存在：

```text
.data/postgres-integration/rap-test-<随机 ID>/
├── docker-preflight.log
├── docker-start.log
├── migrations.log
├── checkpoints.log
├── roles.log
├── doctor.log
├── pytest.log
├── pytest.xml
├── docker-cleanup.log
└── summary.json
```

`summary.json` 记录本次是否实际启动容器、测试结果、doctor 检查、绘图依赖版本、七个核心场景计数、数据库清理状态及耗时。pytest 失败或超时后，即使 JUnit XML 截断或无法读取，仍会保存汇总；可选 `junit_error` 字符串记录异常类型和原因，不覆盖原始 `error` 或 `cleanup_error`。无法取得测试统计时不填造零值；已验证的统计保留。最终汇总读取失败也会返回非零。Docker 预检查失败时返回非零，不运行迁移或测试，不会把数据库测试跳过当作成功。

## 保留与检查数据库

端口被占用时可指定另一端口；需要检查数据可保留本次实例：

```sh
.venv/bin/python -m scripts.postgres_integration --port 55433 --keep-db
```

脚本输出唯一 project 名称和日志目录。仅用于本次容器的 owner 密码保存在该目录的 `docker.env`，权限为 `0600`；目录已被 Git 忽略。用实际 project 和目录替换以下占位符：

```sh
docker compose --env-file .data/postgres-integration/PROJECT/docker.env \
  -f compose.integration.yaml -p PROJECT exec postgres \
  psql -U postgres -d research_agent_integration
```

可检查两个模式的最终结果和实验快照：

```sql
SELECT id, mode, status, tool_calls_used FROM agent.agent_runs ORDER BY created_at;
SELECT run_id, schema_version, captured_at FROM agent.run_experiment_snapshots;
SELECT evidence_id, source_locator->>'kind' AS kind FROM agent.evidence_snapshots;
```

检查完移除本次临时数据库：

```sh
docker compose --env-file .data/postgres-integration/PROJECT/docker.env \
  -f compose.integration.yaml -p PROJECT down
```

## 执行环境限制

需要 Python 3.12 的已安装仓库环境、Docker daemon 访问权限及 Docker Compose 的 `up --wait`。镜像首次使用需要能拉取，已有镜像可复用。这里只启动 PostgreSQL，不安装或调用真实语言模型、Embedding、改写或精排模型。

如果 `docker info` 报 `/var/run/docker.sock` 权限拒绝，启动脚本会立即停止并保存原因。受限执行会话可能无权访问该 socket，需要在有 Docker 访问权限的本机终端执行同一命令。预检查失败只表示该次尝试未执行数据库测试，不改变上文已核验的成功记录。
