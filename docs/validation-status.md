# 个人版第一阶段验证记录

2026-10-12 完成个人实例模型与统一同步执行契约改造。验收使用专用 PostgreSQL 16/pgvector、新个人基线、脚本化模型及 Mock/ASGI Transport，没有迁移历史业务数据。

## 全量合成测试

运行：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55440`。

最终报告目录：`.data/postgres-integration/rap-test-48123a303bee/`。

| 检查 | 结果 |
| --- | --- |
| 全量测试 | 254 通过 |
| 失败、错误、跳过 | 0、0、0 |
| 三个实验数据库专项 | 全部通过 |
| doctor | 三条个人迁移链、角色权限、任务 RLS、checkpoint 等检查全部通过 |
| 真实 HTTP transport | 禁用，未尝试真实模型请求 |
| 测试容器 | 已清理 |
| git diff --check | 通过 |

JUnit、pytest 日志、doctor 报告和 summary.json 位于上述被 gitignore 的报告目录。测试中仍有依赖库对不可变 Mapping 的序列化警告及 Starlette 弃用警告，不影响本次业务输出和断言。

覆盖个人 API 与 loopback 来源/Host/Origin、服务 Token 和关联头、实例及操作级幂等、active/历史证据/逻辑删除、统一状态与严格租约、锁等待后拒绝过期续租、取消/超时/接管优先级、主 checkpoint/blob/中间写入回滚、Agent 保守恢复、Memory 三次尝试与原子产物、RAG 重试和发布事务、工具冲突与合并中途失权、逻辑停止后的物理容量、角色隔离及旧库拒绝且不修改。

## 实际启动冒烟

在另一专用测试数据库中启动本机合成模型 HTTP 服务、两个个人 API 和同步 Worker，使用缓存的本地 tokenizer，验证：

- react 与 plan_execute 两种模式完成文档检索并发布引用。
- 创建会话、提交轮次，提取与摘要任务均完成。
- Agent Worker 收到 SIGTERM 后，未知模型调用结算为 INTERRUPTED_UNKNOWN；stop_requested_at 保持为空，没有误记为用户取消。
- Worker 清理完成后，其在线记录消失。

报告：`.data/personal-verification/smoke-summary.json`，配套服务日志和两种模式的结果保存在同目录。该冒烟只调用本机固定合成响应，不代表真实模型的工具选择能力或真实训练效果。

## 发布边界

新版本必须使用独立个人数据库和新的资料目录。旧库、旧目录、旧代码单独保留作为历史归档与回退组合；禁止将旧代码直接指向新库。初始化不支持旧业务数据升级或 downgrade。

本阶段保留同步调度与每 Run 子进程。后续异步阶段复用 execution/contracts.py、store.py 和物理工作登记规则。

## 第二阶段：异步基础设施与资源生命周期

2026-10-12 完成 `RuntimeResources`、线程安全的工作登记、有限单调时钟期限、三类异步数据库池、按服务复用 HTTP、专用有界 IO/CPU 执行器、CPU 整池回收，以及正式 API 入口的独立退出 watchdog。两个 API lifespan 持有资源，readiness 和 Worker 状态查询已异步接入。接口及配置说明见 [async-resources.md](async-resources.md)。

最终运行：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55441`。
报告目录：`.data/postgres-integration/rap-test-2b870717ba84/`；包含 pytest.xml、pytest.log、doctor.log、summary.json 和 resource-verification.json。

| 检查 | 结果 |
| --- | --- |
| 全量合成测试 | 290 通过；0 失败、0 错误、0 跳过 |
| 新资源专项 | 28 项容量/取消/HTTP/CPU/生命周期测试，8 项专用 PostgreSQL 边界测试通过 |
| 旧个人版及第一阶段执行契约 | 全部继续通过 |
| 三个实验数据库专项、doctor | 全部通过 |
| HTTP 保护 | 模型端点和其他真实 transport 继续禁止；仅作用域内精确 127.0.0.1/随机端口测试服务允许 |
| 两个正式 API 入口 | 数据库不可用时 `/health` 返回 200；SIGTERM 后正常关闭 |
| 永久卡住线程 | 独立子进程内验证，watchdog 在期限内以 70 退出；事件循环被阻塞时同样退出 |
| CPU 整池回收 | 复用、取消、崩溃、其他在途工作明确中断、未提交工作保留、下一 generation 可运行 |
| 进程组清理 | 独立子进程验证组长先退出后，忽略 TERM 的后代仍被 KILL；直接进程及管理线程回收，无旧组残留 |
| 关闭及取消 | 重复取消不提前释放流式容量；部分启动回滚；并发关闭等待同一次关闭；全部关闭共用原期限 |
| 期限竞争 | 已过期工作不提交；完成回调先于超时协程时，也不执行已过期排队函数 |
| 失败关闭报告 | 保留实际未完成数量、借用者和 cleanup 数量；shutdown_complete=false 时 watchdog 不解除 |
| 池会话及事务 | 三种 profile 设置、control 独立容量、同一借出连接、局部设置复用不污染、取消/SQL 异常回滚、坏连接丢弃、RLS/角色/只读限制全部通过 |
| 最终泄漏检查 | 测试 API、CPU spawn、multiprocessing tracker 和独立探针进程均无残留；专用容器已清理 |
| 静态边界 | CPU 私有字段仅在 cpu_backend.py，生产强制退出仅在 scripts/serve.py；迁移的异步路径没有同步 DB 调用或业务侧自建资源 |
| git diff --check | 通过 |

本轮仍有第一阶段已有的 Starlette 弃用和 Pydantic 不可变 Mapping 序列化警告，共 33 条；无新资源异常、待清理任务或连接/进程泄漏警告。成功关闭会先结束执行器和借用事务/响应，再关闭客户端/数据库池。失控线程测试在可强制结束的独立进程中执行，未挂住 pytest 主进程。

本阶段未新增数据库迁移，持久执行契约版本仍为 1。同步 Worker 认领循环、ReAct/Plan、Memory、RAG 文档处理及受保护 Saver 保留第一阶段路径。发布重启两个 API，继续使用同一第一阶段个人数据库；无需重建、迁移数据或改变任务状态、预算及恢复策略。必须使用 `scripts.serve` 或由嵌入宿主自行接入资源退出 handler，才能保证失控线程下的进程退出。
