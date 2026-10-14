# 第二阶段：异步资源与统一关闭

每个 API lifespan 创建一套 `RuntimeResources`。资源属于创建进程和事件循环；模块导入不会创建池、客户端或执行器。同步 Worker、受保护 Saver、执行图、Memory 和文档流水线保留第一阶段实现；不得在这些同步业务函数里调用 `asyncio.run()`、建立后台事件循环或将整个事务/Run 送入线程池。

```python
from execution.infra.runtime import RuntimeResources
from execution.infra.types import OperationDeadline, WorkOwner

async with RuntimeResources(role="agent-worker") as resources:
    deadline = OperationDeadline.after(remaining_seconds)
    async with resources.db.transaction("normal", deadline, readonly=True) as conn:
        rows = await (await conn.execute("SELECT id FROM execution.tasks")).fetchall()
    response = await resources.http.request(
        "rag", "POST", url, deadline=deadline,
        headers={"Authorization": token, "X-Tool-Call-Id": call_id}, json=payload)
    value = await resources.io.run(sync_adapter, input_data, owner=owner, deadline=deadline)
```

`remaining_seconds` 由业务层按第一阶段执行期限计算。资源层不调用同步 `execution.scope.call_timeout()`，不验证持久租约、不处理预算或结算。每次业务发送/提交仍需要执行权校验。系统探测可以不传 owner，但期限必须有限。

`WorkOwner(task, lease_token, call_id=None)` 按 `(TaskRef, lease_token)` 区分执行尝试；`cancel_attempt(owner)` 停止该尝试的当前工作和后续接纳，不影响其他 token。调用 ID 只标识工作归属。`OperationDeadline` 使用当前事件循环的单调时钟，覆盖容量等待、资源借用、执行和完整结果传输。创建期限后不能跨 loop 使用资源。

## 数据库

仅打开 role 所属数据库，control、normal、bulk 分别有独立池。`psycopg-pool==3.3.3` 已显式声明，其他依赖不变。池使用 `open=False`、`min_size=0`、dict row、autocommit；lifespan 显式 open/close。启动不等待数据库连接，`/health` 仍表达进程存活。

每个 transaction 独占一个借出连接，用显式事务执行全部操作。保留 DSN options 和已有连接/语句/锁超时；事务内用局部设置取 profile 超时与剩余期限的小值。只读事务、局部设置在归还后消失。异常和取消回滚；无法确认 idle 的连接被关闭并丢弃。连接获取等待不超过配置值与操作剩余时间的小值。

不自动重试事务。提交期间断连或取消不能推断为未提交，业务适配层必须沿用持久幂等与未知提交处理。不要跨业务操作共享借出的连接，也不要把连接交给线程或计算进程。

## HTTP

逻辑服务为 `agent-model`、`rag`、`embedding`、`rewrite`、`rerank`。首次使用创建一个 AsyncClient，之后复用；可选端点不会产生启动依赖。关闭权属于 resources。适配层必须通过此接口请求，认证、关联头和网络 timeout 按请求传入，不能修改共享 headers。

默认不读取环境代理、不跟随重定向、不重试。`request()` 读取完整响应并关闭；流式调用必须使用：

```python
async with resources.http.stream("agent-model", "POST", url,
                                 deadline=deadline, json=payload) as response:
    async for chunk in response.aiter_bytes():
        consume(chunk)
```

流式借用直到响应关闭才释放容量；取消和重复取消也必须完成关闭。网络 timeout 外还有整次操作 deadline。同步模型调用和原有业务错误映射尚未迁移。

## 执行器

IO 使用专用 ThreadPoolExecutor，CPU 使用 spawn ProcessPoolExecutor。外层队列有界，只有空闲执行槽位才 submit；队列满的调用者在自身协程等待，不产生额外后台等待 Task。运行中的 Future 被取消等待后仍占槽位，结果和异常被消费且不再交付。排队取消不会执行函数。每次 IO 提交单独复制 ContextVar；同步 SDK 的线程安全和自身 timeout 由适配层明确。

CPU 仅接受可导入的顶层函数和可序列化参数，禁止传连接、客户端、整个执行上下文或 MappingProxy。稳定输入转换为普通数据，大文档优先使用受控文件路径。CPU 函数不能访问执行状态、发网络请求或建立业务执行器。本阶段不加载 Docling/tokenizer。

IO 工作超过取消后的清理宽限，整个资源实例停止接纳并请求所属进程退出；不会新建替补线程池。CPU 超过宽限或池崩溃时冻结 generation，所有已提交工作明确中断为 `CpuPoolReset`，不自动重试；外层未提交工作保留原 deadline。先回收旧池全部进程和管理线程，再允许下一 generation 使用新池。旧回调只能处理自身登记。

CPU 强制关闭局限在 `execution/infra/cpu_backend.py`，只支持 CPython 3.12/Linux，其他运行环境首次启用 CPU 时明确拒绝。初始化子进程使用独立进程组，只对捕获的自有子进程及其同 ID 进程组发送 TERM/KILL，并确认直接进程和管理线程结束。升级 Python 必须先替换/重新验证此适配器。

## 生命周期和退出

`NEW → STARTING → RUNNING → QUIESCING → CLOSING → CLOSED`。启动部分失败逆序清理。`quiesce()` 拒绝新工作并唤醒等待者；借出事务/响应在关闭时取消并完成回滚/关闭。先处理执行器，再处理借用者，最后关闭 HTTP 客户端和数据库池。CPU 重建期间 DB/HTTP 继续使用；回收失败才要求整个所属进程退出。

`aclose()` 幂等，所有调用等待同一次关闭。关闭共用一个期限；调用者取消不能取消必要清理。返回状态、退出原因、异常类型、实际未完成工作、借用者与清理任务数量，以及 shutdown_complete；不输出 DSN、Token、请求正文或计算输入。异常超期时不能声称资源已物理回收，必须由进程入口完成退出。`snapshot()` 仅返回状态、容量、排队、占用、停止未完成、generation 和重建次数。

正式启动必须使用 `python -m scripts.serve --service agent|rag`。入口收到资源退出请求时让 Uvicorn 停止接收连接并执行正常关闭；普通停机同样启动独立线程 watchdog。超过退出宽限，watchdog 终止自有 CPU 进程并以状态 70 强制退出，避免失控线程阻塞解释器退出。普通资源类不调用 `os._exit()`。直接 `uvicorn ...` 或嵌入 API 的宿主必须自行接入 `app.state.resource_exit_handler`，否则无法提供进程级强制退出保证。

## 配置

配置在资源创建/启动时解析并验证为有限正数，运行中不改池。数据库连接容量按进程计算，上层仍需限制整体请求/Run 数量。

| 环境变量 | 默认值 |
| --- | --- |
| ASYNC_DB_CONTROL / ASYNC_DB_NORMAL / ASYNC_DB_BULK | 2 / 4 / 1 |
| ASYNC_DB_WAITING | 32 |
| ASYNC_DB_ACQUIRE_TIMEOUT_SECONDS | 5 |
| ASYNC_HTTP_CONNECTIONS / ASYNC_HTTP_KEEPALIVE | 16 / 8 |
| ASYNC_HTTP_CONCURRENCY / ASYNC_HTTP_PENDING | 8 / 16 |
| ASYNC_IO_WORKERS / ASYNC_IO_PENDING | 4 / 16 |
| ASYNC_CPU_WORKERS / ASYNC_CPU_PENDING | 1 / 4 |
| EXECUTOR_CLEANUP_GRACE_SECONDS | 5 |
| CPU_POOL_RESET_TIMEOUT_SECONDS | 30 |
| WORKER_EXIT_GRACE_SECONDS | 30 |

沿用 `DB_CONNECT_TIMEOUT_SECONDS` 和三个 profile 的 `DB_*STATEMENT_TIMEOUT_SECONDS` / `DB_*LOCK_TIMEOUT_SECONDS`。keepalive 不能大于最大连接数。

## 接入与发布边界

两个 API 的 `/health/ready` 使用两秒异步 control 只读事务；`/v1/workers` 使用五秒异步 normal 只读事务。响应模型不变，不可用返回 503，Worker 错误正文脱敏。`/health` 不访问 DB/模型。其他同步路径尚未迁移。独立测试入口使用 Worker role 验证将来的异步 Worker 接口。

本阶段不迁移数据库结构、任务状态、预算或恢复语义。发布复用第一阶段个人数据库，重启两个 API 即可；同步 Worker 保留原入口。可回退第一阶段代码与同一数据库。后续异步适配必须复用这里定义的资源归属、取消容量、CPU 回收与关闭顺序。

参考：[Psycopg pool](https://www.psycopg.org/psycopg3/docs/api/pool.html)、[HTTPX async](https://www.python-httpx.org/async/)、[Python 3.12 Executor](https://docs.python.org/3.12/library/concurrent.futures.html)。


## 第三阶段接入

上述同步保留边界属于第二阶段历史说明。当前 API 与 Worker 的全执行链都已接入 resources，SDK facade 同样执行容量接纳、单次调用期限和发送前授权。`begin_shutdown()` 固定首次退出期限；`aclose()` 复用它。quiesce 后只有持久层的受限 control 清理能力可借连接，新业务工作仍拒绝。详见 [异步执行层](async-execution.md)。

## 第四阶段接入补充

当前业务链与两个 Worker 已全面使用异步资源。执行器可接收 `before_submit` 异步回调：获取实际执行容量后，在所属 attempt 上下文中确认授权，再提交同步函数。资源层不解释回调中的持久规则；等待授权的任务数量同样受执行槽位限制，取消后等待真实授权任务结束。

SDK HTTP 流式读取保留调用原期限，关闭完成后释放借用。业务调用期限覆盖资源排队与结果处理；具体预留、结算、异常和业务执行入口见 [调用适配说明](call-adapters.md)。
