"""ELC-only dispatcher. Spatial/parcel/BRDF processing is not part of this edition."""
import argparse
from datetime import datetime
from pathlib import Path
import traceback

from .config import load_config
from .preprocess import run_stage
from .roi_studio_bridge import atomic_json


def main(argv=None):
    parser = argparse.ArgumentParser(description="Native ELC reflectance preparation")
    parser.add_argument("--config", required=True)
    parser.add_argument("--stage", required=True, choices=(
        "elc_fit", "elc_apply_priority", "elc_apply", "integrated_preflight", "integrated"))
    args = parser.parse_args(argv)
    cfg = load_config(args.config)
    out = Path(cfg["output"]["directory"])
    status_path = out / "pipeline_status.json"
    status = dict(stage=args.stage, started=datetime.now().astimezone().isoformat(), state="running")
    atomic_json(status_path, status)
    try:
        if args.stage.startswith("integrated"):
            from . import integrated
            integrated.preflight(cfg) if args.stage == "integrated_preflight" else integrated.run(cfg)
        else:
            run_stage(cfg, args.stage, wait=True)
        status.update(state="complete", finished=datetime.now().astimezone().isoformat())
        atomic_json(status_path, status)
        print("COMPLETE: " + args.stage, flush=True)
        return 0
    except Exception as exc:
        status.update(state="failed", error=str(exc), finished=datetime.now().astimezone().isoformat())
        atomic_json(status_path, status)
        traceback.print_exc()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
