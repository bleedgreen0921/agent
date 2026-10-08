# 实验分析工具（首版）

实验 Provider 将时间 ID 命名的实验目录关联到 YAML 配置及 CSV/JSON 结果，供 Agent 检索、选取对照、确定性计算，并生成可引用的文件依据。共享逻辑位于 `experiment_service/catalogue.py`，离线演示与在线工具使用同一实现。

这是科研 Agent 平台首个领域应用的已落地工具，面向个人自动调制识别研究中的实验查找和对照分析。团队 ID 作为底层授权边界保留，科研项目由根目录与配置中的 `run.project_id` 标识，两者不等同。项目全貌见[定位与能力现状](platform-capability-assessment.md)。

首版支持仓库中 `examples/amc_experiments` 的合成格式，要求 `synthetic: true`。尚未适配真实训练框架日志、任意 CSV/JSON 字段或多个科研项目根目录；接入真实数据时应增加格式适配器并保留相同验证规则。工具不训练模型、不读取论文正文，也不解析结果图片。论文检索继续使用已有 RAG Provider。

## 启用

先使用现有 owner / runtime DSN 应用迁移并更新授权：

```sh
.venv/bin/python -m db.migrate
.venv/bin/python -m db.bootstrap
```

新建环境仍按 README 初始化 LangGraph checkpoint。实验目录在 Worker 所在机器上可读；默认不启用实验 Provider。选择一个已有团队作为个人科研空间，将它的 UUID 配置在服务端，使用该团队 Key 或其用户 Key 创建 Run / 会话。

```sh
export AGENT_EXPERIMENT_ROOT='/home/qjj/RAG-Agent/research-agent-platform/examples/amc_experiments'
export AGENT_EXPERIMENT_TEAM_ID='替换为已创建团队的 UUID'
export AGENT_TOOL_PROVIDERS='agent_service.tools.rag:tools,agent_service.tools.experiments:tools'
.venv/bin/python -m db.config_check --role agent-worker
.venv/bin/python -m db.doctor --json
.venv/bin/python -m agent_service.worker
```

只验证实验工具时，可将 `AGENT_TOOL_PROVIDERS` 设置为 `agent_service.tools.experiments:tools`，该 Worker 配置不要求 RAG URL/token。仍需 Agent 数据库与兼容的模型端点。已有进程需在设置变量后重启。绝对路径可按实际部署修改；模型和客户端不能传入根目录、团队 ID 或租约。

## 工具与参数

| 工具 | 参数 | 返回内容 |
| --- | --- | --- |
| `search_experiments` | `filters`，可选 `status` / `limit` / `offset` | 配置精确检索、验证状态、加权指标和分页；默认仅 `ready`，`status: null` 包含所有状态 |
| `read_experiment` | `experiment_id` | 完整 YAML 配置、状态、问题、指标、源文件哈希 |
| `compare_experiment_configs` | `left_id` / `right_id` | 全部有效配置差异；仅忽略 `run` 元数据 |
| `find_experiment_controls` | `baseline_id` / `changed_paths`，可选 `limit` / `offset` | 与基线恰好只存在声明差异的完整有效实验 ID，支持分页 |
| `compare_experiment_metrics` | `left_id` / `right_id` / `changed_paths` | 总体、低 SNR、按 SNR 的准确率及百分点差异 |
| `summarize_experiment_confusions` | `experiment_id`，可选 `snr_db` / `top_k` | 真实类别→预测类别的错分计数排序，以及按真实类别样本数计算的错误比例 |

示例过滤：`{"model.attention.enabled": true, "training.seed": 42}`。这是结构化字段查询，值的类型必须匹配，`true`、`1`、`"1"` 不等价；未知字段返回可恢复错误。关键词、范围查询和按指标排序尚未实现。

检索和对照查找均按实验 ID 升序分页，默认 `limit=20`、`offset=0`，要求 `limit` 在 1–50、`offset>=0`。两者返回 `total`（整个快照中的匹配数量）、`offset` 和 `next_offset`；检索的 `items`、对照查找的 `experiment_ids` 仅包含当前页。完整 JSON 响应按 UTF-8 字节计数，最大 256 KiB；超限时自动减少当前页数量，并保留所选实验的完整文件清单。因此下一次请求必须沿返回的 `next_offset` 继续，不能自行增加 `limit`；`next_offset: null` 表示没有后续页。筛选条件、基线和声明变量应在翻页时保持相同。若单条结果或必要来源仍超限，返回可恢复错误 `EXPERIMENT_RESULT_TOO_LARGE`。

建议提交的任务：

> 查找与 20261001_090000 仅在 model.attention.enabled 上不同的实验，列出配置差异，比较总体、低 SNR 和各 SNR 准确率，再列出基线错分最多的调制类别对。明确这些是合成结果，说明统计口径，并引用配置与结果文件依据。

可使用 `react` 或 `plan_execute`。对于合成基线与仅开启注意力的实验，程序计算总体 70%→75.5%，增加 5.5 个百分点；低 SNR 为 SNR≤−5 dB，增加 11 个百分点。模型负责将自然语言目标转为工具参数和组织结论；字段相等判断、计数核对、加权计算及排序由程序执行。

## 有效比较规则

- 完整 YAML 条件、`provenance.config_is_effective: true`、完成状态与完整结果覆盖是 `ready` 的前提。
- CSV 每个 SNR×类别只能有一行；准确率必须与整数计数一致。JSON 总结、低 SNR 总结与混淆矩阵必须与 CSV 对账。
- 总体和低 SNR 准确率为 `sum(n_correct)/sum(n_total)`，不平均组准确率。混淆矩阵行是真实类别，列是预测类别。
- 两个结果必须同为 `ready`，实际配置差异集合必须等于 `changed_paths`，对应 SNR×类别的样本数必须一致。数据集、划分、类别及评测条件不能声明为有效对照变量。
- seed 仍是配置条件；跨 seed 必须显式声明 `training.seed`。YAML 键顺序和时间 ID 改变不表示新的随机种子重复实验。单对实验差异仅为描述性结果，不作显著性或因果结论。

缺少配置条件、训练失败、结果缺失或对账失败的实验保留在目录中，分别标为 `unknown_conditions`、`failed`、`incomplete_results`、`invalid_results`；配置 YAML 本身不能解析时，本次建快照失败。CSV 解析器错误（包括字段超过解析器限制）仅将对应实验标为 `invalid_results`，记录 `RESULT_VALIDATION_FAILED:Error`，其余实验继续登记；文件大小限制不等于 CSV 字段大小限制。对账失败的指标不能用于比较。

## 快照、身份与引用

第一次实验工具调用将登记结果、配置、已验证计数、混淆矩阵和源文件 SHA-256 保存到 `agent.run_experiment_snapshots`。同一 Run 的后续步骤及恢复只使用该快照；新 Run 重新读取目录。并发首次调用通过 Run 行锁和唯一键选择同一快照。输入文件各读取一次，解析和哈希使用同一份字节。快照固定已读取内容，不提供整个目录在同一时刻的文件系统事务；外部训练程序应在结果写完后再提交任务。

每次工具调用核对持久 Run 的团队、有效租约和状态。配置中的团队绑定防止其他团队读取本地科研目录；首次调用未经授权时不扫描文件。限制为最多 200 个实验、每实验 64 个文件、单文件 2 MiB、总输入 16 MiB，拒绝实验目录和文件符号链接。该格式用于小型结构化实验资料，模型 checkpoint 等大文件应放在根目录之外。

运行角色对实验快照仅有 `SELECT/INSERT`。升级必须执行 `db.bootstrap` 更新权限；readiness 和 doctor 也检查新表。迁移允许文档 ID 为空，以保存非文档引用；旧 PDF / Markdown 文档证据仍要求文档与版本 ID。如果已有实验引用，降级迁移会拒绝恢复旧的非空约束，避免静默删除引用。

所有工具声明为 `read_only` 且产生证据，走现有统一预算、trace 与 checkpoint。`source_locator.kind="experiment"` 的引用包含项目 ID、实验 ID、源文件相对路径和哈希、快照哈希、计算方法及版本、`synthetic` 标志；不伪造文档 ID。最终 claim 引用实际获得的 evidence ID，发布后保存被引用的结果快照。trace 仅保存操作、数量、快照哈希和 evidence ID；完整配置及结果可能保存在 checkpoint / 实验快照 / 引用内容中。

检索引用的实验 ID 与文件清单只覆盖实际返回的当前页；对照查找覆盖基线与当前页对照，去除重复实验。筛选与 `total` 基于整个不可变快照，`snapshot_sha256` 仍标识完整快照。检索无命中或超出末页时，实验及文件列表为空，但仍返回快照级查询证据；对照查找的空页保留基线来源。缩页试算不会写入 evidence、notice 或调用 metadata，只有通过大小检查的最终响应会记录。

所有实验结果带 `EXPERIMENT_SYNTHETIC` 通知，最终生成提示要求注明合成材料。输入格式问题、未知 ID 或无效比较返回可恢复工具错误；团队授权错误终止 Run。根目录或方法版本改变时，旧 Run 会拒绝混用新配置。

本次大目录与异常处理修复将实验 Provider、工具声明及方法版本升为 `2`，快照 schema 仍为 `1`，无需新增数据库迁移。新版读取方法版本 `1` 的旧 Run 快照时返回 `EXPERIMENT_SNAPSHOT_INCOMPATIBLE`，不会重新扫描并替换原快照。部署前应由旧版 Worker 完成已有 Run，再切换新版；仍需执行的旧任务创建新 Run。

## 验证与范围

本机 Docker 的一键数据库初始化与测试见[数据库集成测试](postgres-integration.md)，默认使用临时 PostgreSQL 16/pgvector、合成实验与脚本化模型：

```sh
.venv/bin/python -m scripts.postgres_integration
```

```sh
.venv/bin/pytest -q tests/test_experiment_fixtures.py tests/test_experiment_tools.py
# 专用测试数据库中，先按仓库 CI 初始化迁移、checkpoint 和角色：
.venv/bin/pytest -q tests/test_agent_runtime.py -k experiment
```

单元测试使用真实 LangChain / LangGraph 和脚本化模型验证工具调用链、预算钩子、错误结算、checkpoint 证据恢复与最终引用，数据库连接用内存替身。数据库集成测试使用真实 PostgreSQL，覆盖两个执行模式的实际持久发布、快照并发、租约校验和运行角色不可变权限。

2026-10-08 本机 Docker 修复后全量验收已完成：217 项通过、0 失败、0 错误、0 跳过，其中三个实验数据库专项全部通过；实验单元专项为 71 项通过。使用合成数据与脚本化模型，禁止真实 HTTP transport。直接运行 pytest 且未配置 DSN 时仍会跳过数据库用例；一键 Docker 入口要求专项实际通过。详细版本和报告见[验收记录](validation-status.md)。真实模型的自然语言工具选择能力仍需另行验证。

现阶段登记是每 Run 固定的文件目录快照，还没有独立实验 CRUD API、跨 Run 常驻实验库或前端。工具结果通过现有 Run JSON 返回。Markdown/CSV 文件仍由离线 `scripts.experiment_fixture_demo` 导出；在线文件生成、下载及文件产物引用可在后续独立 Artifact 工具中接入。
