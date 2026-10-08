# Agent 工具扩展接口

Provider 是科研平台承载通用工具扩展的接口。当前已接入文档 RAG 和实验分析两类内置 Provider，共八个工具；统一预算、可信身份、trace 和证据发布复用于两类工具。当前注册配置作用于部署/Worker，尚未提供按科研项目选择工具的策略层。

| Provider | 工厂 | 工具 | 默认启用 |
| --- | --- | --- | --- |
| `rag_evidence`，版本 `1` | `agent_service.tools.rag:tools` | `search_evidence`、`read_evidence` | 是 |
| `experiment_analysis`，版本 `1` | `agent_service.tools.experiments:tools` | 配置检索、详情、差异、对照、指标比较、混淆统计，共六个 | 否；需配置实验根目录与团队 |

Agent 核心执行器不直接依赖 RAG 客户端。部署通过 `AGENT_TOOL_PROVIDERS` 加载工具提供者，每项使用 `python.module:factory` 格式，多个提供者用逗号分隔。默认值是：

```text
agent_service.tools.rag:tools
```

默认提供者只负责把独立 RAG HTTP API 适配为 `search_evidence` 和 `read_evidence` 两个 LangChain 工具。新增工具不需要修改 `agent_service.execution`。一个最小提供者如下：

```python
from langchain.tools import ToolRuntime, tool

from agent_service.tooling import ToolDeclaration, ToolExecutionContext, ToolProvider


@tool
def convert_units(value: float, source: str, target: str,
                  runtime: ToolRuntime[ToolExecutionContext]) -> str:
    """Convert a measurement between supported units."""
    # team_id/run_id 来自已持久化并认领的 Run，不接受模型或客户端覆盖。
    team_id = runtime.context.team_id
    return perform_conversion(value, source, target, team_id)


def tools():
    return ToolProvider(
        name="unit_conversion", version="1",
        tools=(ToolDeclaration(convert_units, "1", "read_only", False),),
    )
```

启用多个提供者：

```sh
export AGENT_TOOL_PROVIDERS='agent_service.tools.rag:tools,agent_service.tools.experiments:tools'
```

实验 Provider 还要求 `AGENT_EXPERIMENT_ROOT` 和 `AGENT_EXPERIMENT_TEAM_ID`，具体见[实验工具启用说明](experiment-tools.md)。自定义 Python Provider 使用同一 `module:factory` 约定。

工厂必须返回 `ToolProvider`，声明提供者名称和版本；每个 `ToolDeclaration` 必须携带 `BaseTool`、版本、`read_only` 或 `side_effect` 类型，以及是否产生可引用证据的布尔值。旧式纯工具列表不再接受。Worker 启动时验证声明与全局工具名唯一性；缺失或重复会立即报错。声明在首次认领时写入不可变的 v2 Manifest，历史 v1 Manifest 继续可读。声明只用于记录和诊断，不改变认领、checkpoint、恢复安全性或执行策略。

所有加载的工具都会自动经过同一中间件，在调用前预留 Run 工具预算、写入持久调用记录，并关联触发它的模型调用。参数 trace 只保存参数名和整体 SHA-256；通用结果 trace 只保存结果类型。提供者可以通过 `runtime.context.add_tool_metadata(...)` 增加不含敏感正文的状态、服务请求 ID 或结果摘要。

可处理的业务错误可以抛出 `RecoverableToolError`，中间件会把数据库 trace 和返回给 Agent 的 `ToolMessage` 都标记为错误。身份、授权和配额等必须终止 Run 的错误抛出 `FatalToolError`。其他未分类异常会把工具 trace 和 Run 都记录为 `TOOL_CALL_FAILED` 并停止本次执行，不会自动重试。未知工具或参数校验失败同样记录为失败的工具调用，但作为错误观察交回 Agent，不立即终止 Run。

普通工具的输出会进入 ReAct 或 Plan 步骤上下文和最终生成摘要。能够产生可引用证据的工具还需把符合公共 `Evidence` 契约的数据放入 `runtime.context.evidence`；最终发布仍只接受本 Run 实际收到的 evidence ID。这样可以扩展计算、搜索或数据查询工具，同时维持统一的预算、身份、trace 和引用规则。

能够恢复的证据工具还应在 JSON 返回值中包含 `evidences` 数组或直接返回单个 Evidence，供 checkpoint 消息恢复引用。现有内置[实验分析 Provider](experiment-tools.md) 使用 `source_locator.kind="experiment"` 记录项目、实验、文件哈希和计算方法；PDF/Markdown 等文档来源沿用原契约，文档和版本 ID 仍为必需。实验依据不能使用虚构的文档 ID。

当前声明元数据用于记录与诊断，不提供副作用工具的自动重试、幂等保证或审批节点。实验工具中的文件读取、计数核对和比较方法由程序确定，模型只选择工具和参数。在线生成 Markdown/CSV 文件的 Artifact Provider 尚待接入。

2026-10-08 已通过两个执行模式的实验 Provider 数据库发布测试及快照并发测试，修复后完整合成测试 217 项通过。验证使用脚本化模型，真实模型的工具选择能力不在本次验收范围；详见[验收记录](validation-status.md)。
