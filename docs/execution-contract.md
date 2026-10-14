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


## 第三阶段持久执行协议

当前执行路径已全部异步化并移除每任务进程。基础 Task／ToolOutcome 契约仍为 1；新增 Agent 恢复协议 2，以实际图记录和同事务回执保护恢复，取代批量 checkpointed 确认。完整接口、期限、取消、未知提交和增量升级说明见 [异步执行层](async-execution.md)。

## 结算、确认与控制观察的边界

业务写入通过 `store.assert_active` 执行任务身份、token、状态、停止、租约和总期限检查；纯判断复用 `LeaseSnapshot.refusal`。写入先锁任务行，结算再锁调用行；取得锁后和写入结束前分别读取数据库实际时间。模型／工具结算不使用只读控制快照作为授权。

Saver 负责核对实际图输出与内部调用标记，将图记录和回执写入同一事务。回执确认原语 `receipts.confirm` 也在调用方事务内执行开始、结束两次任务执行权检查；不能因为原语脱离 Saver 而绕过授权。旧尝试调用的输出仍须有已有有效回执，才能由当前执行者携带到新 checkpoint。

`control_state` 和 Worker 控制循环通过同一个 `control_refusal` 判断观察结果。停止状态即使没有停止时间字段也判为取消；非 running 的活动观察不能返回 running。观察只决定本地停止／监督动作，后续持久写入必须重新锁定和校验。

失败与中断结算是控制操作：在任务锁内先拒绝旧 token 或非活动状态，再按取消、总期限和租约处理。同一 token 可以结算已请求的取消或总期限超时；单纯失租不允许业务失败覆盖当前状态。Worker 退出的受限控制结算可以处理当前 token 的过期尝试，不能修改接管者。

`AlreadyApplied` 只核实同一调用已经以相同内容结算。它不写入新结果，不更新完成时间，不赋予当前执行权；失权后的不同结算请求返回拒绝。恢复仍要求实际图记录及回执验证，不依赖布尔 checkpointed 字段。批量 `mark_checkpointed_calls` 已移除。
