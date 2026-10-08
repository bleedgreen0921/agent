# 自动调制识别虚拟实验

本目录包含 12 个按时间 ID 命名的虚拟实验，供实验登记、配置检索、结果比较和文件导出演示使用。所有配置、曲线和指标都是合成材料，未训练模型、未使用真实射频样本，也不代表 RadioML 或任何真实数据集的效果。

已可通过[实验分析 Provider](../../docs/experiment-tools.md) 接入 Agent 的六个实验工具，按说明配置根目录和团队即可启用。离线演示与 Provider 复用同一套登记、比较和计数核对逻辑；在线调用额外保存每 Run 不可变输入快照与文件引用。

## 文件组织

```text
amc_experiments/
├── manifest.json          # 数据集约定和生成方式
├── inventory.csv          # 实验列表、场景及预期状态
├── expected.json          # 查询、配置差异和指标的验收答案
└── experiments/
    └── 20261001_090000/
        ├── config.yaml
        ├── notes.md
        ├── training_history.csv
        ├── results_by_snr.csv
        ├── metrics.json
        └── confusion_matrices.json
```

失败实验只有配置、研究记录、部分训练曲线和 `failure.json`，不伪造测试结果。图片与论文不属于这批数据的生成范围。

## 实验清单

| 实验 ID | 场景 | 预期登记状态 |
| --- | --- | --- |
| 20261001_090000 | 基线，seed=42 | ready |
| 20261001_103000 | 仅开启注意力 | ready |
| 20261001_120000 | 仅开启数据增强 | ready |
| 20261001_133000 | 同时开启注意力与增强 | ready |
| 20261002_090000 | 基线，seed=123 | ready |
| 20261002_103000 | 注意力模型，seed=123 | ready |
| 20261002_120000 | 注意力模型改变学习率 | ready |
| 20261002_133000 | 注意力模型使用另一划分 | ready，但不进入同划分对照 |
| 20261003_090000 | 重复基线，YAML 键顺序不同 | ready，配置指纹与基线相同 |
| 20261003_103000 | 训练失败，没有测试结果 | failed |
| 20261003_120000 | 缺少 10 dB 的测试结果 | incomplete_results |
| 20261003_133000 | 缺少数据划分标识 | unknown_conditions |

每份 YAML 包含数据集、预处理、增强、模型、优化器、训练、评测和来源信息。`run` 保存时间 ID、标签和输出位置；只有 `run` 元数据不参与语义配置比较，随机种子和数据划分仍参与。

## 结果格式与计算口径

`results_by_snr.csv` 一行表示一个实验、测试集合、SNR 和调制类别的计数。列为：

```text
experiment_id,synthetic,split,snr_db,modulation,n_total,n_correct,accuracy
```

类别为 BPSK、QPSK、16QAM、64QAM；SNR 为 -10、-5、0、5、10 dB。每个 SNR、每类的样本数分别为 20、30、40、50、60，完整测试共 800 个虚拟样本。因此跨 SNR 汇总需要按样本数加权。

- `accuracy` 为 0～1 的数值，等于 `n_correct / n_total`。
- `metrics.json` 保存由这些整数计数汇总的结果；局部覆盖必须标记为 `partial`。
- `confusion_matrices.json` 是未归一化整数矩阵，行是真实类别，列是预测类别，类别顺序显式声明。每行和与 CSV 分母一致，对角线与正确数一致。
- `training_history.csv` 是固定公式生成的虚拟训练/验证曲线，没有逐 epoch 测试指标。
- YAML 是此演示的实际生效配置，没有命令行覆盖。checkpoint 选择规则为最小验证损失；这些字段用于测试口径核对，不表示执行过训练。

基线的总体准确率为 560/800=70%；注意力实验为 604/800=75.5%，差异为 5.5 个百分点。低 SNR 定义为 -10、-5 dB，差异为 11 个百分点。这些是预设验收值，不是科研结论。

## 生成和离线验证

在仓库根目录执行：

合成数据文件已加入 `.gitignore`，仅保留本说明和生成脚本供版本管理。新克隆仓库后先执行生成命令，再运行离线演示或启用实验 Provider；默认导出和数据库测试报告位于同样被忽略的 `.data/`。

```sh
.venv/bin/python -m scripts.synthetic_experiments
.venv/bin/python -m scripts.experiment_fixture_demo
.venv/bin/python -m pytest -q tests/test_experiment_fixtures.py
```

生成器采用固定时间 ID 和整数规则，不使用随机数。重复生成结果相同；如果目标文件被修改，生成器在写任何文件前拒绝覆盖。需要重建时可选择新的目录：

```sh
.venv/bin/python -m scripts.synthetic_experiments --output .data/amc_fixture
.venv/bin/python -m scripts.experiment_fixture_demo --data .data/amc_fixture --output .data/experiment_demo
```

演示从 YAML 和结果文件构建索引，登记全部实验，再用 `expected.json` 验收；预期答案不参与登记和结果计算。导出到独立目录，默认 `.data/experiment_demo`：

- `catalogue.json`：完整登记快照、配置指纹、源文件哈希、验证状态和问题。
- `attention_search.json`：同划分、完整且开启注意力的四个实验。
- `config_diff.csv`：基线与注意力实验的单字段差异。
- `accuracy_comparison.csv`：两者按 SNR 的整数计数、准确率和百分点差异。
- `report.md`：统计口径、来源、对比结果及异常说明。
- `validation.json`：16 项预期答案检查结果。

筛选 ready 状态不意味着任意两个 ready 实验可以直接归因比较。严格对照还会检查全部非 run 配置，要求变化路径恰好等于指定字段。缺失条件不是相同条件，重复基线也不是第二个随机种子。

离线演示本身无需 PostgreSQL、API、模型或 tokenizer。它与已接入 Agent 的实验 Provider 复用同一套登记和计算逻辑；在线工具还提供每 Run 的数据库输入快照及实验文件来源引用。当前只支持本目录约定的合成格式，独立实验管理 API、跨 Run 常驻实验库和真实训练日志适配仍需补全。

2026-10-08 本机 Docker 全量合成测试 199 项通过，包含两个 Agent 模式的实验数据库发布及快照并发三个专项，未调用真实模型。详情见[验收记录](../../docs/validation-status.md)。
