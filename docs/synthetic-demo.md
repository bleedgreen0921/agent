# 合成资料联动验收与 HTTP 演示

当前推荐通过 Docker 科研专项验证文档检索、实验对照、科研绘图、checkpoint 恢复与混合引用。原有独立文档 HTTP Mock 演示保留在本页后半部分，用于检查多个服务进程的接线。

## 科研联动验收

安装仓库固定依赖后，在可访问 Docker 的终端执行：

```sh
.venv/bin/python -m scripts.postgres_integration --suite research
.venv/bin/python -m scripts.postgres_integration --suite full
```

入口自动创建隔离 PostgreSQL/pgvector，初始化迁移、checkpoint 与角色，再上传合成 Markdown、运行实际 RAG Worker 并调用实际检索 API。tokenizer、Embedding、改写、精排与 Agent 模型全部使用脚本化替身或 Mock；真实 HTTP transport 被阻断，不下载 tokenizer。ReAct 与 Plan-and-Execute 均验证文档与实验证据、图片哈希、当前 Run 引用、恢复及历史快照稳定性，结束后默认清理本次数据库容器和网络。

核心任务是结合注意力假设和评测口径，比较基线与注意力实验，生成准确率–SNR 曲线及基线混淆矩阵，并识别与计数矛盾的研究记录。已核对总体 70%→75.5%、差异 5.5 个百分点、低 SNR 差异 11 个百分点，基线矩阵为 800 样本、560 正确数。材料均为合成数据，单对结果不构成显著性或因果证明。

2026-10-08 科研专项 68 项、全量 299 项通过，均为 0 失败、0 错误、0 跳过，七个核心数据库场景全部实际执行。版本、报告和清理记录见[验收记录](validation-status.md)，入口与环境说明见[数据库集成测试](postgres-integration.md)。

只需查看合成材料或离线图片时，无需启动数据库或模型服务：

```sh
.venv/bin/python -m scripts.synthetic_research --output .data/research/materials
.venv/bin/python -m scripts.experiment_plot_demo \
  --root .data/research/materials/experiment_inputs --output .data/research/plots \
  accuracy 20261001_090000 20261001_103000
.venv/bin/python -m scripts.experiment_plot_demo \
  --root .data/research/materials/experiment_inputs --output .data/research/plots \
  confusion 20261001_090000
```

`materials/documents/` 仅包含方法、评测和矛盾记录三份 Markdown，任务和预期答案在该目录之外，不进入检索或计算。每张图保存本地 PNG、SVG、数据和来源清单，输出路径相对于绘图根目录。完整参数、来源绑定及人工核对清单见[科研图生成与资料联动](experiment-plots.md)。

## 独立文档 HTTP Mock 演示

本演示使用固定输出的本地 HTTP Mock 验证服务接线、数据边界和引用流转。它不衡量模型或检索质量，也不能替代真实模型与真实资料试跑。Mock 默认只监听 `127.0.0.1`。

这部分只演示受限 TXT 的文档链路，使用本地 HTTP 服务和预先准备的 Qwen3-Embedding-0.6B tokenizer 文件。先按 README 完成空数据库迁移、checkpoint 建表和角色授权，然后在项目根目录设置四个数据库 DSN。将下面的 tokenizer 路径替换为现有本地目录，再生成演示凭据和共享配置：

```sh
export ADMIN_KEY="$(.venv/bin/python -m identity.bootstrap_admin)"
export RAG_SERVICE_TOKEN="$(.venv/bin/python -c 'import secrets; print(secrets.token_urlsafe(32))')"
export RAG_BASE_URL=http://127.0.0.1:8001
export RAG_FILES_DIR="$PWD/.data/rag"
export RAG_TOKENIZER_PATH='/绝对路径/本地Qwen3-Embedding-0.6B-tokenizer'
export HF_HUB_OFFLINE=1
export TRANSFORMERS_OFFLINE=1

export RAG_EMBEDDING_URL=http://127.0.0.1:8090
export RAG_REWRITE_URL=http://127.0.0.1:8090
export RAG_REWRITE_MODEL=synthetic
export RAG_RERANK_URL=http://127.0.0.1:8090
export AGENT_MODEL_URL=http://127.0.0.1:8090
export AGENT_MODEL=synthetic
export AGENT_MODEL_KEY=synthetic
export AGENT_TOOL_PROVIDERS='agent_service.tools.rag:tools'
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

HTTP Mock 的 Agent 响应固定为文档检索流程；科研联动测试的工具序列由独立脚本化模型驱动。Embedding Mock 返回 1024 维向量，reranker 使用 `/rerank` 契约。Mock 服务仅用于本机接线检查；真实模型与真实资料评估按[后续能力清单](platform-capability-assessment.md)安排。
