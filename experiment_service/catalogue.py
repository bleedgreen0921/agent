"""Deterministic experiment catalogue and calculations shared by tools and demos."""

import csv
import hashlib
import io
import json
import math
import os
import stat
from pathlib import Path

import yaml

REQUIRED_CONDITIONS = (
    "data.dataset.name", "data.dataset.version", "data.dataset.split.fingerprint",
    "data.dataset.classes", "data.dataset.sequence_length", "training.seed",
    "evaluation.split", "evaluation.snr_db", "evaluation.accuracy_unit",
    "evaluation.aggregation", "evaluation.checkpoint_selection",
    "provenance.training_code_revision", "provenance.config_is_effective",
)


def flatten(value: dict, prefix: str = "") -> dict:
    """Keep lists as ordered values; an absent key is distinct from explicit null."""
    if prefix.count(".") >= 32:
        raise ValueError("Configuration nesting exceeds 32 levels")
    fields = {}
    for key, item in value.items():
        if not isinstance(key, str) or "." in key:
            raise ValueError("Configuration keys must be strings without dots")
        path = f"{prefix}.{key}" if prefix else key
        if isinstance(item, dict) and item:
            fields.update(flatten(item, path))
        else:
            fields[path] = item
        if len(fields) > 10000:
            raise ValueError("Configuration contains too many fields")
    return fields


def canonical(value) -> str:
    # JSON also keeps true distinct from 1, unlike ordinary Python equality.
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(",", ":"))


def semantic_fields(config: dict) -> dict:
    return {key: value for key, value in flatten(config).items()
            if key != "run" and not key.startswith("run.")}


def config_diff(left: dict, right: dict) -> list[dict]:
    left_fields, right_fields = semantic_fields(left), semantic_fields(right)
    changes = []
    for key in sorted(left_fields.keys() | right_fields.keys()):
        left_present, right_present = key in left_fields, key in right_fields
        if left_present != right_present or canonical(left_fields.get(key)) != canonical(right_fields.get(key)):
            changes.append({"path": key, "left_present": left_present,
                            "right_present": right_present, "left": left_fields.get(key),
                            "right": right_fields.get(key)})
    return changes


def read_results(directory: Path, config: dict, contents: dict[str, bytes] | None = None) -> tuple[list[dict], dict, list[str]]:
    def read(name):
        if contents is None:
            return (directory / name).read_text(encoding="utf-8")
        if name not in contents:
            raise FileNotFoundError(name)
        return contents[name].decode("utf-8")

    raw = list(csv.DictReader(io.StringIO(read("results_by_snr.csv"))))
    labels = config["evaluation"]["class_order"]
    snrs = config["evaluation"]["snr_db"]
    run_id = config["run"]["experiment_id"]
    if (not raw or len(labels) != len(set(labels)) or len(snrs) != len(set(snrs))
        or labels != config["data"]["dataset"]["classes"]
        or config["evaluation"]["accuracy_unit"] != "fraction"
        or config["evaluation"]["aggregation"] != "sample_weighted"):
        raise ValueError("Unsupported or inconsistent evaluation schema")
    rows, seen = [], set()
    for item in raw:
        snr, total, correct = int(item["snr_db"]), int(item["n_total"]), int(item["n_correct"])
        accuracy = float(item["accuracy"])
        key = (snr, item["modulation"])
        if (key in seen or snr not in snrs or item["modulation"] not in labels
            or item["experiment_id"] != run_id or item["synthetic"] != "true"
            or item["split"] != config["evaluation"]["split"]
            or total <= 0 or not 0 <= correct <= total or not math.isfinite(accuracy)
            or not math.isclose(accuracy, correct / total, abs_tol=1e-12, rel_tol=0)):
            raise ValueError("Invalid or duplicate result row")
        seen.add(key)
        rows.append({**item, "snr_db": snr, "n_total": total, "n_correct": correct,
                     "accuracy": accuracy})
    observed = sorted({row["snr_db"] for row in rows})
    if any((snr, label) not in seen for snr in observed for label in labels):
        raise ValueError("Incomplete class coverage within an observed SNR")
    total = sum(row["n_total"] for row in rows)
    correct = sum(row["n_correct"] for row in rows)
    low = [row for row in rows if row["snr_db"] <= -5]
    low_total = sum(row["n_total"] for row in low)
    calculated = {"n_total": total, "n_correct": correct, "accuracy": correct / total,
                  "low_snr_accuracy": sum(row["n_correct"] for row in low) / low_total
                  if low_total else None}
    summary = json.loads(read("metrics.json"))
    if (summary["experiment_id"] != run_id or summary["synthetic"] is not True
        or summary["split"] != config["evaluation"]["split"]
        or summary["accuracy_unit"] != "fraction" or summary["aggregation"] != "sample_weighted"
        or summary["observed_snr_db"] != observed
        or summary["n_total"] != total or summary["n_correct"] != correct
        or not math.isclose(summary["accuracy"], calculated["accuracy"], abs_tol=1e-12, rel_tol=0)
        or summary["checkpoint"]["selected_by"] != config["evaluation"]["checkpoint_selection"]):
        raise ValueError("Summary does not reconcile with source rows")
    if (summary["low_snr"]["n_total"] != low_total
        or summary["low_snr"]["n_correct"] != sum(row["n_correct"] for row in low)
        or summary["low_snr"]["snr_db"] != [snr for snr in observed if snr <= -5]
        or (low_total and not math.isclose(summary["low_snr"]["accuracy"],
                                           calculated["low_snr_accuracy"], abs_tol=1e-12, rel_tol=0))):
        raise ValueError("Low-SNR summary does not reconcile")
    confusion = json.loads(read("confusion_matrices.json"))
    if (confusion["experiment_id"] != run_id or confusion["synthetic"] is not True
        or confusion["labels"] != labels or confusion["rows"] != "true_label"
        or confusion["columns"] != "predicted_label" or confusion["normalized"] is not False
        or confusion["split"] != config["evaluation"]["split"]
        or sorted(item["snr_db"] for item in confusion["matrices"]) != observed):
        raise ValueError("Invalid confusion matrix metadata")
    by_key = {(row["snr_db"], row["modulation"]): row for row in rows}
    for group in confusion["matrices"]:
        matrix = group["counts"]
        if len(matrix) != len(labels):
            raise ValueError("Invalid confusion matrix shape")
        for index, cells in enumerate(matrix):
            row = by_key[(group["snr_db"], labels[index])]
            if (len(cells) != len(labels) or any(type(cell) is not int or cell < 0 for cell in cells)
                or sum(cells) != row["n_total"] or cells[index] != row["n_correct"]):
                raise ValueError("Confusion matrix does not reconcile with source rows")
    missing = sorted(set(snrs) - set(observed))
    if summary["coverage"] != ("partial" if missing else "complete"):
        raise ValueError("Declared coverage does not match actual coverage")
    return rows, calculated, [f"MISSING_SNR:{snr}" for snr in missing]


def register(root: Path, *, max_experiments: int = 200,
             max_file_bytes: int = 2 * 1024 * 1024,
             max_total_bytes: int = 16 * 1024 * 1024) -> list[dict]:
    """Rebuild a deterministic local index from source files, never expected.json."""
    root = root.resolve(strict=True)
    folder = root / "experiments"
    if folder.is_symlink() or not folder.is_dir():
        raise ValueError("Experiment folder must be a real directory")
    paths = []
    for directory in sorted(folder.iterdir()):
        if directory.is_symlink():
            raise ValueError("Symbolic links are not accepted in experiment folders")
        if directory.is_dir() and (directory / "config.yaml").exists():
            paths.append(directory / "config.yaml")
        if len(paths) > max_experiments:
            raise ValueError("Experiment count exceeds the catalogue limit")
    entries, seen_ids, total_bytes = [], set(), 0
    for path in paths:
        contents = {}
        for file in sorted(path.parent.iterdir()):
            if file.is_symlink():
                raise ValueError("Symbolic links are not accepted as experiment files")
            if not file.is_file():
                continue
            if len(contents) >= 64:
                raise ValueError("Experiment contains more than 64 files")
            descriptor = os.open(file, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
            with os.fdopen(descriptor, "rb") as source:
                info = os.fstat(source.fileno())
                if not stat.S_ISREG(info.st_mode) or info.st_size > max_file_bytes:
                    raise ValueError("Experiment source must be a bounded regular file")
                content = source.read(max_file_bytes + 1)
            total_bytes += len(content)
            if len(content) > max_file_bytes or total_bytes > max_total_bytes:
                raise ValueError("Experiment file bytes exceed the catalogue limit")
            contents[file.name] = content
        try:
            config = yaml.safe_load(contents["config.yaml"].decode("utf-8"))
        except yaml.YAMLError as exc:
            raise ValueError("Invalid experiment YAML") from exc
        if (not isinstance(config, dict) or config.get("synthetic") is not True
            or type(config.get("schema_version")) is not int or config["schema_version"] != 1):
            raise ValueError(f"Expected a synthetic YAML mapping: {path}")
        run_id = config["run"]["experiment_id"]
        if run_id != path.parent.name or run_id in seen_ids:
            raise ValueError("Experiment IDs must be unique and match their directory")
        seen_ids.add(run_id)
        fields = flatten(config)
        issues = [f"MISSING_CONDITION:{key}" for key in REQUIRED_CONDITIONS
                  if key not in fields or fields[key] is None]
        if fields.get("provenance.config_is_effective") is not True:
            issues.append("CONFIG_NOT_EFFECTIVE")
        files = [{"path": str((path.parent / name).relative_to(root)),
                  "sha256": hashlib.sha256(content).hexdigest()}
                 for name, content in contents.items()]
        entry = {"experiment_id": run_id, "synthetic": True, "config": config,
                 "config_fingerprint": hashlib.sha256(canonical(semantic_fields(config)).encode()).hexdigest(),
                 "files": files, "metrics": None, "rows": [], "issues": issues,
                 "confusion": None}
        if config["run"]["status"] == "failed":
            entry["status"] = "failed"
        elif config["run"]["status"] != "completed":
            entry["status"] = "incomplete_results"
            issues.append("TRAINING_NOT_COMPLETED")
        else:
            try:
                entry["rows"], entry["metrics"], result_issues = read_results(path.parent, config, contents)
                entry["confusion"] = json.loads(contents["confusion_matrices.json"])
                issues.extend(result_issues)
                entry["status"] = "incomplete_results" if result_issues else (
                    "unknown_conditions" if issues else "ready")
            except (OSError, ValueError, KeyError, TypeError, OverflowError, csv.Error) as exc:
                entry["status"] = "invalid_results"
                issues.append(f"RESULT_VALIDATION_FAILED:{type(exc).__name__}")
        entries.append(entry)
    if not entries:
        raise ValueError("No experiment configurations found")
    return entries


def search(entries: list[dict], filters: dict, status: str | None = "ready") -> list[dict]:
    matches = []
    for entry in entries:
        if status is not None and entry["status"] != status:
            continue
        fields = flatten(entry["config"])
        if all(key in fields and canonical(fields[key]) == canonical(value)
               for key, value in filters.items()):
            matches.append(entry)
    return matches


def controls(entries: list[dict], baseline_id: str, changed_paths: set[str]) -> list[str]:
    baseline = next(entry for entry in entries if entry["experiment_id"] == baseline_id)
    if baseline["status"] != "ready" or not changed_paths:
        raise ValueError("Controls need a ready baseline and explicit changed paths")
    return [entry["experiment_id"] for entry in entries if entry["status"] == "ready"
            and {item["path"] for item in config_diff(baseline["config"], entry["config"])} == changed_paths]


def metric_comparison(left: dict, right: dict, changed_paths: set[str]) -> list[dict]:
    actual = {item["path"] for item in config_diff(left["config"], right["config"])}
    if left["status"] != "ready" or right["status"] != "ready" or actual != changed_paths:
        raise ValueError("Experiments are not ready or contain undeclared configuration differences")
    populations = [{(row["snr_db"], row["modulation"]): row["n_total"] for row in entry["rows"]}
                   for entry in (left, right)]
    if populations[0] != populations[1]:
        raise ValueError("Experiments have different evaluation sample populations")
    output = []
    for snr in left["config"]["evaluation"]["snr_db"]:
        groups = [[row for row in entry["rows"] if row["snr_db"] == snr] for entry in (left, right)]
        totals = [sum(row["n_total"] for row in group) for group in groups]
        correct = [sum(row["n_correct"] for row in group) for group in groups]
        output.append({"synthetic": "true", "left_experiment_id": left["experiment_id"],
                       "right_experiment_id": right["experiment_id"], "snr_db": snr,
                       "left_n_total": totals[0], "left_n_correct": correct[0],
                       "right_n_total": totals[1], "right_n_correct": correct[1],
                       "left_accuracy": correct[0] / totals[0], "right_accuracy": correct[1] / totals[1],
                       "delta_percentage_points": round(100 * (correct[1] / totals[1] - correct[0] / totals[0]), 10)})
    return output
