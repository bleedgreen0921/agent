import json

import pytest

from scripts.experiment_fixture_demo import (
    config_diff, controls, main, metric_comparison, register, run_demo, search, verify,
)
from scripts.synthetic_experiments import generate, payloads


@pytest.fixture
def fixture_root(tmp_path):
    root = tmp_path / "experiments_fixture"
    generate(root)
    return root


def test_generated_fixture_matches_reproducible_generator(fixture_root):
    files = payloads()
    assert len(files) == 73
    for relative, content in files.items():
        assert (fixture_root / relative).read_bytes() == content.encode("utf-8"), relative


def test_generation_is_repeatable_and_refuses_modified_files(fixture_root):
    before = {path.relative_to(fixture_root): path.read_bytes()
              for path in fixture_root.rglob("*") if path.is_file()}
    generate(fixture_root)
    assert before == {path.relative_to(fixture_root): path.read_bytes()
                      for path in fixture_root.rglob("*") if path.is_file()}
    changed = fixture_root / "experiments/20261001_103000/config.yaml"
    changed.write_text("user-modified configuration\n", encoding="utf-8")
    removed = fixture_root / "experiments/20261001_090000/notes.md"
    removed.unlink()
    with pytest.raises(FileExistsError, match="Refusing to overwrite"):
        generate(fixture_root)
    assert changed.read_text(encoding="utf-8") == "user-modified configuration\n"
    assert not removed.exists()  # Collision preflight must precede all writes.


def test_registration_search_and_expected_comparisons(fixture_root):
    entries = register(fixture_root)
    expected = json.loads((fixture_root / "expected.json").read_text(encoding="utf-8"))
    result = verify(entries, expected)
    assert len(entries) == 12
    assert result["passed"] and len(result["checks"]) == 16
    attention = search(entries, {"model.attention.enabled": True,
                                 "data.dataset.split.fingerprint": "synthetic-split-v1"})
    assert [item["experiment_id"] for item in attention] == [
        "20261001_103000", "20261001_133000", "20261002_103000", "20261002_120000"]
    assert controls(entries, "20261001_090000", {"model.attention.enabled"}) == ["20261001_103000"]


def test_canonical_config_ignores_runtime_metadata_but_not_seed(fixture_root):
    by_id = {item["experiment_id"]: item for item in register(fixture_root)}
    base, repeated = by_id["20261001_090000"], by_id["20261003_090000"]
    assert config_diff(base["config"], repeated["config"]) == []
    assert base["config_fingerprint"] == repeated["config_fingerprint"]
    assert [item["path"] for item in config_diff(base["config"], by_id["20261002_090000"]["config"])] == ["training.seed"]


@pytest.mark.parametrize("left,right", [
    ({"model": {"enabled": True}}, {"model": {"enabled": 1}}),
    ({"model": {"value": 1}}, {"model": {"value": "1"}}),
    ({"model": {}}, {"model": {"value": None}}),
    ({"model": {"channels": [32, 64]}}, {"model": {"channels": [64, 32]}}),
])
def test_diff_preserves_types_absence_and_list_order(left, right):
    assert config_diff(left, right)


def test_missing_field_is_distinct_from_null_and_false_in_search():
    entries = [{"experiment_id": str(index), "status": "ready", "config": config}
               for index, config in enumerate([{}, {"x": None}, {"x": False}, {"x": 0}])]
    assert [item["experiment_id"] for item in search(entries, {"x": None})] == ["1"]
    assert [item["experiment_id"] for item in search(entries, {"x": False})] == ["2"]


def test_metrics_are_sample_weighted_and_not_an_average_of_groups(fixture_root):
    entries = register(fixture_root)
    baseline, attention = entries[:2]
    assert baseline["metrics"]["n_total"] == 800
    assert baseline["metrics"]["n_correct"] == 560
    assert baseline["metrics"]["accuracy"] == 0.7
    assert attention["metrics"]["accuracy"] == 0.755
    comparisons = metric_comparison(baseline, attention, {"model.attention.enabled"})
    group_average = sum(row["left_accuracy"] for row in comparisons) / len(comparisons)
    assert group_average == pytest.approx(0.6281666666666667)
    assert group_average != pytest.approx(baseline["metrics"]["accuracy"])
    assert [row["delta_percentage_points"] for row in comparisons] == pytest.approx([12.5, 10, 8.75, 3, 0.8333333333])


@pytest.mark.parametrize("run_id", ["20261002_133000", "20261003_103000", "20261003_120000", "20261003_133000"])
def test_strict_comparison_rejects_changed_split_or_incomplete_conditions(fixture_root, run_id):
    by_id = {item["experiment_id"]: item for item in register(fixture_root)}
    with pytest.raises(ValueError, match="not ready|undeclared"):
        metric_comparison(by_id["20261001_090000"], by_id[run_id], {"model.attention.enabled"})


@pytest.mark.parametrize("file,mutation", [
    ("metrics.json", "summary_count"),
    ("confusion_matrices.json", "matrix_count"),
    ("confusion_matrices.json", "class_order"),
])
def test_corrupted_json_cannot_pass_result_validation(fixture_root, file, mutation):
    path = fixture_root / "experiments/20261001_090000" / file
    value = json.loads(path.read_text(encoding="utf-8"))
    if mutation == "summary_count":
        value["n_correct"] += 1
    elif mutation == "matrix_count":
        value["matrices"][0]["counts"][0][0] += 1
    else:
        value["labels"].reverse()
    path.write_text(json.dumps(value), encoding="utf-8")
    entry = register(fixture_root)[0]
    assert entry["status"] == "invalid_results"
    assert entry["issues"] == ["RESULT_VALIDATION_FAILED:ValueError"]


def test_missing_results_remain_registered_and_excluded(fixture_root):
    (fixture_root / "experiments/20261001_103000/results_by_snr.csv").unlink()
    entries = register(fixture_root)
    assert len(entries) == 12
    assert entries[1]["status"] == "invalid_results"
    assert entries[1]["issues"] == ["RESULT_VALIDATION_FAILED:FileNotFoundError"]
    assert controls(entries, "20261001_090000", {"model.attention.enabled"}) == []


def test_verification_failure_does_not_publish_demo_outputs(fixture_root, tmp_path):
    expected_path = fixture_root / "expected.json"
    expected = json.loads(expected_path.read_text(encoding="utf-8"))
    expected["known_metrics"]["20261001_090000"]["accuracy"] = 0.9
    expected_path.write_text(json.dumps(expected), encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(ValueError, match="expectations failed"):
        run_demo(fixture_root, output)
    assert not output.exists()


def test_demo_persists_index_and_reports_without_modifying_inputs(fixture_root, tmp_path):
    before = {path: path.read_bytes() for path in fixture_root.rglob("*") if path.is_file()}
    output = tmp_path / "output"
    result = run_demo(fixture_root, output)
    assert result["registered"] == 12 and result["verified_checks"] == 16
    assert {path.name for path in output.iterdir()} == {
        "catalogue.json", "attention_search.json", "config_diff.csv",
        "accuracy_comparison.csv", "report.md", "validation.json"}
    saved = {path.name: path.read_bytes() for path in output.iterdir()}
    run_demo(fixture_root, output)
    assert saved == {path.name: path.read_bytes() for path in output.iterdir()}
    assert before == {path: path.read_bytes() for path in fixture_root.rglob("*") if path.is_file()}
    registered = json.loads((output / "catalogue.json").read_text(encoding="utf-8"))
    assert len(registered["experiments"]) == 12
    assert "5.50 个百分点" in (output / "report.md").read_text(encoding="utf-8")
    with pytest.raises(ValueError, match="outside"):
        run_demo(fixture_root, fixture_root / "outputs")


@pytest.mark.parametrize("failure", ["missing_directory", "invalid_yaml", "invalid_expectations"])
def test_demo_cli_reports_original_failure_without_publishing(fixture_root, tmp_path, capsys, failure):
    root = fixture_root
    if failure == "missing_directory":
        root = tmp_path / "nonexistent"
    elif failure == "invalid_yaml":
        (root / "experiments/20261001_090000/config.yaml").write_text("broken: [}", encoding="utf-8")
    else:
        path = root / "expected.json"
        expected = json.loads(path.read_text())
        expected["known_metrics"]["20261001_090000"]["accuracy"] = 0.9
        path.write_text(json.dumps(expected), encoding="utf-8")
    output = tmp_path / "output"
    with pytest.raises(SystemExit) as error:
        main(["--data", str(root), "--output", str(output)])
    assert error.value.code == 1
    captured = capsys.readouterr()
    assert captured.out == ""
    assert captured.err.startswith("Experiment demo failed: ")
    assert "NameError" not in captured.err and "Traceback" not in captured.err
    expected_message = {"missing_directory": "nonexistent", "invalid_yaml": "Invalid experiment YAML",
                        "invalid_expectations": "Fixture expectations failed"}[failure]
    assert expected_message in captured.err
    assert not output.exists()
