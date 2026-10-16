#!/usr/bin/env python3
"""Validate the adjoint velocity sign with three forward CST solves.

Example (run on the CST server from the repository root):
    python scripts/verify_velocity_sign.py \
      --config configs/coupler.yaml \
      --samples results/coupler/first_iteration_diagnostic/boundary_samples.csv \
      --x 6 --y -1.6 --delta-mm 0.05 --sigma-mm 0.3

The script compares baseline |S31| with localized outward and inward boundary
perturbations. It uses a dedicated output directory and does not alter the
production geometry or optimizer setting.
"""
from __future__ import annotations

import argparse
import csv
import json
from pathlib import Path

import numpy as np

from eaopt.config import CaseConfig
from eaopt.geometry.contour import extract_contours
from eaopt.pipeline import make_level_set, movable_contours
from eaopt.solver.cst import CstSolver
from eaopt.optimize.objective import fom_key


def read_reference(path: Path, x: float, y: float) -> dict:
    with path.open(newline="", encoding="utf-8") as f:
        rows = list(csv.DictReader(f))
    if not rows:
        raise ValueError(f"No boundary samples in {path}")
    row = min(rows, key=lambda r: (float(r["x_mm"]) - x) ** 2 +
              (float(r["y_mm"]) - y) ** 2)
    return {
        "sample_xy_mm": [float(row["x_mm"]), float(row["y_mm"])],
        "distance_mm": float(np.hypot(float(row["x_mm"]) - x,
                                      float(row["y_mm"]) - y)),
        "delta_p": float(row["delta_p_1_per_m"]),
        "active_in_reference_csv": bool(int(row["active"])),
    }


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__)
    p.add_argument("--config", type=Path, default=Path("configs/coupler.yaml"))
    p.add_argument("--samples", type=Path, required=True,
                   help="boundary_samples.csv from diagnose_first_iteration.py")
    p.add_argument("--x", type=float, required=True, help="target boundary x (mm)")
    p.add_argument("--y", type=float, required=True, help="target boundary y (mm)")
    p.add_argument("--delta-mm", type=float, default=0.05,
                   help="outward/inward perturbation amplitude")
    p.add_argument("--sigma-mm", type=float, default=0.3,
                   help="Gaussian localization radius along the boundary")
    p.add_argument("--work-dir", type=Path, default=None)
    args = p.parse_args()
    if args.delta_mm <= 0 or args.sigma_mm <= 0:
        p.error("--delta-mm and --sigma-mm must be positive")

    cfg = CaseConfig.from_yaml(args.config)
    work = (args.work_dir or Path(cfg.output.dir) / "velocity_sign_check").resolve()
    work.mkdir(parents=True, exist_ok=True)
    cfg.output.dir = str(work)
    reference = read_reference(args.samples, args.x, args.y)

    base = make_level_set(cfg)
    phi0 = np.array(base.phi, copy=True)
    X, Y = np.meshgrid(base.xs, base.ys, indexing="ij")
    r2 = (X - args.x) ** 2 + (Y - args.y) ** 2
    bump = np.exp(-0.5 * r2 / args.sigma_mm ** 2)
    # Signed distance is negative in metal; subtracting moves the interface
    # outward, while adding moves it inward.
    cases = [("baseline", 0.0), ("outward", 1.0), ("inward", -1.0)]
    solver = CstSolver(cfg, auto_init=True, save_fields=False, attach=False,
                       clear_results=True, log=print)
    values = {}
    try:
        for iteration, (name, direction) in enumerate(cases):
            base.phi = phi0.copy() - direction * args.delta_mm * bump
            contours = movable_contours(base, cfg)
            solver.begin_iteration(iteration)
            solver.build_model(contours, [])
            sol = solver.solve_forward()
            values[name] = float(abs(sol.s_params[fom_key(cfg)]))
            print(f"{name:8s}: |S{cfg.objective.to_port}{cfg.objective.from_port}|="
                  f"{values[name]:.10g}")
    finally:
        solver.close()

    slope = (values["outward"] - values["inward"]) / (2 * args.delta_mm)
    expected_direction = "outward" if reference["delta_p"] > 0 else "inward"
    favored_direction = "outward" if slope > 0 else "inward" if slope < 0 else "undetermined"
    sign_match = ((reference["delta_p"] > 0 and slope > 0) or
                  (reference["delta_p"] < 0 and slope < 0))
    recommended_sign = 1.0 if sign_match else -1.0
    report = {
        "objective": f"S{cfg.objective.to_port}{cfg.objective.from_port}",
        "target_xy_mm": [args.x, args.y],
        "delta_mm": args.delta_mm,
        "sigma_mm": args.sigma_mm,
        "reference_boundary_sample": reference,
        "fom_magnitude": values,
        "centered_outward_derivative_per_mm": slope,
        "finite_difference_favored_direction": favored_direction,
        "adjoint_delta_p_favored_direction": expected_direction,
        "sign_agreement": bool(sign_match),
        "recommended_velocity_sign": recommended_sign,
        "work_dir": str(work),
    }
    report_path = work / "velocity_sign_report.json"
    report_path.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(f"centered d|S|/d(outward)={slope:+.6g} per mm")
    print(f"FD favors {favored_direction}; CSV adjoint delta_p favors {expected_direction}; "
          f"agreement={sign_match}")
    print(f"recommended optimizer.velocity_sign={recommended_sign:+.0f}")
    print(f"report: {report_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
