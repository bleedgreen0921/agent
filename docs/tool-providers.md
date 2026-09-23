# Agent 工具扩展接口

Agent 核心执行器不直接依赖 RAG 客户端。部署通过 `AGENT_TOOL_PROVIDERS` 加载工具提供者，每项使用 `python.module:factory` 格式，多个提供者用逗号分隔。默认值是：

```text
agent_service.tools.rag:tools
```

默认提供者只负责把独立 RAG HTTP API 适配为 `search_evidence` 和 `read_evidence` 两个 LangChain 工具。新增工具不需要修改 `agent_service.execution`。一个最小提供者如下：

```python
from langchain.tools import ToolRuntime, tool

from agent_service.tooling import ToolExecutionContext


@tool
def convert_units(value: float, source: str, target: str,
                  runtime: ToolRuntime[ToolExecutionContext]) -> str:
    """Convert a measurement between supported units."""
    # team_id/run_id 来自已持久化并认领的 Run，不接受模型或客户端覆盖。
    team_id = runtime.context.team_id
    return perform_conversion(value, source, target, team_id)


def tools():
    return [convert_units]
```

启用多个提供者：

```sh
export AGENT_TOOL_PROVIDERS='agent_service.tools.rag:tools,my_package.agent_tools:tools'
```

注册表要求工具名全局唯一。所有加载的工具都会自动经过同一中间件，在调用前预留 Run 工具预算、写入持久调用记录，并关联触发它的模型调用。参数 trace 只保存参数名和整体 SHA-256；通用结果 trace 只保存结果类型。提供者可以通过 `runtime.context.add_tool_metadata(...)` 增加不含敏感正文的状态、服务请求 ID 或结果摘要。

可处理的业务错误可以抛出 `RecoverableToolError`，中间件会记录失败并把结构化错误观察交回 Agent。身份、授权和配额等必须终止 Run 的错误抛出 `FatalToolError`。其他未分类异常会记录为 `TOOL_CALL_FAILED` 并停止本次执行，不会自动重试。

普通工具的输出会进入 ReAct 或 Plan 步骤上下文和最终生成摘要。能够产生可引用证据的工具还需把符合公共 `Evidence` 契约的数据放入 `runtime.context.evidence`；最终发布仍只接受本 Run 实际收到的 evidence ID。这样可以扩展计算、搜索或数据查询工具，同时维持统一的预算、身份、trace 和引用规则。
