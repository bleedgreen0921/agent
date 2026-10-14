# 个人实例架构

Agent 和 RAG 是两个本机服务，共用一个个人数据库的 `execution` schema，各自使用 `agent` 与 `rag` 业务 schema。两个运行角色通过 task kind 的 RLS 和 schema 权限隔离；迁移 owner 单独初始化 checkpoint 和角色。实例没有业务身份维度。

`execution.tasks` 持有三类任务的状态、尝试、期限、租约、停止请求和 Worker 归属。`agent.agent_runs`、`agent.memory_jobs`、`rag.processing_jobs` 以带 kind 约束的一对一关联保存业务输入。业务状态视图只是查询适配，不保存第二份状态。

两个 Worker 使用 asyncio 调度任务，API 与 Worker 各自持有 RuntimeResources。Agent 保留数据库全局并发锁；每个 Run 是协程，Memory 默认一个消费槽位。模型、Embedding 和服务调用使用共享异步 HTTP；文件与序列化进入有界 IO 执行器；文档准备和实验分析进入可复用 CPU 池。RAG 在 bulk 末尾锁定任务，控制协程仍可续租。

TaskStore、AgentStore、MemoryStore、DocumentStore 统一持久操作。图记录与调用回执同事务提交，恢复核查真实 checkpoint／成功 pending write、线程归属和内容摘要。started 或缺少有效回执的调用禁止恢复；最终生成仍在图外。事实、摘要、文档索引和任务终态各自原子发布。

运行中写入统一锁定任务、检查 token、状态、租约与总期限，提交前再次检查数据库实时时钟。受保护 Saver 在 checkpoint 的实际写连接上执行该规则，覆盖主记录、blob 和中间写入。工具只得到只读上下文，内容与 artifact 经执行层结算、验证后才合并。

调用发送前检查完整输入与输出预留，并从持久 token 账本取得有限配额；在线 RAG 用签名子配额计入原 Run。图保存薄证据目录和不可变正文引用，恢复检查引用完整性。已发布依据永久保留，终态恢复状态及私有诊断归档按明确期限清理；维护进程使用独立窄权限角色。配置与边界见 [上下文与保留协议](context-and-retention.md)。

只读输入快照、Manifest、个人事实、原始轮次和引用产物不能被 runtime UPDATE/DELETE。检索审计通过 Run、工具调用、请求、检索与证据 ID 关联。RAG 搜索过滤已删除文档和非 active 版本，历史证据可以按 ID 读取；已发布引用独立保存。

支持入口固定绑定 loopback 并关闭 forwarded headers，应用同时校验来源、Host、Origin。业务免认证；Agent → RAG 使用独立服务 Token。

取消停止协程等待后，仍运行的线程／CPU 工作保留物理容量。Worker 保留 draining 句柄；IO 无法回收时停止接纳并退出，CPU 使用整池回收。正常停机共享一个退出期限，受限 control 清理通道用于结算和核实结果。详见 [第三阶段实现](async-execution.md) 和 [异步资源](async-resources.md)。
