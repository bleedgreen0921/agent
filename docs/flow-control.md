# 工具并发、下游压力、接纳与执行进展

C05、C07–C10 在公共异步执行层实现。Worker 仍以 asyncio Task 调度业务，线程和共享 CPU 池只承担声明的阻塞与计算工作。新增控制不重置 Run 总期限、调用预算或恢复身份，也不自动重放外部请求。

## 工具边界

每个调用取得独立的工具上下文、参数和图状态副本，运行时不暴露图存储／写入通道。Provider 禁止 InjectedState、InjectedStore 和 Command；返回内容与 ToolOutcome artifact 必须一致。同步工具继续经过受控执行器。

默认每 Run 两个工具、每 Worker 四个工具，等待队列十六个；单个模型输出最多十个工具调用。分别使用 AGENT_TOOL_CONCURRENCY、AGENT_TOOL_WORKER_CONCURRENCY、AGENT_TOOL_PENDING、AGENT_TOOL_BATCH_LIMIT 配置。Worker 的工具策略写入 Manifest 并在 Run 上冻结。模型的大批次在框架展开工具协程之前拒绝；等待名额时取消不会预留工具调用预算。

结果按模型消息在图中的顺序、工具索引稳定合并，不按完成顺序选择 rank 或通知内容。内容冲突仍失败；合并时再次校验执行权，不能重新结算已经成功的调用。排队协程也属于 attempt 的取消集合。默认 work_owner() 继承当前 CallContext ID；线程／CPU Future 尚未结束时工具名额仍被占用，清理超期沿用原退出或 CPU 回收规则。

## 下游压力

普通 HTTP 和 OpenAI SDK 都经过 DownstreamGovernor；SDK 自动重试仍为零。默认按规范化 origin 共享数据库令牌桶，每秒一个请求、突发两次。增加 API／Worker 进程不会增加同一组额度。

DOWNSTREAM_REQUEST_RATE 和 DOWNSTREAM_REQUEST_BURST 设置默认额度。DOWNSTREAM_GROUPS 是逻辑服务到组名的 JSON 映射，DOWNSTREAM_LIMITS 是组名到 rate／burst 的 JSON 映射。例如：

```json
{"agent-model":"local-model","embedding":"local-model"}
```

```json
{"local-model":{"rate":1,"burst":2}}
```

逻辑服务为 agent-model、embedding、rewrite、rerank、rag。同一服务的不同地址或共享账号的多个服务需要用显式组名合并；默认 origin 分组不推断 DNS 别名或供应商账号关系。配置必须在相关进程间一致；启动注册及发送路径核对 policy hash。配置不一致拒绝执行，不采用最后写入者的配置。

许可在短事务中取得，发送前复查执行权。等待速率、冷却或探测时释放本地 HTTP 容量；等待耗用原请求／任务期限。许可提交未知时不发送，不退款、不自动再次取得许可。请求数限制不等同于精确 token 配额。

429 分别记录 MODEL_RATE_LIMITED、EMBEDDING_RATE_LIMITED 等原因，和 Run 的 QUOTA_EXCEEDED 分开。支持 Retry-After 秒数及 HTTP 日期；缺失、非法或已过期时采用 5 秒起步、最高 60 秒的指数冷却并加只延长等待的抖动。并行的迟到 429 可以延长冷却，旧成功不能清除新的失败代次。

连续三次连接／超时／5xx 进入冷却；401／403 单独识别。冷却结束只允许一个实际请求探测，不额外发送收费模型请求。429 将有效发送速率减半，最低配置额度的八分之一；连续十次成功增加配置额度的十分之一，直到恢复上限。输入错误不作为瞬时失败重试，恢复探测的明确输入拒绝可以证明传输已恢复。

在线 RAG 的可降级模型分支遇到已知冷却或探测等待时直接降级；正常配额等待仍使用原请求预算，审计保留具体原因。Dense／FTS 仍使用同一只读 REPEATABLE READ 快照。Memory／文档保留原有有限任务重试，但下一次可用时间不早于相关下游冷却结束，原总期限不变。自定义 Provider 的网络调用必须使用资源层 HTTP／SDK 适配，才能获得这些保证。

## 接纳与公平认领

新任务创建与容量／速率判断同事务完成。已存在的幂等提交直接重放，不重新收费，也不因当前队列已满而拒绝。queued、running、stopping 均计入未完成任务；过期但尚未结算的记录也计入。

| 入口 | 默认速率／突发 | 排队／未完成上限 | 配置前缀 |
| --- | --- | --- | --- |
| Run、会话轮次 | 10／分钟，3 | 8／10 | AGENT |
| 文档、新版本 | 6／分钟，2 | 8／10 | RAG_DOCUMENT |
| 在线搜索 | 2／秒，4 | 全局在途 8 | RAG_SEARCH |
| Memory 派生任务 | 来源事务接纳 | 未完成加预留共 32 | MEMORY |

前三类前缀支持 _SUBMIT_RATE（每秒）、_SUBMIT_BURST、_QUEUE_CAPACITY、_UNFINISHED_CAPACITY。Memory 使用 MEMORY_UNFINISHED_CAPACITY。搜索不建立持久任务队列，_QUEUE_CAPACITY 不表示额外搜索等待名额。

会话提交预留两个 Memory 名额，一个转换为提取任务，一个留给摘要；完成发布时转换摘要名额，失败或取消释放未用名额。最终结果不会因为新增摘要任务遇到队列上限而回滚。接纳不执行清扫，不反向锁定已有任务。

上传在 multipart 解析／落盘前取得全局两个逻辑请求名额，总期限 120 秒；此处仅限制在途上传，正式新文档／新版本提交才消费提交速率。拒收不读取 body。直接内部上传接口也取得名额；API 不重复取得。临时文件及提交未知沿用已有清理规则。

必要 Worker／下游持续不可用三十秒后，提交返回 503；短暂异常仍受容量限制。速率或容量超限返回 429，附 Retry-After。下游冷却结束允许一个试探性新任务，没有 Worker 则不允许；查看接纳状态不会消费试探资格。读取结果、取消和诊断不受这些提交限制。

认领先清扫，再判断下游冷却和执行槽位；暂停启动工作不阻止过期任务结算。各任务类型在数据库保存公平游标，两类均可认领时恢复／新任务 1:1 交替；另一类为空时不保留空槽。类内排序稳定，游标只随成功认领提交，回滚不消耗轮次。公平保证适用于当前可用且未被其他事务锁定的候选，不承诺固定等待秒数。

## 阶段进展与中断

持久阶段覆盖模型／工具、HTTP、受控 IO／CPU、数据库操作和 Memory 模型调用。每个并行活动独立记录开始、固定期限和完成事件；等待原因不刷新 progress_at、progress_seq 或期限。心跳和续租不算业务进展。

请求阶段沿用现有调用预算；文档解析新增 RAG_PARSE_TIMEOUT_SECONDS，默认 900 秒；SQL 沿用 control／normal／bulk 完整操作预算。没有登记在途活动的编排默认允许 PROGRESS_IDLE_SECONDS=60 秒。所有阶段仍受原任务总期限约束。

Worker 使用 PROGRESS_POLL_SECONDS=5 秒检查，80% 阶段预算产生预警，超期后在任务锁内核实当前 token、实际阶段、控制优先级和数据库时间。确认后先持久终结／撤销执行权，再取消本地工作。CPU 阶段真正超时不会按通用 MODEL_TIMEOUT 反复重试。

Agent 仍有 started 调用或无有效图回执时使用 INTERRUPTED_UNKNOWN，否则 EXECUTION_STALLED；原因记录为 progress_timeout。Memory／文档使用 EXECUTION_STALLED，阶段无进展不自动重试。用户取消、总期限、失租与接管保持原有优先级；已经发布的结果不被旧监测决定覆盖。数据库不可用时不声称已持久结算。

告警写入本地日志和去重记录；重复观察只更新 last_seen_at，恢复会关闭相应服务告警。没有外部邮件或消息发送。

## 状态与发布

Run／文档状态增加 progress，包含最后进展、阶段期限、等待原因和最近 Worker 观察到的 draining 信息。draining=null 表示没有新鲜观察；这些字段是观察，不授予执行权，也不提供未经验证的完成时间预测。Worker 状态和离线诊断包含两类队列、下游冷却、告警和资源占用。

/health 保持存活接口；/health/ready 保持基础服务检查。新增 /health/accepting 按 runs／turns 或 documents／search 报告接纳状态；一个服务部分能力可用时仍返回 200，全部拒收时返回 503。数据库启动时暂时不可用不阻止 /health，实际提交和外部发送仍须取得数据库授权。

Owner 可用以下命令查看压力并确认已处理的告警：

```bash
.venv/bin/python -m db.flow_control
.venv/bin/python -m db.flow_control --acknowledge SCOPE KEY CODE
```

新增 execution_0002 和 agent_personal_0004，图版本 v3；恢复协议 2、Manifest schema 4、状态 schema v1、ToolOutcome 契约 1。发布必须停止旧 API／Worker，备份个人数据库与资料目录，然后执行：

```bash
.venv/bin/python -m db.migrate
.venv/bin/python -m db.bootstrap
.venv/bin/python -m db.flow_control --configure
.venv/bin/python -m db.doctor --json
```

configure 要求没有在线 Worker、有效执行租约或在途请求；异常退出可能需要等待旧租约过期。它统一配置声明的策略和服务绑定，保留已有下游冷却，不通过重启绕开限流。变更共享额度或地址后，停止相关进程并再次 configure。

未开始且无 Manifest 的队列任务升级为 v3；已开始的旧图按恢复兼容规则结算，不转换旧 checkpoint。历史终态可读。旧、新 Worker 不混跑；回退使用匹配的代码及备份数据库。上述命令尚未在正在使用的个人数据库上执行。
