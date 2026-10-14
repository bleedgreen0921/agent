# 工具 Provider 扩展

`AGENT_TOOL_PROVIDERS` 使用逗号分隔的 `module:factory`。工厂返回 `ToolProvider(name, version, tools, contract_version=1)`。每个 ToolDeclaration 声明工具、业务方法版本、`read_only/side_effect` 和是否产出证据。Provider 的执行契约版本与实验计算方法版本分别演进。

工具必须使用 `response_format="content_and_artifact"`。content 是模型可见观察，artifact 是 `ToolOutcome.to_artifact()` 返回的纯 JSON。工具收到 `execution.contracts.ExecutionContext`，包含 TaskRef、token、deadline、step ID、call ID、期限和不可变输入，没有可写的共享证据或通知。

```python
from langchain.tools import tool, ToolRuntime
from agent_service.tooling import ToolDeclaration, ToolProvider, ToolExecutionContext
from execution.contracts import ToolOutcome

@tool(response_format="content_and_artifact")
async def echo(value: str, runtime: ToolRuntime[ToolExecutionContext]) -> tuple[str, dict]:
    """Return an observation."""
    content = "echo: " + value
    return content, ToolOutcome(content).to_artifact()

def tools():
    return ToolProvider("echo", "1", (ToolDeclaration(echo, "1", "read_only", False),), contract_version=1)
```

外部调用前使用 context 的期限计算有效超时；不得私自重试未知外部结果。执行层统一预留预算、结算调用、验证证据模型和安全元数据、重新检查执行权、合并结果。同一证据 ID 的业务内容冲突失败；rank 变化不构成内容冲突。通知按 code 去重保留首次内容。迟到、取消或失租结果不能合并，预留预算不退款。

允许的 metadata 为 status、result_summary、service_request_id、retrieval_id、evidence_ids、error_code。status 为 succeeded、failed、degraded；其他字段必须可序列化。不要将连接、客户端、身份或可写状态放入 artifact。

未知工具、非法参数或独立意图拒绝在分发前停止整批，写入拒绝审计并部分发布，不预留实际工具额度。RecoverableToolError 产生可恢复观察；FatalToolError 保留服务认证、配额等终止分类；未预期的 Provider 异常映射为 TOOL_CALL_FAILED。取消和失租直接停止执行，不包装成模型失败。

内置 RAG 和实验 Provider 可作为完整示例。实验输入持久化由 execution.inputs 完成，Provider 只读取稳定快照并计算来源引用。


第三阶段要求原生 coroutine 工具；没有异步入口的 Provider 在启动时拒绝。同步 SDK 显式用 `resources().io.run` 包装，并传 WorkOwner 与 OperationDeadline；CPU 函数必须为可导入的顶层纯函数。禁止直接 `asyncio.to_thread()`、默认 Runnable 线程回退或自建池／HTTP 客户端。内置实验 Provider 演示了普通快照数据进入 CPU 池的适配。

执行层生成调用标记并保存到消息元数据；Provider 不生成 checkpoint 回执，也不向模型正文或 artifact 注入执行标记。只有已持久结算的真实图输出才能获得恢复回执。

第四阶段提供 `managed_io_tool(function, name=..., description=..., args_schema=...)` 作为显式同步适配。function 收到冻结工具上下文和普通参数字典，返回 content/artifact；包装器使用统一 IO 池、期限和提交前授权。模型与工具均通过 CallRunner 预留、调用和结算。调用标记在结算确认后生成，证据合并不持锁等待数据库。详见 [调用适配说明](call-adapters.md)。

当前执行层还限制每 Run／Worker 的工具并发和单次模型工具批次。运行时图状态与参数是逐调用副本，不支持 InjectedState、InjectedStore 或 Command；结果按图顺序合并。work_owner() 自动继承当前调用 ID，取消后底层工作结束前继续占据名额。网络必须经过资源 HTTP／SDK 适配以使用共享配额。详见 [流量与进展控制](flow-control.md)。

副作用 Provider 必须额外声明 `ToolIntentSpec(operation, effect_fields)`，请求通过 `tool_authorizations` 给出所有关键参数的精确范围与次数。声明进入不可变 Manifest，授权用量和调用预算同事务预留；通过授权仍需独立意图检查。定义及合成测试示例见 [验收说明](acceptance.md)。
