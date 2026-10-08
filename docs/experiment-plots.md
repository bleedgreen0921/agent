# 科研图生成与合成资料联动

两个绘图工具与离线命令共享 `experiment_service/plotting.py` 的计算、渲染和文件校验。仅支持现有合成实验格式，不训练或调用真实模型，不读取结果图。本次交付本地 PNG、SVG、绘图数据及来源清单；文件下载、报告导出和界面另行实现。

## Agent 启用

沿用[实验工具](experiment-tools.md)的根目录与团队配置，增加独立版本 `1` 的 Provider：

```sh
export AGENT_EXPERIMENT_ROOT='/绝对路径/experiment_inputs'
export AGENT_EXPERIMENT_TEAM_ID='已有团队的 UUID'
export AGENT_EXPERIMENT_PLOT_ROOT='/绝对路径/experiment_plots'
export AGENT_TOOL_PROVIDERS='agent_service.tools.rag:tools,agent_service.tools.experiments:tools,agent_service.tools.experiment_plots:tools'
.venv/bin/python -m db.config_check --role agent-worker
.venv/bin/python -m agent_service.worker
```

`AGENT_EXPERIMENT_PLOT_ROOT` 可选，默认是仓库下 `.data/experiment_plots`，必须为绝对路径且位于实验输入目录之外。输出路径、身份和样式均由服务端控制，不接受模型参数。绘图也可单独启用，不依赖分析 Provider 已加载，但仍共用方法版本 `2`、schema `1` 的分析输入快照；六个原分析工具与已有快照兼容。

| 工具 | 参数 | 规则 |
| --- | --- | --- |
| `plot_experiment_accuracy` | `experiment_ids: list[str]` | 1–5 个唯一 ID，全部 `ready`；沿调用顺序绘制 |
| `plot_experiment_confusion` | `experiment_id: str`；可选 `snr_db: int`、`normalization: "count" / "row"` | 全部 SNR 或一个已存在 SNR；默认整数计数 |

同图曲线必须具有相同的完整数据集配置、完整评测配置和每个 SNR×类别样本数。每点准确率为 `sum(n_correct)/sum(n_total)`，纵轴固定 0–100%。其他配置可以改变，数据中保存相对首个实验的全部有效配置差异（沿用忽略 `run` 元数据的规则）。多 seed 同图不表示 seed 汇总，也不自动成为严格对照；图中注明描述性比较，不能据此声称显著性或因果关系。

矩阵行为真实类别、列为预测类别，顺序沿用已验证类别。跨 SNR 先累加原始计数，再按真实类别样本总数归一化；不平均比例。数据同时保存计数、显示矩阵、类别顺序及实际 SNR。上限为 32 个类别、128 个 SNR。超过 12 类时不添加单元格数字，以保持可读性。

## 本地文件与引用

在线目录为 `<team_id>/<run_id>/<plot_id>/`，每个目录恰有以下文件：

| 文件 | 内容 |
| --- | --- |
| `plot.png` | 200 DPI 图片；曲线 8×5 英寸，矩阵 6×6 英寸 |
| `plot.svg` | 矢量图；矩阵及色条均为矢量网格 |
| `data.json` | 实际绘图数据、参数、计数与配置差异 |
| `manifest.json` | Run、首次工具调用 ID、快照与源文件哈希、绘图方法、依赖版本及前三个文件的哈希与大小 |

Matplotlib 固定为 `3.11.2`，使用 DejaVu Sans、固定配色、默认样式、进程内渲染锁及 Agg PNG 后端。SVG 内部 ID 固定，日期元数据移除。[后端文档](https://matplotlib.org/stable/users/explain/figure/backends.html)和 [SVG 元数据文档](https://matplotlib.org/stable/api/backend_svg_api.html)说明了两种输出方式和日期控制。相同环境下重复生成的 PNG/SVG/数据可逐字节复现；跨依赖版本不承诺图片字节一致。

`plot_id` 由输入快照、完整绘图数据、参数、绘图版本和依赖版本确定。同一 Run 的重复调用验证来源绑定与全部文件哈希后复用原产物和首次工具调用 ID。损坏的目录返回错误并保留现场，不覆盖文件。不同 Run 使用独立目录。

先验证持久身份、有效租约、输入状态和参数，再创建临时目录。发布前在数据库事务中锁定 Run，取得行锁后用独立查询重新核对身份、租约和截止时间；取得目录文件锁并完成文件与响应校验后，在原事务中再次核对授权，然后发布或复用产物。时间判断使用数据库 `clock_timestamp()`，覆盖等待锁期间自然过期的情况。首次实验输入快照写入也使用同一行锁后复核。Linux `renameat2(RENAME_NOREPLACE)` 原子发布完整目录，目录锁协调并发生成。文件访问使用锚定的目录描述符，拒绝路径越界与各级符号链接。本地存储实现依赖 Linux `/proc/self/fd` 和 `renameat2`，与当前 Linux Worker 部署相同。

PNG/SVG 各最多 2 MiB，数据、清单及完整工具响应各最多 256 KiB。参数错误为 `INVALID_EXPERIMENT_PLOT`，同图条件不一致为 `INCOMPARABLE_EXPERIMENTS`；渲染、超限或存储失败为可恢复错误 `EXPERIMENT_PLOT_FAILED`。失败清理本次临时目录，不新增成功证据或 metadata。进程被强制终止、文件发布后数据库失败可能留下未引用目录，本次不增加通用清理系统。

两工具声明为 `side_effect`、`produces_evidence=True`，走统一预算、trace 和 checkpoint。工具响应及证据保存绘图数据与四个文件的相对路径、媒体类型、大小、SHA-256，不包含图片字节。`ExperimentSourceLocator` 仍指向原实验输入；新图方法版本为 `1`，原分析仍为 `2`。trace 摘要也记录 plot ID 与产物元数据。恢复可从工具消息取回证据及图片元数据，无需重新绘图。

最终生成提示要求文档观点引用文档证据、数值与图片引用对应实验证据，说明合成属性及比较限制。本地相对路径要结合服务端输出根目录查看，不能当成可点击下载链接。公共 Run 响应未增加字段；这些信息位于现有引用内容中。

## 离线复现

离线命令不需要数据库或模型，使用相同输入快照构造、校验与渲染。清单标为 `origin="offline"`，Run 和首次调用 ID 为 `null`，目录为 `offline/<plot_id>/`。

```sh
.venv/bin/python -m scripts.synthetic_research --output .data/research/materials
.venv/bin/python -m scripts.experiment_plot_demo \
  --root .data/research/materials/experiment_inputs --output .data/research/plots \
  accuracy 20261001_090000 20261001_103000
.venv/bin/python -m scripts.experiment_plot_demo \
  --root .data/research/materials/experiment_inputs --output .data/research/plots \
  confusion 20261001_090000
.venv/bin/python -m scripts.experiment_plot_demo \
  --root .data/research/materials/experiment_inputs --output .data/research/plots \
  confusion 20261001_090000 --snr-db -10 --normalization row
```

## 合成资料与实际检索链路

`scripts.synthetic_research` 独立生成三份 Markdown：注意力方法假设、样本加权与低 SNR 口径、含错误“增加 20 个百分点”的矛盾研究记录。标题和正文均注明合成。复用原 12 个实验且不改变原生成器或原 `expected.json`；新联动任务与验收答案保存在 `task.txt` 和根目录 `expected.json`，不放入 `documents/`，不上传或用于实验计算。生成器拒绝覆盖已修改文件。

[联动专项](../tests/test_research_integration.py)实际创建测试团队和凭据、上传 Markdown、运行 RAG Worker 解析/分块/索引/激活，然后让真实 RAG Provider 和 `RagClient` 经 TestClient 的进程内 HTTP transport 调用实际 API。Mock 仅替换 tokenizer、Embedding、改写、精排及 Agent 模型；数据库 Dense/FTS/融合检索、ACL 和审计保留真实实现，没有伪造搜索响应或替换检索 SQL。

脚本化工具序列为文档检索、实验检索、严格对照筛选、指标比较、两个绘图工具。`react` 和 `plan_execute` 均在最终生成前人为中断，随后由新租约从真实 checkpoint 恢复并发布混合引用。恢复后不重放工具。检查总体 70%→75.5%、差异 5.5 个百分点、低 SNR 差异 11 个百分点、基线 800 样本与 560 正确数，并核对文档行号、实验与图片哈希、当前 Run 的调用/检索审计及持久证据快照。另覆盖无命中、RAG 不可用、无效对照、绘图失败、跨团队拒绝和历史引用稳定。

```sh
.venv/bin/python -m scripts.postgres_integration --suite research
.venv/bin/python -m scripts.postgres_integration --suite full
```

科研专项和全量入口都要求七个核心数据库场景实际通过，且全部所选测试 0 失败、0 错误、0 跳过；详见[数据库入口](postgres-integration.md)和[验收记录](validation-status.md)。真实 HTTP transport 继续被阻断，tokenizer 使用脚本化替身，不下载模型。

## 人工核对清单

- 对照 `data.json` 核对曲线各点的正确数、样本数和准确率，确认全配置差异与比较限制披露完整。
- 查看默认双曲线、五曲线、计数矩阵及行归一化矩阵，确认图例、刻度、合成标识与数值没有遮挡。
- 从原始 CSV/混淆 JSON 核对矩阵方向、类别顺序、800 样本与 560 对角计数；按聚合计数复算归一化。
- 对照原文行号核对文档观点；方法假设不写为已证实结论，错误研究记录不充当实验数值依据。
- 核对文档 claim 与实验/图片 claim 的 evidence ID、当前 Run 的调用及审计记录、SHA-256 和持久快照。
- 确认答案注明材料全部合成、描述性单对结果不证明显著性或因果、本地文件路径不是下载链接。

这些检查验证固定场景的工程机制和结论支持性。真实模型的理解、跨来源推理与自主工具选择仍留到后续评估。
