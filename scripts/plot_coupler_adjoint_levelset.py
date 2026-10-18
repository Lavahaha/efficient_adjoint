"""Render the full-layout field snapshots and S-parameter history of a coupler run.

The optimizer writes compact ``adjoint_levelset_state_NNN.npz`` snapshots and
``sparams_history.json`` as it runs. This script can recreate their PNGs later
without starting CST or rerunning any simulations.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import matplotlib
matplotlib.use("Agg")

from eaopt import plotting
from eaopt.config import CaseConfig


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", default="configs/coupler.yaml")
    parser.add_argument("--output-dir", help="run directory; defaults to <output.dir>/adjoint_levelset")
    parser.add_argument("--only", choices=("all", "states", "sparams"), default="all")
    args = parser.parse_args()

    config_path = Path(args.config).expanduser()
    if not config_path.is_absolute():
        config_path = ROOT / config_path
    cfg = CaseConfig.from_yaml(config_path)
    outdir = (Path(args.output_dir).expanduser() if args.output_dir else
              Path(cfg.output.dir).expanduser() / "adjoint_levelset")
    if not outdir.is_absolute():
        outdir = ROOT / outdir
    if not outdir.is_dir():
        raise FileNotFoundError(f"Adjoint level-set output directory does not exist: {outdir}")

    history_path = outdir / "sparams_history.json"
    history = json.loads(history_path.read_text(encoding="utf-8")) if history_path.is_file() else None

    if args.only in ("all", "states"):
        snapshots = sorted(outdir.glob("adjoint_levelset_state_*.npz"))
        current_states = ({int(row["iteration"]) for row in history}
                          if history is not None else None)
        rendered = 0
        for snapshot in snapshots:
            with np.load(snapshot, allow_pickle=False) as data:
                iteration = int(data["iteration"])
                if current_states is not None and iteration not in current_states:
                    continue
                plotting.adjoint_levelset_state_figure(
                    cfg,
                    data["phi"], data["x"], data["y"],
                    data["e_x"], data["e_y"], data["e_mag"],
                    iteration=iteration, z_used=float(data["z_mm"]),
                    path=outdir / f"fig6b_iter_{iteration:03d}.png",
                )
                rendered += 1
        print(f"Rendered {rendered} state plot(s) in {outdir}")

    if args.only in ("all", "sparams"):
        if history is not None:
            target = plotting.adjoint_levelset_sparams_figure(
                cfg, history, outdir / "fig7_sparams_vs_iteration.png")
            print(f"Rendered S-parameter plot: {target}")
        else:
            print(f"No S-parameter history found at {history_path}")


if __name__ == "__main__":
    main()
