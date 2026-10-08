# Research Agent Platform

面向个人科研工作流的可扩展 Agent 执行与证据分析平台，以自动调制识别的文献检索和实验对比为首个应用场景。当前是已完成合成数据工程验收的 MVP：Agent 负责规划、工具选择和结果组织，工具负责文档检索、条件核对和确定性计算，结论关联可追溯的文档或实验文件依据。

核心支持持久 Run、ReAct 和固定 Plan-and-Execute，通过服务端 Provider 加载 RAG、实验分析与绘图工具，并统一执行身份校验、预算、trace 和引用发布。RAG 是默认的文档证据 Provider；实验 Provider 已接入，需配置后启用。身份、RAG 和 Agent 数据分别位于三个 schema，实验分析逻辑是共享 Python 模块。

主要使用者是个人研究者。现有团队与用户设计保留为底层授权和隔离机制；个人部署可创建一个团队作为科研空间。自动默认空间、科研项目管理和个人工作台尚未实现。完整定位、能力边界和后续清单见[项目能力现状](docs/platform-capability-assessment.md)。

个人科研实验扩展已有一批[自动调制识别虚拟实验](examples/amc_experiments/README.md)：12 份时间 ID 命名的 YAML 配置，关联 CSV/JSON 结果和 Markdown 记录。[实验分析 Provider](docs/experiment-tools.md) 已提供六个工具，支持配置检索、详情、差异、对照选择、准确率比较和混淆统计，接入统一预算、trace 与文件来源引用；需按说明配置根目录和团队后启用。运行 `.venv/bin/python -m scripts.experiment_fixture_demo` 仍可离线登记、检索、比较并导出 Markdown/CSV，无需数据库或模型。首版工具只支持这批合成格式，指标不代表真实模型效果。

[科研图生成](docs/experiment-plots.md)提供两个独立工具及共享实现的离线命令，输出本地 PNG/SVG、绘图数据和来源清单。`scripts.synthetic_research` 可生成方法、评测口径与矛盾记录，使用 `scripts.postgres_integration --suite research` 验证真实 PostgreSQL/RAG 与实验分析、绘图、恢复和混合引用的完整链路。

2026-10-08 已核验本机 Docker 修复后全量测试：**299 项通过，0 失败、0 错误、0 跳过**，包括三个原实验数据库专项、两个绘图专项和两个资料联动专项；全程使用合成数据与脚本化模型，未调用真实模型。报告、版本和验证范围见[验收记录](docs/validation-status.md)。真实训练日志适配、真实论文与实验联动评估、在线 Markdown/CSV 文件生成与下载仍需补全。

## 合成资料与绘图快速体验

安装下述固定依赖后，在仓库根目录执行。离线生成资料和图片无需数据库、API 或模型：

```sh
.venv/bin/python -m scripts.synthetic_research --output .data/research/materials
.venv/bin/python -m scripts.experiment_plot_demo \
  --root .data/research/materials/experiment_inputs --output .data/research/plots \
  accuracy 20261001_090000 20261001_103000
.venv/bin/python -m scripts.experiment_plot_demo \
  --root .data/research/materials/experiment_inputs --output .data/research/plots \
  confusion 20261001_090000
```

每个 `plots/offline/<plot_id>/` 目录包含 PNG、SVG、绘图数据与来源清单。Agent 使用相同实现，并将产物元数据保存在当前 Run 的实验证据中。图片为本地文件，通过配置的输出根目录查看；使用说明见[科研绘图](docs/experiment-plots.md)。

有 Docker 时，可直接执行真实 RAG 与实验工具的联动验收：

```sh
.venv/bin/python -m scripts.postgres_integration --suite research
.venv/bin/python -m scripts.postgres_integration --suite full
```

科研专项已通过 68 项测试，覆盖真实上传、RAG Worker、检索 SQL、对照计算、绘图、恢复和引用发布；模型组件使用脚本化替身或 Mock，真实 HTTP 请求被阻断。完整流程见[合成资料演示与验收](docs/synthetic-demo.md)。

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
| `AGENT_EXPERIMENT_ROOT` | 可选实验 Provider 的绝对根目录，包含 `experiments/*/config.yaml` |
| `AGENT_EXPERIMENT_PLOT_ROOT` | 可选绘图输出根目录，必须为绝对路径并位于实验输入目录之外；默认仓库 `.data/experiment_plots` |
| `AGENT_EXPERIMENT_TEAM_ID` | 服务端绑定该科研目录所属团队的 UUID；实验 Provider 启用时必填 |
| `IDENTITY_ADMIN_DATABASE_URL` | `identity_admin` 连接；仅 RAG 内部管理 API 写身份数据 |
| `RAG_SERVICE_TOKEN` | Agent→RAG 证据路由的独立 Bearer 秘密；至少 32 随机字节的 URL 安全编码 |
| `RAG_FILES_DIR` | API 与文档 Worker 共享的本地持久文件目录，默认 `.data/rag`；只能部署在同一主机 |
| `RAG_TOKENIZER_PATH` | Qwen3-Embedding-0.6B tokenizer 的本地目录或 Hugging Face 模型标识；默认后者，首次可下载 tokenizer |
| `RAG_EMBEDDING_URL` / `RAG_EMBEDDING_KEY` | OpenAI 兼容 Embedding 服务根地址与可选 Bearer Key；不配置时文档处理失败 |
| `RAG_REWRITE_URL` / `RAG_REWRITE_MODEL` / `RAG_REWRITE_KEY` | OpenAI 兼容 Chat Completions 改写端点；缺失或临时失败时用原 query 并记录降级 |
| `RAG_RERANK_URL` / `RAG_RERANK_MODEL` / `RAG_RERANK_KEY` | 独立 `/rerank` 服务，默认模型名 `bge-reranker-v2-m3`；临时失败时保留 RRF 排序 |
| `RAG_MODEL_TIMEOUT_SECONDS` | 模型 HTTP 单次超时，默认 30 秒 |
| `AGENT_RAG_TIMEOUT_SECONDS` | Agent→RAG HTTP 读、写及连接池等待超时，默认 120 秒；连接等待固定最多 5 秒 |
| `RAG_BASE_URL` | Agent Worker 使用的 RAG API 根地址 |
| `AGENT_MODEL_URL` / `AGENT_MODEL` / `AGENT_MODEL_KEY` | OpenAI 兼容 Chat Completions 服务根地址、模型名和 Key；根地址可含或不含 `/v1` |
| `AGENT_MODEL_TIMEOUT_SECONDS` | Agent 模型请求超时，默认 120 秒；适用于 ReAct、规划和最终生成 |
| `AGENT_EMBEDDING_URL` / `AGENT_EMBEDDING_MODEL` / `AGENT_EMBEDDING_KEY` | Agent 独立调用的个人记忆 Embedding 端点、模型和可选 Key；URL/Key 未设置时沿用 RAG 的端点配置，模型默认 `Qwen3-Embedding-0.6B` |
| `AGENT_MEMORY_EMBED_TIMEOUT_SECONDS` / `AGENT_MEMORY_POLL_SECONDS` | 个人记忆 Embedding 超时及后台任务轮询间隔，默认 15/1 秒 |
| `APP_REVISION` | 可选的部署版本标识；优先写入 Run Execution Manifest，未设置时回退到 Git HEAD，最后使用 `unknown` |
| `AGENT_QUEUE_TIMEOUT_SECONDS` | Run 排队期限，默认 60 秒 |
| `AGENT_EXECUTION_TIMEOUT_SECONDS` | 首次认领后的总墙钟期限，默认 300 秒；模型与工具等待均计入，期限到达后父 Worker 终止子进程 |
| `AGENT_MAX_CONCURRENT_RUNS` | 所有父 Worker 共用的数据库全局并发上限，默认 2 |
| `AGENT_LEASE_SECONDS` / `AGENT_HEARTBEAT_SECONDS` | 执行租约与父 Worker 心跳周期，默认 120/30 秒 |
| `AGENT_TOOL_PROVIDERS` | Worker 启用的服务端工具提供者，逗号分隔的 `module:factory`；默认只加载 RAG 证据工具 |
| `DB_CONNECT_TIMEOUT_SECONDS` | 运行时数据库连接上限，默认 10 秒 |
| `DB_STATEMENT_TIMEOUT_SECONDS` / `DB_LOCK_TIMEOUT_SECONDS` | 普通数据库语句与锁等待上限，默认 120/30 秒 |
| `DB_CONTROL_STATEMENT_TIMEOUT_SECONDS` / `DB_CONTROL_LOCK_TIMEOUT_SECONDS` | Worker 领取、控制、续租与失败记录上限，默认 15/5 秒 |
| `DB_BULK_STATEMENT_TIMEOUT_SECONDS` / `DB_BULK_LOCK_TIMEOUT_SECONDS` | 文档批量发布上限，默认 3600/60 秒；总任务期限仍独立生效 |
| `RAG_DOCUMENT_TIMEOUT_SECONDS` | 文档首次领取后的跨尝试总期限，默认 14400 秒（4 小时）；超时直接失败 |

DSN 例：`postgresql://role:password@127.0.0.1:5432/research_agent`。各运行角色需不同密码，且不能使用 owner DSN。把秘密放在进程环境或受控秘密管理系统中，不提交 `.env`。生成服务秘密可用：

```sh
.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))'
```

## 准备数据库与迁移

一键创建独立测试 PostgreSQL、初始化数据库并执行全量合成测试：

```sh
.venv/bin/python -m scripts.postgres_integration --suite full
```

该入口使用 `compose.integration.yaml`，默认绑定 `127.0.0.1:55432`，结束后清理本次临时数据库。详见[数据库集成测试](docs/postgres-integration.md)。如需手工准备数据库，可按以下方式启动：

```sh
docker run -d --name research-agent-test-pg -e POSTGRES_PASSWORD=change-me -e POSTGRES_DB=research_agent -p 127.0.0.1:5432:5432 pgvector/pgvector:pg16
```

设置上述四个 DSN 后，按顺序运行：

```sh
.venv/bin/python -m db.migrate
.venv/bin/python -m agent_service.checkpoints
.venv/bin/python -m db.bootstrap
.venv/bin/python -m db.doctor
```

`db.migrate` 依次运行 `identity`、`rag`、`agent` 三条独立 Alembic 链，各有自己的 `alembic_version`。RAG 链安装 `vector` 扩展。`agent_service.checkpoints` 由数据库 owner 显式建立 LangGraph 恢复表；API 和 Worker 都不会自动建表或迁移。`db.bootstrap` 创建或重置三个运行角色的密码、授权各自业务 schema，并只向 RAG/Agent 账号开放身份表的 `SELECT`；`agent_runtime` 对 Manifest、轮次原文、摘要、事实、Run 记忆快照和实验输入快照只有 `SELECT`、`INSERT`。Agent 账号无 RAG schema 使用权，RAG 账号无 Agent schema 使用权。RAG 与 Agent 账号对放置 pgvector 的 `public` schema 仅有 `USAGE`，用于解析向量类型。数据库账号配置应在专用新数据库进行，脚本会收紧 `public` schema 权限。

`db.doctor` 是严格只读的部署诊断：检查配置、四个数据库角色的连接、迁移 head、pgvector、checkpoint、权限隔离、索引 revision/Embedding 覆盖、过期租约、调用一致性和资料文件一致性。默认每项输出一行，`--json` 输出稳定的机器可读结构；只有 PASS/WARN 时退出 0，任一 ERROR 时退出 1。它不创建资料目录，也不提供自动修复：

```sh
.venv/bin/python -m db.doctor
.venv/bin/python -m db.doctor --json
```

启动前可分别运行 `.venv/bin/python -m db.config_check --role agent-api`、`agent-worker`、`rag-api`、`rag-worker`。检查只解析本地配置，不连接数据库或模型；各进程启动时也会执行相同检查。配置错误只报告变量名与约束，不输出密钥。文档总期限是当前缺少真实数据集时的宽松防卡死值，后续用代表性资料校准。

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

团队 Key 可在身份服务创建用户并签发独立用户 Key；签发明文仍只返回一次。用户 Key 只用于自己的会话和个人记忆，不能调用团队单 Run 接口。撤销用户 Key 后立即失效：

```sh
curl -X POST http://127.0.0.1:8001/v1/team/users -H "Authorization: Bearer $TEAM_KEY" -H 'Content-Type: application/json' -d '{"name":"alice"}'
curl -X POST http://127.0.0.1:8001/v1/team/users/USER_UUID/keys -H "Authorization: Bearer $TEAM_KEY" -H 'Content-Type: application/json' -d '{}'
curl -X POST http://127.0.0.1:8001/v1/team/users/USER_UUID/keys/KEY_ID/revoke -H "Authorization: Bearer $TEAM_KEY"
```

## 资料处理与证据 API

使用管理 Key 上传四种支持格式（PDF、DOCX、Markdown、TXT），`team_ids` 是 JSON 数组字符串。上传仅创建待处理版本与任务，文档 Worker 完成解析、512 token/128 token overlap 正文切块、1024 维 Embedding 和全文索引后才发布 active 版本。PDF/DOCX 使用 Docling，Markdown 使用 markdown-it-py，TXT 保留行号。表格作为整表片段，原 HTML 结构在 RAG 内部保存。首次版本处理失败时不可检索；更新失败时旧版继续可用。

```sh
curl -X POST http://127.0.0.1:8001/v1/admin/documents -H "Authorization: Bearer $ADMIN_KEY" -F 'title=Policy' -F 'visibility=restricted' -F 'team_ids=["TEAM_UUID"]' -F 'file=@policy.pdf'
curl http://127.0.0.1:8001/v1/admin/documents/DOCUMENT_UUID/versions/VERSION_UUID -H "Authorization: Bearer $ADMIN_KEY"
```

新版本用 `POST /v1/admin/documents/{id}/versions` 上传，可选 `Idempotency-Key`；同键同内容重放返回已有版本，同键不同内容为 409。`PUT /v1/admin/documents/{id}/access` 更新逻辑文档 ACL；`DELETE /v1/admin/documents/{id}` **仅逻辑删除**，不物理清理原文件、旧片段或 Agent 报告。搜索与按 ID 读取都按当前 ACL 和删除状态过滤；仍获授权的团队可按 ID 读取已被新版本取代的旧片段。

证据端点仅接受独立服务 Bearer，并要求 Agent 从持久 Run 注入的 `X-Team-Id`、`X-Run-Id`、`X-Tool-Call-Id`。`POST /v1/evidence/search` 请求如 `{"query":"policy retention","top_k":5}`；`GET /v1/evidence/{evidence_id}` 只返回单片段。检索执行 dense 50、FTS 50、RRF 前 40、精排后 top_k；查询 SQL 在候选截断前应用 ACL。两路召回均不可用返回 503；单路或精排临时故障在响应 `degradations` 中标出。当前 pgvector 使用精确距离排序，尚无近似向量索引。

RAG 检索审计记录 operation、关联 ID、原始/改写 query 的 SHA-256、最终 evidence ID 顺序、降级和稳定错误码。它不保存原始 query、向量、完整候选分数或证据正文；迁移前的审计行仅回填 `operation`，无法可靠重建的新字段保持 `NULL`。

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

用户 Key 可创建、列出和读取自己的会话，并用必填 `Idempotency-Key` 提交轮次。每轮都是独立 Run，同一会话同时只能有一个排队、运行或取消中的 Run；同键同请求重放返回原轮次，同键不同请求为 409。跨用户读取返回 404。失败或取消的轮次仍保留用户原文，不生成助手回答。轮次读取返回原文和 Run 状态、结果；个人记忆列表是只读的。

```sh
curl -X POST http://127.0.0.1:8002/v1/conversations -H "Authorization: Bearer $USER_KEY"
curl -X POST http://127.0.0.1:8002/v1/conversations/CONVERSATION_UUID/turns \
  -H "Authorization: Bearer $USER_KEY" -H 'Idempotency-Key: turn-1' \
  -H 'Content-Type: application/json' -d '{"task":"项目 A 使用 MySQL","mode":"react"}'
curl http://127.0.0.1:8002/v1/conversations/CONVERSATION_UUID -H "Authorization: Bearer $USER_KEY"
curl http://127.0.0.1:8002/v1/conversations/CONVERSATION_UUID/turns/TURN_UUID -H "Authorization: Bearer $USER_KEY"
curl -X POST http://127.0.0.1:8002/v1/conversations/CONVERSATION_UUID/turns/TURN_UUID/cancel -H "Authorization: Bearer $USER_KEY"
curl http://127.0.0.1:8002/v1/memories -H "Authorization: Bearer $USER_KEY"
```

Worker 首次执行会固定所选摘要版本、近期与未被摘要覆盖的原文轮次、相关事实 ID、顺序和降级状态；恢复时复用快照。个人事实只从用户原话提取，带来源轮次、原文片段和时间；明确对象的相关历史陈述会一起补取。没有明确变化关系的两条陈述可能并存，不应推断哪条是当前状态。个人记忆不属于 RAG 引用证据，也不是高优先级指令。Embedding 检索失败时 Run 继续使用会话上下文并在结果中给出 `MEMORY_DEGRADED` 通知。后台提取从轮次提交时排队，回答生成后另排摘要任务；失败、取消仍可提取用户原话。任务有五分钟租约和最多三次尝试，后台模型调用不占 Run 预算。完整原文作为归档；摘要和提取结果可核查，但可能出错。首版事实仅追加，不自动覆盖或删除。

```sh
curl -X POST http://127.0.0.1:8002/v1/runs \
  -H "Authorization: Bearer $TEAM_KEY" \
  -H 'Idempotency-Key: demo-1' \
  -H 'Content-Type: application/json' \
  -d '{"task":"查证资料中的保留期限并说明依据","mode":"plan_execute"}'
curl http://127.0.0.1:8002/v1/runs/RUN_UUID -H "Authorization: Bearer $TEAM_KEY"
curl -X POST http://127.0.0.1:8002/v1/runs/RUN_UUID/cancel -H "Authorization: Bearer $TEAM_KEY"
curl 'http://127.0.0.1:8002/v1/runs?status=queued&limit=30' -H "Authorization: Bearer $TEAM_KEY"
```

`GET /v1/runs` 只列出凭据所属团队的 Run。`GET /v1/admin/runs` 需要管理 Key，可选 `team_id` 筛选全平台 Run；两个列表都支持 `status`、`created_after`、`created_before`，时间参数必须带时区，边界为不包含端点。列表按 `(created_at DESC, id DESC)` 排序，`limit` 默认 30、最大 100。响应为 `{"items":[...],"next_cursor":"..."}`；下一页原样使用 `next_cursor` 作为 `cursor`，并保持所有筛选条件不变。末页的游标为 `null`，不计算总数。每项只含 `id`、`mode`、`status`、`created_at`、`started_at`、`finished_at`、`error_code`；管理列表另含 `team_id`。游标格式无效或与筛选条件不符时返回 422。

```sh
curl 'http://127.0.0.1:8002/v1/admin/runs?team_id=TEAM_UUID&status=running&limit=30' -H "Authorization: Bearer $ADMIN_KEY"
curl http://127.0.0.1:8002/v1/admin/runs/summary -H "Authorization: Bearer $ADMIN_KEY"
```

管理概览返回 `queued_within_deadline`、`queued_past_deadline`、`running_lease_valid`、`running_lease_expired`、`cancelling` 五项数量，以及 `oldest_claimable_created_at`（无可认领 Run 时为 `null`）。它按查询时数据库中已持久化的状态统计；读取不会结算超时、估算精确队列位置或判断 Worker 是否在线。

父 Worker 从 PostgreSQL 认领 Run，每个 Run 启动一个独立子进程。首次认领与状态变更在同一事务中写入不可变的 v2 Execution Manifest，记录部署 revision、执行图/提示集版本、模型非秘密配置摘要、工具提供者及逐工具声明、预算及两个调用超时值；历史 v1 Manifest 继续可读，恢复认领沿用快照，旧 Manifest 缺少超时字段时使用当前配置。两个超时配置只接受有限正数，Worker 启动时验证；工具声明也在 Worker 启动时验证。Manifest 不保存模型 Key、RAG token 或完整模型 URL。所有模型和工具调用在发出前持久预留额度；默认每个 Run 最多 16 次模型调用（保留最后一次用于最终生成）和 10 次工具调用。只有已经显式确认写入 LangGraph checkpoint 的调用才允许恢复；失租时若存在结果未知或尚未确认持久化的外部调用，Run 以 `INTERRUPTED_UNKNOWN` 失败，避免重放。内部调用 trace 只保存安全摘要；LangGraph checkpoint 可能保存原始消息，首版不自动清理。

RAG 搜索可能依次等待查询改写、Embedding 和精排；RAG 服务内部这三步仍使用各自的 30 秒模型超时与降级流程。Agent 的 HTTP 网络超时按连接及读写等阶段计时，不保证严格的整次调用墙钟上限；Run 总期限继续提供最终墙钟限制。模型超时使 Run 以 `MODEL_TIMEOUT` 失败；RAG 搜索超时按不可用降级，工具 trace 记 `RAG_TIMEOUT`。超时只表示调用方未及时收到结果，不证明远端未执行，因此不会自动重试。

管理 Key 可读取原始业务 trace，也可读取安全的确定性时间线。Timeline 只聚合 Agent schema，不在线查询 RAG；`retrieval_id`、`service_request_id` 和 `tool_call_id` 用于离线关联。旧 Run 没有 Manifest 时返回 `manifest: null`，时间线不返回 task、最终 answer、原始 prompt、工具原始参数或证据正文：

```sh
curl http://127.0.0.1:8002/v1/admin/runs/RUN_UUID/trace -H "Authorization: Bearer $ADMIN_KEY"
curl http://127.0.0.1:8002/v1/admin/runs/RUN_UUID/timeline -H "Authorization: Bearer $ADMIN_KEY"
.venv/bin/python -m db.trace_run RUN_UUID
.venv/bin/python -m db.trace_run RUN_UUID --include-candidates
```

`db.trace_run` 分别使用 `AGENT_DATABASE_URL` 和 `RAG_DATABASE_URL` 的只读事务，输出 v2 JSON：Agent 时间线 ID、claim → 证据 ID → 所有记录该证据的工具调用 → RAG 审计 ID，以及查询哈希、索引 revision、降级信息和检索阶段状态及候选数量。关联标为 `matched`、`fields_missing`、`conflict` 或“未观测到关联记录”；冲突记录仍显示，但不视为已核实。默认输出省略候选详情；`--include-candidates` 展示 Dense 前 50、FTS 前 50、完整融合候选及精排前 40 的 ID、名次和分数。旧审计行和读取证据审计显示“未记录候选过程”。命令不输出 task、答案、claim 文本、prompt、查询或证据正文；数据库不可用时以非零状态退出，不输出追踪 JSON。运行角色的权限限制仍适用。部署时先应用当前 RAG 迁移链，再部署 RAG 服务和新版追踪命令。

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
curl http://127.0.0.1:8001/health/ready
curl http://127.0.0.1:8002/health/ready
```

`/health` 只表示 API 进程存活，保持原有响应。`/health/ready` 是轻量就绪检查：数据库连接、必要表及身份表可读时，Agent 返回 `{"service":"agent","status":"ok"}`；RAG 还要求服务 token 已配置且 active index 指针有效。不可用时返回 503，`status` 为 `unavailable`，不输出连接信息或异常详情。探测有短连接和查询超时，不调用模型或完整 `db.doctor`，也不检查 Worker 在线情况。两个服务各自的管理员接口 `GET /v1/admin/workers` 返回父 Worker 最近心跳、在线实例数、待处理任务与过期租约；最近 90 秒有心跳视为在线。生产监控应对在线实例数为零、队列过期和租约过期告警；心跳不能证明外部模型可用。`db.doctor` 在有积压却无在线 Worker 或发现过期任务时给出 WARN。

使用专用测试数据库及上述四个 DSN 执行 `.venv/bin/pytest -q`。测试在数据库中创建随机命名团队、凭据、Run 和合成文档；请勿指向生产库。无数据库变量时数据库测试会跳过。Agent 的 RAG 适配器只通过 HTTP/JSON 契约交互，从持久 Run 读取可信团队 ID。React、Plan-and-Execute、checkpoint、预算和结果发布使用脚本化模型验证；真实模型端点以及真实 Embedding、改写和精排服务的效果尚未验证。数据流见 [架构图](docs/architecture.md)。

本机 Docker 可通过 `.venv/bin/python -m scripts.postgres_integration` 自动创建独立 PostgreSQL 16/pgvector 测试实例，初始化迁移、checkpoint 和角色后运行三个原实验数据库场景；`--suite research` 运行绘图与资料联动专项，`--suite full` 运行整个测试集。后两者均要求七个核心数据库场景实际通过，且所有所选测试 0 失败、0 错误、0 跳过。使用合成资料和脚本化模型，额外禁止真实 HTTP 调用，默认结束后清理本次容器。配置与保留数据库步骤见[数据库集成测试](docs/postgres-integration.md)。

已有部署升级时先运行 `python -m db.migrate`：Agent 新增不可变实验输入快照，引用支持实验文件来源；迁移链还包含 Agent/RAG Worker 心跳表和 RAG 文档任务持久总期限。随后运行 `python -m db.bootstrap` 授权新表并收紧实验快照权限，再按角色运行配置检查。新环境仍需执行 checkpoint setup；已有 checkpoint 表无需重建。确认 `python -m db.doctor` 无 ERROR 后部署 API 和新 Worker，并检查管理员 Worker 状态接口。追踪命令仍只使用两个运行角色 DSN。

合成资料生成、离线绘图、无真实 HTTP 的联动验收和独立文档 HTTP Mock 步骤见[合成资料演示](docs/synthetic-demo.md)。固定输出用于工程验收；真实模型的理解、自主工具选择和跨来源推理仍需后续单独评估。
