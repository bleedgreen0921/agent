"""Offline registration/search/comparison demo for the synthetic AMC fixture.

Uses the same catalogue and calculations as the Agent experiment provider.
"""

import argparse
from collections import Counter
import csv
import json
import math
from pathlib import Path

from experiment_service.catalogue import (
    REQUIRED_CONDITIONS, canonical, config_diff, controls, flatten,
    metric_comparison, read_results, register, search, semantic_fields,
)
from scripts.synthetic_experiments import DEFAULT_ROOT, csv_text, json_text


def verify(entries: list[dict], expected: dict) -> dict:
    by_id = {entry["experiment_id"]: entry for entry in entries}
    checks = {"registration": dict(Counter(entry["status"] for entry in entries)) == expected["registration_status_counts"]}
    for query in expected["queries"]:
        actual = [entry["experiment_id"] for entry in search(entries, query["filters"], query["status"])]
        checks[query["name"]] = actual == query["experiment_ids"]
    for pair in expected["comparisons"]:
        paths = [item["path"] for item in config_diff(by_id[pair["left"]]["config"], by_id[pair["right"]]["config"])]
        checks[f"diff:{pair['left']}:{pair['right']}"] = paths == pair["changed_paths"]
    for run_id, metrics in expected["known_metrics"].items():
        checks[f"metrics:{run_id}"] = all(
            math.isclose(by_id[run_id]["metrics"][key], value, abs_tol=1e-12, rel_tol=0)
            for key, value in metrics.items())
    checks["attention_only_controls"] = controls(entries, "20261001_090000", {"model.attention.enabled"}) == expected["attention_only_controls"]
    baseline, attention = by_id["20261001_090000"], by_id["20261001_103000"]
    for label, key in (("overall", "accuracy"), ("low_snr", "low_snr_accuracy")):
        delta = 100 * (attention["metrics"][key] - baseline["metrics"][key])
        checks[f"delta:{label}"] = math.isclose(delta, expected["baseline_vs_attention"][f"{label}_delta_percentage_points"], abs_tol=1e-10)
    failed = [name for name, passed in checks.items() if not passed]
    if failed:
        raise ValueError("Fixture expectations failed: " + ", ".join(failed))
    return {"synthetic": True, "passed": True, "checks": checks}


def run_demo(root: Path, output: Path) -> dict:
    if output.resolve() == root.resolve() or root.resolve() in output.resolve().parents:
        raise ValueError("Demo output must be outside the fixture source directory")
    entries = register(root)
    expected = json.loads((root / "expected.json").read_text(encoding="utf-8"))
    validation = verify(entries, expected)
    by_id = {entry["experiment_id"]: entry for entry in entries}
    baseline, attention = by_id["20261001_090000"], by_id["20261001_103000"]
    differences = config_diff(baseline["config"], attention["config"])
    query = expected["queries"][0]
    selected = search(entries, query["filters"], query["status"])
    comparison = metric_comparison(baseline, attention, {"model.attention.enabled"})
    overall = round(100 * (attention["metrics"]["accuracy"] - baseline["metrics"]["accuracy"]), 10)
    low = round(100 * (attention["metrics"]["low_snr_accuracy"] - baseline["metrics"]["low_snr_accuracy"]), 10)
    files = {
        "catalogue.json": json_text({"schema_version": 1, "synthetic": True, "experiments": entries}),
        "attention_search.json": json_text({"synthetic": True, "filters": query["filters"],
                                           "status": query["status"], "experiment_ids": [entry["experiment_id"] for entry in selected]}),
        "config_diff.csv": csv_text([{"synthetic": "true", "path": item["path"],
            "left_present": str(item["left_present"]).lower(), "right_present": str(item["right_present"]).lower(),
            "left_value": canonical(item["left"]), "right_value": canonical(item["right"])} for item in differences]),
        "accuracy_comparison.csv": csv_text(comparison),
        "validation.json": json_text(validation),
        "report.md": (
            "# 自动调制识别虚拟实验比较\n\n全部数值来自手工构造的整数计数，未执行训练，"
            "不代表真实模型效果。\n\n"
            f"登记了 {len(entries)} 个实验；状态分布：`{canonical(Counter(entry['status'] for entry in entries))}`。\n\n"
            f"基线 `{baseline['experiment_id']}` 与改进 `{attention['experiment_id']}` "
            "仅 `model.attention.enabled` 不同；run 元数据不参与配置比较。\n\n"
            f"总体准确率分别为 {baseline['metrics']['accuracy']:.2%} 和 {attention['metrics']['accuracy']:.2%}，"
            f"差异为 {overall:.2f} 个百分点；低 SNR（-10、-5 dB）差异为 {low:.2f} 个百分点。\n\n"
            "口径：测试集合，准确率=正确样本数/总样本数，跨 SNR 按样本数加权；"
            "各 SNR 样本数不同，不直接平均组准确率。仅比较一个种子，不能据此宣称稳定提升。\n\n"
            "## 来源\n\n"
            f"配置：`experiments/{baseline['experiment_id']}/config.yaml`、"
            f"`experiments/{attention['experiment_id']}/config.yaml`。\n\n"
            "数据：对应实验目录的 `results_by_snr.csv`；JSON 汇总和混淆矩阵已与整数计数核对。"
            "文件 SHA-256 见 `catalogue.json`。\n\n"
            "## 检索与异常\n\n"
            f"同划分、结果完整且开启注意力的实验：`{canonical([entry['experiment_id'] for entry in selected])}`。\n\n"
            "失败实验、缺少 SNR 的实验及缺少划分标识的实验仍登记，但不进入 ready 筛选和严格对照。"
            "重复基线具有相同语义配置指纹，不应当作第二个随机种子。\n\n"
            "## 附件\n\n`config_diff.csv` 保存配置差异；`accuracy_comparison.csv` 保存按 SNR 的比较；"
            "`validation.json` 保存预期答案核验结果。\n"
        ),
    }
    # This command owns these six named demo outputs; it never modifies inputs.
    output.mkdir(parents=True, exist_ok=True)
    for name, content in files.items():
        (output / name).write_bytes(content.encode("utf-8"))
    return {"synthetic": True, "registered": len(entries),
            "status_counts": dict(Counter(entry["status"] for entry in entries)),
            "attention_matches": query["experiment_ids"], "verified_checks": len(validation["checks"]),
            "overall_delta_percentage_points": overall, "low_snr_delta_percentage_points": low,
            "output": str(output.resolve())}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--data", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path, default=Path(".data/experiment_demo"))
    args = parser.parse_args(argv)
    try:
        result = run_demo(args.data, args.output)
    except (OSError, ValueError, KeyError, TypeError, StopIteration) as exc:
        parser.exit(1, f"Experiment demo failed: {exc}\n")
    print(json_text(result), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
