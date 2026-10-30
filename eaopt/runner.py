"""Shared CST adapter and runner for adjoint_levelset case entries.

Each CST build/solve is stored as an evaluation (``iter_NNN``) under a separate
output directory. The optimizer's own outer-iteration history is written to
``adjoint_levelset_history.jsonl``.
"""
from __future__ import annotations

import json
import sys
from dataclasses import asdict
from datetime import datetime, timezone
from pathlib import Path

import numpy as np
from scipy import ndimage

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import adjoint_levelset as als
from eaopt.cli import base_parser, banner, make_log, safe_console
from eaopt.config import BoxSpec, CaseConfig
from eaopt.geometry.levelset import LevelSet2D, make_level_set
from eaopt.optimize.constraints import (ConnectivityGuard, apply_min_feature,
                                        apply_min_gap,
                                        build_velocity_mask)
from eaopt.optimize.objective import multi_frequency_fom
from eaopt.solver.case import load_case
from eaopt import artifacts, plotting
from eaopt.geometry.contour import classify_metal_regions


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


class CstAdjointLevelSetProblem:
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
        self.prefactor_scale = float(prefactor_scale)
        self.frequencies = tuple(map(float, cfg.objective.frequencies_ghz or ()))
        self.requested_z_mm = float(cfg.sampling.field_z_mm)
        self.output_dir = output_dir
        self.log = log
        self.next_eval_id = 0
        self.eval_log = output_dir / "cst_evaluations.jsonl"
        with self.eval_log.open("x", encoding="utf-8"):
            pass
        self.s_key = (int(cfg.objective.to_port), int(cfg.objective.from_port))
        self.forward_only = self.s_key[0] == self.s_key[1]

    def _next_id(self) -> int:
        result = self.next_eval_id
        self.next_eval_id += 1
        return result

    def _write_eval(self, row: dict) -> None:
        with self.eval_log.open("a", encoding="utf-8") as f:
            f.write(json.dumps(row, ensure_ascii=False) + "\n")

    def _save_geometry_failure(self, eval_id: int, ls: als.LevelSet,
                               error: Exception, contours=None,
                               prepared_contours=None) -> None:
        """Persist the exact candidate that failed validation or CST model creation."""
        out = self.output_dir / "geometry_failures"
        out.mkdir(parents=True, exist_ok=True)
        stem = f"geometry_failure_eval_{eval_id:03d}"
        npz_path = out / f"{stem}.npz"
        json_path = out / f"{stem}.json"
        payload = {
            "phi": np.asarray(ls.phi),
            "x": np.asarray(ls.grid.x),
            "y": np.asarray(ls.grid.y),
        }
        contour_info = []
        for stage, group in (("input", contours), ("prepared", prepared_contours)):
            for i, contour in enumerate(group or []):
                pts = np.asarray(contour, dtype=float)
                payload[f"{stage}_contour_{i}"] = pts
                finite = pts[np.all(np.isfinite(pts), axis=1)]
                lengths = (np.linalg.norm(np.diff(finite, axis=0), axis=1)
                           if len(finite) > 1 else [])
                contour_info.append({
                    "stage": stage,
                    "index": i,
                    "vertices": int(len(pts)),
                    "finite_vertices": int(len(finite)),
                    "bbox_mm": ([finite.min(axis=0).tolist(), finite.max(axis=0).tolist()]
                                if len(finite) else None),
                    "minimum_segment_mm": (
                        float(np.min(lengths)) if len(lengths) else None),
                })
        np.savez_compressed(npz_path, **payload)
        json_path.write_text(json.dumps({
            "eval_id": eval_id,
            "error_type": type(error).__name__,
            "error": str(error),
            "grid_step_mm": float(ls.grid.dx),
            "contours": contour_info,
            "snapshot_npz": npz_path.name,
        }, ensure_ascii=False, indent=2), encoding="utf-8")
        self.log(f"[geometry failure] eval={eval_id:03d} candidate saved: {npz_path}")

    def forward(self, ls: als.LevelSet) -> als.ForwardResult:
        eval_id = self._next_id()
        evaluation_dir = artifacts.iteration_dir(self.output_dir, eval_id)
        evaluation_dir.mkdir(parents=True, exist_ok=False)
        np.savez_compressed(evaluation_dir / "levelset.npz", phi=ls.phi,
                            x=ls.grid.x, y=ls.grid.y, cst_eval_id=eval_id)
        contours = None
        try:
            metal = ls.phi < 0.0
            enclosed_air = ndimage.binary_fill_holes(metal) & ~metal
            if (enclosed_air.any() and
                    not getattr(self.solver.M, "SUPPORTS_NESTED_CONTOURS", False)):
                raise ValueError(
                    "The current level set contains an enclosed air hole. The existing "
                    "CST geometry builder extrudes every contour as solid metal and "
                    "cannot represent hole subtraction; the solve was stopped to avoid "
                    "evaluating a different geometry."
                )
            contours = als.boundary_polygons(ls)
            if hasattr(self.solver, "last_prepared_polys"):
                self.solver.last_prepared_polys = None
            self.solver.begin_iteration(eval_id)
            self.solver.build_model(contours, None)
        except Exception as exc:
            try:
                self._save_geometry_failure(
                    eval_id, ls, exc, contours,
                    getattr(self.solver, "last_prepared_polys", None))
            except Exception as save_exc:
                self.log(f"[geometry failure] snapshot save failed: {save_exc}")
            raise
        sol = self.solver.solve_forward()
        if self.s_key not in sol.s_params:
            raise KeyError(f"CST forward result is missing S{self.s_key[0]},{self.s_key[1]}")
        fields, z_used = _field_bundle(
            sol, self.requested_z_mm, self.setup.metal_thickness_mm)
        # Negative z samples the substrate side of the metal interface; planes
        # above the conductor are in the CST vacuum region.
        self.eps = als.EPS0 * (
            float(self.setup.eps_r) if z_used <= 1e-6 else 1.0)
        s = complex(sol.s_params[self.s_key])
        frequency_data = self._frequency_data(sol) if self.frequencies else {}
        fom = (multi_frequency_fom(
            {f: row[0] for f, row in frequency_data.items()}, self.cfg)[0]
            if self.frequencies else als.fom_value_and_weight(s, self.fom_kind)[0])
        self._write_eval({
            "eval_id": eval_id,
            "kind": "forward",
            "s_real": s.real,
            "s_imag": s.imag,
            "objective_value": fom,
            "field_z_mm": z_used,
            "eps_f_per_m": self.eps,
            "sample_offset_mm": self.sample_offset,
        })
        self.log(f"[adjoint_levelset] forward eval={eval_id:03d}: "
                  f"FoM={fom:.8g}, z={z_used:g} mm")
        return als.ForwardResult(
            s=s,
            fields=fields,
            extra={"eval_id": eval_id, "z_mm": z_used,
                   "x": fields.x.copy(), "y": fields.y.copy(),
                   "s_params": dict(sol.s_params), "frequency_data": frequency_data,
                   "objective_value": fom},
        )

    def _frequency_data(self, sol):
        if set(sol.frequency_results) != set(self.frequencies):
            raise ValueError(f"CST frequency results must match {self.frequencies}")
        data = {}
        for freq in self.frequencies:
            row = sol.frequency_results[freq]
            fields, z = _field_bundle(
                row, self.requested_z_mm, self.setup.metal_thickness_mm)
            eps = als.EPS0 * (float(self.setup.eps_r) if z <= 1e-6 else 1.0)
            data[freq] = (dict(row.s_params), fields, z, eps)
        return data

    def objective_value(self, fwd):
        return fwd.extra["objective_value"]

    def boundary_gradient(self, ls, fwd, boundary):
        if not self.frequencies:
            adj = self.adjoint(ls, fwd)
            return np.real(als.shape_derivative_complex(
                boundary, fwd.fields, adj, self.prefactor, self.sample_offset,
                self.eps, self.mu))
        forward = fwd.extra["frequency_data"]
        backward = (forward if self.forward_only else
                    self._frequency_data(self.solver.solve_backward()))
        _, _, weights = multi_frequency_fom(
            {f: row[0] for f, row in forward.items()}, self.cfg)
        gradient = np.zeros(len(boundary.pts))
        for freq, weight in zip(self.frequencies, weights):
            params, fields, z, eps = forward[freq]
            _, adj, adj_z, _ = backward[freq]
            if (abs(z - adj_z) > 1e-9 or
                    not np.array_equal(fields.x, adj.x) or
                    not np.array_equal(fields.y, adj.y)):
                raise ValueError(f"Forward/backward field grids differ at {freq:g} GHz")
            prefactor = self.prefactor_scale * (-2j * 2 * np.pi * freq * 1e9 /
                                               self.setup.port_power_w)
            sensitivity = als.shape_derivative_complex(
                boundary, fields, adj, prefactor, self.sample_offset, eps, self.mu)
            s = params[self.s_key]
            _, chain = als.fom_value_and_weight(s, "db")
            gradient += float(weight) * als.real_gradient(sensitivity, chain)
        return gradient

    def e_field_for_plot(self, eval_id: int, box: BoxSpec):
        grid = self.solver.export_field_for_plot(box, eval_id=eval_id, tag="fwd")
        iz, z_used = _select_z_plane(
            grid, self.requested_z_mm, self.setup.metal_thickness_mm)
        e_plane = np.asarray(grid.plane(iz), dtype=complex)
        magnitude = np.sqrt(np.sum(np.abs(e_plane) ** 2, axis=-1))
        axes = grid.axes()
        return np.asarray(axes[0]), np.asarray(axes[1]), magnitude, z_used

    def adjoint(self, ls: als.LevelSet, fwd: als.ForwardResult) -> als.FieldBundle:
        if self.forward_only:
            self._write_eval({
                "eval_id": fwd.extra["eval_id"],
                "kind": "adjoint",
                "field_z_mm": fwd.extra["z_mm"],
                "fields_reused_from": "forward",
            })
            return fwd.fields
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

def _movement_constraints(ls_old: LevelSet2D, cfg: CaseConfig):
    # This is the same fixed-region / allowed-region mask used by the legacy
    # runner. Fractional edge tapers remain fractional in the velocity field.
    velocity_mask = build_velocity_mask(ls_old, cfg, taper_mm=1.0)

    # Fixed-metal spacing is projected through the shared apply_min_gap()
    # helper. Keeping this runner free of a second SDF implementation avoids
    # diverging behavior when min_gap_mm is zero.
    force_air = np.zeros_like(ls_old.phi, dtype=bool)

    if cfg.constraints.allowed_region is not None:
        b = cfg.constraints.allowed_region
        X, Y = np.meshgrid(ls_old.xs, ls_old.ys, indexing="ij")
        force_air |= (X < b.x[0]) | (X > b.x[1]) | (Y < b.y[0]) | (Y > b.y[1])

    anchor = None
    force_metal = None
    if cfg.constraints.require_connected:
        # Include nodes exactly on the polygon boundary; these commonly carry
        # phi == -0.0 and are valid connectivity anchors.
        metal = ls_old.phi <= 0.0
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
    artifacts.save_json(path, value)


def _run_output_dir(requested: Path) -> Path:
    """Keep existing runs intact, including their CST projects and partial files."""
    requested = requested.expanduser().resolve()
    if not requested.exists():
        requested.mkdir(parents=True)
        return requested
    if not any(requested.iterdir()):
        return requested
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
    output = requested / f"run_{stamp}"
    output.mkdir()
    return output


def _save_evaluation(problem, candidate, fwd, details, field_box):
    """Persist each solve before CST replaces its active geometry and fields."""
    eval_id = int(fwd.extra["eval_id"])
    out = artifacts.iteration_dir(problem.output_dir, eval_id)
    groups = ({f"@{freq:g}GHz": row[0]
               for freq, row in fwd.extra["frequency_data"].items()}
              if problem.frequencies else {"": fwd.extra["s_params"]})
    params_db = {}
    for suffix, params in groups.items():
        for (response, stimulus), value in sorted(params.items()):
            magnitude = abs(complex(value))
            params_db[f"S{response}{stimulus}{suffix}"] = (
                float(20.0 * np.log10(magnitude)) if magnitude > 0.0 else None)
    row = dict(details, cst_eval_id=eval_id,
               objective_value=als.problem_value(problem, fwd),
               time_utc=datetime.now(timezone.utc).isoformat(),
               s_params_db=params_db,
               snapshot=f"{out.name}/evaluation_state.npz",
               figure=f"{out.name}/evaluation.png")
    # Save the decision before the optional wide-field export or rendering can fail.
    _write_json(out / "evaluation.json", row)
    with (problem.output_dir / "optimization_evaluations.jsonl").open("a", encoding="utf-8") as stream:
        stream.write(json.dumps(row, ensure_ascii=False) + "\n")
    ex, ey, e_mag, z_used = problem.e_field_for_plot(eval_id, field_box)
    payload = dict(cst_eval_id=eval_id, phi=candidate.phi,
                   x=candidate.grid.x, y=candidate.grid.y,
                   e_x=ex, e_y=ey, e_mag=e_mag, z_mm=z_used,
                   sample_x=fwd.fields.x, sample_y=fwd.fields.y,
                   e_forward=fwd.fields.E, h_forward=fwd.fields.H)
    for i, (freq, (_, fields, z, eps)) in enumerate(fwd.extra["frequency_data"].items()):
        payload.update({f"frequency_{i}_ghz": freq, f"frequency_{i}_x": fields.x,
                        f"frequency_{i}_y": fields.y, f"frequency_{i}_z_mm": z,
                        f"frequency_{i}_eps": eps,
                        f"frequency_{i}_E": fields.E, f"frequency_{i}_H": fields.H})
    with (out / "evaluation_state.npz").open("xb") as stream:
        np.savez_compressed(stream, **payload)
    plotting.adjoint_levelset_state_figure(
        problem.cfg, candidate.phi, candidate.grid.x, candidate.grid.y,
        ex, ey, e_mag, iteration=details.get("it"), z_used=z_used,
        evaluation_id=eval_id, path=out / "evaluation.png")
    problem.log(f"[evaluation] eval={eval_id:03d}, phase={row['phase']}, "
                f"FoM={row['objective_value']:.8g}, accepted={row['accepted']}: {out}")
    return row


def main(config_default: str = str(ROOT / "configs" / "coupler.yaml"),
         expected_case: str = "coupler", maximize: bool = True) -> None:
    safe_console()
    parser = base_parser(
        f"Run the adjoint_levelset optimizer on the CST {expected_case}",
        config_default=config_default,
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
    from eaopt.solver.cst import CstSolver

    cfg = CaseConfig.from_yaml(args.config)
    if cfg.name != expected_case:
        raise ValueError(
            f"This runner expects case {expected_case!r}; config name is {cfg.name!r}")
    if args.iterations is not None:
        if args.iterations <= 0:
            raise ValueError("--iterations must be positive")
        cfg.optimizer.max_iterations = args.iterations

    setup, model = load_case(cfg)
    requested_output = (Path(args.output_dir) if args.output_dir else
                        Path(cfg.output.dir) / "adjoint_levelset")
    output_dir = _run_output_dir(requested_output)
    cfg.output.dir = str(output_dir)
    log_path = Path(args.log).expanduser().resolve() if args.log else output_dir / "run.log"
    if log_path.exists():
        log_path = output_dir / log_path.name
    log_path.parent.mkdir(parents=True, exist_ok=True)
    log = make_log(log_path)
    log(f"[run] output: {output_dir}; log: {log_path}")
    _write_json(output_dir / "run_config.json", {
        "time_utc": datetime.now(timezone.utc).isoformat(),
        "config_path": str(Path(args.config).expanduser().resolve()),
        "config": asdict(cfg), "setup": asdict(setup), "arguments": vars(args),
    })
    log(cfg.summary())
    banner(log, f"Run adjoint_levelset: {cfg.name}", cfg, setup)

    ls_old = make_level_set(cfg)
    dx = float(cfg.design_region.grid_step_mm)
    if not (np.allclose(np.diff(ls_old.xs), dx) and np.allclose(np.diff(ls_old.ys), dx)):
        raise ValueError("adjoint_levelset requires the design-region grid to be uniformly spaced")
    grid = als.Grid2D(ls_old.nx, ls_old.ny, dx,
                      x0=float(ls_old.xs[0]), y0=float(ls_old.ys[0]))
    ls = als.LevelSet(grid, ls_old.phi)
    design_mask, force_metal, force_air, anchor = _movement_constraints(ls_old, cfg)
    topology_guard = (ConnectivityGuard(ls_old, cfg, log=log)
                     if cfg.constraints.require_connected else None)
    if topology_guard is not None:
        initial_violation = topology_guard.violations(ls_old)
        if initial_violation:
            raise ValueError(f"Initial design violates require_connected: {initial_violation}")
    topology_candidate = LevelSet2D(ls_old.box, ls_old.dx)

    def project_geometry(trial):
        """Apply shared geometric projections and repair connectivity in place."""
        topology_candidate.phi = np.asarray(trial.phi, dtype=float).copy()
        apply_min_gap(topology_candidate, cfg)
        apply_min_feature(
            topology_candidate, cfg.constraints.min_feature_mm,
            force_air=force_air, force_metal=force_metal)
        if topology_guard is not None and not topology_guard.apply(topology_candidate, log=log):
            raise RuntimeError(
                "Connectivity repair failed for the updated geometry; refusing to "
                "run CST on a topology that violates require_connected."
            )
        trial.phi = topology_candidate.phi.copy()
        trial.reinit()
        if topology_guard is not None:
            design_mask[:] = build_velocity_mask(
                topology_candidate, cfg, taper_mm=1.0,
                frozen=topology_guard.frozen)

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
    log(f"[adjoint_levelset] pseudo_time_step={cfg.optimizer.pseudo_time_step:g}; "
        f"CFL={cfg.optimizer.cfl:g}; "
        f"extension band={cfg.level_set.extension_band_mm:g} mm")
    log("[adjoint_levelset] each valid update is committed with fixed pseudo_time_step; "
        "FoM changes are recorded without acceptance filtering")
    log("[adjoint_levelset] signed-distance reinitialization runs on every update")
    log("[adjoint_levelset] prefactor is (-2j*omega/Pin) times "
        f"scale={prefactor_scale:g}; validate it with --gradient-check")

    solver = None
    try:
        solver = CstSolver(
            cfg,
            setup=setup,
            model=model,
            auto_init=True,
            save_fields=args.save_fields,
            attach=args.attach or None,
            force_new=args.force_new,
            log=log,
        )
        problem = CstAdjointLevelSetProblem(
            cfg, setup, solver, sample_offset, prefactor_scale, output_dir, log=log)
        opt_cfg = als.OptimizerConfig(
            maximize=bool(maximize),
            pseudo_time_step=float(cfg.optimizer.pseudo_time_step),
            cfl=float(cfg.optimizer.cfl),
            smooth_sigma_cells=1.5,
            band_cells=float(cfg.level_set.extension_band_mm) / dx,
        )
        optimizer = als.AdjointLevelSetOptimizer(
            problem, design_mask, opt_cfg, force_metal=force_metal,
            force_air=force_air, anchor=anchor,
            geometry_projector=project_geometry)
        layout = model.layout_view()
        fixed_metal_polygons = model.fixed_metal_polygons()
        substrate = next(shape for shape in layout["shapes"] if shape[0] == "substrate")
        substrate_xy = np.asarray(substrate[1], dtype=float)
        field_box = BoxSpec(
            x=(float(substrate_xy[:, 0].min()), float(substrate_xy[:, 0].max())),
            y=(float(substrate_xy[:, 1].min()), float(substrate_xy[:, 1].max())),
        )
        sparams_history = []
        evaluation_history = []

        def save_evaluation(candidate, fwd, details):
            evaluation_history.append(_save_evaluation(
                problem, candidate, fwd, details, field_box))

        if args.gradient_check:
            check = als.gradient_check(
                problem, ls, design_mask=design_mask,
                evaluation_callback=save_evaluation)
            _write_json(output_dir / "gradient_check.json", check)
            for row in check:
                log(f"[gradient_check] delta={row['delta_cells']:g} cells, "
                    f"adjoint={row['slope_adjoint']:.8g}, FD={row['slope_fd']:.8g}, "
                    f"ratio={row['ratio']:.8g}")

        history_path = output_dir / "adjoint_levelset_history.jsonl"
        with history_path.open("w", encoding="utf-8") as history_file:
            def callback(it, ls_before, ls_after, rec):
                row = dict(rec)
                row["cst_eval_id"] = rec["eval_id_after"]
                history_file.write(json.dumps(row, ensure_ascii=False) + "\n")
                history_file.flush()
                np.savez_compressed(
                    output_dir / f"phi_before_iter_{it:03d}.npz",
                    phi=ls_before.phi,
                    x=ls_before.grid.x,
                    y=ls_before.grid.y,
                    cst_eval_id=np.asarray(rec["eval_id_before"], dtype=np.int64),
                )
                np.savez_compressed(
                    output_dir / f"phi_after_iter_{it:03d}.npz",
                    phi=ls_after.phi,
                    x=ls_after.grid.x,
                    y=ls_after.grid.y,
                    cst_eval_id=np.asarray(rec["eval_id_after"], dtype=np.int64),
                )
                log(f"[adjoint_levelset] iter={it:03d}: "
                    f"FoM {rec['f_before']:.8g} -> {rec['f_after']:.8g} "
                    f"(Δ={rec['delta_f']:+.4g}), "
                    f"pseudo_dt={rec['pseudo_time_step_used']:.4g}, "
                    f"move≈{rec['predicted_displacement']:.4g} mm, "
                    f"updated={rec['updated']}, "
                    f"reason={rec['reason']}")

            def save_state(state_it, state_ls, fwd):
                eval_id = int(fwd.extra["eval_id"])
                evaluation_dir = artifacts.iteration_dir(output_dir, eval_id)
                with np.load(evaluation_dir / "evaluation_state.npz") as data:
                    np.savez_compressed(
                        output_dir / f"adjoint_levelset_state_{state_it:03d}.npz",
                        iteration=np.asarray(state_it, dtype=np.int32),
                        **{key: data[key] for key in (
                            "cst_eval_id", "phi", "x", "y", "e_x", "e_y", "e_mag", "z_mm")})
                    plotting.adjoint_levelset_state_figure(
                        cfg, state_ls.phi, state_ls.grid.x, state_ls.grid.y,
                        data["e_x"], data["e_y"], data["e_mag"],
                        iteration=state_it, z_used=float(data["z_mm"]),
                        layout=layout, fixed_metal_polygons=fixed_metal_polygons,
                        path=output_dir / f"fig6b_iter_{state_it:03d}.png")
                evaluation = artifacts.load_json(evaluation_dir / "evaluation.json")
                sparams_history.append({
                    "iteration": state_it,
                    "cst_eval_id": eval_id,
                    "s_params_db": evaluation["s_params_db"],
                })
                _write_json(output_dir / "sparams_history.json", sparams_history)
                log(f"[adjoint_levelset] saved full-layout field/shape plot for state "
                    f"{state_it:03d}: {output_dir / f'fig6b_iter_{state_it:03d}.png'}")

            final_ls = optimizer.run(ls, n_iter=cfg.optimizer.max_iterations,
                                     callback=callback, state_callback=save_state,
                                     evaluation_callback=save_evaluation)

        iteration_records = [r for r in optimizer.history if r.get("updated")]

        np.savez_compressed(
            output_dir / "phi_final.npz",
            phi=final_ls.phi,
            x=final_ls.grid.x,
            y=final_ls.grid.y,
        )
        final_fom = (optimizer.history[-1]["f_after"] if optimizer.history
                     else float("nan"))
        final_contours = als.boundary_polygons(final_ls)
        if getattr(model, "SUPPORTS_NESTED_CONTOURS", False):
            final_regions = classify_metal_regions(final_contours)
            final_polygons = [region.outer for region in final_regions]
            final_polygons += [
                hole for region in final_regions for hole in region.holes]
            final_shape = {
                "unit": "mm",
                "n_polygons": len(final_polygons),
                "polygons": [polygon.tolist() for polygon in final_polygons],
                "regions": [
                    {"outer": region.outer.tolist(),
                     "holes": [hole.tolist() for hole in region.holes],
                     "depth": region.depth}
                    for region in final_regions
                ],
            }
        else:
            final_shape = [np.asarray(poly, dtype=float).tolist()
                           for poly in final_contours]
        _write_json(output_dir / "final_shape.json", final_shape)
        plotting.adjoint_levelset_sparams_figure(
            cfg, sparams_history, output_dir / "fig7_sparams_vs_iteration.png")
        plotting.adjoint_levelset_sparams_figure(
            cfg, evaluation_history, output_dir / "fig7_sparams_vs_evaluation.png",
            x_key="cst_eval_id")
        _write_json(output_dir / "adjoint_levelset_summary.json", {
            "case": cfg.name,
            "objective": (
                "maximize soft-min dB margin" if problem.frequencies else
                f"{'maximize' if maximize else 'minimize'} "
                f"|S{cfg.objective.to_port}{cfg.objective.from_port}|"),
            "final_fom": final_fom,
            "stop_reason": optimizer.stop_reason,
            "pseudo_time_step_initial": cfg.optimizer.pseudo_time_step,
            "iterations_requested": cfg.optimizer.max_iterations,
            "iterations_recorded": len(iteration_records),
            "cst_evaluations": problem.next_eval_id,
            "evaluations_recorded": len(evaluation_history),
            "sample_offset_mm": sample_offset,
            "requested_field_z_mm": cfg.sampling.field_z_mm,
            "prefactor_scale": prefactor_scale,
            "output_dir": str(output_dir),
            "state_plots": len(sparams_history),
            "sparams_plot": str(output_dir / "fig7_sparams_vs_iteration.png"),
        })
        log(f"[adjoint_levelset] complete: final FoM={final_fom:.8g}; outputs in {output_dir}")
    finally:
        if solver is not None:
            solver.close()


if __name__ == "__main__":
    main()
