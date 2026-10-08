import copy
import hashlib
import json
import types
import xml.etree.ElementTree as ET
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import pytest
from langchain.tools import ToolRuntime
from PIL import Image

from agent_service.tools import experiments, experiment_plots
from agent_service.tooling import FatalToolError, RecoverableToolError, ToolExecutionContext
from experiment_service import plotting
from scripts.synthetic_experiments import generate
from scripts.experiment_plot_demo import main

BASE = "20261001_090000"
ATTENTION = "20261001_103000"
IDS = [BASE, ATTENTION, "20261001_120000", "20261001_133000", "20261002_090000"]


@pytest.fixture
def snapshot(tmp_path):
    root = tmp_path / "inputs"
    generate(root)
    return experiments.capture_snapshot(experiments.Settings(root, uuid4()))


@pytest.mark.parametrize("count", [1, 2, 5])
def test_accuracy_weighted_points_order_and_complete_config_differences(snapshot, count):
    data, sources = plotting.accuracy_data(snapshot, IDS[:count])
    assert data["experiment_ids"] == IDS[:count]
    assert [entry["experiment_id"] for entry in sources] == IDS[:count]
    assert data["curves"][0]["points"][0] == {"snr_db": -10, "n_correct": 26, "n_total": 80,
                                               "accuracy": 0.325, "accuracy_percent": 32.5}
    totals = [sum(point["n_correct"] for point in curve["points"]) for curve in data["curves"]]
    assert totals[:2] == [560, 604][:count]
    if count == 5:
        assert {item["path"] for item in data["curves"][4]["config_changes_from_first"]} == {"training.seed"}
    # Unequal class populations demonstrate weighting, independent of fixture's balanced classes.
    source = copy.deepcopy(snapshot)
    entry = next(item for item in source["entries"] if item["experiment_id"] == BASE)
    entry["rows"][0].update(n_total=100, n_correct=50)
    result, _ = plotting.accuracy_data(source, [BASE])
    assert result["curves"][0]["points"][0]["accuracy"] == (50 + 8 + 5 + 4) / 160


@pytest.mark.parametrize("ids", [[], [BASE, BASE], IDS + ["20261002_103000"], ["unknown"],
                                  ["20261003_103000"], ["20261003_120000"], ["20261003_133000"]])
def test_invalid_accuracy_inputs(snapshot, ids):
    with pytest.raises(plotting.PlotInputError):
        plotting.accuracy_data(snapshot, ids)


@pytest.mark.parametrize("change", ["split", "classes", "snr", "samples", "evaluation"])
def test_incompatible_populations(snapshot, change):
    entry = next(item for item in snapshot["entries"] if item["experiment_id"] == ATTENTION)
    if change == "split":
        entry["config"]["data"]["dataset"]["split"]["fingerprint"] = "other"
    elif change == "classes":
        entry["config"]["data"]["dataset"]["classes"].reverse()
    elif change == "snr":
        entry["config"]["evaluation"]["snr_db"].reverse()
    elif change == "samples":
        entry["rows"][0]["n_total"] += 1
    else:
        entry["config"]["evaluation"]["checkpoint_selection"] = "other"
    with pytest.raises(plotting.IncomparablePlots):
        plotting.accuracy_data(snapshot, [BASE, ATTENTION])


@pytest.mark.parametrize("normalization", ["count", "row"])
def test_confusion_direction_counts_and_aggregate_before_normalization(snapshot, normalization):
    data, sources = plotting.confusion_data(snapshot, BASE, normalization=normalization)
    counts = data["counts"]
    assert data["labels"] == ["BPSK", "QPSK", "16QAM", "64QAM"]
    assert sum(map(sum, counts)) == 800 and sum(counts[i][i] for i in range(4)) == 560
    entry = sources[0]
    for i in range(4):
        for j in range(4):
            assert counts[i][j] == sum(group["counts"][i][j] for group in entry["confusion"]["matrices"])
            assert data["display_matrix"][i][j] == (counts[i][j] if normalization == "count" else counts[i][j] / sum(counts[i]))
    if normalization == "row":
        mean = sum(group["counts"][0][0] / sum(group["counts"][0]) for group in entry["confusion"]["matrices"]) / 5
        assert data["display_matrix"][0][0] != mean
    selected, _ = plotting.confusion_data(snapshot, BASE, -10, normalization)
    assert selected["snr_db"] == [-10] and selected["counts"] == entry["confusion"]["matrices"][0]["counts"]


@pytest.mark.parametrize("kwargs", [{"snr_db": 99}, {"snr_db": True}, {"normalization": "other"},
                                     {"experiment_id": "unknown"}])
def test_invalid_confusion_inputs(snapshot, kwargs):
    with pytest.raises(plotting.PlotInputError):
        plotting.confusion_data(snapshot, **{"experiment_id": BASE, **kwargs})


@pytest.mark.parametrize("field,count", [("labels", 33), ("matrices", 129)])
def test_render_bounds(snapshot, field, count):
    snapshot["entries"][0]["confusion"][field] = list(range(count))
    with pytest.raises(plotting.PlotInputError):
        plotting.accuracy_data(snapshot, [BASE])


@pytest.mark.parametrize("kind", ["accuracy", "five", "count", "row"])
def test_render_decode_svg_labels_hashes_and_reproducibility(snapshot, tmp_path, kind):
    data, sources = (plotting.accuracy_data(snapshot, IDS if kind == "five" else IDS[:2]) if kind in {"accuracy", "five"}
                     else plotting.confusion_data(snapshot, BASE, normalization=kind))
    outputs = []
    for index in range(2):
        store = plotting.PlotStore(tmp_path / f"out-{index}", Path("offline"), snapshot, data, sources)
        with store.stage() as (_, publish):
            result = publish()
        files = {item["name"]: (store.root / item["path"]).read_bytes() for item in result["artifacts"]}
        for item in result["artifacts"]:
            assert len(files[item["name"]]) == item["size_bytes"]
            assert hashlib.sha256(files[item["name"]]).hexdigest() == item["sha256"]
        image = Image.open(store.root / result["artifacts"][0]["path"])
        image.load()
        assert image.size == ((1600, 1000) if kind in {"accuracy", "five"} else (1200, 1200))
        svg = ET.fromstring(files["plot.svg"])
        text = " ".join(svg.itertext())
        assert "Synthetic data" in text and BASE in text
        if kind in {"accuracy", "five"}:
            assert "SNR (dB)" in text and "Accuracy (%)" in text
        else:
            assert "True label" in text and "Predicted label" in text
            assert not list(svg.iter("{http://www.w3.org/2000/svg}image"))
        assert "<dc:date>" not in files["plot.svg"].decode()
        manifest = json.loads(files["manifest.json"])
        assert manifest["origin"] == "offline" and manifest["run_id"] is None and manifest["first_tool_call_id"] is None
        outputs.append(files)
    assert outputs[0] == outputs[1]


def test_repeated_and_concurrent_publication_reuses_first_manifest(snapshot, tmp_path):
    data, sources = plotting.accuracy_data(snapshot, [BASE])
    def invoke(index):
        store = plotting.PlotStore(tmp_path / "plots", Path("run"), snapshot, data, sources,
                                   run_id="run", tool_call_id=str(index))
        with store.stage() as (_, publish):
            return publish()
    with ThreadPoolExecutor(max_workers=3) as executor:
        results = list(executor.map(invoke, range(3)))
    assert results[0] == results[1] == results[2] == invoke(99)
    assert len(list((tmp_path / "plots/run").iterdir())) == 1
    image = tmp_path / "plots" / results[0]["artifacts"][0]["path"]
    image.write_bytes(b"corrupt")
    with pytest.raises(ValueError, match="hash"):
        invoke(4)
    assert image.read_bytes() == b"corrupt"


@pytest.fixture
def harness(snapshot, tmp_path, monkeypatch):
    settings = experiments.Settings(tmp_path / "inputs", uuid4())
    context = ToolExecutionContext(uuid4(), uuid4(), team_id=settings.team_id)
    context.tool_call_ids["call"] = uuid4()
    runtime = ToolRuntime(state={}, context=context, config={}, stream_writer=lambda _: None,
                          tool_call_id="call", store=None)
    monkeypatch.setattr(experiments, "load_snapshot", lambda *args: snapshot)
    @contextmanager
    def connection(*args, **kwargs):
        yield None
    monkeypatch.setattr(experiment_plots, "connect", connection)
    checks = []
    monkeypatch.setattr(experiments, "_authorize_run", lambda *args, **kwargs: checks.append(kwargs))
    root = tmp_path / "plots"
    tools = {item.tool.name: item for item in experiment_plots.build_provider(settings, root).tools}
    def invoke():
        return json.loads(tools["plot_experiment_accuracy"].tool.func(experiment_ids=[BASE, ATTENTION], runtime=runtime))
    return types.SimpleNamespace(root=root, context=context, runtime=runtime, checks=checks,
                                 tools=tools, invoke=invoke, settings=settings)


def test_tool_version_evidence_and_repeated_manifest(harness):
    result = harness.invoke()
    assert result == harness.invoke()
    assert harness.checks == [{"lock": True}, {"lock": False}] * 2
    assert all(item.kind == "side_effect" and item.produces_evidence and item.version == "1" for item in harness.tools.values())
    locator = result["evidences"][0]["source_locator"]
    assert locator["method_version"] == "1" and locator["synthetic"]
    assert harness.context.evidence[result["evidences"][0]["evidence_id"]] == result["evidences"][0]
    manifest = json.loads((harness.root / result["data"]["artifacts"][-1]["path"]).read_text())
    assert manifest["first_tool_call_id"] == str(harness.context.tool_call_ids["call"])
    assert all(not path.name.startswith(".tmp-") for path in harness.root.rglob("*"))


@pytest.mark.parametrize("existing", [False, True])
def test_plot_response_validation_expiry_prevents_publication_or_reuse(harness, monkeypatch, existing):
    if existing:
        harness.invoke()
        harness.context.evidence.clear()
        harness.context.tool_metadata.clear()
        harness.context.notices.clear()
    before = {path.relative_to(harness.root): path.read_bytes()
              for path in harness.root.rglob("*") if path.is_file()}
    build_response = experiments._build_response
    active = True
    calls = 0

    def expire_during_validation(*args, **kwargs):
        nonlocal active, calls
        result = build_response(*args, **kwargs)
        calls += 1
        if calls == 2:  # The response validation inside the publication callback.
            active = False
        return result

    def authorize(*args, **kwargs):
        if not active:
            raise PermissionError("execution lease was revoked")

    monkeypatch.setattr(experiments, "_build_response", expire_during_validation)
    monkeypatch.setattr(experiments, "_authorize_run", authorize)
    with pytest.raises(PermissionError, match="lease was revoked"):
        harness.invoke()
    assert not harness.context.evidence and not harness.context.tool_metadata and not harness.context.notices
    assert not list(harness.root.rglob(".tmp-*"))
    assert before == {path.relative_to(harness.root): path.read_bytes()
                      for path in harness.root.rglob("*") if path.is_file()}


def test_storage_permission_error_is_recoverable(harness, monkeypatch):
    monkeypatch.setattr(plotting, "render", lambda *args: (_ for _ in ()).throw(PermissionError("storage denied")))
    with pytest.raises(RecoverableToolError) as exc:
        harness.invoke()
    assert exc.value.code == "EXPERIMENT_PLOT_FAILED"
    assert not harness.context.evidence and not harness.context.tool_metadata
    assert not list(harness.root.rglob("plot.png")) and not list(harness.root.rglob(".tmp-*"))


def test_concurrent_rename_winner_requires_fresh_authorization(harness, monkeypatch):
    import errno
    import shutil

    winner_files = {}
    active = True
    build_response = experiments._build_response
    calls = 0

    def concurrent_rename(source_fd, source, target_fd, target, flags):
        # Simulate a writer outside the flock protocol winning RENAME_NOREPLACE.
        directory = Path(f"/proc/self/fd/{target_fd}") / target.decode()
        shutil.copytree(Path(f"/proc/self/fd/{source_fd}") / source.decode(), directory)
        winner_files.update({path.name: path.read_bytes() for path in directory.iterdir()})
        plotting.ctypes.set_errno(errno.EEXIST)
        return -1

    def expire_during_winner_validation(*args, **kwargs):
        nonlocal active, calls
        result = build_response(*args, **kwargs)
        calls += 1
        if calls == 3:  # Candidate before publish, candidate under flock, then winner.
            active = False
        return result

    def authorize(*args, **kwargs):
        if not active:
            raise PermissionError("execution lease was revoked")

    monkeypatch.setattr(plotting.ctypes, "CDLL", lambda *args, **kwargs: types.SimpleNamespace(renameat2=concurrent_rename))
    monkeypatch.setattr(experiments, "_build_response", expire_during_winner_validation)
    monkeypatch.setattr(experiments, "_authorize_run", authorize)
    with pytest.raises(PermissionError, match="lease was revoked"):
        harness.invoke()
    assert not harness.context.evidence and not harness.context.tool_metadata and not harness.context.notices
    assert not list(harness.root.rglob(".tmp-*"))
    assert winner_files and winner_files == {path.name: path.read_bytes()
                                            for path in harness.root.rglob("*") if path.is_file()}


@pytest.mark.parametrize("failure", ["render", "image_size", "data_size", "manifest_size", "response_size", "lease"])
def test_failed_tools_leave_no_evidence_metadata_or_artifacts(harness, monkeypatch, failure):
    if failure == "render":
        monkeypatch.setattr(plotting, "render", lambda *args: (_ for _ in ()).throw(RuntimeError("render failed")))
    elif failure == "image_size":
        monkeypatch.setattr(plotting, "MAX_IMAGE_BYTES", 1)
    elif failure == "data_size":
        monkeypatch.setattr(plotting, "MAX_JSON_BYTES", 1)
    elif failure == "manifest_size":
        data, _ = plotting.accuracy_data(experiments.load_snapshot(None, None), [BASE, ATTENTION])
        monkeypatch.setattr(plotting, "MAX_JSON_BYTES", len(plotting.canonical(data).encode()))
    elif failure == "response_size":
        monkeypatch.setattr(experiments, "MAX_RESPONSE_BYTES", 1)
    else:
        monkeypatch.setattr(experiments, "_authorize_run", lambda *args, **kwargs: (_ for _ in ()).throw(PermissionError("revoked")))
    with pytest.raises(PermissionError if failure == "lease" else RecoverableToolError) as exc:
        harness.invoke()
    if failure != "lease":
        assert exc.value.code == "EXPERIMENT_PLOT_FAILED"
    assert not harness.context.evidence and not harness.context.tool_metadata and not harness.context.notices
    assert not list(harness.root.rglob("plot.png")) and not list(harness.root.rglob(".tmp-*"))


def test_invalid_tool_and_cross_team_inputs(harness):
    with pytest.raises(RecoverableToolError) as error:
        harness.tools["plot_experiment_accuracy"].tool.func(experiment_ids=[], runtime=harness.runtime)
    assert error.value.code == "INVALID_EXPERIMENT_PLOT"
    harness.context.team_id = uuid4()
    with pytest.raises(FatalToolError):
        harness.invoke()
    assert not harness.context.evidence and not harness.root.exists()


@pytest.mark.parametrize("level", ["root", "team", "run", "artifact", "file"])
def test_symlinks_rejected_without_writing_target(harness, tmp_path, level):
    outside = tmp_path / "outside"
    outside.mkdir()
    if level in {"artifact", "file"}:
        result = harness.invoke()
        harness.context.evidence.clear()
        harness.context.tool_metadata.clear()
        image = harness.root / result["data"]["artifacts"][0]["path"]
        if level == "file":
            image.unlink()
            image.symlink_to(outside / "plot.png")
        else:
            import shutil
            shutil.rmtree(image.parent)
            image.parent.symlink_to(outside, target_is_directory=True)
    else:
        path = harness.root
        if level in {"team", "run"}:
            path = path / str(harness.context.team_id)
        if level == "run":
            path /= str(harness.context.run_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.symlink_to(outside, target_is_directory=True)
    with pytest.raises(RecoverableToolError) as exc:
        harness.invoke()
    assert exc.value.code == "EXPERIMENT_PLOT_FAILED" and not harness.context.evidence
    assert not list(outside.iterdir())


def test_output_path_validation_and_offline_cli(tmp_path, capsys):
    root = tmp_path / "inputs"
    generate(root)
    settings = experiments.Settings(root, uuid4())
    for bad in (Path("relative"), root / "plots", tmp_path / ".." / "bad"):
        with pytest.raises(RuntimeError):
            experiment_plots.build_provider(settings, bad)
    for command in (["accuracy", BASE, ATTENTION], ["confusion", BASE, "--normalization", "row"]):
        assert main(["--root", str(root), "--output", str(tmp_path / "out"), *command]) == 0
        assert json.loads(capsys.readouterr().out)["status"] == "ok"
