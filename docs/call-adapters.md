# 第四阶段：调用适配与业务执行边界

第四阶段在第三阶段的异步执行链上收敛调用逻辑，当时不新增迁移。后续修复将当前图版本提升为 v5、Manifest schema 为 6；ToolOutcome 契约 1、恢复协议 2 保留。当前发布包含增量迁移，见 [上下文与保留协议](context-and-retention.md)；继续使用当前个人数据库与文件目录。

## 调用协调

`execution.calls.CallRunner` 接收 CallContext、预留、调用、结算及业务错误分类函数。调用 ID 在事务前生成，预留确认后才发送请求。资源排队之后由 HTTP transport 或执行器的 before_submit 回调再次校验执行权。原生异步请求直接等待；明确的同步 I/O 使用有界 IO 池，CPU 函数使用共享 CPU 池。

`AgentCallStore` 是资源注入的显式调用账本接口。已存在的预留返回 AlreadyApplied；它证明预留存在，不能证明请求尚未发送。CallRunner 遇到它交回控制层核实，禁止重放。结算在请求执行的异常捕获范围之外：提交未知、结算冲突或结果合并失败不能触发第二次结算。

AttemptContext 持有任务、token、停止信号、总期限与子任务跟踪。同一身份的嵌套入口沿用原上下文，不能替换停止信号或期限。框架并发调用任务在第一次等待前登记；Agent 入口结束时取消并等待仍存活的调用任务。清理超期请求所属进程退出。

## 期限与控制

单次调用期限为 attempt、上层调用和当前配置期限的小值，覆盖预留等待、容量排队、请求、响应读取与验证。子操作只继承或缩短它。调用失败后的结算退出当前 call_scope，使用剩余 attempt 时间和数据库 profile 限制；总期限／租约已失效时由持久控制结算处理。

RAG 的 X-Request-Budget-Seconds 在 HTTP 容量接纳及授权后按真实剩余时间写入。SDK transport 设置请求局部 timeout；流式响应完整读取和关闭保留原期限与容量归属。取消不能提前释放仍在执行的线程或 CPU Future。

ExecutionRevoked、取消、数据库不可用、PersistenceOutcomeUnknown、WorkStopped、ResourceClosed 和要求进程退出的信号不进入业务降级。SDK 包装这些异常时，沿显式 cause 恢复原控制信号。普通文件 PermissionError 是业务错误，不是执行权拒绝。CPU 整池中断保持 CPU_POOL_RESET；资源层不重试，业务任务按已有持久策略处理。

## 模型与工具

原生 ChatOpenAI／AsyncOpenAI 复用资源 HTTP transport，max_retries=0，同步入口拒绝使用。结构化响应显式解析校验并保留 usage。自定义模型通过 register_async_model(model_type, async_structured_adapter) 注册，适配器返回 ProviderReply；聊天入口还要求实现原生 _agenerate，不能使用 BaseChatModel 的线程回退。

模型中间件和 Planner／最终生成复用 model_calls；Memory 使用相同传输和结构化适配，但不消耗 Agent Run 的预算。Provider 只返回结果，调用标记在持久结算确认后添加。Saver 继续负责图记录与回执的同事务提交。

同步工具可通过 managed_io_tool(function, name=..., description=..., args_schema=...) 包装；function 接收冻结的 ToolExecutionContext 和普通参数字典，返回 content 与 ToolOutcome artifact。同步 SDK 的自身超时、线程安全与资源归属仍须显式满足资源契约。同步工具未经包装在 Provider 校验时拒绝。

实验工具的顶层计算函数只接收方法名、普通参数和稳定快照，不创建 Provider、Tool 或 Runtime。快照准备与复制经过受控执行器；方法版本、证据来源、分页与大小限制不变。

工具结果先验证并结算，再校验执行权并合并。共享锁内只进行同步冲突检查和数据替换；合并期间也检查保守换算的租约与总期限。Run 级证据冲突不会改写已经完成的调用。触发模型关联从实际 AIMessage 恢复，不使用共享“最近调用”状态。

## 三条业务链与监督

Agent 图只组织 Planner、步骤、ReAct 和最终答案。runner 管理输入、上下文、partial 与持久发布。预算耗尽使用当前及持久图中的有效摘要和证据；最终生成只有一条路径，发布未知不再次生成。

Memory 和 RAG 共用 Embedding 响应验证：索引完整且唯一，恢复输入顺序，维度一致／匹配要求，数值有限并拒绝布尔值。Memory 可选事实检索只在明确 ProviderFailure 时降级，控制、数据库错误和实现错误向上交付。

RAG 改写／Embedding 与分词并发准备，随后在同一连接的只读 REPEATABLE READ 事务中读取索引版本、依次执行 Dense／FTS，保证两路使用同一数据快照。每路 SQL 使用保存点隔离可降级错误；取消和连接丢失仍中止整个读取。准备期间切换索引时，同模型同维度的向量可复用；模型或维度改变则降级为 FTS，舍弃旧向量。重排和审计在事务外执行。TaskGroup 错误选择优先识别控制，再选择不可重试业务错误。文档 Embedding 使用批次 16、最多四个消费任务，输出顺序保持。产物仍由持久层原子发布。

pipelines 负责三类业务入口、异常分类与持久最终处理，返回 AttemptOutcome。Worker 只认领、启动、监督、续租、核实、draining 和关闭，不识别模型或解析异常，也不直接写执行 SQL。FINISHED 表示已经持久完成；RECONCILE／UNKNOWN 交由持久结束接口检查真实状态及恢复条件。

Agent Worker 启动时通过受控 IO 准备 Provider、配置和 Manifest 模板；认领事务只插入已准备数据。独立认领入口也在事务前准备 Manifest。恢复要求模型、图、提示和工具身份匹配；不兼容任务按 INTERRUPTED_UNKNOWN 结算，termination_reason=configuration_mismatch。密钥不写入 Manifest。

文档 CPU 工作只使用本地模型缓存：tokenizer 启用 local_files_only，CPU 子进程设置 HF／Transformers 离线模式。PDF 转换向 Docling 显式传入本地 artifacts，保持 OCR、表格和布局处理；沿用 DOCLING_ARTIFACTS_PATH，未配置时使用 Docling cache_dir/models。模型应在部署准备阶段下载到本地，缓存缺失时任务明确失败，不在计算进程内下载。
