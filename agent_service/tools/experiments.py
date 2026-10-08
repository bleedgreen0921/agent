"""Read-only, team-bound experiment tools with immutable per-Run inputs."""

import hashlib
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from uuid import UUID

from langchain.tools import ToolRuntime, tool
from psycopg.types.json import Jsonb

from agent_service.tooling import (
    FatalToolError, RecoverableToolError, ToolDeclaration, ToolExecutionContext, ToolProvider,
)
from contracts.v1 import Evidence, ExperimentSourceLocator
from db.connection import connect
from experiment_service.catalogue import (
    canonical, config_diff, controls, flatten, metric_comparison, register, search,
)


VERSION = "2"
MAX_RESPONSE_BYTES = 256 * 1024
STATUSES = {"ready", "failed", "incomplete_results", "unknown_conditions", "invalid_results"}
SYNTHETIC_NOTICE = {"code": "EXPERIMENT_SYNTHETIC",
                    "message": "Experiment evidence uses synthetic fixtures, not real training results."}


@dataclass(frozen=True)
class Settings:
    root: Path
    team_id: UUID

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
        return Settings(root, UUID(os.environ["AGENT_EXPERIMENT_TEAM_ID"]))
    except (KeyError, ValueError, OSError):
        raise RuntimeError("AGENT_EXPERIMENT_ROOT must be an existing absolute experiment root; "
                           "AGENT_EXPERIMENT_TEAM_ID must be its authorized team UUID") from None


def _authorize_run(conn, context: ToolExecutionContext, settings: Settings, *, lock=False):
    row = conn.execute("""SELECT team_id FROM agent.agent_runs
        WHERE id=%s AND lease_token=%s AND status='running'
          AND leased_until>now() AND execution_deadline_at>now()""" + (" FOR UPDATE" if lock else ""),
        (context.run_id, context.lease_token)).fetchone()
    if not row:
        raise PermissionError("execution lease was revoked")
    if row["team_id"] != settings.team_id or row["team_id"] != context.team_id:
        raise FatalToolError("ACCESS_DENIED")


def capture_snapshot(settings: Settings) -> dict:
    try:
        entries = register(settings.root)
        projects = {entry["config"]["run"]["project_id"] for entry in entries}
        if len(projects) != 1 or not all(isinstance(value, str) and value for value in projects):
            raise ValueError("A root must contain exactly one research project")
        return {"schema_version": 1, "method_version": VERSION,
                "root_sha256": settings.root_sha256, "project_id": projects.pop(),
                "snapshot_sha256": hashlib.sha256(canonical(entries).encode()).hexdigest(),
                "entries": entries}
    except (OSError, ValueError, KeyError, TypeError, RecursionError) as exc:
        raise RecoverableToolError("EXPERIMENT_SOURCE_INVALID",
                                   f"Experiment source validation failed ({type(exc).__name__}).") from exc


def load_snapshot(context: ToolExecutionContext, settings: Settings) -> dict:
    """Validate the current lease each time; concurrent first calls reuse the winner."""
    with connect("AGENT_DATABASE_URL", profile="control") as conn:
        _authorize_run(conn, context, settings)
        existing = conn.execute("SELECT snapshot FROM agent.run_experiment_snapshots WHERE run_id=%s",
                                (context.run_id,)).fetchone()
    if existing:
        snapshot = existing["snapshot"]
    else:
        captured = capture_snapshot(settings)
        with connect("AGENT_DATABASE_URL", profile="control") as conn:
            _authorize_run(conn, context, settings, lock=True)
            conn.execute("""INSERT INTO agent.run_experiment_snapshots(run_id,schema_version,snapshot)
                VALUES (%s,1,%s) ON CONFLICT (run_id) DO NOTHING""", (context.run_id, Jsonb(captured)))
            snapshot = conn.execute("SELECT snapshot FROM agent.run_experiment_snapshots WHERE run_id=%s",
                                    (context.run_id,)).fetchone()["snapshot"]
    if (snapshot["schema_version"] != 1 or snapshot["method_version"] != VERSION
        or snapshot["root_sha256"] != settings.root_sha256):
        raise RecoverableToolError("EXPERIMENT_SNAPSHOT_INCOMPATIBLE",
                                   "The Run snapshot requires its original experiment root and tool version.")
    return snapshot


def _inputs(runtime, settings):
    context = runtime.context
    if context.team_id != settings.team_id:
        raise FatalToolError("ACCESS_DENIED")
    context.business_call_id(runtime.tool_call_id)
    return load_snapshot(context, settings)


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


def _publish_response(runtime, snapshot, operation, sources, response, evidence):
    """Record only the final response after its byte budget has been checked."""
    context = runtime.context
    context.evidence[evidence["evidence_id"]] = evidence
    if SYNTHETIC_NOTICE not in context.notices:
        context.notices.append(dict(SYNTHETIC_NOTICE))
    context.add_tool_metadata(runtime.tool_call_id,
        result_summary={"status": "ok", "operation": operation, "synthetic": True,
                        "source_experiment_count": len(sources), "snapshot_sha256": snapshot["snapshot_sha256"]},
        evidence_ids=[evidence["evidence_id"]])
    return response


def _respond(runtime, snapshot, operation, data, sources, method):
    response, evidence = _build_response(snapshot, operation, data, sources, method)
    if len(response.encode("utf-8")) > MAX_RESPONSE_BYTES:
        raise RecoverableToolError("EXPERIMENT_RESULT_TOO_LARGE", "Narrow the query or request fewer experiments.")
    return _publish_response(runtime, snapshot, operation, sources, response, evidence)


def _respond_page(runtime, snapshot, operation, matched, limit, offset, make_page, method):
    """Shrink a count-limited page until its complete response fits the byte budget."""
    page = matched[offset:offset + limit]
    while True:
        data, sources = make_page(page)
        next_offset = offset + len(page)
        data.update(total=len(matched), offset=offset,
                    next_offset=next_offset if next_offset < len(matched) else None)
        response, evidence = _build_response(snapshot, operation, data, sources, method)
        if len(response.encode("utf-8")) <= MAX_RESPONSE_BYTES:
            return _publish_response(runtime, snapshot, operation, sources, response, evidence)
        if len(page) <= 1:
            raise RecoverableToolError("EXPERIMENT_RESULT_TOO_LARGE",
                                       "A single result or its required provenance exceeds the response-size limit.")
        page = page[:-1]


def build_provider(settings: Settings) -> ToolProvider:
    @tool
    def search_experiments(filters: dict[str, Any], runtime: ToolRuntime[ToolExecutionContext],
                           status: str | None = "ready", limit: int = 20, offset: int = 0) -> str:
        """Search YAML configuration dot paths with exact typed values. Use status=null to include failures.

        Examples: {"model.attention.enabled": true, "training.seed": 42}.
        Returns validation status, sample-weighted metrics, and pagination; no semantic text search.
        Sources cover the current page. Pages may contain fewer than limit items to fit the
        response-size limit; follow next_offset to retrieve the remaining matches.
        """
        snapshot = _inputs(runtime, settings)
        known = {key for entry in snapshot["entries"] for key in flatten(entry["config"])}
        if (status is not None and status not in STATUSES or not 1 <= limit <= 50
            or offset < 0 or len(filters) > 16 or any(key not in known for key in filters)):
            raise RecoverableToolError("INVALID_EXPERIMENT_FILTER", "Check configuration paths, status and pagination.")
        matched = sorted(search(snapshot["entries"], filters, status), key=lambda entry: entry["experiment_id"])
        def make_page(page):
            return {"filters": filters, "status": status, "items": [_summary(entry) for entry in page]}, page
        return _respond_page(runtime, snapshot, "search_experiments", matched, limit, offset, make_page,
                             "Exact typed YAML filters and total over the full immutable snapshot; "
                             "source files cover current-page experiments only; registration status from "
                             "source reconciliation; metrics = sum(correct)/sum(total).")

    @tool
    def read_experiment(experiment_id: str, runtime: ToolRuntime[ToolExecutionContext]) -> str:
        """Read an experiment's complete YAML config, validation issues, metrics and hashed source file list."""
        snapshot = _inputs(runtime, settings)
        entry = _entry(snapshot, experiment_id)
        return _respond(runtime, snapshot, "read_experiment",
                        {**_summary(entry), "config": entry["config"], "files": entry["files"]}, [entry],
                        "Parsed effective YAML configuration; counts reconciled across CSV, metrics JSON and confusion JSON.")

    @tool
    def compare_experiment_configs(left_id: str, right_id: str,
                                   runtime: ToolRuntime[ToolExecutionContext]) -> str:
        """Compare YAML configs by dot path, retaining types, missing keys and list order; ignore only run metadata."""
        snapshot = _inputs(runtime, settings)
        left, right = _entry(snapshot, left_id), _entry(snapshot, right_id)
        changes = config_diff(left["config"], right["config"])
        data = {"left_id": left_id, "right_id": right_id, "changes": changes,
                "ignored_prefix": "run", "identical_effective_config": not changes,
                "statuses": [left["status"], right["status"]]}
        return _respond(runtime, snapshot, "compare_experiment_configs", data, [left, right],
                        "Canonical JSON equality for flattened YAML fields, excluding run metadata only.")

    @tool
    def find_experiment_controls(baseline_id: str, changed_paths: list[str],
                                 runtime: ToolRuntime[ToolExecutionContext],
                                 limit: int = 20, offset: int = 0) -> str:
        """Find ready experiments differing from baseline in exactly the declared study variables.

        Example changed_paths=["model.attention.enabled"]. Seed differences count unless explicitly declared.
        Dataset/evaluation changes cannot be declared as valid controls.
        Sources cover the baseline and current page. Pages may contain fewer than limit
        IDs to fit the response-size limit; follow next_offset for the remaining controls.
        """
        snapshot = _inputs(runtime, settings)
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
        return _respond_page(runtime, snapshot, "find_experiment_controls", matched, limit, offset, make_page,
                             "Ready inputs only; full effective config difference set equals declared study variables; "
                             "matching and total over the full immutable snapshot; source files cover the baseline "
                             "and current-page controls only.")

    @tool
    def compare_experiment_metrics(left_id: str, right_id: str, changed_paths: list[str],
                                   runtime: ToolRuntime[ToolExecutionContext]) -> str:
        """Compute sample-weighted overall, low-SNR (<=-5 dB) and per-SNR accuracy differences in percentage points.

        Both inputs must be ready and differ in exactly changed_paths; [] permits identical configurations.
        Dataset/evaluation must match. A single pair gives descriptive differences, not significance or causality.
        """
        snapshot = _inputs(runtime, settings)
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
        return _respond(runtime, snapshot, "compare_experiment_metrics", data, [left, right],
                        "Accuracy=sum(n_correct)/sum(n_total); low SNR<=-5 dB; delta=100*(right-left); strict config differences.")

    @tool
    def summarize_experiment_confusions(experiment_id: str, runtime: ToolRuntime[ToolExecutionContext],
                                        snr_db: int | None = None, top_k: int = 10) -> str:
        """Rank off-diagonal true-to-predicted modulation errors by count for one ready experiment.

        Optional snr_db selects a single SNR. Row error rates use all true-class samples as denominator.
        """
        snapshot = _inputs(runtime, settings)
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
        return _respond(runtime, snapshot, "summarize_experiment_confusions",
                        {"experiment_id": experiment_id, "snr_db": [group["snr_db"] for group in groups],
                         "total_errors": sum(pair["count"] for pair in pairs), "pairs": pairs[:top_k],
                         "next_rank": top_k + 1 if len(pairs) > top_k else None}, [entry],
                        "Sum validated confusion counts by true/predicted class; exclude diagonal; sort count descending; denominator=true-class samples.")

    tools = (search_experiments, read_experiment, compare_experiment_configs, find_experiment_controls,
             compare_experiment_metrics, summarize_experiment_confusions)
    return ToolProvider("experiment_analysis", VERSION,
                        tuple(ToolDeclaration(item, VERSION, "read_only", True) for item in tools))


def tools() -> ToolProvider:
    return build_provider(settings_from_env())
