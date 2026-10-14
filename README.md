# 个人科研 Agent

一个本机个人实例，包含 Agent、会话记忆、RAG 文档检索和实验分析。Agent 支持 `react` 与 `plan_execute` 两种模式，使用 PostgreSQL 持久化预算、调用轨迹、checkpoint、输入快照和引用。Worker 使用 asyncio 调度 Run、Memory 和文档任务；进程仅用于共享 CPU 执行器。

业务接口免认证，仅接受本机实际连接、loopback Host 和（存在时）loopback Origin。Agent → RAG 使用独立 `RAG_SERVICE_TOKEN`。实例不保存团队、用户、业务 Key 或 ACL。

## 安装与新库初始化

需要 Python 3.12、PostgreSQL 16 和 pgvector。CPU 依赖锁包含解析器和 LangChain 运行时：

```bash
python3 -m venv .venv
.venv/bin/pip install --extra-index-url https://download.pytorch.org/whl/cpu -r requirements.lock
```

停止旧 API 与 Worker，确认子进程退出，并单独归档旧数据库和原资料目录。创建一个新的空数据库及资料目录，配置以下环境变量。运行角色会由 bootstrap 创建；它们与迁移 owner 使用同一个新数据库。

```bash
export MIGRATION_DATABASE_URL='postgresql://owner:OWNER_PASSWORD@127.0.0.1/research_personal'
export AGENT_DATABASE_URL='postgresql://agent_runtime:AGENT_PASSWORD@127.0.0.1/research_personal'
export RAG_DATABASE_URL='postgresql://rag_runtime:RAG_PASSWORD@127.0.0.1/research_personal'
export RAG_FILES_DIR="$PWD/.data/personal-rag"
export RAG_SERVICE_TOKEN="$(openssl rand -base64 48)"
.venv/bin/python -m db.migrate
.venv/bin/python -m agent_service.checkpoints
.venv/bin/python -m db.bootstrap
.venv/bin/python -m db.doctor --json
```

初始化顺序为 `execution → rag → agent`，对应 `execution_0001`、`rag_personal_0001`、`agent_personal_0002`。已初始化的个人库可以重复执行。检测到旧身份表、旧字段、旧 revision 或未知非空库时，在修改前拒绝。没有历史业务迁移或自动 downgrade。回退必须使用旧代码及原旧库、旧资料目录。

## 启动

配置可兼容 Chat Completions 的模型和 Embedding 服务：

```bash
export AGENT_MODEL_URL='http://127.0.0.1:9000'
export AGENT_MODEL='your-model-name'
export RAG_EMBEDDING_URL='http://127.0.0.1:9000'
export RAG_BASE_URL='http://127.0.0.1:8001'
# 按需配置 AGENT_MODEL_KEY、RAG_EMBEDDING_KEY、AGENT_EMBEDDING_URL。
# 离线运行时，RAG_TOKENIZER_PATH 指向本机已保存的 tokenizer 目录。
.venv/bin/python -m db.config_check --role agent-worker
.venv/bin/python -m db.config_check --role rag-worker
```

分别在四个终端启动：

```bash
.venv/bin/python -m scripts.serve --service rag
.venv/bin/python -m rag_service.worker
.venv/bin/python -m scripts.serve --service agent
.venv/bin/python -m agent_service.worker
```

支持的 API 启动入口固定绑定 `127.0.0.1` 并关闭 forwarded headers。不要通过反向代理扩大业务访问范围。两个服务提供 `/health`、`/health/ready` 和 `/v1/workers`。

```bash
curl -X POST http://127.0.0.1:8002/v1/runs \
  -H 'Content-Type: application/json' -H 'Idempotency-Key: example-1' \
  -d '{"task":"查找记录保留期限并给出引用","mode":"react"}'
curl -X POST http://127.0.0.1:8001/v1/documents \
  -F 'title=记录规范' -F 'file=@policy.txt'
curl -X POST http://127.0.0.1:8002/v1/conversations
```

接口细节见 [个人 API 契约](contracts/README.md)。原管理路径已删除，没有兼容别名。

## 持久执行与工具

`execution.tasks` 是 Agent、Memory、文档任务执行状态的唯一来源。业务表只保存输入和产物关联；只读视图提供已有业务状态名称。运行角色按任务类型使用 RLS，并隔离各自业务 schema。

首次领取固定总期限：Agent 排队 60 秒、执行 300 秒；Memory 执行 900 秒、最多三次；文档执行 14400 秒、最多三次、重试延迟 10 秒。调用与等待都计入期限，调用超时取配置与剩余时间较小者。过期租约不能续活，失权执行者不能预留预算、写 checkpoint 或发布产物。详情见 [执行契约](docs/execution-contract.md)。

工具接收只读执行上下文，返回 content 与可序列化的 `ToolOutcome` artifact。执行层负责预留、结算、冲突检查和合并。Provider 必须声明执行契约 v1；旧自定义 Provider 会在启动时拒绝。扩展说明见 [工具 Provider](docs/tool-providers.md)。

实验分析可单独启用：

```bash
.venv/bin/python -m scripts.synthetic_experiments --output .data/amc_fixture
export AGENT_EXPERIMENT_ROOT="$PWD/.data/amc_fixture"
export AGENT_TOOL_PROVIDERS='agent_service.tools.experiments:tools'
```

输入服务捕获每 Run 的不可变实验快照，Provider 只分析稳定输入。科研项目 ID、实验 ID 和文件哈希仍保留。参见 [实验工具](docs/experiment-tools.md)。

## 验证和诊断

```bash
.venv/bin/python -m scripts.postgres_integration --suite full
.venv/bin/python -m scripts.postgres_integration --suite experiments
.venv/bin/python -m db.doctor --json
.venv/bin/python -m db.trace_run RUN_UUID --include-candidates
```

集成入口创建独立 Docker PostgreSQL/pgvector，覆盖生产配置，初始化新基线和角色，禁用真实 HTTP，使用脚本化模型和 Mock/ASGI Transport。报告必须零失败、零错误、零跳过，测试结束清理专用容器。`--keep-db` 仅保留该测试容器；凭据只写入权限为 0600 的 `.data` 文件。数据库测试清理仅在该入口设置的 `PERSONAL_TEST_DATABASE=1` 下执行。

更多说明：[架构](docs/architecture.md)、[数据库验收](docs/postgres-integration.md)、[合成演示](docs/synthetic-demo.md)、[验证记录](docs/validation-status.md)。

第三阶段已将完整执行链接入异步持久存储和资源层，并以真实图记录与调用回执保护恢复。资源容量和 CPU 平台约束见 [异步资源](docs/async-resources.md)，增量升级、调用级恢复和退出说明见 [异步执行层](docs/async-execution.md)。升级第二阶段个人库需停止旧 Worker、备份数据库并执行增量迁移；禁止旧、新 Worker 混跑。

工具并发、共享下游额度、接纳容量、公平认领及无进展中断见 [流量与进展控制](docs/flow-control.md)。目标验收、证据支持、工具意图和副作用授权见 [验收说明](docs/acceptance.md)。

输入预检、token 预算、按需证据加载、私有归档及回收见 [上下文与保留协议](docs/context-and-retention.md)。当前迁移 head 为 `execution_0004`／`rag_personal_0002`／`agent_personal_0006`，图 v5、Manifest schema 6。升级需停止旧执行链并备份；维护角色和回收服务单独配置，不会在启动 API 时自动启用。
