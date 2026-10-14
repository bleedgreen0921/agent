# Personal execution contract v1

This release is one personal instance. It has no user, team, credential or ACL dimension. Business APIs are unauthenticated and restricted to actual loopback peers, loopback Host and (when present) loopback Origin. Forwarded headers are ignored. Agent evidence requests require the independent RAG service token and Run/call correlation headers.

`execution.tasks` owns every execution state. Agent, Memory and document business tables link one-to-one through `(id, kind)` and contain input/product data only. RLS restricts the two runtime roles by task kind. Public mappings are `stopping → cancelling` for Agent, `queued → pending` and `completed → done` for Memory, and previously attempted queued documents → `retry`.

A write or external call requires the correct task kind and ID, matching token, running status, unexpired lease and unexpired total deadline. The task is locked in the write transaction using database `clock_timestamp()`, then checked again immediately before finishing the transaction. A heartbeat cannot resurrect an expired lease. Long RAG bulk writes lock the task at the end to let the parent renew during preparation. Checkpoint, blob and intermediate writes use the same protected connection/transaction.


Priority: existing terminal state; mismatched token; submitted cancellation; total deadline; ordinary failure/recovery. A displaced executor never changes its successor. Reservations are not refunded. Unknown external results are never replayed. Agent recovery requires all calls settled and explicitly checkpointed; final generation stays outside the graph.

The first claim fixes the policy duration and deadline; waits and calls count against it. Agent queues for 60 seconds and executes for 300 seconds. Memory executes for 900 seconds with three claims and immediate retry. Documents execute for 14400 seconds with three claims and a ten-second retry delay. Calls use `min(configured timeout, remaining time)` before reserving or sending.

Tools receive frozen execution context and stable frozen inputs. They return model-visible content plus serializable v1 `ToolOutcome` artifacts. The execution layer validates outcomes, settles persistent calls, rechecks authority, then merges evidence and notices. Conflicting content for an existing evidence ID fails; notice codes preserve the first message. Late results are discarded.

The Worker owns clients and actual work capacity; an attempt owns its token, deadline and stop signal; a call owns its trace and budget reservation. Logical cancellation does not release physical capacity. The shared WorkHandle records that distinction. Synchronous processes remain in this phase. Cleanup grace defaults to five seconds and Worker exit grace to thirty seconds. A process that cannot stop must not free its slot. A future thread executor must stop claiming and exit if underlying work cannot terminate.

Initialization order: execution → rag → agent, then owner-created checkpoint tables and runtime grants. Only an empty database or the matching personal revision chains are accepted. Old databases are rejected before mutation. Historical databases and file directories remain separate archives; rollback requires the old code and its own old database/files. This release does not upgrade or downgrade historical business data.

## 第二阶段资源契约

异步基础设施定义于 `execution/infra/`，完整接口、配置和关闭约束见 [异步资源说明](async-resources.md)。`WorkRegistry` 保留同步 Worker 的 register/release/occupied 接口，并使用线程锁登记队列、物理 Future、逻辑停止与 generation。逻辑取消不释放正在运行的物理容量，结果停止与完成回调竞争只能完成/释放一次。

`RuntimeResources` 只管理进程内工作和资源，不代替此文定义的数据库执行权、预算、checkpoint 保护或持久结算。执行契约版本保持 1；`WorkOwner`、`OperationDeadline`、`WorkRecord` 仅是基础设施本地类型。CPU 整池中断明确交付 `CpuPoolReset`；后续业务迁移必须自行按持久策略结算，不得由资源层自动重试。
