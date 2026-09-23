# 合成资料全链路演示

本演示使用固定输出的本地 HTTP Mock 验证服务接线、数据边界和引用流转。它不衡量模型或检索质量，也不能替代真实模型与真实资料试跑。Mock 默认只监听 `127.0.0.1`。

先按 README 完成空数据库迁移、checkpoint 建表和角色授权，然后在项目根目录设置四个数据库 DSN。生成演示凭据和共享配置：

```sh
export ADMIN_KEY="$(.venv/bin/python -m identity.bootstrap_admin)"
export RAG_SERVICE_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export RAG_BASE_URL=http://127.0.0.1:8001
export RAG_FILES_DIR="$PWD/.data/rag"
export RAG_TOKENIZER_PATH=Qwen/Qwen3-Embedding-0.6B

export RAG_EMBEDDING_URL=http://127.0.0.1:8090
export RAG_REWRITE_URL=http://127.0.0.1:8090
export RAG_REWRITE_MODEL=synthetic
export RAG_RERANK_URL=http://127.0.0.1:8090
export AGENT_MODEL_URL=http://127.0.0.1:8090
export AGENT_MODEL=synthetic
export AGENT_MODEL_KEY=synthetic
```

在五个终端中保留相同环境变量，分别启动模型 Mock、两个 API 和两个 Worker：

```sh
.venv/bin/uvicorn scripts.mock_model_service:app --host 127.0.0.1 --port 8090
.venv/bin/uvicorn rag_service.app:app --host 127.0.0.1 --port 8001
.venv/bin/uvicorn agent_service.app:app --host 127.0.0.1 --port 8002
.venv/bin/python -m rag_service.worker
.venv/bin/python -m agent_service.worker
```

第六个终端运行演示客户端：

```sh
.venv/bin/python -m scripts.synthetic_demo --mode react
.venv/bin/python -m scripts.synthetic_demo --mode plan_execute
```

每次执行都会创建独立团队和团队 Key、上传一份受限 TXT、等待文档索引、提交 Run 并等待终态。成功输出包含 `completed` Run、逐条 claim，以及正文含 `seven years` 的引用快照；输出不包含团队 Key。若任一步失败，命令以非零状态退出并指出失败阶段。

真实部署应把模型变量改为经过授权的可信 HTTP 服务。Agent 模型必须支持工具调用和结构化输出；Embedding 必须返回 1024 维向量；reranker 使用 `/rerank` 契约。不要把 Mock 服务暴露到非本机网络，也不要用它得出准确率、延迟或成本结论。
