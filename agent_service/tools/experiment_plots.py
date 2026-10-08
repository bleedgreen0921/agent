"""Lease-bound local scientific figures using the immutable experiment snapshot."""

import os
from pathlib import Path
from typing import Literal

from langchain.tools import ToolRuntime, tool

from agent_service.tooling import RecoverableToolError, ToolDeclaration, ToolExecutionContext, ToolProvider, FatalToolError
from agent_service.tools import experiments
from db.connection import connect
from experiment_service.plotting import (
    VERSION, METHOD, PlotInputError, PlotStore, accuracy_data, confusion_data,
)

DEFAULT_ROOT = Path(__file__).resolve().parents[2] / ".data" / "experiment_plots"


def plot_root(settings: experiments.Settings) -> Path:
    root = Path(os.environ.get("AGENT_EXPERIMENT_PLOT_ROOT", str(DEFAULT_ROOT)))
    if not root.is_absolute() or ".." in root.parts or root.resolve().is_relative_to(settings.root.resolve()):
        raise RuntimeError("AGENT_EXPERIMENT_PLOT_ROOT must be absolute and outside the experiment input root")
    return root


def build_provider(settings: experiments.Settings, root: Path) -> ToolProvider:
    if not root.is_absolute() or ".." in root.parts or root.resolve().is_relative_to(settings.root.resolve()):
        raise RuntimeError("Plot output root must be absolute and outside the experiment input root")

    def execute(runtime, operation, prepare):
        snapshot = experiments._inputs(runtime, settings)
        context = runtime.context
        try:
            data, sources = prepare(snapshot)
        except PlotInputError as exc:
            raise RecoverableToolError(exc.code, str(exc)) from exc
        store = PlotStore(root, Path(str(context.team_id)) / str(context.run_id), snapshot, data, sources,
                          run_id=str(context.run_id), tool_call_id=str(context.business_call_id(runtime.tool_call_id)))
        def response(data):
            raw, evidence = experiments._build_response(snapshot, operation, data, sources, METHOD, method_version=VERSION)
            if len(raw.encode()) > experiments.MAX_RESPONSE_BYTES:
                raise ValueError("Plot response exceeds limit")
            return raw, evidence
        lease_error = None
        def authorize(conn, *, lock=False):
            nonlocal lease_error
            try:
                experiments._authorize_run(conn, context, settings, lock=lock)
            except PermissionError as exc:
                lease_error = exc
                raise

        try:
            with store.stage() as (candidate, publish):
                response(candidate)
                with connect("AGENT_DATABASE_URL", profile="control") as conn:
                    authorize(conn, lock=True)
                    def validate_publication(data):
                        response(data)
                        # PlotStore invokes this under the directory flock, after
                        # any wait and validation, immediately before publish/reuse.
                        authorize(conn)
                    final_data = publish(validate_publication)
                    raw, evidence = response(final_data)
        except (FatalToolError, RecoverableToolError):
            raise
        except Exception as exc:
            if exc is lease_error:
                raise
            raise RecoverableToolError("EXPERIMENT_PLOT_FAILED",
                                       f"Local plot rendering or storage failed ({type(exc).__name__}); retry after correction.") from exc
        result = experiments._publish_response(runtime, snapshot, operation, sources, raw, evidence)
        context.tool_metadata[runtime.tool_call_id]["result_summary"].update(
            plot_id=final_data["plot_id"], artifacts=final_data["artifacts"])
        return result

    @tool
    def plot_experiment_accuracy(experiment_ids: list[str], runtime: ToolRuntime[ToolExecutionContext]) -> str:
        """Plot 1..5 ready experiments as sample-weighted accuracy vs SNR with PNG/SVG local artifacts.

        Dataset, evaluation and per-SNR/class populations must match. Other configuration changes are
        disclosed. Synthetic descriptive results do not establish significance or causality. Paths are local.
        """
        return execute(runtime, "plot_experiment_accuracy", lambda snapshot: accuracy_data(snapshot, experiment_ids))

    @tool
    def plot_experiment_confusion(experiment_id: str, runtime: ToolRuntime[ToolExecutionContext],
                                  snr_db: int | None = None, normalization: Literal["count", "row"] = "count") -> str:
        """Plot a ready experiment's true-row/predicted-column confusion matrix as local PNG/SVG.

        Optional SNR selects one group. Otherwise sum counts over all SNR before optional row
        normalization. Sources are synthetic; response paths identify local files, not download links.
        """
        return execute(runtime, "plot_experiment_confusion",
                       lambda snapshot: confusion_data(snapshot, experiment_id, snr_db, normalization))

    return ToolProvider("experiment_plots", VERSION, tuple(
        ToolDeclaration(item, VERSION, "side_effect", True)
        for item in (plot_experiment_accuracy, plot_experiment_confusion)))


def tools() -> ToolProvider:
    settings = experiments.settings_from_env()
    return build_provider(settings, plot_root(settings))
