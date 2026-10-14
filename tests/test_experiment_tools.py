import copy
import csv
import hashlib
import json
import shutil
import types
from contextlib import contextmanager
from uuid import uuid4

import pytest
import yaml
from langchain.tools import ToolRuntime
from langchain_core.language_models.fake_chat_models import FakeMessagesListChatModel
from langchain_core.messages import AIMessage, ToolMessage
from langchain_core.runnables import RunnableLambda
from langgraph.checkpoint.memory import InMemorySaver
from pydantic import ValidationError

from agent_service import execution, tooling
from agent_service.manifest import execution_manifest
from agent_service.runtime import BudgetExhausted
from agent_service.tools import experiments
from agent_service.tooling import RecoverableToolError, RunExecutionState, load_providers, merge_outcome
from execution.contracts import ExecutionContext, TaskRef, TaskKind, ToolOutcome, freeze, thaw
from execution.inputs import capture_experiment_data
from datetime import datetime, timedelta, timezone

def capture_snapshot(settings):
    return capture_experiment_data(settings, experiments.VERSION)
from contracts.v1 import Evidence, Result
from experiment_service.catalogue import canonical, register
from scripts.synthetic_experiments import generate


BASE = "20261001_090000"
ATTENTION = "20261001_103000"
VARIABLE = "model.attention.enabled"


@pytest.fixture
def source(tmp_path):
    root = tmp_path / "fixture"
    generate(root)
    return experiments.Settings(root)


@pytest.fixture
def harness(source, monkeypatch):
    snapshot = capture_snapshot(source)
    calls = []
    state = RunExecutionState(uuid4(), uuid4())
    context = ExecutionContext(TaskRef(state.run_id, TaskKind.AGENT), state.lease_token,
                               datetime.now(timezone.utc)+timedelta(minutes=5), call_id=uuid4(),
                               inputs=freeze({'experiment':snapshot}))
    # Domain tests can replace their input dataset without involving a database.
    monkeypatch.setattr(experiments, '_inputs', lambda *args: calls.append(args) or snapshot)
    runtime = ToolRuntime(state={}, context=context, config={}, stream_writer=lambda _: None,
                          tool_call_id='test-call', store=None)
    tools = {item.tool.name:item.tool for item in experiments._sync_provider(source).tools}
    metadata = {}
    def invoke(name, **arguments):
        content, artifact = tools[name].func(runtime=runtime, **arguments)
        outcome = ToolOutcome.from_artifact(artifact)
        merge_outcome(state, outcome)
        metadata['test-call'] = thaw(outcome.metadata)
        return json.loads(content)
    return types.SimpleNamespace(settings=source, snapshot=snapshot, context=state, readonly=context,
                                 runtime=runtime, tools=tools, invoke=invoke, calls=calls, metadata=metadata)


@pytest.fixture
def large_catalogue(harness):
    """Forty real, validated experiments at the per-experiment file limit."""
    root = harness.settings.root
    originals = {path.name: path.read_bytes() for path in (root / "experiments" / BASE).iterdir()}
    shutil.rmtree(root / "experiments")
    ids = [BASE] + [f"20261008_000{index:03d}" for index in range(1, 40)]
    for index, experiment_id in enumerate(ids):
        directory = root / "experiments" / experiment_id
        directory.mkdir(parents=True)
        for name, content in originals.items():
            (directory / name).write_bytes(content.replace(BASE.encode(), experiment_id.encode()))
        path = directory / "config.yaml"
        config = yaml.safe_load(path.read_text())
        config["model"]["attention"]["enabled"] = index > 0
        path.write_text(yaml.safe_dump(config), encoding="utf-8")
        for number in range(64 - len(originals)):
            (directory / f"附加实验资料_{number:02d}.txt").write_text("small input\n", encoding="utf-8")
    snapshot = capture_snapshot(harness.settings)
    assert len(snapshot["entries"]) == 40
    assert all(entry["status"] == "ready" and len(entry["files"]) == 64 for entry in snapshot["entries"])
    harness.snapshot.clear()
    harness.snapshot.update(snapshot)
    return harness


def assert_page_sources(harness, result, ids):
    locator = Evidence.model_validate(result["evidences"][0]).source_locator
    assert locator.experiment_ids == ids
    expected = {file["path"]: file["sha256"] for entry in harness.snapshot["entries"]
                if entry["experiment_id"] in ids for file in entry["files"]}
    assert {file.path: file.sha256 for file in locator.source_files} == expected
    assert [file.path for file in locator.source_files] == sorted(expected)
    assert locator.snapshot_sha256 == harness.snapshot["snapshot_sha256"]
    for file in locator.source_files:
        assert file.sha256 == hashlib.sha256((harness.settings.root / file.path).read_bytes()).hexdigest()


def test_large_catalogue_exact_search_has_only_page_provenance(large_catalogue):
    result = large_catalogue.invoke("search_experiments", filters={"run.experiment_id": BASE}, limit=1)
    assert result["data"]["total"] == 1
    assert result["data"]["next_offset"] is None
    assert_page_sources(large_catalogue, result, [BASE])


@pytest.mark.parametrize("operation", ["search_experiments", "find_experiment_controls"])
def test_large_catalogue_byte_pagination_visits_every_match(large_catalogue, operation):
    harness = large_catalogue
    expected = [entry["experiment_id"] for entry in harness.snapshot["entries"]]
    arguments = {"filters": {}} if operation == "search_experiments" else {
        "baseline_id": BASE, "changed_paths": [VARIABLE]}
    if operation == "find_experiment_controls":
        expected.remove(BASE)
    offset, visited, pages = 0, [], []
    while True:
        raw, artifact = harness.tools[operation].func(runtime=harness.runtime, limit=50, offset=offset, **arguments)
        merge_outcome(harness.context, ToolOutcome.from_artifact(artifact))
        assert len(raw.encode("utf-8")) <= experiments.MAX_RESPONSE_BYTES
        result = json.loads(raw)
        data = result["data"]
        ids = ([item["experiment_id"] for item in data["items"]]
               if operation == "search_experiments" else data["experiment_ids"])
        assert ids and data["total"] == len(expected) and data["offset"] == offset
        assert_page_sources(harness, result, ids if operation == "search_experiments" else [BASE, *ids])
        pages.append(result)
        visited.extend(ids)
        if data["next_offset"] is None:
            break
        assert data["next_offset"] == offset + len(ids)
        offset = data["next_offset"]
        assert len(pages) <= len(expected)
    assert len(pages) > 1  # A full count-limited page exceeds the actual byte budget.
    assert visited == expected
    assert len(harness.context.evidence) == len(pages)  # No evidence for discarded trial pages.


@pytest.mark.parametrize("operation,arguments,sources", [
    ("search_experiments", {"filters": {"training.seed": 999}}, []),
    ("search_experiments", {"filters": {}, "offset": 999}, []),
    ("find_experiment_controls", {"baseline_id": BASE, "changed_paths": [VARIABLE], "offset": 999}, [BASE]),
    ("find_experiment_controls", {"baseline_id": BASE, "changed_paths": ["training.epochs"]}, [BASE]),
])
def test_empty_pages_have_snapshot_evidence_without_catalogue_file_list(harness, operation, arguments, sources):
    result = harness.invoke(operation, **arguments)
    data = result["data"]
    assert data["next_offset"] is None
    assert data.get("items", data.get("experiment_ids")) == []
    assert_page_sources(harness, result, sources)


@pytest.mark.parametrize("arguments", [{"limit": 0}, {"limit": 51}, {"offset": -1}])
def test_control_pagination_rejects_invalid_arguments(harness, arguments):
    with pytest.raises(RecoverableToolError) as error:
        harness.invoke("find_experiment_controls", baseline_id=BASE, changed_paths=[VARIABLE], **arguments)
    assert error.value.code == "INVALID_EXPERIMENT_FILTER"
    assert not harness.context.evidence


@pytest.mark.parametrize("operation", ["search_experiments", "find_experiment_controls"])
def test_single_result_over_byte_budget_leaves_context_unchanged(harness, monkeypatch, operation):
    arguments = ({"filters": {"run.experiment_id": BASE}} if operation == "search_experiments" else
                 {"baseline_id": BASE, "changed_paths": [VARIABLE]})
    existing = harness.invoke("read_experiment", experiment_id=BASE)
    before = copy.deepcopy((harness.context.evidence, harness.context.notices, harness.metadata))
    monkeypatch.setattr(experiments, "MAX_RESPONSE_BYTES", 1)
    with pytest.raises(RecoverableToolError) as error:
        harness.invoke(operation, **arguments)
    assert error.value.code == "EXPERIMENT_RESULT_TOO_LARGE"
    assert "single" in error.value.message.lower()
    assert (harness.context.evidence, harness.context.notices, harness.metadata) == before
    assert existing["evidences"][0]["evidence_id"] in harness.context.evidence


def test_response_budget_counts_utf8_bytes_and_accepts_exact_boundary(harness, monkeypatch):
    baseline = next(entry for entry in harness.snapshot["entries"] if entry["experiment_id"] == BASE)
    baseline["issues"] = ["测试字符" * 200]
    arguments = {"filters": {"run.experiment_id": BASE}, "limit": 1}
    raw, _artifact = harness.tools["search_experiments"].func(runtime=harness.runtime, **arguments)
    byte_count = len(raw.encode("utf-8"))
    assert byte_count > len(raw)
    monkeypatch.setattr(experiments, "MAX_RESPONSE_BYTES", byte_count)
    assert harness.tools["search_experiments"].func(runtime=harness.runtime, **arguments)[0] == raw
    before = copy.deepcopy((harness.context.evidence, harness.context.notices, harness.metadata))
    monkeypatch.setattr(experiments, "MAX_RESPONSE_BYTES", byte_count - 1)
    with pytest.raises(RecoverableToolError):
        harness.invoke("search_experiments", **arguments)
    assert (harness.context.evidence, harness.context.notices, harness.metadata) == before


def test_csv_parser_error_is_retained_in_snapshot_and_status_search(harness):
    path = harness.settings.root / "experiments" / BASE / "results_by_snr.csv"
    path.write_text("experiment_id\n" + "x" * (csv.field_size_limit() + 1) + "\n", encoding="utf-8")
    assert path.stat().st_size < 2 * 1024 * 1024
    snapshot = capture_snapshot(harness.settings)
    harness.snapshot.clear()
    harness.snapshot.update(snapshot)
    entry = next(entry for entry in snapshot["entries"] if entry["experiment_id"] == BASE)
    assert entry["status"] == "invalid_results" and entry["issues"] == ["RESULT_VALIDATION_FAILED:Error"]
    assert entry["rows"] == [] and entry["metrics"] is None and entry["confusion"] is None
    assert len(snapshot["entries"]) == 12
    assert harness.invoke("search_experiments", filters={"run.experiment_id": BASE})["data"]["total"] == 0
    for status in (None, "invalid_results"):
        result = harness.invoke("search_experiments", filters={"run.experiment_id": BASE}, status=status)
        assert result["data"]["items"][0]["status"] == "invalid_results"


def test_factory_manifest_and_model_cannot_supply_identity(source, monkeypatch):
    monkeypatch.setenv("AGENT_EXPERIMENT_ROOT", str(source.root))
    monkeypatch.setenv("AGENT_TOOL_PROVIDERS", "agent_service.tools.experiments:tools")
    provider = load_providers()[0][1]
    assert provider.name == "experiment_analysis" and len(provider.tools) == 6
    for declaration in provider.tools:
        assert declaration.kind == "read_only" and declaration.produces_evidence
        assert not ({"runtime", "team_id", "root", "run_id", "lease_token"} & declaration.tool.args.keys())
    manifest = execution_manifest()
    assert manifest["tools"]["providers"][0]["name"] == "experiment_analysis"
    assert provider.version == "2" and all(declaration.version == "2" for declaration in provider.tools)
    assert str(source.root) not in canonical(manifest)


@pytest.mark.parametrize("invalid", ["missing_root", "relative_root", "invalid_root"])
def test_invalid_configuration_fails_before_execution(source, monkeypatch, invalid):
    monkeypatch.setenv("AGENT_EXPERIMENT_ROOT", str(source.root))
    if invalid == "missing_root":
        monkeypatch.delenv("AGENT_EXPERIMENT_ROOT")
    elif invalid == "relative_root":
        monkeypatch.setenv("AGENT_EXPERIMENT_ROOT", "examples/amc_experiments")
    else:
        monkeypatch.setenv('AGENT_EXPERIMENT_ROOT', str(source.root / 'absent'))
    with pytest.raises(RuntimeError, match="AGENT_EXPERIMENT_ROOT"):
        experiments.tools()


def test_search_exact_types_status_pagination_and_no_hits(harness):
    result = harness.invoke("search_experiments", filters={VARIABLE: True}, limit=2)
    assert result["data"]["total"] == 5
    assert result["data"]["next_offset"] == 2
    assert len(result["data"]["items"]) == 2
    assert harness.invoke("search_experiments", filters={VARIABLE: 1})["data"]["total"] == 0
    assert harness.invoke("search_experiments", filters={}, status=None)["data"]["total"] == 12
    assert harness.invoke("search_experiments", filters={}, status="ready")["data"]["total"] == 9
    assert harness.invoke("search_experiments", filters={}, status="failed")["data"]["items"][0]["experiment_id"] == "20261003_103000"
    no_hits = harness.invoke("search_experiments", filters={"training.seed": 999})
    assert no_hits["data"]["total"] == 0 and no_hits["evidences"]


@pytest.mark.parametrize("arguments", [
    {"filters": {"model.attentoin.enabled": True}}, {"filters": {}, "status": "complete"},
    {"filters": {}, "limit": 0}, {"filters": {}, "limit": 51}, {"filters": {}, "offset": -1},
])
def test_invalid_search_is_recoverable(harness, arguments):
    with pytest.raises(RecoverableToolError) as error:
        harness.invoke("search_experiments", **arguments)
    assert error.value.code == "INVALID_EXPERIMENT_FILTER"


def test_read_diff_controls_and_metrics_have_verified_file_sources(harness):
    detail = harness.invoke("read_experiment", experiment_id=BASE)
    assert detail["data"]["config"]["training"]["seed"] == 42
    diff = harness.invoke("compare_experiment_configs", left_id=BASE, right_id=ATTENTION)
    assert [item["path"] for item in diff["data"]["changes"]] == [VARIABLE]
    controls = harness.invoke("find_experiment_controls", baseline_id=BASE, changed_paths=[VARIABLE])
    assert controls["data"]["experiment_ids"] == [ATTENTION]
    assert controls["data"]["total"] == 1 and controls["data"]["offset"] == 0
    assert controls["data"]["next_offset"] is None
    result = harness.invoke("compare_experiment_metrics", left_id=BASE, right_id=ATTENTION, changed_paths=[VARIABLE])
    assert result["data"]["aggregates"]["overall"]["delta_percentage_points"] == 5.5
    assert result["data"]["aggregates"]["low_snr"]["delta_percentage_points"] == 11
    assert len(result["data"]["by_snr"]) == 5
    evidence = Evidence.model_validate(result["evidences"][0])
    assert evidence.document_id is None and evidence.source_locator.kind == "experiment"
    assert set(evidence.source_locator.experiment_ids) == {BASE, ATTENTION}
    assert "sum(n_correct)/sum(n_total)" in evidence.source_locator.method
    for file in evidence.source_locator.source_files:
        assert file.sha256 == hashlib.sha256((harness.settings.root / file.path).read_bytes()).hexdigest()
    metadata = harness.metadata["test-call"]
    assert metadata["evidence_ids"] == [evidence.evidence_id]
    assert "config" not in canonical(metadata) and "rows" not in canonical(metadata)
    assert harness.context.notices == [experiments.SYNTHETIC_NOTICE]


@pytest.mark.parametrize("right_id,changes", [
    ("20261002_133000", [VARIABLE]),
    ("20261002_133000", [VARIABLE, "data.dataset.split.fingerprint"]),
    ("20261003_103000", [VARIABLE]), ("20261003_120000", [VARIABLE]),
    ("20261003_133000", [VARIABLE]), ("20261002_103000", [VARIABLE]),
])
def test_strict_metric_comparison_rejects_invalid_controls(harness, right_id, changes):
    with pytest.raises(RecoverableToolError) as error:
        harness.invoke("compare_experiment_metrics", left_id=BASE, right_id=right_id, changed_paths=changes)
    assert error.value.code == "INCOMPARABLE_EXPERIMENTS"
    assert not harness.context.evidence


def test_seed_difference_is_explicit_and_duplicate_config_is_not_a_new_repeat(harness):
    result = harness.invoke("compare_experiment_metrics", left_id=BASE, right_id="20261002_090000", changed_paths=["training.seed"])
    assert result["data"]["interpretation"] == "descriptive_single_pair"
    repeated = harness.invoke("compare_experiment_configs", left_id=BASE, right_id="20261003_090000")
    assert repeated["data"]["identical_effective_config"]
    assert harness.invoke("compare_experiment_metrics", left_id=BASE, right_id="20261003_090000", changed_paths=[])["data"]["aggregates"]["overall"]["delta_percentage_points"] == 0


def test_matching_config_does_not_hide_different_sample_counts(harness):
    right = next(entry for entry in harness.snapshot["entries"] if entry["experiment_id"] == ATTENTION)
    right["rows"][0]["n_total"] += 1
    with pytest.raises(RecoverableToolError) as error:
        harness.invoke("compare_experiment_metrics", left_id=BASE, right_id=ATTENTION, changed_paths=[VARIABLE])
    assert error.value.code == "INCOMPARABLE_EXPERIMENTS"


def test_non_effective_yaml_config_is_excluded_from_ready(source):
    import yaml
    path = source.root / "experiments" / BASE / "config.yaml"
    config = yaml.safe_load(path.read_text())
    config["provenance"]["config_is_effective"] = False
    path.write_text(yaml.safe_dump(config))
    baseline = register(source.root)[0]
    assert baseline["status"] == "unknown_conditions"
    assert "CONFIG_NOT_EFFECTIVE" in baseline["issues"]


def test_confusions_reconcile_with_overall_errors_and_row_denominators(harness):
    result = harness.invoke("summarize_experiment_confusions", experiment_id=BASE, top_k=50)
    data = result["data"]
    assert data["total_errors"] == 800 - 560
    assert sum(pair["count"] for pair in data["pairs"]) == 240
    assert all(pair["true_label"] != pair["predicted_label"] for pair in data["pairs"])
    assert [pair["count"] for pair in data["pairs"]] == sorted((pair["count"] for pair in data["pairs"]), reverse=True)
    for pair in data["pairs"]:
        assert pair["error_fraction"] == pair["count"] / pair["true_class_n_total"]
    single = harness.invoke("summarize_experiment_confusions", experiment_id=BASE, snr_db=-10, top_k=1)
    assert single["data"]["snr_db"] == [-10] and len(single["data"]["pairs"]) == 1
    with pytest.raises(RecoverableToolError):
        harness.invoke("summarize_experiment_confusions", experiment_id=BASE, snr_db=99)


def test_tool_execution_context_and_inputs_are_readonly(harness):
    from dataclasses import FrozenInstanceError
    with pytest.raises(FrozenInstanceError):
        harness.readonly.lease_token = uuid4()
    with pytest.raises(TypeError):
        harness.readonly.inputs['experiment']['project_id'] = 'changed'
    assert not hasattr(harness.readonly, 'evidence')
    assert not hasattr(harness.readonly, 'notices')


def test_unknown_id_and_path_cannot_access_arbitrary_files(harness):
    for value in ("unknown", "../../.env", str(harness.settings.root)):
        with pytest.raises(RecoverableToolError) as error:
            harness.invoke("read_experiment", experiment_id=value)
        assert error.value.code == "EXPERIMENT_NOT_FOUND"


@pytest.mark.parametrize("target", ["folder", "config", "results"])
def test_catalogue_rejects_symbolic_links(source, tmp_path, target):
    outside = tmp_path / "outside"
    outside.write_text("not experiment input")
    if target == "folder":
        (source.root / "experiments/foreign").symlink_to(tmp_path, target_is_directory=True)
    else:
        path = source.root / "experiments" / BASE / ("config.yaml" if target == "config" else "results_by_snr.csv")
        path.unlink()
        path.symlink_to(outside)
    with pytest.raises(RecoverableToolError) as error:
        capture_snapshot(source)
    assert error.value.code == "EXPERIMENT_SOURCE_INVALID"


def test_catalogue_limits_and_yaml_error_are_bounded(source):
    with pytest.raises(ValueError, match="count"):
        register(source.root, max_experiments=1)
    with pytest.raises(ValueError, match="bounded"):
        register(source.root, max_file_bytes=1)
    with pytest.raises(ValueError, match="bytes"):
        register(source.root, max_total_bytes=1)
    (source.root / "experiments" / BASE / "config.yaml").write_text("broken: [}")
    with pytest.raises(RecoverableToolError) as error:
        capture_snapshot(source)
    assert error.value.code == "EXPERIMENT_SOURCE_INVALID"


def test_document_contract_remains_required_and_experiment_roundtrip(harness):
    document = {"evidence_id": "doc_ev", "document_id": "doc", "document_version_id": "v1",
                "title": "Paper", "content": "Text", "source_locator": {"kind": "pdf", "page_start": 3}}
    assert Evidence.model_validate(document).model_dump(exclude_none=True) == document
    with pytest.raises(ValidationError, match="requires document"):
        Evidence.model_validate({key: value for key, value in document.items() if key != "document_id"})
    result = harness.invoke("read_experiment", experiment_id=BASE)
    citation = result["evidences"][0]
    response = Result(answer="Simulated outcome", claims=[{"text": "Simulation", "support": "evidence",
                      "evidence_ids": [citation["evidence_id"]]}], citations=[citation], notices=[])
    assert Result.model_validate_json(response.model_dump_json()).citations[0].source_locator.kind == "experiment"
    with pytest.raises(ValidationError, match="experiment source identity"):
        Evidence.model_validate({**citation, "document_id": "fake_doc"})
