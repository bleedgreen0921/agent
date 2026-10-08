"""Generate standalone synthetic research documents and acceptance answers.

Expected answers stay outside documents/ and are never uploaded or used for calculations.
The existing experiment fixture generator and its expected answers are unchanged.
"""

import argparse
from pathlib import Path

from scripts.synthetic_experiments import generate as generate_experiments, json_text

DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "examples" / "synthetic_research"
DOCUMENTS = {
    "attention.md": """# Synthetic attention method / 合成注意力方法

Synthetic data / 合成资料。未训练真实模型，未使用真实无线信号。

研究假设：通道注意力可能抑制噪声特征，从而改善低 SNR 调制识别。这是待检验的假设，不是已经证明的结论。

控制比较应核对完整有效配置、测试划分、类别顺序、SNR 覆盖与样本数。仅开启 model.attention.enabled 的实验可以描述该合成单对差异。
""",
    "evaluation.md": """# Synthetic evaluation protocol / 合成评测口径

Synthetic data / 合成资料。准确率必须按样本加权：sum(n_correct) / sum(n_total)，不能直接平均各 SNR 准确率。

低 SNR 定义为 SNR <= -5 dB。准确率的差异以百分点表示；混淆矩阵行是真实类别，列是预测类别，汇总后才能按行归一化。

单对实验只支持描述性比较，不能证明统计显著性或因果关系。不同 seed 同图不等于多 seed 汇总；重复配置也不代表独立重复实验。
""",
    "research_record.md": """# Synthetic contradictory research record / 合成矛盾研究记录

Synthetic data / 合成资料。下面是故意植入的错误研究记录，需要通过实际计数核查。

错误陈述：注意力实验总体准确率提高了 20 个百分点，并且已经证明了因果关系和统计显著性。

该陈述与合成原始计数不符，不应作为数值事实引用；方法假设和研究记录不能替代实验结果。
""",
}
TASK = ("结合合成注意力方法和评测文档，比较基线 20261001_090000 与注意力实验 20261001_103000，"
        "生成准确率–SNR 曲线和基线计数混淆矩阵，核查矛盾研究记录并说明结果限制。引用当前 Run 的文档和实验证据，标明本地文件。")
EXPECTED = {"schema_version": 1, "synthetic": True, "task": TASK,
            "expected_fragments": {"attention.md": "这是待检验的假设", "evaluation.md": "sum(n_correct) / sum(n_total)",
                                   "research_record.md": "20 个百分点"},
            "numbers": {"baseline_n_total": 800, "baseline_n_correct": 560,
                        "baseline_accuracy": 0.7, "attention_accuracy": 0.755,
                        "overall_delta_percentage_points": 5.5, "low_snr_delta_percentage_points": 11},
            "allowed_conclusions": ["合成数据", "描述性单对比较", "不构成显著性或因果证明", "错误记录与计数不符"],
            "citation_requirements": ["文档观点引用文档证据", "数值与图片引用实验证据", "所有引用来自当前 Run",
                                      "文档行号、实验源文件及图片哈希可核查", "本地路径不是下载链接"],
            "scope": "Scripted engineering acceptance; real-model understanding and tool selection require later evaluation."}


def generate(root: Path) -> int:
    payloads = {**{f"documents/{name}": text for name, text in DOCUMENTS.items()},
                "task.txt": TASK + "\n", "expected.json": json_text(EXPECTED)}
    for name, text in payloads.items():
        path = root / name
        if path.exists() and path.read_bytes() != text.encode():
            raise FileExistsError(f"Refusing to overwrite modified synthetic material: {path}")
    generate_experiments(root / "experiment_inputs")
    for name, text in payloads.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(text.encode())
    return len(payloads)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    try:
        count = generate(args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Synthetic research generation failed: {exc}\n")
    print(json_text({"synthetic": True, "documents": 3, "research_files": count, "output": str(args.output.resolve())}), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
