# 第三阶段：异步持久执行层与完整执行链

当前 API、Agent、Memory、文档处理和在线检索使用原生异步接口。每个进程／事件循环有一套 RuntimeResources；Run、Memory 和文档任务是本地 asyncio Task，不再创建独立任务进程。只有资源层的共享 CPU 池使用 spawn 进程。

## 持久接口和事务

`execution.stores` 提供资源注入的 TaskStore、AgentStore、MemoryStore、DocumentStore；`execution.store` 中显式接收 conn 的函数是事务内原语。API 的 ASGI binding 和 Worker attempt 绑定所属资源，不建立同步／异步桥接。业务 SQL 位于 storage 与 execution 持久模块；网络、文件、解析和序列化位于事务外。

control、normal、bulk 保留独立连接池和 profile 超时；操作期限同时受 attempt 单调时钟期限约束。事务取得任务锁后用数据库实际时间复查 token、状态、停止、租约和总期限，提交前再次检查。最终发布同时提交答案、claim、引用、摘要任务与终态。Memory 产物与终态、文档索引与版本切换及终态也分别原子提交。

Agent 认领先取得全局事务锁，清理过期任务，再判断全局并发。默认两个 Run；Memory 默认一个槽位，文档默认一个槽位（`RAG_MAX_CONCURRENT_DOCUMENTS`）。首次认领固定期限，恢复保留期限、预算和输入。认领往返的等待从本地期限中扣除。

调用预留接受稳定 call ID；结算和结果发布区分 Applied、AlreadyApplied、Rejected。重复结算必须内容一致。COMMIT 取消／断连通过 PersistenceOutcomeUnknown 交付，持久层不自动重试。预留未知时不发送外部调用；控制器查询调用与任务状态后结算。最终发布未知时核实已有终态，禁止重新生成答案。操作身份在提交前确定，预算不退款。

## 真实图记录与恢复

新 Run 使用 recovery_protocol=2、graph_version=v5、Manifest schema 6；ToolOutcome 和 execution.tasks 的基础契约仍为 1。Manifest 的恢复契约覆盖实现、实际 schema 和依赖版本，恢复认领与执行入口均验证兼容性；缺失契约的旧状态保留历史查询但拒绝执行。模型消息、ToolMessage 与 Planner 内部状态携带执行层生成的调用标记；标记不包含 token，不进入模型文本、提示或最终答案。

ProtectedSaver 使用异步池短事务，覆盖 checkpoint、blob、pending write。序列化和反序列化使用受控 IO 执行器：先序列化再借连接，先完成读取再反序列化。线程不持有数据库连接，禁止 Saver 同步接口和运行时删除。写入只接受对应 Run 或其实际存在的步骤线程，并等待持久化提交后继续图执行。

实际图记录与不可修改的 call_checkpoint_receipts 在同一事务提交；已有 blob／write 冲突必须核对真正保存的字节。回执核对调用、Run／步骤线程、持久记录和 SHA256。pending write 必须位于当前可恢复 checkpoint；完整 checkpoint 必须位于当前线程的祖先链。恢复时任何 started 调用、缺少有效回执或被破坏的图记录都阻止恢复。成功节点的 pending write 可复用；图外 final 调用没有独立结果缓存，发布前中断仍禁止重放。

旧 checkpointed 只作历史显示；协议 2 在回执事务内同步该字段。队列诊断和 doctor 使用同一回执摘要规则，不能用布尔值制造恢复证明。时间线的调用 details 展示安全的回执关联和恢复协议，不展示 token 或持久消息正文。

## 模型、工具和任务执行

模型通过资源层 SDK facade 复用 HTTP 客户端和容量，禁用同步客户端及自动重试。容量等待后、发送前再次校验执行权；请求期限覆盖响应读取和流式关闭。Structured 输出使用明确的原生异步调用和验证，禁止默认线程 Runnable 回退。工具必须声明原生 coroutine 或显式的受控执行器适配。

实验快照登记与分析、文档解析／切块／索引文本准备使用 CPU 顶层函数，参数为普通数据与受控路径。Embedding 批次 16，最多四批同时执行并恢复原顺序。文档 bulk 末尾才锁任务，控制协程可在准备和插入期间续租。控制结算仅改变任务；文档失败状态由任务视图派生，后续版本创建在文档锁内同步失败元数据，避免反向锁顺序。

在线检索在事务外并发准备改写／Embedding 和原始查询的分词。在同一连接上的只读 REPEATABLE READ 事务中重新读取索引版本、依次执行 Dense 和 FTS；两路查询及文档可见性共享同一快照，单路可降级 SQL 错误由保存点隔离。索引切换若改变模型或维度，旧向量被舍弃并降级为 FTS。随后在事务外融合、重排和写审计。`RAG_SEARCH_TIMEOUT_SECONDS` 默认 120；认证服务请求可以传正有限的 `X-Request-Budget-Seconds`，服务端取较小值。该头仅表示预算。

## 取消和关闭

attempt 以 TaskRef+token 区分。取消／失权后停止接纳结果，取消协程并等待清理；底层线程／CPU Future 未结束时保留 draining 句柄与容量。一个 Run 的失败不传播到其他 Run。IO 超过清理宽限请求整个所属进程退出；CPU 保留第二阶段整池回收策略并明确交付 CpuPoolReset，不由资源层重试。

首次退出请求建立一份 WORKER_EXIT_GRACE_SECONDS（默认 30 秒）期限。停止认领、取消、持久核实／结算和资源关闭共同消耗这份期限。quiesce 后仅允许受限 control 清理事务，禁止预算预留、任务创建和外部调用。数据库不可用时保留租约供接管，不报告虚假的结算成功。API 与两个 Worker 共用顶层 ExitWatchdog；失控线程不会阻塞解释器永久退出。

## 从第二阶段个人库升级

1. 停止接收新任务，排空执行链并停止旧 API／Worker；确认原任务进程均退出。
2. 备份当前个人数据库和资料目录，保留对应第二阶段代码。
3. 运行 `.venv/bin/python -m scripts.prepare_async_upgrade`。停止请求优先，其次总期限，剩余已开始的协议 1 Agent 任务按未知中断结算。脚本拒绝仍在线的 Worker；必要时等待 90 秒的 heartbeat 有效期。
4. 依次运行 `db.migrate`、`agent_service.checkpoints`、`db.bootstrap`、`db.doctor --json`。新增 agent_personal_0002；未开始的 Agent 队列升级协议并保留排队期限。历史终态、Manifest 和 checkpoint 不改写、不伪造回执。
5. 使用 `scripts.serve` 和两个 Worker 原入口启动新版本，运行合成冒烟后恢复提交。

禁止新旧 Worker 混跑。历史终态仍可查询，旧 checkpoint 不提供执行兼容。回退使用第二阶段代码和升级前数据库／资料备份，禁止对新库运行旧代码。

## 第四阶段调用适配收敛

当前 CallRunner 统一预留、期限、调用和结算；业务图与持久最终处理分开。Memory／RAG 共用严格 Embedding 验证，Worker 使用 AttemptOutcome 监督三类业务。完整接口与控制边界见 [调用适配说明](call-adapters.md)。没有新增数据库迁移；继续使用协议 2、图 v2 和原个人数据库。

后续 C01–C03 修复新增 `agent_personal_0003`：Manifest schema 升为 4，明确回滚后的完整数据库操作最多尝试三次，提交未知通过稳定操作 ID 和锁后持久记录核查。详见 [恢复与事务说明](recovery-and-transactions.md)，其中包含现有个人库的增量发布步骤。

后续 C05、C07–C10 修复新增 `execution_0002` 和 `agent_personal_0004`，当时图版本提升为 v3。工具隔离及名额、数据库共享下游配额、原子接纳、Memory 派生名额预留、公平认领和阶段进展中断统一接入执行层。当前配置、接口及增量发布步骤见 [流量与进展控制](flow-control.md)。

C14–C17 新增 `agent_personal_0005`，当时图 v4、状态 v2、Manifest schema 5。目标、步骤与最终结果采用独立验收；语义失败直接部分发布，工具动作要求意图关联，副作用另需显式请求授权。当前规则见 [验收说明](acceptance.md)。

C18–C24 新增 `execution_0003`／`execution_0004`、`rag_personal_0002`、`agent_personal_0006`，当前图 v5、状态 v3、提示 v4、Manifest schema 6。输入预检、任务 token 账本、调用归档、外置正文和有限维护权限见 [上下文与保留协议](context-and-retention.md)，包含最新个人库发布顺序。
