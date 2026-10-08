"""Generate a small, deterministic AMC experiment fixture, not trained results."""

import argparse
import csv
import io
import json
from pathlib import Path

import yaml


DEFAULT_ROOT = Path(__file__).resolve().parents[1] / "examples" / "amc_experiments"
CLASSES = ["BPSK", "QPSK", "16QAM", "64QAM"]
SNRS = [-10, -5, 0, 5, 10]
SAMPLES_PER_CLASS = [20, 30, 40, 50, 60]
BASE_CORRECT = [[9, 8, 5, 4], [18, 17, 12, 11], [30, 29, 23, 22],
                [44, 43, 37, 35], [57, 56, 51, 49]]
ATTENTION_GAIN = [[3, 3, 2, 2], [3, 3, 3, 3], [3, 3, 4, 4],
                  [1, 1, 2, 2], [0, 0, 1, 1]]
AUGMENTATION_GAIN = [[2, 2, 1, 1], [2, 2, 2, 2], [2, 2, 3, 3],
                     [1, 1, 1, 1], [0, 0, 0, 0]]

# Explicit cases keep expected query and comparison answers reviewable.
CASES = [
    {"id": "20261001_090000", "scenario": "baseline", "note": "基线，seed=42"},
    {"id": "20261001_103000", "scenario": "attention", "attention": True,
     "note": "仅开启注意力；与基线构成单字段对照"},
    {"id": "20261001_120000", "scenario": "augmentation", "augmentation": True,
     "note": "仅开启数据增强；与基线构成单字段对照"},
    {"id": "20261001_133000", "scenario": "combined", "attention": True,
     "augmentation": True, "note": "同时开启注意力与增强，不能把全部差异归因于一个模块"},
    {"id": "20261002_090000", "scenario": "baseline_seed123", "seed": 123,
     "note": "基线的另一随机种子"},
    {"id": "20261002_103000", "scenario": "attention_seed123", "attention": True,
     "seed": 123, "note": "注意力模型的另一随机种子"},
    {"id": "20261002_120000", "scenario": "learning_rate", "attention": True,
     "learning_rate": 0.0001, "note": "注意力模型降低学习率，配置存在额外差异"},
    {"id": "20261002_133000", "scenario": "different_split", "attention": True,
     "split": "synthetic-split-v2", "note": "使用另一测试划分，不纳入同划分对照"},
    {"id": "20261003_090000", "scenario": "duplicate_config", "reverse_keys": True,
     "note": "重复基线，YAML 键顺序改变；不应误当作独立随机种子"},
    {"id": "20261003_103000", "scenario": "failed", "attention": True,
     "status": "failed", "note": "虚拟训练失败，仅有部分训练日志，没有测试结果"},
    {"id": "20261003_120000", "scenario": "partial_results", "attention": True,
     "partial": True, "note": "声明五个 SNR，但结果缺少 10 dB，不能完整汇总比较"},
    {"id": "20261003_133000", "scenario": "missing_metadata", "attention": True,
     "missing_split": True, "note": "结果完整但配置缺少划分标识，比较条件未知"},
]


def json_text(value) -> str:
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"


def csv_text(rows: list[dict], fields: list[str] | None = None) -> str:
    output = io.StringIO(newline="")
    writer = csv.DictWriter(output, fieldnames=fields or list(rows[0]), lineterminator="\n")
    writer.writeheader()
    writer.writerows(rows)
    return output.getvalue()


def configuration(case: dict) -> dict:
    config = {
        "schema_version": 1,
        "synthetic": True,
        "run": {"experiment_id": case["id"], "project_id": "synthetic-amc",
                "created_at": f"{case['id'][:4]}-{case['id'][4:6]}-{case['id'][6:8]}T"
                              f"{case['id'][9:11]}:{case['id'][11:13]}:{case['id'][13:15]}+08:00",
                "status": case.get("status", "completed"),
                "output_dir": ".", "tags": [case["scenario"]]},
        "data": {
            "dataset": {"name": "SyntheticAMC", "version": "fixture-v1",
                        "split": {"fingerprint": case.get("split", "synthetic-split-v1"),
                                  "train_ratio": 0.6, "validation_ratio": 0.2, "test_ratio": 0.2},
                        "classes": CLASSES.copy(), "sequence_length": 128,
                        "input_channels": ["I", "Q"]},
            "preprocessing": {"normalization": "sample_rms", "remove_dc": False,
                              "clip_amplitude": None, "dtype": "float32"},
            "augmentation": {"enabled": case.get("augmentation", False),
                             "phase_rotation": {"max_degrees": 10},
                             "amplitude_scaling": {"range": [0.9, 1.1]},
                             "apply_to": "train_only"},
        },
        "model": {
            "name": "SyntheticCNN", "convolution": {"channels": [32, 64, 128],
            "kernel_sizes": [7, 5, 3], "strides": [1, 1, 1], "activation": "relu"},
            "attention": {"enabled": case.get("attention", False), "type": "channel",
                          "reduction_ratio": 8},
            "head": {"hidden_units": 128, "dropout": 0.2, "num_classes": 4},
        },
        "training": {
            "seed": case.get("seed", 42), "epochs": 12, "batch_size": 64,
            "optimizer": {"name": "adamw", "learning_rate": case.get("learning_rate", 0.001),
                          "weight_decay": 0.0001, "betas": [0.9, 0.999]},
            "scheduler": {"name": "cosine", "minimum_learning_rate": 0.00001},
            "loss": {"name": "cross_entropy", "label_smoothing": 0.0},
            "gradient_clip_norm": 1.0, "early_stopping": {"enabled": False, "patience": 5},
        },
        "evaluation": {"split": "test", "snr_db": SNRS.copy(), "accuracy_unit": "fraction",
                       "checkpoint_selection": "minimum_validation_loss",
                       "aggregation": "sample_weighted", "class_order": CLASSES.copy()},
        "provenance": {"training_code_revision": "synthetic-training-v1",
                       "config_is_effective": True, "generator": "scripts.synthetic_experiments"},
    }
    if case.get("missing_split"):
        del config["data"]["dataset"]["split"]["fingerprint"]
    return config


def result_rows(case: dict) -> tuple[list[dict], list[dict]]:
    rows, matrices = [], []
    for snr_index, snr in enumerate(SNRS):
        if case.get("partial") and snr == 10:
            continue
        count = SAMPLES_PER_CLASS[snr_index]
        matrix = []
        for class_index, label in enumerate(CLASSES):
            correct = BASE_CORRECT[snr_index][class_index]
            if case.get("attention"):
                correct += ATTENTION_GAIN[snr_index][class_index]
            if case.get("augmentation"):
                correct += AUGMENTATION_GAIN[snr_index][class_index]
            if case.get("seed") == 123 and class_index in (1, 3):
                correct -= 1
            if "learning_rate" in case and class_index == 0:
                correct += 1
            if "split" in case and class_index == 2:
                correct -= 1
            correct = min(count, max(0, correct))
            cells = [0] * len(CLASSES)
            cells[class_index] = correct
            errors = count - correct
            partner = class_index ^ 1
            cells[partner] = errors * 3 // 4
            cells[(class_index + 2) % len(CLASSES)] = errors - cells[partner]
            matrix.append(cells)
            rows.append({"experiment_id": case["id"], "synthetic": "true", "split": "test",
                         "snr_db": snr, "modulation": label, "n_total": count,
                         "n_correct": correct, "accuracy": correct / count})
        matrices.append({"snr_db": snr, "counts": matrix})
    return rows, matrices


def result_summary(case: dict, rows: list[dict]) -> dict:
    total = sum(row["n_total"] for row in rows)
    correct = sum(row["n_correct"] for row in rows)
    low = [row for row in rows if row["snr_db"] <= -5]
    low_total = sum(row["n_total"] for row in low)
    return {"schema_version": 1, "synthetic": True, "experiment_id": case["id"],
            "split": "test", "accuracy_unit": "fraction", "aggregation": "sample_weighted",
            "coverage": "partial" if case.get("partial") else "complete",
            "checkpoint": {"epoch": 12, "selected_by": "minimum_validation_loss"},
            "observed_snr_db": sorted({row["snr_db"] for row in rows}),
            "n_total": total, "n_correct": correct, "accuracy": correct / total,
            "low_snr": {"snr_db": [-10, -5], "n_total": low_total,
                        "n_correct": sum(row["n_correct"] for row in low),
                        "accuracy": sum(row["n_correct"] for row in low) / low_total},
            "source": "results_by_snr.csv",
            "method": "Hand-specified integer counts; no model training or RF samples."}


def history_rows(case: dict) -> list[dict]:
    epochs = 3 if case.get("status") == "failed" else 12
    return [{"experiment_id": case["id"], "synthetic": "true", "epoch": epoch,
             "train_loss": round(1.5 / (1 + epoch / 3), 6),
             "validation_loss": round(1.6 / (1 + epoch / 3), 6),
             "validation_accuracy": round(0.3 + epoch * 0.035, 6)}
            for epoch in range(1, epochs + 1)]


def expected_answers() -> dict:
    return {
        "schema_version": 1, "synthetic": True,
        "registration_status_counts": {"ready": 9, "failed": 1, "incomplete_results": 1,
                                       "unknown_conditions": 1},
        "queries": [
            {"name": "attention_same_split_ready", "filters": {
                "model.attention.enabled": True,
                "data.dataset.split.fingerprint": "synthetic-split-v1"}, "status": "ready",
             "experiment_ids": ["20261001_103000", "20261001_133000",
                                "20261002_103000", "20261002_120000"]},
            {"name": "baseline_seed42_ready", "filters": {
                "model.attention.enabled": False, "data.augmentation.enabled": False,
                "training.seed": 42}, "status": "ready",
             "experiment_ids": ["20261001_090000", "20261003_090000"]},
        ],
        "comparisons": [
            {"left": "20261001_090000", "right": "20261001_103000",
             "changed_paths": ["model.attention.enabled"]},
            {"left": "20261001_090000", "right": "20261001_120000",
             "changed_paths": ["data.augmentation.enabled"]},
            {"left": "20261001_090000", "right": "20261001_133000",
             "changed_paths": ["data.augmentation.enabled", "model.attention.enabled"]},
            {"left": "20261001_090000", "right": "20261002_090000",
             "changed_paths": ["training.seed"]},
            {"left": "20261001_103000", "right": "20261002_120000",
             "changed_paths": ["training.optimizer.learning_rate"]},
            {"left": "20261001_103000", "right": "20261002_133000",
             "changed_paths": ["data.dataset.split.fingerprint"]},
            {"left": "20261001_090000", "right": "20261003_090000", "changed_paths": []},
            {"left": "20261001_103000", "right": "20261003_133000",
             "changed_paths": ["data.dataset.split.fingerprint"]},
        ],
        "attention_only_controls": ["20261001_103000"],
        "known_metrics": {
            "20261001_090000": {"n_total": 800, "n_correct": 560, "accuracy": 0.7,
                                "low_snr_accuracy": 0.42},
            "20261001_103000": {"n_total": 800, "n_correct": 604, "accuracy": 0.755,
                                "low_snr_accuracy": 0.53},
        },
        "baseline_vs_attention": {"overall_delta_percentage_points": 5.5,
                                  "low_snr_delta_percentage_points": 11.0},
        "policy": "Ignore run metadata only; seed, split, lists and types remain meaningful.",
    }


def payloads() -> dict[str, str]:
    files, inventory = {}, []
    for case in CASES:
        prefix = f"experiments/{case['id']}"
        config = configuration(case)
        ordered = dict(reversed(list(config.items()))) if case.get("reverse_keys") else config
        files[f"{prefix}/config.yaml"] = "# 合成配置：未执行训练。\n" + yaml.safe_dump(
            ordered, sort_keys=False, allow_unicode=True)
        files[f"{prefix}/training_history.csv"] = csv_text(history_rows(case))
        files[f"{prefix}/notes.md"] = (
            f"# 虚拟实验 {case['id']}\n\n本目录为合成测试材料，未执行模型训练。\n\n"
            f"场景：{case['note']}。\n\n配置见 `config.yaml`；整数计数由固定规则构造，"
            "不代表任何真实模型或数据集的效果。\n")
        if case.get("status") == "failed":
            files[f"{prefix}/failure.json"] = json_text({
                "synthetic": True, "experiment_id": case["id"], "status": "failed",
                "error_code": "SYNTHETIC_TRAINING_FAILURE", "completed_epochs": 3})
            status = "failed"
        else:
            rows, matrices = result_rows(case)
            files[f"{prefix}/results_by_snr.csv"] = csv_text(rows)
            files[f"{prefix}/metrics.json"] = json_text(result_summary(case, rows))
            files[f"{prefix}/confusion_matrices.json"] = json_text({
                "schema_version": 1, "synthetic": True, "experiment_id": case["id"],
                "split": "test", "labels": CLASSES, "rows": "true_label",
                "columns": "predicted_label", "normalized": False, "matrices": matrices})
            status = "incomplete_results" if case.get("partial") else (
                "unknown_conditions" if case.get("missing_split") else "ready")
        inventory.append({"experiment_id": case["id"], "synthetic": "true",
                          "scenario": case["scenario"], "expected_status": status,
                          "config_path": f"{prefix}/config.yaml", "description": case["note"]})
    files["inventory.csv"] = csv_text(inventory)
    files["expected.json"] = json_text(expected_answers())
    files["manifest.json"] = json_text({
        "schema_version": 1, "synthetic": True, "project_id": "synthetic-amc",
        "generator": "scripts.synthetic_experiments", "experiment_count": len(CASES),
        "dataset": "Fictional SyntheticAMC fixture-v1; no RadioML data or RF samples.",
        "generation": "Fixed integer confusion counts, derived metrics, fixed timestamps; no randomness.",
        "snr_db": SNRS, "classes": CLASSES, "samples_per_class_by_snr": SAMPLES_PER_CLASS,
        "accuracy_unit": "fraction", "aggregation": "sample_weighted",
        "inventory": "inventory.csv", "expected_answers": "expected.json"})
    return files


def generate(root: Path) -> int:
    """Preflight all collisions before writing; repeat generation is idempotent."""
    files = payloads()
    for relative, content in files.items():
        path = root / relative
        if path.exists() and path.read_bytes() != content.encode("utf-8"):
            raise FileExistsError(f"Refusing to overwrite modified fixture: {path}")
    for relative, content in files.items():
        path = root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        if not path.exists():
            path.write_bytes(content.encode("utf-8"))
    return len(files)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--output", type=Path, default=DEFAULT_ROOT)
    args = parser.parse_args(argv)
    try:
        count = generate(args.output)
    except (OSError, ValueError) as exc:
        parser.exit(1, f"Fixture generation failed: {exc}\n")
    print(json_text({"synthetic": True, "experiments": len(CASES), "files": count,
                     "output": str(args.output.resolve())}), end="")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
