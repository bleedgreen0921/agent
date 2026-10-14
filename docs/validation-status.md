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

## 第三阶段：异步持久执行层与完整执行链

2026-10-12 完成共用异步任务存储、资源注入的业务存储、协议 2 调用恢复、ProtectedSaver 原生异步适配、模型／工具／输入、Agent 两种图、Memory、文档与在线检索，以及两个 Worker 的协程调度。任务进程与 Memory 子进程已移除；资源层保留共享 CPU 池和有界 IO。

正式验收命令：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55444`。
报告目录：`.data/postgres-integration/rap-test-7b82eaa749f2/`；包含 pytest.xml、pytest.log、doctor.log、summary.json。

| 检查 | 结果 |
| --- | --- |
| 全量合成测试 | 296 通过；0 失败、0 错误、0 跳过 |
| 三个实验数据库专项 | 全部通过，包含两种图和并发稳定快照 |
| 个人增量迁移 | 全新库完整迁移至 agent_personal_0002，checkpoint 初始化、授权、doctor 通过 |
| 原资源专项 | 28 项本地资源与 8 项 PostgreSQL 边界全部继续通过 |
| 新恢复专项 | 11 项真实数据库／SDK 专项：稳定预留、重复结算、pending write 恢复、内容损坏、写入冲突、实际事务回滚、发布核实、满额清理、停机控制通道、两类 COMMIT ACK 丢失、SDK HTTP 容量 |
| 执行权与控制 | 三类任务的 token、停止、期限、租约、锁等待后复查与迟到合并全部通过 |
| Worker 监督 | 协程取消隔离、数据库故障保留租约、文档重试分类、退出 watchdog 继续通过 |
| 真实 HTTP 保护 | 全量测试禁止真实模型 transport；只允许明确作用域的精确 loopback 合成端点 |
| 静态／编译 | 生产业务没有同步 DB、同步图调用、默认 to_thread、自建业务池或任务 Process；compileall 和 git diff --check 通过 |

实际启动验证在单独的调试测试库中运行本机合成模型服务、两个正式 API 与两个异步 Worker：文档索引激活；ReAct 和 Plan 各发布一个引用；会话事实提取和摘要任务均完成；实际产生 30 条调用恢复回执；Worker 收到 SIGTERM 后以 0 退出。报告 `.data/postgres-integration/rap-test-f747c7ffec5d/async-smoke/summary.json`，服务日志同目录。API 完成 Uvicorn 正常关闭后按 SIGTERM 的退出约定结束；没有强制超期退出、错误日志或残留任务／CPU 进程。调试容器在验证后清理。

本次保留原有的 33 条 Starlette 弃用和 Pydantic 不可变 Mapping 序列化提示，没有新增未 await coroutine、资源或连接泄漏提示。合成结果证明机制与集成行为，不评价实际 LLM 的科研判断能力。

升级当前第二阶段个人库需停止旧执行链、备份数据库、处理残留协议 1 任务后运行增量迁移和授权；不重新初始化个人库。见 [异步执行层](async-execution.md)。本轮只修改工作区并操作隔离测试数据库，未升级正在使用的个人数据库。

## 第四阶段：调用适配、业务链与 Worker 职责收敛

2026-10-13 完成 CallRunner、显式 AgentCallStore、Attempt／Call 上下文、统一控制异常、模型与工具适配、共享 Embedding 验证、实验纯计算入口、Agent runner、Memory／RAG 分支整理及 Worker 执行结果接入。首次认领的 Manifest 准备移动到事务前；正式 Worker 启动时准备并复用 Provider／配置模板。协议和数据库 schema 不变。

正式全量验收：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55449`。
报告目录：`.data/postgres-integration/rap-test-0654a41895cf/`。

| 检查 | 结果 |
| --- | --- |
| 全量测试 | 339 通过；0 失败、0 错误、0 跳过 |
| 新调用适配专项 | 43 项提交未知、重复预留、控制异常、上下文／期限隔离、Embedding 验证、排队后授权、SDK 流式期限、下游预算、partial、兄弟任务清理、Manifest、usage、受控同步工具、真实 SDK 包装控制信号、受控输入准备、文档消费和离线模型加载验证 |
| 三个实验数据库专项 | 全部通过，两种模式使用直接的 CPU 计算入口并保留引用 |
| doctor／迁移／权限 | 隔离库初始化和已有迁移 head 验证通过；没有新增第四阶段迁移 |
| 资源与持久回归 | 原容量、CPU 回收、停止、未知提交及调用回执测试继续通过 |
| 警告 | 原有 33 条 Starlette／Pydantic 警告；无新增未 await coroutine、连接或执行器泄漏警告 |
| 静态／编译 | git diff --check、compileall 通过；图无预算／结算实现，Worker 无执行 SQL 或模型／解析异常分类 |

实际启动冒烟报告：`.data/postgres-integration/rap-test-17f92c3b6530/phase4-smoke/summary.json`，对应服务日志同目录。两个正式 API、两个异步 Worker 与本机合成模型服务完成：文档 active、ReAct 和 Plan 各一个有效引用、Memory 提取和摘要完成，共 30 条调用回执。两个 Worker 收到 SIGTERM 后均以 0 退出，两个 API 完成 Uvicorn 正常关闭后按 SIGTERM 约定退出。服务日志没有 ERROR、Traceback、未消费任务异常或超期清理记录。

全量测试和冒烟分别使用隔离数据库；没有访问真实模型或正在使用的个人数据库。测试数据库在验证结束后清理。发布继续沿用排空并停止 Worker、更新代码、启动验收的流程，详见 [调用适配说明](call-adapters.md)。

最终边界检查补齐 CPU 模型本地加载：tokenizer 禁止远程元数据请求，PDF 向 Docling 传入本地 artifacts，CPU 子进程使用 HF／Transformers 离线模式。OCR 与表格处理保持开启；PDF 模型缓存需在部署准备时提供。

## 执行权专项复核：过期续租与最终发布

2026-10-13 复核确认当前实现已在任务行锁取得后单独读取 clock_timestamp()，检查任务身份、token、running、停止请求、旧租约和执行总期限。续租 UPDATE 再要求旧租约与总期限有效；最终发布经 store.complete 在产物写入后复查，拒绝时整体回滚。已有结果返回 AlreadyApplied 属于幂等核实，不构成新的发布。本轮未改写生产执行逻辑，仅补充锁后时钟校验注释和数据库回归测试。

新增六项真实 PostgreSQL 测试：续租／发布分别在等待行锁期间跨过租约或总期限，保持原 token、running、无停止请求且无人接管；观察 pg_blocking_pids 确认实际锁等待，以数据库时间确认过期后释放锁，验证拒绝续租／发布且心跳未更新。另在两种期限失效条件下，确认答案、claim、证据快照、引用关联和摘要任务已经写入当前事务，再验证最终复查令全部产物及关联任务回滚。

验收命令：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55450`。
报告：`.data/postgres-integration/rap-test-4ae43a521c99/`。
结果：345 通过，0 失败、0 错误、0 跳过；原有 33 条依赖警告不变。doctor 与三个实验数据库专项通过；隔离容器已清理，没有操作正在使用的个人数据库。git diff --check 与相关模块编译检查通过。

## RAG 双路召回专项修复：同一只读快照

2026-10-13 复核发现原异步检索分别读取索引版本、Dense 和 FTS，使用不同连接及 READ COMMITTED 事务，存在文档或索引版本切换时两路结果混用的窗口。本轮增加事务级隔离参数，在第一条 SELECT 前设置隔离级别；统一召回接口在同一连接的只读 REPEATABLE READ 事务内读取索引版本并依次执行两路 SQL。快照只保证本次读取视图一致，不阻止另一个事务发布新版本或删除文档；后续请求读取新快照。

改写／Embedding 与分词仍在事务外并发准备，重排和审计也在事务外。准备期间切换索引时，同模型同维度的向量用于事务内读取到的索引版本；模型或维度改变则舍弃旧向量，以 FTS 降级继续。每路 SQL 使用保存点，实际语句超时后回滚保存点，让另一路保留原快照继续执行；取消或连接丢失不伪装为正常召回结果。

新增十一项真实数据库验证：在索引版本读取后或 Dense 完成后，用另一连接提交文档／索引切换并删除旧向量，或逻辑删除文档，证明当前两路仍读取旧版本而下一次读取看到变更；两路分别注入真实 SQL 超时，验证保存点恢复；准备期间同配置、模型变化、维度变化三类索引切换及实际审计版本；取消后的连接归还；同一池连接的只读和隔离级别局部设置复用不污染。测试同步使用事件屏障。

验收命令：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55451`。
报告：`.data/postgres-integration/rap-test-946ffd82d065/`。
结果：356 通过，0 失败、0 错误、0 跳过；原有 33 条依赖警告不变。doctor 与三个实验数据库专项通过；隔离容器已清理，未操作个人数据库。无需 schema 迁移。git diff --check 与相关模块编译检查通过。

## PostgreSQL 时间语义复核与修复

2026-10-13 全面检查生产 SQL 中 now()、clock_timestamp()、statement_timestamp() 及历史默认值。租约、执行权、最终发布已有锁后实际时间保护；本轮修复 Worker 心跳的事务开始时间、文档删除的锁前表达式求值、调用开始和 Manifest 捕获的默认事务时间，以及监控／doctor 的旧时间和队列汇总跨查询阈值漂移。普通记录的事务创建时间默认值保留，关键事件显式覆盖历史默认值。规则及保留原因见 [database-time.md](database-time.md)。

任务创建、首次认领、续租用单次实际时间采样统一相关时间字段。候选查询只负责筛选，认领 UPDATE 在目标行已锁定后采样新的时间，复查可用时间、排队期限和既有执行期限；超期不分配新 token 或增加 attempts。恢复回执验证后重查期限；恢复和可重试失败的排队 UPDATE 均再次要求期限有效，超期按三类任务各自的超时错误结算。恢复保留首次开始时间、期限和预算。

新增 25 项专用 PostgreSQL 测试：六种事件的真实锁等待与锁释放后时间；三类任务分别在排队期限／执行期限跨界时拒绝认领；恢复验证期间跨界；三类任务创建／认领／续租字段的共同时间基准和期限保持；两个 Worker 状态报告的实际采样和共用阈值；三类 doctor 在旧事务里识别新过期租约；队列汇总跨过期限后仍使用同一观察阈值；三类可重试失败在更新前跨过期限时结算为超时。使用 pg_blocking_pids、事件屏障及 pg_sleep_until 的数据库期限，避免仅靠客户端猜测延时。

最终验收：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55454`。
报告：`.data/postgres-integration/rap-test-48d54160f8f5/`。
结果：381 通过，0 失败、0 错误、0 跳过；原有 33 条依赖警告不变。doctor、三个实验数据库专项和既有执行权／召回快照测试通过；相关模块 compileall、AST 和 git diff --check 通过。专用容器已清理，未操作个人数据库，无新增 schema 迁移。

报告共用时间阈值与数据快照一致性保持区分：监控数据仍是 READ COMMITTED 的观察，不作为执行权；RAG 双路使用原只读 REPEATABLE READ。数据库实际时间仍取服务器墙钟；本地容量、调用和退出预算继续使用单调时钟。

## R01 专项复核：结算、checkpoint 确认与控制语义

2026-10-13 复核确认 R01 所列 now()、UPDATE FROM 代替任务锁、control_state 不读取租约或使用应用时钟的旧实现，已不在当前运行路径中。当前模型／工具结算先锁任务行，再锁调用行，写入前后使用 assert_active；fail_run 经过统一的锁后失败结算；批量 mark_checkpointed_calls 已删除。Saver 的主记录、blob、pending write 和回执一直在同一受保护事务中提交。

本轮补齐回执确认原语自身的边界：receipts.confirm 在调用方事务内开始和结束都执行任务行锁及实际时间校验，不再只依赖 Saver 外层授权。持久写入、Agent control_state 与 Worker 控制循环的纯判断复用 LeaseSnapshot.refusal；stopping 即使没有停止时间字段也按取消处理，非 running 状态不能被报告为 running。失败结算拒绝旧 token 和非活动状态，保留取消／总期限控制结算及受限 Worker 退出规则；清扫停止状态也按取消结算。

新增 41 项真实 PostgreSQL 测试：模型／工具结算、主 checkpoint、pending write、失败结算分别在任务行锁等待中交织取消、接管、租约过期和总期限过期（20 项）；直接调用确认原语的四种失权及两种写入后过期（6 项）；两种 Saver 写入在回执确认后跨过两种期限的图记录／回执／布尔标记整体回滚（4 项）；控制状态、持久授权、Worker 动作和失败结算九种优先级组合；两类调用的 AlreadyApplied 核实不更新已有数据、不赋予执行权（2 项）。锁竞争通过 pg_blocking_pids 和事件屏障确认，期限通过数据库实际时间验证。

本轮统一的是执行契约，不将控制观察当作写入授权。失败／中断结算属于控制操作，可按同一 token 处理已取消或总期限过期任务；业务调用结算与 checkpoint 写入继续要求有效租约和期限。AlreadyApplied 仅核实已有相同结果；恢复仍检查真实图记录和回执。

最终验收：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55455`。
报告：`.data/postgres-integration/rap-test-c0941cad2f5d/`。
结果：422 通过，0 失败、0 错误、0 跳过；原有 33 条依赖警告不变。doctor 与三个实验数据库专项通过；相关模块 compileall、git diff --check 通过。专用容器已清理，未操作个人数据库，无新增 schema 迁移。契约说明见 [execution-contract.md](execution-contract.md)。

## C01–C03：恢复兼容、有限事务重试与提交未知核查

2026-10-13 复核确认 Manifest 与生产 CallContext 的稳定调用身份已有部分保护，但恢复检查不完整、明确回滚后没有统一事务重试、提交未知缺少统一的身份与核查结果。本轮新增 Manifest schema 4 的完整恢复契约、`TransactionRolledBack` 有限重试和 OperationIdentity／Resolution 核查。认领与执行入口均拒绝不兼容状态；输入准备和外部请求不进入事务重试；预留未知不发送请求，结算和最终发布核实已提交数据后可确认成功。

新增 46 项验证，包括七类恢复版本／schema 不兼容、直接执行入口与 Worker 缓存后的实现变更、工具 schema／描述／依赖／源代码变化、真实数据库死锁和 REPEATABLE READ 写冲突、事务中途及 COMMIT 阶段确认回滚、稳定调用 ID 与预算、三次总尝试及嵌套限制、随机等待与期限／取消、不重试连接和唯一约束错误、真实提交后响应丢失、实际回滚后的未知响应、模型仅执行一次、最终结果仅发布一次、核查不可用、核查等待在途提交、跨 Run 身份冲突及不完整恢复描述。数据库竞争使用锁与事件屏障。

最终验收：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55459`。
报告：`.data/postgres-integration/rap-test-8590f76093b0/`。
结果：468 通过，0 失败、0 错误、0 跳过；原有 33 条依赖警告不变。doctor、数据库角色权限、三条迁移链和三个实验数据库专项通过；专用容器已清理，未操作个人数据库。相关模块编译与 git diff --check 通过。

本轮新增 `agent_personal_0003` 增量迁移，仅扩展 Manifest schema 约束；个人数据库仍需在停止旧 API／Worker并备份后执行 migrate、bootstrap 和 doctor。旧 Manifest／checkpoint 不转换、不伪造回执，已有历史结果保留。部署规则和实现边界见 [恢复与事务说明](recovery-and-transactions.md)。


## C05、C07–C10：工具隔离、压力、接纳、公平与进展

2026-10-13 完成工具状态／参数隔离、确定性合并、Run／Worker 工具名额及批次限制；数据库共享令牌桶、429 分类、冷却、单次探测与速率恢复；三类任务和在线搜索接纳、会话 Memory 预留、multipart 前上传接纳；数据库交替认领；可信阶段记录、等待原因、无进展中断与去重告警。SDK 自动重试保持关闭，提交未知不发送请求，物理工作继续占据实际名额。

新增 55 项真实 PostgreSQL／受控 HTTP 专项，涵盖跨进程共享配额、并发容量竞争、幂等重放、Memory 名额转换与回滚、冷却头和迟到 429、单次探测、提交未知、SDK 429、搜索取消、公平游标回滚、工具私有状态与图顺序、线程取消后容量、工具等待者取消、调用归属、进展优先级／重复事件、实际 CPU 超时终结、上传拒收前不读取 body、角色隔离、配置保留冷却、正常配额等待不错误降级、CPU 命名信号量回收、排队原因说明及 CPU_POOL_RESET 公共响应。

全量验收：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55467`。
报告：`.data/postgres-integration/rap-test-756d066e2b70/`。
结果：523 通过，0 失败、0 错误、0 跳过；原有 33 条依赖警告不变。doctor、数据库角色隔离及三个实验数据库专项通过。专用容器已清理，没有访问真实模型或个人数据库。

新增 migration head 为 execution_0002、agent_personal_0004；RAG 仍为 rag_personal_0001。新图 v3，Manifest schema 4，协议 2。个人数据库尚未升级；发布需要停止旧 API／Worker、备份、migrate、bootstrap、flow_control --configure 和 doctor。规则见 [流量与进展控制](flow-control.md)。


默认额度正式冒烟：`.data/postgres-integration/rap-flow-smoke-28fd6844eb37/services/summary.json`。两个正式 API、两个 Worker 和本机合成模型完成文档 active、ReAct／Plan 各一个有效引用、Memory 提取及摘要；累计 30 条调用回执。runs／turns／documents／search 四种接纳能力均正常，两个 Worker 以 0 退出；API 完成应用关闭后按 SIGTERM 约定退出。服务日志无 ERROR、Traceback、resource_tracker 泄漏、未 await coroutine 或清理超期。专用容器已清理。

实际冒烟补齐了正常速率等待与故障冷却的区分，以及 CPU 管理线程对命名信号量的引用回收。编译与 git diff --check 通过。告警仅记录在本地和数据库，没有外部通知发送。

## C14–C17 目标、证据与工具验收

2026-10-13 完成独立目标与步骤验收、证据语义检查、工具意图和显式副作用授权，以及结构化摘要和条件传播。检查使用既有调用协调器与资源，默认 16 次模型预算中保留两次用于最终生成和验收；验收不通过直接 partial，无自动修正。

最终全量运行：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55475`。

报告：`.data/postgres-integration/rap-test-485295843a16/summary.json`，561 通过，失败、错误、跳过均为 0，三个实验数据库专项通过。doctor 验证迁移 head 为 execution_0002、rag_personal_0001、agent_personal_0005，角色权限及任务隔离通过。真实 HTTP transport 禁用，无真实模型调用。

新增测试覆盖模型说完成但独立验收失败、目标遗漏、条件丢失、来源真实但语义不支持、正文绕过结论列表、实验模拟/单位/数值/因果限制、明确授权与意图分别检查、授权次数竞争和稳定 ID、提交未知不得变成 partial、真实 checkpoint 回执，以及持久判断后的恢复不重复调用。

正式入口冒烟：`.data/postgres-integration/rap-flow-smoke-1129358d91ae/services/summary.json`。默认下游限流配置下，两个 API 和两个 Worker 完成文档处理、ReAct、Plan、会话事实提取和摘要；两种 Agent 各有有效引用，生成 108 条实际图调用回执。关闭日志没有资源泄漏或异常，两次验证的专用容器均已清理。

compileall 与 git diff --check 通过。仅验证了专用数据库，没有对当前个人数据库执行迁移、停止服务或部署；现有个人实例的发布步骤见 [acceptance.md](acceptance.md)。语义判断属于模型辅助检查，不承诺绝对正确。

## C18、C19、C20、C21、C22、C24：上下文、保留与 token 计量

2026-10-14 完成完整请求输入预检、模型输出上限、任务 token 账本及有限跨服务配额；证据目录与按需正文加载、每步骤一次受预算约束的摘要、分组最终证据核验；不可变图正文、恢复依赖校验与永久核验 pin；私有请求／响应归档和脱敏；登记产物的隔离回收与窄权限维护角色；检索阶段及容量／限速／连接／执行器等待计时。不增加价格或货币费用功能。

最终验收：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55487`。

报告：`.data/postgres-integration/rap-test-79728546c072/summary.json`。584 通过，失败、错误、跳过均为 0，三个实验数据库专项通过。doctor 验证 migration head 为 execution_0004、rag_personal_0002、agent_personal_0006，角色与 RLS 通过。保留 43 条同类 Starlette／Pydantic 依赖提示，无未 await coroutine、未消费任务异常或资源泄漏提示。真实 HTTP transport 在全量测试中禁用。

新增 23 项测试覆盖：完整 schema／工具输入与输出上限、并发 token 竞争、稳定 ID 不重发、未派发释放和实际 usage 幂等、真实 SDK 请求归档、敏感字段与嵌套 JSON 脱敏、超长请求不发送、真实 checkpoint 回执和正文损坏、活动任务与永久依据保护、隔离后延迟删除、保留 hold、必需证据不可截断、摘要仅一次且需真实回执、分组核验关联全部实际调用与防篡改、提交确认丢失不派发、本地 tokenizer 指纹变化拒绝加载、累计体积告警、跨角色配额与失败状态核实、过期配额与超估阻止发送、实际文件锁与版本引用保护、归档改名／索引中断回收、限流前拒绝释放配额、畸形搜索响应仅查询状态不重发，以及 token 耗尽后 FTS 降级。检索计时在既有候选 trace 测试内增加实际连接等待和 SQL／阶段断言。

正式服务冒烟：`.data/postgres-integration/rap-flow-smoke-51d23006b9e1/services/summary.json`。默认下游额度下，两个 API、两个 Worker 和本机合成模型完成文档 active、ReAct 与 Plan 各一个有效引用、Memory 提取与摘要；108 条实际调用回执。Worker 均以 0 退出，API 完成正常应用关闭后按 SIGTERM 约定退出；没有 ERROR、Traceback、资源泄漏或清理超期。

增量迁移专项：`.data/postgres-integration/rap-upgrade-bc5c175153a9/summary.json`。先初始化旧 head execution_0002／rag_personal_0001／agent_personal_0005，写入历史答案、引用、Manifest 和四类任务，再升级当前 head。历史答案及引用原文、旧 Manifest 和排队期限均不变；仅未开始且无 Manifest 的任务升级 v5。checkpoint 初始化、bootstrap、doctor 和维护角色独立登录及有限回收权限均通过。

所有验证使用独立 PostgreSQL／pgvector 容器并在结束后清理，没有访问真实模型或个人数据库。compileall 与 git diff --check 通过。新图 v5、状态 v3、提示 v4、Manifest schema 6；个人实例仍需按 [上下文与保留协议](context-and-retention.md) 停止旧执行链、备份和增量迁移。维护服务模板已提供，未在个人实例安装或启动。
