# 当前能力边界

当前支持一个本机个人实例：Run、会话轮次、个人历史事实、文档检索与实验分析。没有团队、用户、业务凭据或 ACL。业务接口免认证且限 loopback；服务证据调用保留独立 Token。

持久执行状态、租约、总期限、重试策略、checkpoint 保护和工具 artifact 已统一。Worker 使用 asyncio.Task 调度 Agent、Memory 和文档任务，已移除每 Run 子进程和常驻 Memory 子进程。模型、数据库与服务请求采用原生异步 I/O；文件、序列化和明确的同步工具进入有界线程执行器；文档准备与实验分析使用可复用的 CPU 进程池。各 API／Worker 进程分别持有 RuntimeResources，在进程内复用资源和跟踪取消后的实际工作。实现与验收见 [调用适配说明](call-adapters.md) 和 [验证记录](validation-status.md)。

文档解析支持 TXT、Markdown、PDF、DOCX，检索融合稠密向量与全文检索，可选 rewrite/rerank，引用快照独立保存。实验 Provider 适配仓库的合成实验格式，保留项目、实验和文件来源信息。真实训练日志适配、独立实验管理 API 和跨 Run 常驻实验库仍待扩展。

验证入口与限制见 [数据库验收](postgres-integration.md)，执行规则见 [执行契约](execution-contract.md)。
