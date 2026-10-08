"""Validated scientific plot data and deterministic, bounded local artifacts."""

import ctypes
import errno
import fcntl
import hashlib
import json
import os
import platform
import shutil
import stat
import threading
from contextlib import contextmanager
from pathlib import Path
from uuid import uuid4

import matplotlib as mpl
import numpy as np
from matplotlib.backends.backend_agg import FigureCanvasAgg
from matplotlib.figure import Figure
from PIL import __version__ as pillow_version

from experiment_service.catalogue import canonical, config_diff

VERSION = "1"
MAX_IMAGE_BYTES = 2 * 1024 * 1024
MAX_JSON_BYTES = 256 * 1024
RENDER_LOCK = threading.Lock()
COLORS = ("#0072B2", "#D55E00", "#009E73", "#CC79A7", "#E69F00")
METHOD = "Sample-weighted accuracy; sum raw confusion counts before row normalization; descriptive synthetic results."


class PlotInputError(ValueError):
    code = "INVALID_EXPERIMENT_PLOT"


class IncomparablePlots(PlotInputError):
    code = "INCOMPARABLE_EXPERIMENTS"


def select_entries(snapshot: dict, ids: list[str]) -> list[dict]:
    if (not isinstance(ids, list) or not 1 <= len(ids) <= 5
        or any(not isinstance(item, str) for item in ids) or len(set(ids)) != len(ids)):
        raise PlotInputError("Provide 1..5 unique experiment IDs.")
    by_id = {entry["experiment_id"]: entry for entry in snapshot["entries"]}
    if any(item not in by_id for item in ids):
        raise PlotInputError("Unknown experiment ID in this Run snapshot.")
    entries = [by_id[item] for item in ids]
    for entry in entries:
        if entry["status"] != "ready":
            raise PlotInputError("Plot inputs must have ready, fully validated results.")
        if len(entry["confusion"]["labels"]) > 32 or len(entry["confusion"]["matrices"]) > 128:
            raise PlotInputError("Plots support at most 32 classes and 128 SNR values.")
    return entries


def accuracy_data(snapshot: dict, experiment_ids: list[str]) -> tuple[dict, list[dict]]:
    entries = select_entries(snapshot, experiment_ids)
    baseline = entries[0]
    def population(entry):
        return sorted((row["snr_db"], row["modulation"], row["n_total"]) for row in entry["rows"])
    for entry in entries[1:]:
        if (canonical(entry["config"]["data"]["dataset"]) != canonical(baseline["config"]["data"]["dataset"])
            or canonical(entry["config"]["evaluation"]) != canonical(baseline["config"]["evaluation"])
            or population(entry) != population(baseline)):
            raise IncomparablePlots("Dataset, evaluation configuration and SNR/class sample counts must match.")
    curves = []
    for entry in entries:
        points = []
        for snr in sorted(entry["config"]["evaluation"]["snr_db"]):
            rows = [row for row in entry["rows"] if row["snr_db"] == snr]
            correct, total = sum(row["n_correct"] for row in rows), sum(row["n_total"] for row in rows)
            points.append({"snr_db": snr, "n_correct": correct, "n_total": total,
                           "accuracy": correct / total, "accuracy_percent": 100 * correct / total})
        curves.append({"experiment_id": entry["experiment_id"], "points": points,
                       "config_changes_from_first": config_diff(baseline["config"], entry["config"])})
    return {"kind": "accuracy", "synthetic": True, "interpretation": "descriptive",
            "experiment_ids": experiment_ids, "curves": curves}, entries


def confusion_data(snapshot: dict, experiment_id: str, snr_db: int | None = None,
                   normalization: str = "count") -> tuple[dict, list[dict]]:
    entries = select_entries(snapshot, [experiment_id])
    if normalization not in {"count", "row"} or (snr_db is not None and type(snr_db) is not int):
        raise PlotInputError("Use an integer SNR and normalization=count or row.")
    confusion = entries[0]["confusion"]
    groups = [group for group in confusion["matrices"] if snr_db is None or group["snr_db"] == snr_db]
    if not groups:
        raise PlotInputError("Requested SNR is absent from this experiment.")
    labels = confusion["labels"]
    counts = [[sum(group["counts"][i][j] for group in groups) for j in range(len(labels))]
              for i in range(len(labels))]
    display = counts if normalization == "count" else [[cell / sum(row) for cell in row] for row in counts]
    return {"kind": "confusion", "synthetic": True, "interpretation": "descriptive",
            "experiment_id": experiment_id, "requested_snr_db": snr_db,
            "snr_db": sorted(group["snr_db"] for group in groups), "normalization": normalization,
            "labels": labels, "rows": "true_label", "columns": "predicted_label",
            "counts": counts, "display_matrix": display}, entries


def dependencies() -> dict:
    return {"matplotlib": mpl.__version__, "numpy": np.__version__,
            "pillow": pillow_version, "python": platform.python_version()}


def plot_id(snapshot: dict, data: dict) -> str:
    return hashlib.sha256(canonical({"snapshot_sha256": snapshot["snapshot_sha256"],
                                    "data": data, "render_version": VERSION,
                                    "dependencies": dependencies()}).encode()).hexdigest()


def render(data: dict, directory: Path) -> None:
    """Use a local Figure with Agg PNG and vector pcolormesh SVG, under one process lock."""
    style = {key: value for key, value in mpl.rcParamsDefault.items() if key != "backend"}
    style.update({"font.family": "DejaVu Sans", "font.size": 10,
                  "svg.hashsalt": "experiment-plots-v1", "svg.fonttype": "none"})
    with RENDER_LOCK, mpl.rc_context(style):
        figure = Figure(figsize=(8, 5) if data["kind"] == "accuracy" else (6, 6), dpi=200)
        FigureCanvasAgg(figure)
        ax = figure.subplots()
        if data["kind"] == "accuracy":
            for index, curve in enumerate(data["curves"]):
                ax.plot([point["snr_db"] for point in curve["points"]],
                        [point["accuracy_percent"] for point in curve["points"]],
                        color=COLORS[index], marker="o", linewidth=1.8, label=curve["experiment_id"])
            ax.set(xlabel="SNR (dB)", ylabel="Accuracy (%)", ylim=(0, 100),
                   title="Accuracy vs SNR — Synthetic data")
            ax.legend(loc="lower right", fontsize=8)
            ax.grid(alpha=0.25)
            figure.subplots_adjust(left=0.11, right=0.96, bottom=0.15, top=0.89)
            figure.text(0.5, 0.035, "Descriptive comparison; no significance or causal inference", ha="center", fontsize=9)
        else:
            matrix = np.array(data["display_matrix"])
            n = len(data["labels"])
            mesh = ax.pcolormesh(np.arange(n + 1), np.arange(n + 1), matrix, cmap="Blues",
                                 vmin=0, vmax=1 if data["normalization"] == "row" else None,
                                 edgecolors="white", linewidth=0.5, rasterized=False)
            ax.set(xlim=(0, n), ylim=(n, 0), aspect="equal", xlabel="Predicted label", ylabel="True label")
            ax.set_xticks(np.arange(n) + 0.5, data["labels"], rotation=45, ha="right", fontsize=max(4, 10 - n // 6))
            ax.set_yticks(np.arange(n) + 0.5, data["labels"], fontsize=max(4, 10 - n // 6))
            if n <= 12:
                threshold = float(matrix.max()) / 2
                for i in range(n):
                    for j in range(n):
                        label = str(data["counts"][i][j]) if data["normalization"] == "count" else f"{matrix[i,j]:.1%}"
                        ax.text(j + 0.5, i + 0.5, label, ha="center", va="center",
                                fontsize=max(5, 11 - n // 2), color="white" if matrix[i,j] > threshold else "black")
            snr_label = "All SNR" if data["requested_snr_db"] is None else f"SNR {data['requested_snr_db']} dB"
            ax.set_title(f"Synthetic data — {snr_label}\n{data['experiment_id']}", fontsize=11)
            colorbar = figure.colorbar(mesh, ax=ax, fraction=0.046, pad=0.04,
                            label="Count" if data["normalization"] == "count" else "True-class fraction")
            colorbar.solids.set_rasterized(False)
            figure.subplots_adjust(left=0.17, right=0.84, bottom=0.20, top=0.86)
        try:
            figure.savefig(directory / "plot.png", format="png", dpi=200, metadata={"Software": "experiment-plots-v1"})
            figure.savefig(directory / "plot.svg", format="svg", metadata={"Date": None, "Creator": "experiment-plots-v1"})
        finally:
            figure.clear()


def bounded_read(directory_fd: int, name: str, limit: int) -> bytes:
    fd = os.open(name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory_fd)
    with os.fdopen(fd, "rb") as file:
        info = os.fstat(file.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_size > limit:
            raise ValueError("Artifact must be a bounded regular file")
        content = file.read(limit + 1)
    if len(content) > limit:
        raise ValueError("Artifact size limit exceeded")
    return content


def file_metadata(name: str, content: bytes) -> dict:
    return {"name": name, "media_type": {".png": "image/png", ".svg": "image/svg+xml", ".json": "application/json"}[Path(name).suffix],
            "size_bytes": len(content), "sha256": hashlib.sha256(content).hexdigest()}


@contextmanager
def directory_fd(path: Path):
    """Traverse absolute paths using anchored descriptors, rejecting every symlink."""
    if not path.is_absolute() or ".." in path.parts:
        raise ValueError("Output path must be absolute without parent traversal")
    fd = os.open("/", os.O_RDONLY | os.O_DIRECTORY)
    try:
        for part in path.parts[1:]:
            try:
                os.mkdir(part, mode=0o700, dir_fd=fd)
            except FileExistsError:
                pass
            child = os.open(part, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=fd)
            os.close(fd)
            fd = child
        yield fd
    finally:
        os.close(fd)


class PlotStore:
    """Stage complete artifacts, then atomically publish without replacing existing outputs.

    The caller holds the durable Run lock during publish. Directory flock also
    serializes offline writers and readers across processes. All I/O is fd anchored.
    """
    def __init__(self, root: Path, relative: Path, snapshot: dict, data: dict, sources: list[dict],
                 *, run_id: str | None = None, tool_call_id: str | None = None):
        if relative.is_absolute() or any(part in {"..", "."} for part in relative.parts):
            raise ValueError("Invalid artifact namespace")
        self.root, self.relative = root, relative
        self.data = data
        self.id = plot_id(snapshot, data)
        files = {item["path"]: item for entry in sources for item in entry["files"]}
        self.binding = {"schema_version": 1, "plot_id": self.id, "origin": "agent" if run_id else "offline",
                        "run_id": run_id, "snapshot_sha256": snapshot["snapshot_sha256"],
                        "source_files": [files[path] for path in sorted(files)], "method": METHOD,
                        "method_version": VERSION, "dependencies": dependencies()}
        self.tool_call_id = tool_call_id

    def response_data(self, manifest: dict, manifest_bytes: bytes) -> dict:
        artifacts = [*manifest["artifacts"], file_metadata("manifest.json", manifest_bytes)]
        return {**self.data, "plot_id": self.id, "artifacts": [
            {**item, "path": (self.relative / self.id / item["name"]).as_posix()} for item in artifacts]}

    def _existing(self, parent: int):
        try:
            fd = os.open(self.id, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
        except FileNotFoundError:
            return None
        try:
            raw = bounded_read(fd, "manifest.json", MAX_JSON_BYTES)
            manifest = json.loads(raw)
            if (set(manifest) != {*self.binding, "first_tool_call_id", "artifacts"}
                or any(manifest.get(key) != value for key, value in self.binding.items())
                or [item["name"] for item in manifest["artifacts"]] != ["plot.png", "plot.svg", "data.json"]):
                raise ValueError("Stored manifest binding differs")
            for metadata in manifest["artifacts"]:
                name = metadata["name"]
                content = bounded_read(fd, name, MAX_JSON_BYTES if name.endswith("json") else MAX_IMAGE_BYTES)
                if file_metadata(name, content) != metadata:
                    raise ValueError("Stored artifact hash differs")
                if name == "data.json" and content != canonical(self.data).encode():
                    raise ValueError("Stored plotting data differs")
            return self.response_data(manifest, raw)
        finally:
            os.close(fd)

    @contextmanager
    def stage(self):
        with directory_fd(self.root / self.relative) as parent:
            fcntl.flock(parent, fcntl.LOCK_EX)
            try:
                existing = self._existing(parent)
            finally:
                fcntl.flock(parent, fcntl.LOCK_UN)
            if existing is not None:
                yield existing, lambda validate=None: self._verify_publish(parent, None, validate)
                return
            name = ".tmp-" + uuid4().hex
            os.mkdir(name, mode=0o700, dir_fd=parent)
            fd = os.open(name, os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW, dir_fd=parent)
            temporary = Path(f"/proc/self/fd/{fd}")
            try:
                content = canonical(self.data).encode()
                if len(content) > MAX_JSON_BYTES:
                    raise ValueError("Plot data exceeds limit")
                (temporary / "data.json").write_bytes(content)
                render(self.data, temporary)
                artifacts = [file_metadata(file, bounded_read(fd, file, MAX_JSON_BYTES if file.endswith("json") else MAX_IMAGE_BYTES))
                             for file in ("plot.png", "plot.svg", "data.json")]
                manifest = {**self.binding, "first_tool_call_id": self.tool_call_id, "artifacts": artifacts}
                raw = canonical(manifest).encode()
                if len(raw) > MAX_JSON_BYTES:
                    raise ValueError("Plot manifest exceeds limit")
                (temporary / "manifest.json").write_bytes(raw)
                candidate = self.response_data(manifest, raw)
                yield candidate, lambda validate=None: self._verify_publish(parent, name, validate, candidate)
            finally:
                os.close(fd)
                # fd-anchored rmtree never follows a substituted symlink.
                try:
                    shutil.rmtree(name, dir_fd=parent)
                except FileNotFoundError:
                    pass

    def _verify_publish(self, parent: int, temporary: str | None, validate=None, candidate=None):
        fcntl.flock(parent, fcntl.LOCK_EX)
        try:
            existing = self._existing(parent)
            if existing is not None:
                if validate:
                    validate(existing)
                return existing
            if temporary is None:
                raise ValueError("Previously published artifacts disappeared")
            if validate:
                validate(candidate)
            # Linux renameat2(RENAME_NOREPLACE) also protects against writers that
            # do not participate in the directory flock protocol.
            rename = ctypes.CDLL(None, use_errno=True).renameat2
            rename.argtypes = [ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint]
            rename.restype = ctypes.c_int
            if rename(parent, temporary.encode(), parent, self.id.encode(), 1) != 0:
                code = ctypes.get_errno()
                if code != errno.EEXIST:
                    raise OSError(code, os.strerror(code))
                winner = self._existing(parent)
                if winner is None:
                    raise ValueError("Concurrent artifact namespace changed")
                if validate:
                    validate(winner)
                return winner
            return self._existing(parent)
        finally:
            fcntl.flock(parent, fcntl.LOCK_UN)
