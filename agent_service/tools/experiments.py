"""Read-only, personal experiment tools with immutable per-Run inputs."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain.tools import ToolRuntime, tool

from agent_service.tooling import (
    RecoverableToolError, ToolDeclaration, ToolExecutionContext, ToolProvider,
)
from contracts.v1 import Evidence, ExperimentSourceLocator
from experiment_service.catalogue import (
    canonical, config_diff, controls, flatten, metric_comparison, search,
)


VERSION = "2"
MAX_RESPONSE_BYTES = 256 * 1024
STATUSES = {"ready", "failed", "incomplete_results", "unknown_conditions", "invalid_results"}
SYNTHETIC_NOTICE = {"code": "EXPERIMENT_SYNTHETIC",
                    "message": "Experiment evidence uses synthetic fixtures, not real training results."}


@dataclass(frozen=True)
class Settings:
    root: Path

    @property
    def root_sha256(self):
        return hashlib.sha256(str(self.root).encode()).hexdigest()


def settings_from_env() -> Settings:
    try:
        raw = Path(os.environ["AGENT_EXPERIMENT_ROOT"])
        if not raw.is_absolute():
            raise ValueError()
        root = raw.resolve(strict=True)
        if not root.is_dir() or not (root / "experiments").is_dir():
            raise ValueError()
        return Settings(root)
    except (KeyError, ValueError, OSError):
        raise RuntimeError("AGENT_EXPERIMENT_ROOT must be an existing absolute experiment root") from None


def _inputs(runtime, settings):
    from execution.contracts import thaw
    return _snapshot_data(runtime.context.inputs, settings)


def _snapshot_data(inputs, settings):
    from execution.contracts import thaw
    snapshot = inputs.get('experiment')
    if snapshot is None:
        raise RecoverableToolError('EXPERIMENT_SOURCE_INVALID', 'Stable experiment input was not captured')
    if snapshot['method_version'] != VERSION or snapshot['root_sha256'] != settings.root_sha256:
        raise RecoverableToolError('EXPERIMENT_SNAPSHOT_INCOMPATIBLE', 'Run requires its original experiment root and tool version')
    return thaw(snapshot)


def _entry(snapshot, experiment_id):
    for entry in snapshot["entries"]:
        if entry["experiment_id"] == experiment_id:
            return entry
    raise RecoverableToolError("EXPERIMENT_NOT_FOUND", "Experiment ID is absent from this Run snapshot.")


def _summary(entry):
    return {key: entry[key] for key in ("experiment_id", "status", "synthetic", "config_fingerprint", "metrics", "issues")}


def _changed_paths(paths, *, allow_empty=False):
    if (not allow_empty and not paths) or len(paths) != len(set(paths)) or len(paths) > 32:
        raise RecoverableToolError("INVALID_COMPARISON", "Declare a unique list of changed configuration paths.")
    # These define the evaluation population and measurement, never an ablation variable.
    if any(path.startswith(("data.dataset.", "evaluation."))
           or path in {"data.dataset", "evaluation", "synthetic", "schema_version",
                       "provenance.config_is_effective"} for path in paths):
        raise RecoverableToolError("INCOMPARABLE_EXPERIMENTS", "Dataset and evaluation conditions must match.")
    return set(paths)


def _build_response(snapshot, operation, data, sources, method):
    """Serialize a candidate without recording evidence or tool metadata."""
    content = canonical({"operation": operation, "data": data, "synthetic": True})
    files = {item["path"]: item for entry in sources for item in entry["files"]}
    locator = ExperimentSourceLocator(
        project_id=snapshot["project_id"], experiment_ids=[entry["experiment_id"] for entry in sources],
        source_files=[files[path] for path in sorted(files)],
        snapshot_sha256=snapshot["snapshot_sha256"], method=method,
        method_version=VERSION, synthetic=True,
    )
    digest = hashlib.sha256((content + canonical(locator.model_dump())).encode()).hexdigest()
    evidence = Evidence(evidence_id="exp_" + digest, title=operation, content=content,
                        source_locator=locator).model_dump(exclude_none=True)
    response = canonical({"status": "ok", "synthetic": True, "data": data, "evidences": [evidence],
                          "notices": [SYNTHETIC_NOTICE]})
    return response, evidence


def _outcome(snapshot, operation, sources, response, evidence):
    """Record only the final response after its byte budget has been checked."""
    from execution.contracts import ToolOutcome
    return response, ToolOutcome(response, (evidence,), (SYNTHETIC_NOTICE,), {
        'result_summary': {'status':'ok', 'operation':operation, 'synthetic':True,
                           'source_experiment_count':len(sources), 'snapshot_sha256':snapshot['snapshot_sha256']},
        'evidence_ids': [evidence['evidence_id']]}).to_artifact()


def _respond(snapshot, operation, data, sources, method):
    response, evidence = _build_response(snapshot, operation, data, sources, method)
    if len(response.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise RecoverableToolError("EXPERIMENT_RESULT_TOO_LARGE", "Narrow the query or request fewer experiments.")
    return _outcome(snapshot, operation, sources, response, evidence)


def _respond_page(snapshot, operation, matched, limit, offset, make_page, method):
    """Shrink a count-limited page until its complete response fits the byte budget."""
    page = matched[offset:offset + limit]
    while True:
        data, sources = make_page(page)
        next_offset = offset + len(page)
        data.update(total=len(matched), offset=offset,
                    next_offset=next_offset if next_offset < len(matched) else None)
        response, evidence = _build_response(snapshot, operation, data, sources, method)
        if len(response.encode("utf-8")) <= MAX_RESPONSE_BYTES:
            return _outcome(snapshot, operation, sources, response, evidence)
        if len(page) <= 1:
            raise RecoverableToolError("EXPERIMENT_RESULT_TOO_LARGE",
                                       "A single result or its required provenance exceeds the response-size limit.")
        page = page[:-1]


def search_experiments(snapshot, filters: dict[str, Any],
                       status: str | None = "ready", limit: int = 20, offset: int = 0) -> tuple[str, dict]:
    """Search YAML configuration dot paths with exact typed values. Use status=null to include failures.

    Examples: {"model.attention.enabled": true, "training.seed": 42}.
    Returns validation status, sample-weighted metrics, and pagination; no semantic text search.
    Sources cover the current page. Pages may contain fewer than limit items to fit the
    response-size limit; follow next_offset to retrieve the remaining matches.
    """
    known = {key for entry in snapshot["entries"] for key in flatten(entry["config"])}
    if (status is not None and status not in STATUSES or not 1 <= limit <= 50
        or offset < 0 or len(filters) > 16 or any(key not in known for key in filters)):
        raise RecoverableToolError("INVALID_EXPERIMENT_FILTER", "Check configuration paths, status and pagination.")
    matched = sorted(search(snapshot["entries"], filters, status), key=lambda entry: entry["experiment_id"])
    def make_page(page):
        return {"filters": filters, "status": status, "items": [_summary(entry) for entry in page]}, page
    return _respond_page(snapshot, "search_experiments", matched, limit, offset, make_page,
                         "Exact typed YAML filters and total over the full immutable snapshot; "
                         "source files cover current-page experiments only; registration status from "
                         "source reconciliation; metrics = sum(correct)/sum(total).")


def read_experiment(snapshot, experiment_id: str) -> tuple[str, dict]:
    """Read an experiment's complete YAML config, validation issues, metrics and hashed source file list."""
    entry = _entry(snapshot, experiment_id)
    return _respond(snapshot, "read_experiment",
                    {**_summary(entry), "config": entry["config"], "files": entry["files"]}, [entry],
                    "Parsed effective YAML configuration; counts reconciled across CSV, metrics JSON and confusion JSON.")


def compare_experiment_configs(snapshot, left_id: str, right_id: str,
                               ) -> tuple[str, dict]:
    """Compare YAML configs by dot path, retaining types, missing keys and list order; ignore only run metadata."""
    left, right = _entry(snapshot, left_id), _entry(snapshot, right_id)
    changes = config_diff(left["config"], right["config"])
    data = {"left_id": left_id, "right_id": right_id, "changes": changes,
            "ignored_prefix": "run", "identical_effective_config": not changes,
            "statuses": [left["status"], right["status"]]}
    return _respond(snapshot, "compare_experiment_configs", data, [left, right],
                    "Canonical JSON equality for flattened YAML fields, excluding run metadata only.")


def find_experiment_controls(snapshot, baseline_id: str, changed_paths: list[str],

                             limit: int = 20, offset: int = 0) -> tuple[str, dict]:
    """Find ready experiments differing from baseline in exactly the declared study variables.

    Example changed_paths=["model.attention.enabled"]. Seed differences count unless explicitly declared.
    Dataset/evaluation changes cannot be declared as valid controls.
    Sources cover the baseline and current page. Pages may contain fewer than limit
    IDs to fit the response-size limit; follow next_offset for the remaining controls.
    """
    baseline = _entry(snapshot, baseline_id)
    if not 1 <= limit <= 50 or offset < 0:
        raise RecoverableToolError("INVALID_EXPERIMENT_FILTER", "Check pagination: limit=1..50 and offset>=0.")
    paths = _changed_paths(changed_paths)
    try:
        ids = controls(snapshot["entries"], baseline_id, paths)
    except ValueError as exc:
        raise RecoverableToolError("INCOMPARABLE_EXPERIMENTS", str(exc)) from exc
    selected = set(ids)
    matched = sorted((entry for entry in snapshot["entries"] if entry["experiment_id"] in selected),
                     key=lambda entry: entry["experiment_id"])
    def make_page(page):
        sources = {entry["experiment_id"]: entry for entry in [baseline, *page]}
        return {"baseline_id": baseline_id, "changed_paths": sorted(paths),
                "experiment_ids": [entry["experiment_id"] for entry in page]}, list(sources.values())
    return _respond_page(snapshot, "find_experiment_controls", matched, limit, offset, make_page,
                         "Ready inputs only; full effective config difference set equals declared study variables; "
                         "matching and total over the full immutable snapshot; source files cover the baseline "
                         "and current-page controls only.")


def compare_experiment_metrics(snapshot, left_id: str, right_id: str, changed_paths: list[str],
                               ) -> tuple[str, dict]:
    """Compute sample-weighted overall, low-SNR (<=-5 dB) and per-SNR accuracy differences in percentage points.

    Both inputs must be ready and differ in exactly changed_paths; [] permits identical configurations.
    Dataset/evaluation must match. A single pair gives descriptive differences, not significance or causality.
    """
    left, right = _entry(snapshot, left_id), _entry(snapshot, right_id)
    paths = _changed_paths(changed_paths, allow_empty=True)
    try:
        rows = metric_comparison(left, right, paths)
    except ValueError as exc:
        raise RecoverableToolError("INCOMPARABLE_EXPERIMENTS", str(exc)) from exc
    aggregates = {}
    for name, key in (("overall", "accuracy"), ("low_snr", "low_snr_accuracy")):
        a, b = left["metrics"][key], right["metrics"][key]
        aggregates[name] = {"left_accuracy": a, "right_accuracy": b,
                            "delta_percentage_points": round(100 * (b - a), 10) if a is not None and b is not None else None}
    data = {"left_id": left_id, "right_id": right_id, "changed_paths": sorted(paths),
            "aggregates": aggregates, "by_snr": rows, "accuracy_unit": "fraction",
            "difference_unit": "percentage_points", "interpretation": "descriptive_single_pair"}
    return _respond(snapshot, "compare_experiment_metrics", data, [left, right],
                    "Accuracy=sum(n_correct)/sum(n_total); low SNR<=-5 dB; delta=100*(right-left); strict config differences.")


def summarize_experiment_confusions(snapshot, experiment_id: str,
                                    snr_db: int | None = None, top_k: int = 10) -> tuple[str, dict]:
    """Rank off-diagonal true-to-predicted modulation errors by count for one ready experiment.

    Optional snr_db selects a single SNR. Row error rates use all true-class samples as denominator.
    """
    entry = _entry(snapshot, experiment_id)
    if entry["status"] != "ready":
        raise RecoverableToolError("INCOMPARABLE_EXPERIMENTS", "Confusions require complete validated results.")
    confusion = entry["confusion"]
    groups = [group for group in confusion["matrices"] if snr_db is None or group["snr_db"] == snr_db]
    if not groups or not 1 <= top_k <= 50:
        raise RecoverableToolError("INVALID_EXPERIMENT_FILTER", "Check SNR and top_k (1..50).")
    labels = confusion["labels"]
    totals = [sum(sum(group["counts"][i]) for group in groups) for i in range(len(labels))]
    pairs = []
    for i, label in enumerate(labels):
        for j, prediction in enumerate(labels):
            count = sum(group["counts"][i][j] for group in groups)
            if i != j and count:
                pairs.append({"true_label": label, "predicted_label": prediction,
                              "count": count, "true_class_n_total": totals[i], "error_fraction": count / totals[i]})
    pairs.sort(key=lambda pair: (-pair["count"], pair["true_label"], pair["predicted_label"]))
    return _respond(snapshot, "summarize_experiment_confusions",
                    {"experiment_id": experiment_id, "snr_db": [group["snr_db"] for group in groups],
                     "total_errors": sum(pair["count"] for pair in pairs), "pairs": pairs[:top_k],
                     "next_rank": top_k + 1 if len(pairs) > top_k else None}, [entry],
                    "Sum validated confusion counts by true/predicted class; exclude diagonal; sort count descending; denominator=true-class samples.")


COMPUTATIONS = {'search_experiments': search_experiments, 'read_experiment': read_experiment, 'compare_experiment_configs': compare_experiment_configs, 'find_experiment_controls': find_experiment_controls, 'compare_experiment_metrics': compare_experiment_metrics, 'summarize_experiment_confusions': summarize_experiment_confusions}


def experiment_compute(name, arguments, snapshot):
    """CPU boundary: ordinary inputs and outputs, no framework construction."""
    return COMPUTATIONS[name](snapshot, **arguments)


def _declaration(function, execute):
    import inspect
    from langchain_core.tools import StructuredTool
    parameters = list(inspect.signature(function).parameters.values())[1:]
    parameters.append(inspect.Parameter('runtime', inspect.Parameter.KEYWORD_ONLY,
                                       annotation=ToolRuntime[ToolExecutionContext]))
    execute.__signature__ = inspect.Signature(parameters)
    execute.__annotations__ = {p.name: p.annotation for p in parameters}
    return StructuredTool.from_function(coroutine=execute, name=function.__name__,
        description=inspect.getdoc(function), response_format='content_and_artifact')


def _sync_provider(settings):
    # Offline domain-test compatibility only. Runtime uses build_provider below.
    from langchain_core.tools import StructuredTool
    import inspect
    def adapt(function):
        def execute(**arguments):
            runtime = arguments.pop('runtime')
            return function(_inputs(runtime, settings), **arguments)
        template = _declaration(function, execute)
        return StructuredTool.from_function(func=execute, name=template.name,
            description=template.description, args_schema=template.args_schema,
            response_format='content_and_artifact')
    return ToolProvider('experiment_analysis', VERSION, tuple(
        ToolDeclaration(adapt(fn), VERSION, 'read_only', True) for fn in COMPUTATIONS.values()), contract_version=1)


def build_provider(settings: Settings) -> ToolProvider:
    from execution.persistence import resources
    from execution.scope import work_owner, operation_deadline, authorize
    def adapt(function):
        async def execute(**arguments):
            runtime = arguments.pop('runtime')
            limit = await operation_deadline(120)
            owner = work_owner(runtime.context.call_id)
            snapshot = await resources().io.run(_snapshot_data, runtime.context.inputs, settings, owner=owner, deadline=limit, before_submit=authorize)
            return await resources().cpu.run(experiment_compute, function.__name__, arguments,
                snapshot, owner=owner, deadline=limit, before_submit=authorize)
        return _declaration(function, execute)
    return ToolProvider('experiment_analysis', VERSION, tuple(
        ToolDeclaration(adapt(fn), VERSION, 'read_only', True) for fn in COMPUTATIONS.values()), contract_version=1)


def tools() -> ToolProvider:
    return build_provider(settings_from_env())
