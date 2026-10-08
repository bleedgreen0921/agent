# 合成数据工程验收记录

记录日期：2026-10-08。该记录依据实验工具大目录响应、CSV 异常隔离和演示命令诊断修复后的本机 Docker 测试报告，描述此次代码状态和测试环境；后续代码变更需重新验收。

## 验收结果

| 项目 | 结果 |
| --- | --- |
| 测试命令 | `.venv/bin/python -m scripts.postgres_integration --suite full` |
| Compose project | `rap-test-65336e0dc929` |
| 数据库 | PostgreSQL 16，镜像 `pgvector/pgvector:pg16`，pgvector 扩展 0.8.6 |
| 全量测试 | 217 项通过；0 失败、0 错误、0 跳过 |
| 实验单元专项 | 71 项通过，新增 18 项回归用例 |
| 实验数据库专项 | 3 项全部通过 |
| 初始化诊断 | doctor 共 15 项，全部 `pass` |
| 模型与资料 | 合成实验/合成文档、脚本化模型和内存 Mock；真实 HTTP transport 禁止 |
| 耗时 | pytest 31.83 秒；初始化、测试与清理总计 37.09 秒 |
| 清理 | 本次 PostgreSQL 容器与 Compose 网络已移除 |
| 警告 | 1 条 Starlette/AnyIO 弃用警告，无测试失败 |

217 是全仓库测试总数，包含单元、接口和数据库测试；其中 3 项是实验 Provider 的数据库专项用例，不应把全部 217 项描述为实验数据库专项。

修复前的历史验收为 199 项通过，报告目录为 `.data/postgres-integration/rap-test-9dcb1e72d64a/`，当时实验 Provider / 方法版本为 `1`。此次新增 18 项回归用例；修复后的实验单元专项命令 `.venv/bin/pytest -q tests/test_experiment_fixtures.py tests/test_experiment_tools.py` 为 71 项通过。新用例覆盖 40 个合法实验各含 64 个源文件、检索与对照按字节缩页及完整遍历、空页证据、UTF-8 与大小边界、超限时上下文不变、CSV 解析失败隔离、CLI 诊断和旧快照版本拒绝。原有用例同时增加了版本和对照分页字段断言。

## 已核验的版本

| 组件 | 版本 |
| --- | --- |
| identity Alembic head | `identity_0002` |
| rag Alembic head | `rag_0005` |
| agent Alembic head | `agent_0009` |
| Agent graph | `v1` |
| Prompt set | `v2` |
| Execution Manifest schema | `2` |
| 实验 Provider / 方法版本 | `2` |
| 实验快照 schema | `1` |

迁移测试现在从各 Alembic 迁移链读取当前 head，与数据库实际版本比较，保持对新增迁移的检查。

## 实验数据库专项

| 用例 | 结果 | 验证内容 |
| --- | --- | --- |
| `test_experiment_provider_publishes_file_citations_with_real_db_in_both_modes[react]` | 通过 | 实验检索、对照筛选、确定性指标比较、预算与调用记录、输入快照、引用持久发布 |
| `test_experiment_provider_publishes_file_citations_with_real_db_in_both_modes[plan_execute]` | 通过 | 固定计划下的同一实验工具链与结果发布 |
| `test_experiment_snapshot_concurrency_and_durable_lease_checks` | 通过 | 并发首次登记共用快照、原文件变化后沿用快照、有效租约与可信团队检查 |

两个执行模式的发布测试还验证运行角色不能修改或删除实验输入快照，以及原配置变化后已发布引用保持一致。此前失败的 `test_migrations_permissions_and_key_lifecycle` 已在本次全量测试中通过。

## 验证结论与范围

本次已验证真实 PostgreSQL 下的迁移、隔离角色、checkpoint、Agent 执行、合成实验分析及文件依据引用。模型选择工具的步骤由脚本固定，用于验证接线和状态行为；未调用真实语言模型、Embedding、改写或精排模型，也未进行真实自动调制识别训练。

检索证据现在只含当前页的完整文件清单，对照查找包含基线与当前页对照；两者按完整响应的 UTF-8 字节数遵守 256 KiB 上限，并返回可继续遍历的 `next_offset`。CSV 字段超过解析器限制时仅对应实验为 `invalid_results`，其他登记及快照捕获继续成功。演示命令遇到无效目录、YAML 或预期答案时正确退出并报告原始错误，不发布新产物。方法版本 `1` 的旧快照在新版下仍明确拒绝复用，不重新扫描替换；快照 schema 与数据库迁移 head 不变。

真实论文检索质量、真实模型自然语言工具选择、跨来源推理质量、真实实验配置适配、统计结论质量及生产负载仍需另行验收。当前可确认的是合成数据流程的工程执行与持久化链路。

## 原始证据与复现

本次原始报告目录为 `.data/postgres-integration/rap-test-65336e0dc929/`：

- `summary.json`：成功状态、计数、doctor 结果与总耗时。
- `pytest.xml` / `pytest.log`：逐用例状态、全量通过记录及耗时。
- `doctor.log`：连接、迁移、角色权限、索引、租约、Worker 状态及文件一致性检查。
- `docker-cleanup.log`：容器和网络移除记录。

`.data` 被 Git 忽略，原始日志保留在测试机器上；本文保留可随仓库阅读的结果摘要。复现流程见[本机 Docker 数据库集成测试](postgres-integration.md)，再次执行相同命令会建立新的独立测试实例并生成新的报告目录。
