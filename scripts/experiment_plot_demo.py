"""Render offline synthetic experiment plots with the same validation and artifacts as Agent tools."""

import argparse
from pathlib import Path
from uuid import UUID

from agent_service.tools.experiments import Settings, capture_snapshot
from experiment_service.catalogue import canonical
from experiment_service.plotting import PlotStore, accuracy_data, confusion_data
from scripts.synthetic_experiments import DEFAULT_ROOT


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--root", type=Path, default=DEFAULT_ROOT)
    parser.add_argument("--output", type=Path, default=Path(".data/experiment_plots"))
    sub = parser.add_subparsers(dest="kind", required=True)
    accuracy = sub.add_parser("accuracy")
    accuracy.add_argument("experiment_ids", nargs="+")
    confusion = sub.add_parser("confusion")
    confusion.add_argument("experiment_id")
    confusion.add_argument("--snr-db", type=int)
    confusion.add_argument("--normalization", choices=("count", "row"), default="count")
    args = parser.parse_args(argv)
    try:
        root, output = args.root.absolute(), args.output.absolute()
        if output.resolve().is_relative_to(root.resolve()):
            raise ValueError("Plot output must be outside the experiment input root")
        snapshot = capture_snapshot(Settings(root.resolve(strict=True), UUID(int=0)))
        data, sources = (accuracy_data(snapshot, args.experiment_ids) if args.kind == "accuracy" else
                         confusion_data(snapshot, args.experiment_id, args.snr_db, args.normalization))
        store = PlotStore(output, Path("offline"), snapshot, data, sources)
        with store.stage() as (_, publish):
            result = publish()
        print(canonical({"status": "ok", "synthetic": True, "output_root": str(output), "data": result}))
    except Exception as exc:
        parser.exit(1, f"Plot generation failed: {exc}\n")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
