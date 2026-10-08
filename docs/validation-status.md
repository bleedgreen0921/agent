# 合成数据工程验收记录

记录日期：2026-10-08。科研图生成与合成资料联动已完成本机 Docker 全量验收。以下数字来自实际 JUnit、`summary.json` 与清理日志；不代表真实模型或真实实验结果评估。

## 实际结果

| 项目 | 结果 |
| --- | --- |
| 科研专项命令 | `.venv/bin/python -m scripts.postgres_integration --suite research` |
| 科研专项报告 | `.data/postgres-integration/rap-test-65491e12191d/` |
| 科研专项测试 | 68 项通过，0 失败、0 错误、0 跳过；pytest 35.58 秒，总计 40.41 秒 |
| 全量命令 | `.venv/bin/python -m scripts.postgres_integration --suite full` |
| 全量报告 | `.data/postgres-integration/rap-test-a796d3358ecb/` |
| 全量测试 | 299 项通过，0 失败、0 错误、0 跳过；pytest 64.48 秒，总计 70.25 秒 |
| 核心数据库场景 | 原实验 3、绘图 2、联动 2，共 7 个全部实际通过 |
| 绘图单元用例 | 46 项，包含计算、拒绝输入、图片解码、矢量 SVG、复现、并发、损坏、超限、失败清理、路径安全及发布/复用的再次授权 |
| 绘图与联动数据库用例 | 18 项：绘图与联动各两个模式、4 个异常场景、3 个发布前撤销场景、6 个锁等待过期场景、1 个并发与损坏场景 |
| 本轮修复回归 | 22 项：1 个快照授权、4 个绘图、6 个真实锁等待、11 个 JUnit 与失败汇总场景 |
| 初始化诊断 | 两次 doctor 均为 15 项全部 `pass` |
| 模型与资料 | 固定合成实验和 Markdown；脚本化 Agent/tokenizer，Mock Embedding/改写/精排；真实 HTTP transport 禁止 |
| 实际链路 | PostgreSQL/pgvector、文档上传 API、RAG Worker 解析/分块/索引/激活、Dense/FTS/融合 SQL、ACL、审计、Agent Provider/客户端及持久结果 |
| 清理 | 两次专用 PostgreSQL 容器和 Compose 网络均已移除；`database_cleaned_up=true`；未保留数据库 |
| 警告 | 1 条已有 Starlette/AnyIO 弃用警告，无测试失败 |
| 依赖一致性 | `.venv/bin/python -m pip check`：No broken requirements found |

299 是全仓库测试总数，不能把全部测试描述为数据库联动场景。本轮租约竞态和失败汇总修复增加 22 项测试。此前绘图与联动交付增加 60 项测试，得到 277 项全量通过（`rap-test-f085e36b3775`）及 58 项科研专项通过（`rap-test-b700fa3ae140`）。更早实验工具修复后的全量结果为 217 项通过，报告为 `.data/postgres-integration/rap-test-65336e0dc929/`；版本 `1` 的 199 项报告为 `rap-test-9dcb1e72d64a`。原 12 个实验、原生成器与原预期答案保持不变。

## 七个必需核心场景

| 用例 | 实际结果 | 验证内容 |
| --- | --- | --- |
| `test_experiment_provider_publishes_file_citations_with_real_db_in_both_modes[react]` | 通过 | 原实验检索、对照、指标、预算、输入快照与引用发布 |
| `test_experiment_provider_publishes_file_citations_with_real_db_in_both_modes[plan_execute]` | 通过 | 固定计划下的原工具链与持久发布 |
| `test_experiment_snapshot_concurrency_and_durable_lease_checks` | 通过 | 原快照并发复用、来源变化后固定输入、租约与团队隔离 |
| `test_experiment_plots_publish_and_resume_in_both_modes[react]` | 通过 | 曲线与矩阵、本地文件/哈希/清单、持久引用及新租约恢复 |
| `test_experiment_plots_publish_and_resume_in_both_modes[plan_execute]` | 通过 | 固定计划下的同一绘图与恢复链路 |
| `test_synthetic_research_real_rag_mixed_citations_and_resume[react]` | 通过 | 真实上传、Worker、检索 SQL/审计、分析、绘图、恢复、混合引用与矛盾陈述识别 |
| `test_synthetic_research_real_rag_mixed_citations_and_resume[plan_execute]` | 通过 | 固定计划下的同一文档–实验–图片证据链 |

四个新增核心场景均在最终生成前人为中断，待租约失效后重新认领，由真实 checkpoint 恢复。恢复证据内容与中断前完全相同，工具调用总数不增加；最终发布的引用内容和 locator 与恢复证据一致。文档观点引用文档 evidence，实验数值及图片引用实验 evidence，所有引用来自当前 Run 的已获得证据。

数值已核对：基线 560/800=70%，注意力 604/800=75.5%，总体差异 5.5 个百分点；SNR≤−5 dB 差异 11 个百分点。计数矩阵合计 800，对角计数 560，类别顺序和矩阵方向与原输入一致；行归一化使用跨 SNR 聚合后的计数。研究记录中的“增加 20 个百分点”被脚本化答案明确指出与实际计数不符，不当作数值事实。

另验证真实发布前租约过期、执行期限过期、取消状态都阻止发布并清理临时目录；并发绘图选择一个清单，重复调用校验后复用，损坏不覆盖。无命中、真实索引不可用、无效严格对照和渲染失败不会生成虚构引用。跨团队文档读取拒绝，实验工具提前拒绝错误团队；源配置变化及文档新版本激活后，已发布引用保持稳定。

本轮六个真实锁等待场景覆盖行锁持有者不修改记录、文件锁等待以及已有图片复用中的租约/截止时间自然过期。用事件协调进入等待，通过 `pg_blocking_pids()` 或非阻塞 flock 确认实际争用，再以数据库时钟确认过期；修复前六个场景均因未拒绝发布而失败，报告为 `rap-test-93eaa72dbbef`，容器已清理。修复后全部拒绝发布、清理临时目录且不新增成功证据；复用失败保留原产物。授权复核在取得 Run 行锁后执行，并在目录文件锁内完成文件和响应校验后再次执行。

失败汇总用例注入 pytest 超时、截断/不可读/读取时消失的 JUnit，以及同时发生的清理失败，确认 `summary.json` 保留主错误、可选 `junit_error`、清理结果及耗时。成功验证后最终读取失败也返回非零，保留此前有效统计；无法取得统计时不填造零值。

## 核验版本

| 组件 | 实际版本 |
| --- | --- |
| Python | 3.12.3 |
| Matplotlib / NumPy / Pillow | 3.11.2 / 2.5.3 / 11.3.0 |
| 新绘图依赖 | contourpy 1.4.0、cycler 0.12.1、fonttools 4.66.1、kiwisolver 1.5.1、pyparsing 3.3.3 |
| PostgreSQL 镜像 / pgvector | `pgvector/pgvector:pg16` / 0.8.6 |
| identity / rag / agent Alembic head | `identity_0002` / `rag_0005` / `agent_0009`；本次无新迁移 |
| Agent graph / Prompt set / Execution Manifest schema | `v1` / `v3` / `2` |
| 原实验 Provider / 方法 / 输入快照 schema | `2` / `2` / `1` |
| 绘图 Provider / 方法 / 文件清单 schema | `1` / `1` / `1` |

提示集升级为 `v3`，增加图片引用、合成文档标识和本地路径与下载链接的区别；原公共 Run 响应和数据库结构保持兼容。旧分析快照的方法版本 `2` 继续用于绘图输入。

## 图片样例与视觉核查

通过离线命令生成的样例位于 `.data/research-validation/plots/offline/`，合成材料位于 `.data/research-validation/materials/`；`accuracy.json`、`five.json`、`count.json`、`row.json` 记录完整产物元数据。已实际查看四张 PNG，确认图例、坐标、合成标识和单元格标注没有遮挡，并由测试核对 SVG 可解析且矩阵和色条无内嵌位图。

| 样例 | plot ID / PNG |
| --- | --- |
| 默认双曲线 | [5c0b5f4c75d589c51165ee01e35aa588fb1984ca8c6281f77059cab586b74a61](../.data/research-validation/plots/offline/5c0b5f4c75d589c51165ee01e35aa588fb1984ca8c6281f77059cab586b74a61/plot.png) |
| 五曲线 | [e645e72f9ddac8e473c3a22acb4af0f76f64787f615b11b2134fbb3c282c5a4b](../.data/research-validation/plots/offline/e645e72f9ddac8e473c3a22acb4af0f76f64787f615b11b2134fbb3c282c5a4b/plot.png) |
| 计数矩阵 | [a1ec9522ce770d5d87f7d3fc7f622b671b87abb9ee2662c43e3906056b3c3329](../.data/research-validation/plots/offline/a1ec9522ce770d5d87f7d3fc7f622b671b87abb9ee2662c43e3906056b3c3329/plot.png) |
| 行归一化矩阵 | [c821edb1acf16c059727e95981c118159f58632aeb22fe667a4b049282ba6d5b](../.data/research-validation/plots/offline/c821edb1acf16c059727e95981c118159f58632aeb22fe667a4b049282ba6d5b/plot.png) |

每个样例目录同时保留 SVG、`data.json` 和 `manifest.json`。同环境下，独立目录重复渲染的四个文件逐字节一致。样例与原始日志由 `.gitignore` 排除，保留在本机，不随仓库提交；可按[绘图说明](experiment-plots.md)重新生成。

## 验证范围与复现

本次验证的是固定合成场景的工程机制和结论支持性。工具序列与最终答案由脚本固定，没有验证真实模型的理解、自主工具选择、读图、跨来源推理、真实论文检索质量、真实实验配置适配或生产负载。人工核对清单位于[绘图与联动说明](experiment-plots.md)。

两个最新成功报告目录中均有 `summary.json`、`pytest.xml`、`pytest.log`、`doctor.log` 及 `docker-cleanup.log`。科研专项初次失败记录为 `rap-test-9a5cd3b759c7`（断言把已完成 Run 的失租拒绝当作团队拒绝，并要求含过多 AND 查询词的 FTS 必须命中）；修正测试与查询后已实际重跑成功。其后追加发布前撤销和并发数据库用例，得到历史 58 项科研专项与 277 项全量结果；本轮补齐锁等待和失败汇总回归后，最新结果为 68 项科研专项与 299 项全量通过。

复现：

```sh
.venv/bin/python -m scripts.postgres_integration --suite research
.venv/bin/python -m scripts.postgres_integration --suite full
```

入口建立新的独立数据库并清理容器，不读取生产 `.env`，任何真实 HTTP 请求尝试都会导致失败。详见[数据库测试入口](postgres-integration.md)。
