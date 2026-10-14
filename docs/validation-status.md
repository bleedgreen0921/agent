# 个人版第一阶段验证记录

2026-10-12 完成个人实例模型与统一同步执行契约改造。验收使用专用 PostgreSQL 16/pgvector、新个人基线、脚本化模型及 Mock/ASGI Transport，没有迁移历史业务数据。

## 全量合成测试

运行：`.venv/bin/python -m scripts.postgres_integration --suite full --port 55440`。

最终报告目录：`.data/postgres-integration/rap-test-48123a303bee/`。

| 检查 | 结果 |
| --- | --- |
| 全量测试 | 254 通过 |
| 失败、错误、跳过 | 0、0、0 |
| 三个实验数据库专项 | 全部通过 |
| doctor | 三条个人迁移链、角色权限、任务 RLS、checkpoint 等检查全部通过 |
| 真实 HTTP transport | 禁用，未尝试真实模型请求 |
| 测试容器 | 已清理 |
| git diff --check | 通过 |

JUnit、pytest 日志、doctor 报告和 summary.json 位于上述被 gitignore 的报告目录。测试中仍有依赖库对不可变 Mapping 的序列化警告及 Starlette 弃用警告，不影响本次业务输出和断言。

覆盖个人 API 与 loopback 来源/Host/Origin、服务 Token 和关联头、实例及操作级幂等、active/历史证据/逻辑删除、统一状态与严格租约、锁等待后拒绝过期续租、取消/超时/接管优先级、主 checkpoint/blob/中间写入回滚、Agent 保守恢复、Memory 三次尝试与原子产物、RAG 重试和发布事务、工具冲突与合并中途失权、逻辑停止后的物理容量、角色隔离及旧库拒绝且不修改。

## 实际启动冒烟

在另一专用测试数据库中启动本机合成模型 HTTP 服务、两个个人 API 和同步 Worker，使用缓存的本地 tokenizer，验证：

- react 与 plan_execute 两种模式完成文档检索并发布引用。
- 创建会话、提交轮次，提取与摘要任务均完成。
- Agent Worker 收到 SIGTERM 后，未知模型调用结算为 INTERRUPTED_UNKNOWN；stop_requested_at 保持为空，没有误记为用户取消。
- Worker 清理完成后，其在线记录消失。

报告：`.data/personal-verification/smoke-summary.json`，配套服务日志和两种模式的结果保存在同目录。该冒烟只调用本机固定合成响应，不代表真实模型的工具选择能力或真实训练效果。

## 发布边界

新版本必须使用独立个人数据库和新的资料目录。旧库、旧目录、旧代码单独保留作为历史归档与回退组合；禁止将旧代码直接指向新库。初始化不支持旧业务数据升级或 downgrade。

本阶段保留同步调度与每 Run 子进程。后续异步阶段复用 execution/contracts.py、store.py 和物理工作登记规则。
