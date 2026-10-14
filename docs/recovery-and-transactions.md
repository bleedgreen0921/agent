# 恢复兼容、事务重试与提交未知核查

本轮修复 C01–C03。原实现已有 Manifest、调用级回执和生产调用的稳定 CallContext ID，但缺少完整兼容检查、明确回滚后的统一重试，以及提交未知后的统一核查。以下规则适用于当前异步执行链；Worker 控制循环的数据库退避仍是独立机制。

## C01：恢复兼容与旧状态处理

当前新 Run 的 Manifest schema 为 6，恢复协议仍为 2、图版本为 v5，状态 schema 为 v3，ToolOutcome 契约仍为 1。`agent_service.startup.compatible` 要求模型、Agent 配置、工具声明及 recovery_contract 完整且一致；认领恢复和直接执行入口同时检查持久图版本、恢复协议和 Manifest schema。执行入口在输入准备、模型调用、图状态解码之前完成检查。新增正文引用的完整性、token 策略及保留规则见 [上下文与保留协议](context-and-retention.md)。

recovery_contract 的 SHA256 覆盖以下内容：

- Agent、execution、实验和 contracts 包的 Python 实现，排除迁移目录。
- 工具的实际调用 schema、描述和可定位的异步实现源文件。
- Plan、FinalDraft、Evidence 的实际 JSON Schema。
- LangGraph、LangChain、checkpoint、模型适配、Pydantic、OpenAI、HTTPX 和 psycopg 的安装版本。

只存储摘要及协议说明，不保存源代码、密钥或执行 token。Worker 可以缓存 Provider 和配置，但执行入口会重新计算实现指纹，缓存不能使发生变化的实现通过检查。版本字段缺失、摘要格式错误或没有实现源文件也会拒绝执行。

这里采用明确的旧状态政策：`legacy_policy=reject`、`state_migration=none`。旧 Manifest 和 checkpoint 保留供历史查询，不改写、不补造回执，也不尝试在新实现中解释旧状态。版本不兼容的已开始任务通过控制结算进入 `INTERRUPTED_UNKNOWN`，原因是 `configuration_mismatch`；取消和总期限仍遵守原有优先级。未开始的队列任务在首次认领时生成当前 Manifest。

指纹比较有意保守：没有语义变化的 Python 实现修改也可能阻止恢复。部署应固定代码和依赖，不支持运行期间热替换。无法定位源文件的动态 Provider 必须显式提升其声明版本；本地指纹不能识别远端服务在相同模型名称下的语义变化。若未来需要旧版本继续执行或状态转换，应另行提供版本专属执行器或经过验证的显式迁移，目前没有隐式转换路径。

## C02：仅在确认回滚后有限重试

资源层在事务上下文退出后处理 PostgreSQL 的 `40001`（serialization failure）和 `40P01`（deadlock）。只有连接返回 IDLE、回滚得到确认，才产生 `TransactionRolledBack`。包括 COMMIT 阶段返回的 serialization failure；回滚无法确认时仍为 `PersistenceOutcomeUnknown`。

`execution.retry` 对完整数据库操作重试，最多三次总尝试。默认两次等待采用完整随机抖动，分别位于 0–25ms、0–50ms；等待发生在事务、连接和锁释放之后。原 profile 的操作期限只建立一次，所有尝试和等待共用，同时受当前调用、Run 和退出期限约束。嵌套操作共用外层重试策略，不能将三次扩大为九次。

认领、授权观察、续租、调用预留和结算、受保护 Saver、产物发布及相关任务／业务存储使用该策略。事务内原语仍接收同一 conn，不自行开启重试或新事务。文件准备、模型调用和外部请求不放进重试操作；文档发布继续使用 bulk profile。

连接错误、唯一约束冲突、资源关闭、取消和提交未知不进入此策略。耗尽尝试或期限后交付原失败，由执行控制层处理，不重新获得完整预算。RAG 快照事务遇到 serialization failure／deadlock 时重试整个召回视图；可降级语句超时仍通过原保存点规则处理。

重试完整事务并重新执行其数据库决策，符合 [PostgreSQL 的事务重试说明](https://www.postgresql.org/docs/16/mvcc-serialization-failure-handling.html)。本项目比单纯按 SQLSTATE 重试多一项约束：资源层必须确认回滚。

## C03：稳定身份与提交未知核查

调用 ID 在进入预留事务之前分配；生产执行路径复用不可变 CallContext ID。结果 ID 同样在完整发布操作之前确定。明确回滚后的重试复用同一 ID，不重复扣预算。低层 `_reserve` 拒绝没有 ID 的操作。

`PersistenceOutcomeUnknown` 携带 OperationIdentity 和 Resolution。调用者应保留该身份；使用新 UUID 再提交代表新操作，不能作为原操作的重试。核查通过 control profile 的短事务先取得任务行锁，再读取数据库实际时间和调用／结果记录。这样会等待仍在提交的写入者，避免用锁前快照把在途提交误判为不存在。核查验证 Run、attempt token 和预期内容，不写业务状态。

| 核查状态 | 含义与处理 |
| --- | --- |
| APPLIED | 已有结算或最终结果与预期一致；返回 AlreadyApplied，失败结算返回成功确认 |
| RECORDED（recorded_no_dispatch） | 预留记录已存在；仅证明预算与调用登记，不证明外部请求是否发出 |
| NOT_APPLIED | 锁后未见预期写入且当前仍有执行权；保留未知异常，不自动重放 |
| REJECTED | 身份／内容冲突或当前状态不允许操作；不产生新写入 |
| UNRESOLVED | 核查连接、资源或期限不可用；保留未知异常，交给控制流程处理 |

预留提交未知时，无论核查是否发现记录，都不发送外部请求，也不自动再预留。结算和最终发布只有在实际数据与预期、终态一致时确认成功；发布核查读取已有结果，不能再次调用最终模型。AlreadyApplied 是历史操作已完成的确认，不赋予当前执行权。核查本身只在明确回滚时允许上述有限重试。

本轮没有新增完整调用结果缓存。调用恢复继续要求真实 checkpoint／成功 pending write 与回执；任何 started 调用或未经图确认的外部结果都不能自动重放。

## 发布到已有个人数据库

新增增量迁移 `agent_personal_0003`，只扩展 run_manifests 的 schema_version 约束以允许 4，保留历史记录。本次测试只使用专用隔离数据库，尚未升级正在使用的个人数据库。

1. 停止接收新任务，排空并停止 API 与 Worker，保留对应旧代码。
2. 备份当前个人数据库和资料目录。禁止新旧 Worker 混跑。
3. 使用已配置的 owner／runtime 数据库环境依次执行：

   ```bash
   .venv/bin/python -m db.migrate
   .venv/bin/python -m db.bootstrap
   .venv/bin/python -m db.doctor --json
   ```

4. 确认 Agent head 为 `agent_personal_0003`，execution 和 RAG head 分别仍为 `execution_0001`、`rag_personal_0001`，再启动新 API／Worker并执行合成冒烟。
5. 检查队列、终态及回执一致性后恢复提交。遗留的不兼容已开始任务按上述控制规则结算；历史结果继续可读。

这不是第二阶段到第三阶段的协议升级，不重复执行 `prepare_async_upgrade`。已有 checkpoint 表不需要重新初始化。回退必须使用匹配的代码与备份数据库；本迁移不提供将新执行状态转换回旧版本的 downgrade。

目标与验收使用新的恢复契约版本 2；图内检查结果必须具备真实图写入回执，最终生成和最终验收中断后不重放。见 [验收说明](acceptance.md)。
