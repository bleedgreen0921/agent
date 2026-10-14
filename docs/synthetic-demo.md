# 个人实例合成演示

可运行 `.venv/bin/python -m scripts.mock_model_service` 在本机 8090 端口提供固定的 Chat、Embedding 和 rerank 响应。它只生成合成观察与证据，不产生真实科研结论。

离线运行时将 RAG_TOKENIZER_PATH 配为已缓存的本地 tokenizer 目录，避免依赖远程模型元数据。

按照 README 初始化新个人库，配置 Agent 模型与 RAG Embedding URL 指向本机合成服务，并启动两个 API 和 Worker。执行：

```bash
.venv/bin/python -m scripts.synthetic_demo --mode react
.venv/bin/python -m scripts.synthetic_demo --mode plan_execute
```

演示直接上传文档并等待 active，随后提交 Run、等待结果并验证引用含有 seven years。接口无需业务 Key；服务间 Token 仍由环境配置。另可通过 `/v1/conversations` 创建会话并以带 Idempotency-Key 的 turns 请求提交对话。

实验离线演示无需数据库或模型：

```bash
.venv/bin/python -m scripts.synthetic_experiments --output .data/amc_fixture
.venv/bin/python -m scripts.experiment_fixture_demo --data .data/amc_fixture
```

在线实验工具配置 `AGENT_EXPERIMENT_ROOT` 与实验 Provider，输入服务为每 Run 捕获不可变文件快照并保存哈希，最终引用保留项目、实验、来源文件和方法版本。实验数值是预设整数计数，不能作为真实训练结果或因果结论。
