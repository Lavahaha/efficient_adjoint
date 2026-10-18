"""Run the standalone adjoint_levelset optimizer against the CST coupler model.

Each CST build/solve is stored as an evaluation (``iter_NNN``) under a separate
output directory. The optimizer's own outer-iteration history is written to
``adjoint_levelset_history.jsonl``.
"""
from __future__ import annotations

import json
import sys
from pathlib import Path

import numpy as np
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import adjoint_levelset as als
from eaopt.cli import base_parser, banner, make_log, safe_console
from eaopt.config import CaseConfig
from eaopt.geometry.levelset import LevelSet2D
from eaopt.optimize.constraints import build_velocity_mask
from eaopt.pipeline import make_level_set
from eaopt.solver.case import load_case
from eaopt.solver.cst import CstSolver


def _select_z_plane(field, requested_z_mm: float, metal_thickness_mm: float) -> tuple[int, float]:
    """Honor an exact z=0 face; never silently sample inside the PEC thickness."""
    z = np.asarray(field.axes()[2], dtype=float)
    tol = min(1e-6, max(1e-12, metal_thickness_mm * 0.1))

    if abs(requested_z_mm) <= tol:
        at_interface = np.flatnonzero(np.abs(z) <= tol)
        if len(at_interface):
            i = int(at_interface[np.argmin(np.abs(z[at_interface]))])
            return i, float(z[i])
        above = np.flatnonzero(z >= 0.0)
        if len(above):
            i_near = int(above[np.argmin(np.abs(z[above] - requested_z_mm))])
        else:
            raise ValueError("Field export has no z >= 0 plane for the configured z=0 sample")

    else:
        i_near = int(np.argmin(np.abs(z - requested_z_mm)))
    z_near = float(z[i_near])
    if tol < z_near <= metal_thickness_mm + tol:
        candidates = np.flatnonzero(z > metal_thickness_mm + tol)
        if not len(candidates):
            raise ValueError(
                f"Field export has no z plane above the PEC thickness "
                f"({metal_thickness_mm:g} mm); nearest requested plane is "
                f"z={z_near:g} mm inside the metal."
            )
        i_near = int(candidates[np.argmin(np.abs(z[candidates] - requested_z_mm))])
        z_near = float(z[i_near])
    return i_near, z_near


def _field_bundle(solution, requested_z_mm: float, metal_thickness_mm: float):
    if solution.e_field is None or solution.h_field is None:
        raise ValueError("CST did not return both E and H field grids")

    e_grid, h_grid = solution.e_field, solution.h_field
    axes_e, axes_h = e_grid.axes(), h_grid.axes()
    for axis_i, axis_name in ((0, "x"), (1, "y")):
        if (axes_e[axis_i].shape != axes_h[axis_i].shape or
                not np.allclose(axes_e[axis_i], axes_h[axis_i], rtol=0.0, atol=1e-9)):
            raise ValueError(f"CST E and H {axis_name} axes do not match")

    iz_e, z_e = _select_z_plane(e_grid, requested_z_mm, metal_thickness_mm)
    iz_h, z_h = _select_z_plane(h_grid, requested_z_mm, metal_thickness_mm)
    if abs(z_e - z_h) > 1e-9:
        raise ValueError(f"CST E/H fields selected different z planes ({z_e:g}, {z_h:g} mm)")

    e = np.moveaxis(np.asarray(e_grid.plane(iz_e), dtype=complex), -1, 0)
    h = np.moveaxis(np.asarray(h_grid.plane(iz_h), dtype=complex), -1, 0)
    return als.FieldBundle(x=axes_e[0], y=axes_e[1], E=e, H=h), z_e


class CstCouplerProblem:
    """Adapter from the standalone Problem protocol to the existing CstSolver."""

    fom_kind = "abs"
    mu = als.MU0

    def __init__(self, cfg, setup, solver, sample_offset_mm: float,
                 prefactor_scale: float, output_dir: Path, log=print):
        self.cfg = cfg
        self.setup = setup
        self.solver = solver
        self.sample_offset = float(sample_offset_mm)
        self.eps = als.EPS0 * float(setup.eps_r)
        omega = 2.0 * np.pi * float(setup.frequency_ghz) * 1e9
        pin = float(setup.port_power_w)
        if pin <= 0:
            raise ValueError(f"CST port power must be positive, got {pin:g} W")
        self.prefactor = float(prefactor_scale) * (-2j * omega / pin)
        self.requested_z_mm = float(cfg.sampling.field_z_mm)
        self.output_dir = output_dir
        self.log = log
        self.next_eval_id = 0
        self.eval_log = output_dir / "cst_evaluations.jsonl"
        self.eval_log.write_text("", encoding="utf-8")
        self.s_key = (int(cfg.objective.to_port), int(cfg.objective.from_port))

    def _next_id(self) -> int:
        result = self.next_eval_id
        self.next_eval_id += 1
        return result

    def _write_eval(self, row: dict) -> None:
        with self.eval_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def forward(self, ls: als.LevelSet) -> als.ForwardResult:
        metal = ls.phi < 0.0
        enclosed_air = ndimage.binary_fill_holes(metal) & ~metal
        if enclosed_air.any():
            raise ValueError(
                "The current level set contains an enclosed air hole. The existing "
                "CST geometry builder extrudes every contour as solid metal and "
                "cannot represent hole subtraction; the solve was stopped to avoid "
                "evaluating a different geometry."
            )
        eval_id = self._next_id()
        self.solver.begin_iteration(eval_id)
        self.solver.build_model(als.boundary_polygons(ls), None)
        sol = self.solver.solve_forward()
        if self.s_key not in sol.s_params:
            raise KeyError(f"CST forward result is missing S{self.s_key[0]},{self.s_key[1]}")
        fields, z_used = _field_bundle(
            sol, self.requested_z_mm, self.setup.metal_thickness_mm)
        # z=0 is the substrate interface used by the existing coupler pipeline;
        # planes above the conductor are in the CST vacuum region.
        self.eps = als.EPS0 * (
            float(self.setup.eps_r) if z_used <= 1e-6 else 1.0)
        s = complex(sol.s_params[self.s_key])
        fom, _ = als.fom_value_and_weight(s, self.fom_kind)
        self._write_eval({
            "eval_id": eval_id,
            "kind": "forward",
            "s_real": s.real,
            "s_imag": s.imag,
            "fom_abs_s": fom,
            "field_z_mm": z_used,
            "eps_f_per_m": self.eps,
            "sample_offset_mm": self.sample_offset,
        })
        self.log(f"[adjoint_levelset] forward eval={eval_id:03d}: "
                 f"|S{self.s_key[0]},{self.s_key[1]}|={fom:.8g}, z={z_used:g} mm")
        return als.ForwardResult(
            s=s,
            fields=fields,
            extra={"eval_id": eval_id, "z_mm": z_used,
                   "x": fields.x.copy(), "y": fields.y.copy()},
        )

    def adjoint(self, ls: als.LevelSet, fwd: als.ForwardResult) -> als.FieldBundle:
        sol = self.solver.solve_backward()
        fields, z_used = _field_bundle(
            sol, self.requested_z_mm, self.setup.metal_thickness_mm)
        expected = fwd.extra
        if abs(z_used - expected["z_mm"]) > 1e-9:
            raise ValueError(
                f"Forward/backward field planes differ ({expected['z_mm']:g} vs {z_used:g} mm)"
            )
        for name, coords in (("x", fields.x), ("y", fields.y)):
            if coords.shape != expected[name].shape or not np.allclose(
                    coords, expected[name], rtol=0.0, atol=1e-9):
                raise ValueError(f"Forward/backward field {name} axes do not match")
        self._write_eval({
            "eval_id": expected["eval_id"],
            "kind": "adjoint",
            "field_z_mm": z_used,
        })
        return fields

    def restore_geometry(self, ls: als.LevelSet) -> int:
        """Restore the last accepted geometry after all backtracking trials fail."""
        eval_id = self._next_id()
        self.solver.begin_iteration(eval_id)
        self.solver.build_model(als.boundary_polygons(ls), None)
        self.log(f"[adjoint_levelset] restored accepted geometry as CST eval={eval_id:03d}")
        return eval_id


def _movement_constraints(ls_old: LevelSet2D, cfg: CaseConfig):
    # This is the same fixed-region / allowed-region mask used by the legacy
    # pipeline. Fractional edge tapers remain fractional in the velocity field.
    velocity_mask = build_velocity_mask(ls_old, cfg, taper_mm=1.0)

    force_air = np.zeros_like(ls_old.phi, dtype=bool)
    if cfg.fixed_region:
        fixed = LevelSet2D(ls_old.box, ls_old.dx)
        fixed.init_from_polygons([
            np.asarray(poly.vertices, dtype=float) for poly in cfg.fixed_region
        ])
        keepout_mm = max(float(cfg.constraints.min_gap_mm), 2.0 * ls_old.dx)
        force_air |= fixed.phi < keepout_mm

    if cfg.constraints.allowed_region is not None:
        b = cfg.constraints.allowed_region
        X, Y = np.meshgrid(ls_old.xs, ls_old.ys, indexing="ij")
        force_air |= (X < b.x[0]) | (X > b.x[1]) | (Y < b.y[0]) | (Y > b.y[1])

    anchor = None
    force_metal = None
    if cfg.constraints.require_connected:
        metal = ls_old.phi < 0.0
        anchor = np.zeros_like(metal, dtype=bool)
        anchor[0, :] = metal[0, :]
        anchor[-1, :] = metal[-1, :]
        anchor[:, 0] |= metal[:, 0]
        anchor[:, -1] |= metal[:, -1]
        if not anchor.any():
            raise ValueError(
                "require_connected=true, but the initial metal has no grid nodes "
                "on the design-region border to use as connectivity anchors"
            )
        force_metal = anchor.copy()
    return velocity_mask, force_metal, force_air, anchor


def _write_json(path: Path, value) -> None:
    path.write_text(json.dumps(value, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def main() -> None:
    safe_console()
    parser = base_parser(
        "Run the adjoint_levelset optimizer on the CST coupler",
        config_default="configs/coupler.yaml",
    )
    parser.add_argument("--output-dir", help="output root (default: <configured output>/adjoint_levelset)")
    parser.add_argument("--iterations", type=int, help="override optimizer.max_iterations")
    parser.add_argument("--sample-offset-mm", type=float,
                        help="field offset along the outward normal; PEC defaults to +1 grid cell")
    parser.add_argument("--prefactor-scale", type=float, default=1.0,
                        help="real scale/sign applied to the paper prefactor; default follows velocity_sign")
    parser.add_argument("--gradient-check", action="store_true",
                        help="run central finite-difference checks before optimization (extra CST solves)")
    args = parser.parse_args()
    log = make_log(args.log)

    cfg = CaseConfig.from_yaml(args.config)
    if cfg.name != "coupler":
        raise ValueError(f"This runner is for the coupler case; config name is {cfg.name!r}")
    output_dir = (Path(args.output_dir).expanduser() if args.output_dir else
                  Path(cfg.output.dir).expanduser() / "adjoint_levelset")
    output_dir.mkdir(parents=True, exist_ok=True)
    cfg.output.dir = str(output_dir)
    if args.iterations is not None:
        if args.iterations <= 0:
            raise ValueError("--iterations must be positive")
        cfg.optimizer.max_iterations = args.iterations

    setup, _ = load_case(cfg)
    log(cfg.summary())
    banner(log, "Run adjoint_levelset", cfg, setup)

    ls_old = make_level_set(cfg)
    dx = float(cfg.design_region.grid_step_mm)
    if not (np.allclose(np.diff(ls_old.xs), dx) and np.allclose(np.diff(ls_old.ys), dx)):
        raise ValueError("adjoint_levelset requires the design-region grid to be uniformly spaced")
    grid = als.Grid2D(ls_old.nx, ls_old.ny, dx,
                      x0=float(ls_old.xs[0]), y0=float(ls_old.ys[0]))
    ls = als.LevelSet(grid, ls_old.phi)
    design_mask, force_metal, force_air, anchor = _movement_constraints(ls_old, cfg)

    field_step_xy_mm = setup.resolve_export_steps(cfg.field_export_step_mm)[0]
    if args.sample_offset_mm is not None:
        sample_offset = float(args.sample_offset_mm)
    elif setup.metal_material.lower() == "pec":
        # CST PEC interior fields vanish. Sample one cell beyond the xy boundary,
        # as required by adjoint_levelset.py for a PEC geometry.
        sample_offset = field_step_xy_mm
    else:
        sign = -1.0 if cfg.sampling.sample_side == "inside" else 1.0
        sample_offset = sign * float(cfg.sampling.intersection_offset_mm)

    prefactor_scale = float(args.prefactor_scale) * float(cfg.optimizer.velocity_sign)
    log(f"[adjoint_levelset] output: {output_dir}")
    log(f"[adjoint_levelset] field offset: {sample_offset:g} mm; "
        f"requested z={cfg.sampling.field_z_mm:g} mm; eps_r={setup.eps_r:g}")
    log(f"[adjoint_levelset] step={cfg.optimizer.step_cells:g} cells "
        f"({cfg.optimizer.step_cells * dx:g} mm max), CFL={cfg.optimizer.cfl:g}; "
        f"extension band={cfg.level_set.extension_band_mm:g} mm")
    log("[adjoint_levelset] velocity extension/smoothing and line search come from "
        "adjoint_levelset.py; legacy normalize, WLS, scatter and convergence settings are unused")
    log("[adjoint_levelset] prefactor is (-2j*omega/Pin) times "
        f"scale={prefactor_scale:g}; validate it with --gradient-check")

    solver = None
    try:
        solver = CstSolver(
            cfg,
            setup=setup,
            auto_init=True,
            save_fields=args.save_fields,
            attach=args.attach or None,
            force_new=args.force_new,
            log=log,
        )
        problem = CstCouplerProblem(
            cfg, setup, solver, sample_offset, prefactor_scale, output_dir, log=log)
        opt_cfg = als.OptimizerConfig(
            maximize=True,
            step_cells=float(cfg.optimizer.step_cells),
            step_max_cells=float(cfg.optimizer.step_cells),
            cfl=float(cfg.optimizer.cfl),
            smooth_sigma_cells=1.5,
            band_cells=float(cfg.level_set.extension_band_mm) / dx,
            reinit_every=int(cfg.level_set.reinit_every),
        )
        optimizer = als.AdjointLevelSetOptimizer(
            problem, design_mask, opt_cfg, force_metal=force_metal,
            force_air=force_air, anchor=anchor)

        if args.gradient_check:
            check = als.gradient_check(problem, ls, design_mask=design_mask)
            _write_json(output_dir / "gradient_check.json", check)
            for row in check:
                log(f"[gradient_check] delta={row['delta_cells']:g} cells, "
                    f"adjoint={row['slope_adjoint']:.8g}, FD={row['slope_fd']:.8g}, "
                    f"ratio={row['ratio']:.8g}")

        history_path = output_dir / "adjoint_levelset_history.jsonl"
        with history_path.open("w", encoding="utf-8") as history_file:
            def callback(it, current_ls, rec):
                row = dict(rec)
                row["cst_eval_id"] = problem.next_eval_id - 1
                history_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                history_file.flush()
                np.savez_compressed(
                    output_dir / f"phi_before_iter_{it:03d}.npz",
                    phi=current_ls.phi,
                    x=current_ls.grid.x,
                    y=current_ls.grid.y,
                )
                log(f"[adjoint_levelset] iter={it:03d}: "
                    f"FoM {rec['f']:.8g} -> {rec['f_trial']:.8g}, "
                    f"step={rec['step']:.4g} cells, backtracks={rec['backtracks']}, "
                    f"accepted={rec['accepted']}")

            final_ls = optimizer.run(ls, n_iter=cfg.optimizer.max_iterations,
                                     callback=callback)

        iteration_records = [r for r in optimizer.history if "accepted" in r]
        if iteration_records and not iteration_records[-1]["accepted"]:
            problem.restore_geometry(final_ls)

        np.savez_compressed(
            output_dir / "phi_final.npz",
            phi=final_ls.phi,
            x=final_ls.grid.x,
            y=final_ls.grid.y,
        )
        final_fom = (optimizer.history[-1]["f"] if optimizer.history
                     else float("nan"))
        _write_json(output_dir / "final_shape.json", [
            np.asarray(poly, dtype=float).tolist()
            for poly in als.boundary_polygons(final_ls)
        ])
        _write_json(output_dir / "adjoint_levelset_summary.json", {
            "case": cfg.name,
            "objective": f"maximize |S{cfg.objective.to_port}{cfg.objective.from_port}|",
            "final_fom": final_fom,
            "iterations_requested": cfg.optimizer.max_iterations,
            "iterations_recorded": len(iteration_records),
            "cst_evaluations": problem.next_eval_id,
            "sample_offset_mm": sample_offset,
            "requested_field_z_mm": cfg.sampling.field_z_mm,
            "prefactor_scale": prefactor_scale,
            "output_dir": str(output_dir),
        })
        log(f"[adjoint_levelset] complete: final |S{cfg.objective.to_port}"
            f"{cfg.objective.from_port}|={final_fom:.8g}; outputs in {output_dir}")
    finally:
        if solver is not None:
            solver.close()


if __name__ == "__main__":
    main()
