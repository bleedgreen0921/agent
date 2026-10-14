# 独立 PostgreSQL 验收

运行 `.venv/bin/python -m scripts.postgres_integration --suite full`。入口创建专用 Docker Compose 项目和 PostgreSQL 16/pgvector 容器，数据库名称为 `research_agent_integration`，端口默认 55432。它不会使用当前生产数据库 DSN；临时 RAG 文件位于本次 `.data/postgres-integration` 子目录。

依次初始化 execution、rag、agent 三条个人链、owner checkpoint、两个运行角色，再执行 doctor 和全量 pytest。测试使用脚本化模型、Mock HTTP、ASGI Transport，并拦截真实 HTTP。必须零失败、零错误、零跳过，且两种 Agent 实验执行模式和输入快照并发三个专项均通过。

`--suite experiments` 运行这三个数据库专项。`--keep-db` 保留本次专用容器用于检查，凭据只保存到权限 0600 的 `.data` 文件；默认结束清理容器。日志、JUnit 和 summary.json 位于本次报告目录。失败不能被摘要报告误判为成功。

doctor 检查迁移头、pgvector、checkpoint、个人字段约束、运行角色权限矩阵、任务 RLS、索引覆盖、超期租约、Worker 状态、调用一致性与资料文件引用。连接信息与密码不输出到报告。

从旧版本切换时，先停止所有旧进程并单独保存旧库及资料，再创建新空库和新目录。旧业务数据不迁移。初始化检测到旧表、旧字段、旧 revision 或未知非空库时，拒绝且不修改。
