# Personal execution contract v1

This release is one personal instance. It has no user, team, credential or ACL dimension. Business APIs are unauthenticated and restricted to actual loopback peers, loopback Host and (when present) loopback Origin. Forwarded headers are ignored. Agent evidence requests require the independent RAG service token and Run/call correlation headers.

`execution.tasks` owns every execution state. Agent, Memory and document business tables link one-to-one through `(id, kind)` and contain input/product data only. RLS restricts the two runtime roles by task kind. Public mappings are `stopping → cancelling` for Agent, `queued → pending` and `completed → done` for Memory, and previously attempted queued documents → `retry`.

A write or external call requires the correct task kind and ID, matching token, running status, unexpired lease and unexpired total deadline. The task is locked in the write transaction using database `clock_timestamp()`, then checked again immediately before finishing the transaction. A heartbeat cannot resurrect an expired lease. Long RAG bulk writes lock the task at the end to let the parent renew during preparation. Checkpoint, blob and intermediate writes use the same protected connection/transaction.


Priority: existing terminal state; mismatched token; submitted cancellation; total deadline; ordinary failure/recovery. A displaced executor never changes its successor. Reservations are not refunded. Unknown external results are never replayed. Agent recovery requires all calls settled and explicitly checkpointed; final generation stays outside the graph.

The first claim fixes the policy duration and deadline; waits and calls count against it. Agent queues for 60 seconds and executes for 300 seconds. Memory executes for 900 seconds with three claims and immediate retry. Documents execute for 14400 seconds with three claims and a ten-second retry delay. Calls use `min(configured timeout, remaining time)` before reserving or sending.

Tools receive frozen execution context and stable frozen inputs. They return model-visible content plus serializable v1 `ToolOutcome` artifacts. The execution layer validates outcomes, settles persistent calls, rechecks authority, then merges evidence and notices. Conflicting content for an existing evidence ID fails; notice codes preserve the first message. Late results are discarded.

The Worker owns clients and actual work capacity; an attempt owns its token, deadline and stop signal; a call owns its trace and budget reservation. Logical cancellation does not release physical capacity. The shared WorkHandle records that distinction. Synchronous processes remain in this phase. Cleanup grace defaults to five seconds and Worker exit grace to thirty seconds. A process that cannot stop must not free its slot. A future thread executor must stop claiming and exit if underlying work cannot terminate.

Initialization order: execution → rag → agent, then owner-created checkpoint tables and runtime grants. Only an empty database or the matching personal revision chains are accepted. Old databases are rejected before mutation. Historical databases and file directories remain separate archives; rollback requires the old code and its own old database/files. This release does not upgrade or downgrade historical business data.
