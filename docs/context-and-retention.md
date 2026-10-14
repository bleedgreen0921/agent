# 输入预算、诊断归档与数据生命周期

本轮覆盖 C18、C19、C20、C21、C22、C24。请求在实际发送前执行输入预检和持久 token 预留；图状态使用不可变正文引用减少重复存储；恢复依赖与诊断归档分别保留。这里只统计 token，不引入价格、费用或货币预算。

## 配置和计数

| 配置 | 默认值 | 范围 |
| --- | --- | --- |
| `TOKEN_MODEL_POLICIES` | `{}` | 按模型名设置 context、output、可选本地 tokenizer |
| 未配置模型的 context / output | 32768 / 4096 | 单次完整请求及输出预留 |
| `AGENT_TOKEN_LIMIT` | 262144 | 原 Run，包括在线 RAG 派生请求 |
| `MEMORY_TOKEN_LIMIT` | 65536 | 每个 Memory 任务独立预算 |
| `RAG_DOCUMENT_TOKEN_LIMIT` | 1048576 | 每个文档任务独立预算 |
| `RAG_SEARCH_TOKEN_LIMIT` | 98304 | 独立搜索预算／Run 搜索预分配上限 |
| `CALL_ARCHIVE_DIR` | `.data/call-archives` | 本机私有诊断目录 |

例如两个服务共用 `TOKEN_MODEL_POLICIES='{"my-local-model":{"context":16384,"output":2048,"tokenizer":"/absolute/model/tokenizer"}}'`。策略在资源启动时读取，首次 Run Manifest 固定配置；恢复不接受策略变化。tokenizer 只从本地加载，禁用下载和远程代码，文件指纹进入策略并在首次加载前复核。应事先提供完整本地 tokenizer。

预检包含系统提示、消息、工具描述、结构化 schema 和其他实际请求字段，采用配置的模型上限。输出参数夹到较小值；同时提供两种参数时只保留 `max_completion_tokens` 并取两者较小值。输入加输出预留超过 context 时不发送请求。

有本地 tokenizer 时使用本地模板计数；没有时采用完整请求 UTF-8 字节数加 1024 的保守接纳估计，标记 `estimated_bytes`。这是估计，不保证等于服务端 tokenizer，也不保证任意 Provider 的绝对上界。服务端 usage 独立记录；实际用量超过输入估计或输出预留时保留真实消耗并阻止该预算后续派发。已经在途的请求仍可能继续消耗。

## 上下文和证据

原任务、目标、当前完成条件和继承条件是必需输入。模型历史按完整 AI／Tool 批次选择；证据先传 ID、标题和体积目录，按相关性与需要加载正文。最终引用及核验使用原始完整片段，必需片段装不下时明确返回上下文限制，不能静默截断后声称已核验。

超长历史可触发一次结构化摘要，事实、假设、条件、未解决事项分别表达。Plan 每个步骤最多一次，ReAct 整个阶段最多一次；使用同一模型并计入原次数与 token 预算。摘要必须通过真实 checkpoint 回执确认，不能只用内存返回值充当恢复输出。再次超长直接 partial，不递归压缩，也不额外调用模型修正。连原始目标都无法容纳时以输入限制失败。

最终证据核验单包装不下时按 claim 分组，随后独立检查完整原任务、全部要求和各组判断。各组和全局判断都计入原预算并绑定实际调用；组结果不能替代全局任务验收。次数或 token 不足时不伪造通过。

## token 结算和跨服务配额

`execution.token_budgets` 固定额度与策略，`token_calls` 保存稳定操作 ID、估计、预留、实际 usage 和派发状态。预留明确提交成功才允许发送；已有 ID 不授权重发。提交确认丢失时保留额度并停止派发；只有确认没发送的预留能释放。usage 未返回、断连或取消时保留完整预留，统计为未确认消耗，不填零。

普通 Agent 调用保留最终生成和验收额度。结构化规划、最终生成、摘要、验收、Embedding 和在线 RAG 均进入调用计量；JSON 无法解析时仍记录收到的原始响应和 usage。

在线搜索先在 Run 内预分配有限额度，签名票据绑定 Run、工具调用、请求摘要、期限和模型策略。RAG 使用自己的角色和账本，不读 Agent 表。实际派发后通过搜索状态接口核实结算，未知响应不重复搜索；未确认消耗继续占用 Run 配额，失败但有 usage 的搜索也能核实，子预算超估会阻止父预算继续派发。

改写、Embedding 或重排额度耗尽时，RAG 可按既有降级规则保留 FTS／融合结果，不重试模型。认证或配置错误仍按原规则失败。文档和 Memory 额度耗尽结束对应任务，不借用原 Run 额度。多项 Embedding 输入过长时拆批，保持顺序和同一截止时间；单项装不下则拒绝。

Run／搜索响应的 `token_usage`／`usage` 包含限额、charged、实际输入输出、未确认消耗、估计调用数和 blocked。charged 包含未确认预留，不等于实际 usage；尚无调用时限额可能为 null。自定义异步模型需在 `ProviderReply.metadata` 返回 usage 并遵守输出限制，未提供时按未确认处理。

## checkpoint 和永久依据

`agent.execution_bodies` 保存按 Run 隔离、不可修改的正文，引用带格式、长度与 SHA256。ToolMessage 保存薄目录及正文引用；较大的 checkpoint blob／pending write 也外置。恢复检查实际图记录、调用回执和引用正文；依赖缺失或损坏即拒绝恢复。

单次 checkpoint 记录上限 1 MiB；Run 的图记录、写入、blob 和外置正文累计超过 64 MiB 时周期性告警。测量失败不推翻已提交记录；该数字是逻辑数据体积，不是含索引和膨胀的数据库磁盘大小。

活动任务的恢复数据不自动删除。终态超过 30 天才允许清理 checkpoint、回执及仅用于恢复的正文。已发布答案、引用快照和核验必需正文永久保留，后者有永久 pin。诊断归档不是恢复权威，也不替代历史引用。

当前实验工具是纯计算，没有绘图临时目录；回收器管理已登记上传文件和调用归档。以后新增产物须接入登记协议，不能只靠扫描目录推断归属。

## 私有调用归档

请求发送前归档，响应解析前归档。原生 SDK 保存实际业务请求与响应、参数、关联 ID 和内容摘要。自定义适配器记录框架请求与 `ProviderReply`，其原始网络格式由适配器负责提供。未收到完整响应时保留 incomplete／prepared 状态，不编造响应。

归档为 gzip JSON，目录 0700、文件 0600。认证头不进入正文；已知服务凭据、数据库连接及密码、租约 token，以及显式 password／api_key／authorization 等字段脱敏，包括嵌套 JSON 工具参数。归档记录脱敏次数，对脱敏后的内容校验 SHA256。未知敏感值若出现在普通自然语言中，字段规则无法可靠识别；不要把无关凭据放入业务输入。脱敏位置无法还原原文。

默认保留 30 天，活动关联任务与 hold 受保护。普通 HTTP API、trace 和提示不返回归档正文，只有本机维护命令能读取。请求归档失败阻止派发，响应归档失败记录不完整，不因此重试外部调用。

## 回收和维护

`lifecycle_maintenance` 默认 NOLOGIN；owner 设置 `MAINTENANCE_DATABASE_URL` 运行 bootstrap 后才设置登录密码。该角色只能读归档索引、管理 hold 和执行有限回收函数，不能直接删除业务表。维护进程不能使用 owner DSN。

```bash
# 环境文件保存维护角色 DSN，以及与应用一致的绝对资料／归档目录。
.venv/bin/python -m scripts.lifecycle dry-run
.venv/bin/python -m scripts.lifecycle hold --namespace agent --id RUN_UUID --reason '科研复核'
.venv/bin/python -m scripts.lifecycle release --namespace agent --id RUN_UUID
.venv/bin/python -m scripts.lifecycle archive --namespace agent --id RUN_UUID
.venv/bin/python -m scripts.lifecycle archive --namespace rag --id RUN_UUID
.venv/bin/python -m scripts.lifecycle run-once
.venv/bin/python -m scripts.lifecycle daemon
```

daemon 每 15 分钟检查。文件须写入租期失效、安静至少 24 小时、无数据库引用或 hold、文件锁未被占用，才进入 quarantine；隔离后至少再等 24 小时并复查，才能删除。文件操作在事务外，数据库标记隔离后禁止新引用。移动、删除及索引更新中途崩溃可继续处理。

上传原子改名后提交未知时保留正式文件，等待引用核实，不能在异常处理中删除可能已引用的文件。归档回收处理改名后索引未更新的确定输出名和安静的写入临时文件。未登记旧资料只报告，不自动删除。

hold 可保护 Run、归档 ID、产物 ID 或搜索操作 ID；Run hold 同时保护跨服务归档和恢复数据。hold 不还原已删除文件，也不保证中止已标记且正在执行的回收；调查时应及时设置并备份。

服务示例见 [维护服务模板](../examples/lifecycle-maintenance.service)，不会自动安装或启动。替换用户、路径和环境文件，先 dry-run，再由部署流程启用。

## 检索耗时口径

`candidate_trace.version=2` 增加单调时钟时间线：索引读取、改写、Embedding、稀疏准备、召回事务、Dense SQL、FTS SQL、融合、重排、响应组装；底层记录 DB 借连接、HTTP 容量与限速等待、执行器排队与执行。

并行阶段不能相加为总耗时。HTTP exchange 含传输、服务端处理和读取，不等于 GPU 推理时间；执行器耗时含启动与 IPC，SQL 耗时含服务端等待。总耗时从检索流水线进入开始，至审计前采样，不含接纳、搜索建账、审计自身及后续 usage 汇总／HTTP 返回。失败保留已完成阶段；取消或数据库不可用时审计可能无法持久保存，并记录日志。

## 个人数据库发布

当前 head 为 `execution_0004`、`rag_personal_0002`、`agent_personal_0006`；图 v5、状态 v3、提示 v4、Manifest schema 6，ToolOutcome 仍为 1，恢复契约为 2。个人库增量升级，不重新初始化。

1. 停止新提交，排空并停止旧 API／Worker；备份数据库、资料目录及匹配代码。
2. 按执行契约结算已开始且不兼容任务，不伪造新回执；历史终态结果保留。
3. owner 运行 `db.migrate`、`agent_service.checkpoints`、`db.bootstrap`、`db.flow_control --configure`、`db.doctor --json`。仅未开始且无 Manifest 的队列任务升级新图，保留排队期限。
4. API／Worker 配置相同模型策略和归档目录；自动维护需单独配置窄权限角色，先 dry-run 后启用服务。
5. 启动新 API／Worker，文档、两种 Agent、Memory 冒烟通过后恢复提交，禁止旧新 Worker 混跑。

回退使用匹配代码和备份库，不在新库启动旧代码。本轮开发验收只操作隔离数据库，未执行个人实例迁移或部署。
