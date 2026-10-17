#!/usr/bin/env python3
"""Replay and diagnose one Coupler level-set update from exported CST fields.

Run from the repository root, for example:

    python scripts/diagnose_first_iteration.py \\
        --config configs/coupler.yaml --results results/coupler

The script does not launch CST. It writes a JSON summary, sample-level CSV,
compressed arrays, and a PNG showing the field before/after velocity masking.
"""

from __future__ import annotations

import argparse
import copy
import csv
import json
from pathlib import Path

import numpy as np

from eaopt.adjoint.derivative import shape_derivative
from eaopt.adjoint.fields import FieldGrid, build_wls_stencil
from eaopt.adjoint.sampling import (
    boundary_points,
    boundary_samples,
    field_node_mask,
    scatter_inverse_distance,
)
from eaopt.config import CaseConfig
from eaopt.geometry.extension import extend_velocity, skfmm_available
from eaopt.optimize.constraints import (
    apply_min_gap,
    build_velocity_mask,
    interp_mask_at,
)
from eaopt.optimize.step import fixed_step_velocity
from eaopt.pipeline import make_level_set
from eaopt.solver.cst_setup import COUPLER


def _read_complex_sparams(path: Path) -> dict[tuple[int, int], complex]:
    rows = json.loads(path.read_text(encoding="utf-8"))
    return {(int(row[0]), int(row[1])): complex(float(row[2]), float(row[3]))
            for row in rows}


def _read_field_plane(path: Path, z_requested: float) -> tuple[FieldGrid, dict]:
    """Read a field plane; interpolate to exact z=0 when no exported plane exists."""
    values = np.loadtxt(path, skiprows=2, usecols=range(9))
    z_axis = np.unique(values[:, 2])
    if z_requested == 0.0:
        below = z_axis[z_axis <= 0.0]
        above = z_axis[z_axis >= 0.0]
        if not len(below) or not len(above):
            raise ValueError("field export must span z=0 to interpolate the boundary plane")
        z0 = float(below[-1])
        z1 = float(above[0])
        z_planes = [z0] if np.isclose(z0, z1, rtol=0.0, atol=1e-10) else [z0, z1]
    else:
        z0 = float(z_axis[np.argmin(np.abs(z_axis - z_requested))])
        z1 = z0
        z_planes = [z0]
    planes = [values[np.isclose(values[:, 2], z, atol=1e-9)] for z in z_planes]
    xs, ys = np.unique(planes[0][:, 0]), np.unique(planes[0][:, 1])

    def to_xy(plane):
        if not np.array_equal(np.unique(plane[:, 0]), xs) or not np.array_equal(
                np.unique(plane[:, 1]), ys):
            raise ValueError(f"{path}: z 切片的 x/y 网格不一致")
        ix = np.searchsorted(xs, plane[:, 0])
        iy = np.searchsorted(ys, plane[:, 1])
        components = plane[:, 3::2] + 1j * plane[:, 4::2]
        out = np.empty((len(xs), len(ys), 3), dtype=np.complex128)
        out[ix, iy, :] = components
        return out

    data_xy = to_xy(planes[0])
    if len(planes) == 2:
        data_hi = to_xy(planes[1])
        alpha = (z_requested - z0) / (z1 - z0)
        data_xy = data_xy * (1.0 - alpha) + data_hi * alpha
    data = data_xy[:, :, None, :]
    grid = FieldGrid(
        origin=(float(xs[0]), float(ys[0]), float(z_requested)),
        spacing=(float(xs[1] - xs[0]), float(ys[1] - ys[0]), 0.1),
        data=data,
    )
    meta = {
        "path": str(path),
        "shape_xy": [int(len(xs)), int(len(ys))],
        "spacing_mm": [float(xs[1] - xs[0]), float(ys[1] - ys[0])],
        "z_requested_mm": float(z_requested),
        "z_used_mm": float(z_requested),
        "z_bracket_mm": [z0, z1],
        "finite": bool(np.isfinite(data).all()),
    }
    del values, planes, data_xy
    return grid, meta


def _json_stats(a: np.ndarray) -> dict:
    a = np.asarray(a)
    finite = np.isfinite(a)
    vals = a[finite]
    if vals.size == 0:
        return {"finite_fraction": 0.0, "min": None, "max": None}
    return {
        "finite_fraction": float(finite.mean()),
        "min": float(vals.min()),
        "max": float(vals.max()),
        "mean": float(vals.mean()),
        "rms": float(np.sqrt(np.mean(vals * vals))),
        "nonzero_count": int(np.count_nonzero(np.abs(vals) > 1e-12)),
        "count": int(vals.size),
    }


def _offset_replay(cfg, ls, fields, contours, offset_mm: float):
    """Recompute boundary derivative and velocity assembly for one xy offset."""
    cfg.sampling.intersection_offset_mm = float(offset_mm)
    pts, normals = boundary_samples(contours, ls, cfg)
    ef, hf, eb, hb = fields
    z_req = cfg.sampling.field_z_mm
    z_side = "above" if z_req == 0.0 else "nearest"
    iz, z_used = ef.nearest_z_plane(z_req, side=z_side)
    node_mask = field_node_mask(ls, ef, cfg)
    stencil = build_wls_stencil(
        pts, ef.axes()[:2], node_mask,
        radius_cells=cfg.sampling.wls_radius_cells,
        order=cfg.sampling.wls_order,
    )
    sampled = [f.sample_wls(pts, iz, stencil=stencil).values for f in fields]
    dp = shape_derivative(
        *sampled, COUPLER.port_power_w,
        2 * np.pi * COUPLER.frequency_ghz * 1e9,
        COUPLER.eps_r,
        normals,
    )

    # boundary_samples has already offset pts toward sample_side. Reverse that
    # offset when scattering the velocity back to the actual phi=0 contour.
    side = 1.0 if cfg.sampling.sample_side == "outside" else -1.0
    boundary_xy = pts - side * offset_mm * normals
    mask_actual = build_velocity_mask(ls, cfg, taper_mm=1.0)
    active = interp_mask_at(ls, boundary_xy, mask_actual) > 0.5
    boundary_velocity = fixed_step_velocity(
        dp, cfg.optimizer.velocity_sign, active)
    seed, counts = scatter_inverse_distance(
        ls, boundary_xy, boundary_velocity,
        max_dist_cells=cfg.sampling.scatter_max_cells,
    )
    extended, method = extend_velocity(
        ls, seed, cfg.level_set.extension_band_mm,
        method=cfg.level_set.extension_method, known=counts > 0,
    )
    used = extended * mask_actual

    return {
        "offset_mm": float(offset_mm), "pts": pts, "normals": normals,
        "boundary_xy": boundary_xy, "dp": dp, "active": active,
        "boundary_velocity": boundary_velocity, "seed": seed,
        "counts": counts, "extended": extended, "used": used,
        "mask_actual": mask_actual, "method": method,
        "stencil": stencil, "z_used": z_used,
    }


def _update_once(cfg, phi0, velocity):
    ls = make_level_set(cfg)
    ls.phi = np.array(phi0, copy=True)
    steps = max(1, round(cfg.optimizer.step_cells / cfg.optimizer.cfl))
    for _ in range(steps):
        ls.update(velocity, steps=1, cfl=cfg.optimizer.cfl)
        apply_min_gap(ls, cfg)
    reinitialized = (0 % cfg.level_set.reinit_every == 0)
    if reinitialized:
        ls.reinitialize(band=2 * cfg.level_set.extension_band_mm, iters=40)
    return ls, steps, reinitialized


def _write_plot(path, cfg, ls0, ls1, result):
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(13, 9), constrained_layout=True)
    box = cfg.design_region.box
    extent = [ls0.xs[0] - ls0.dx / 2, ls0.xs[-1] + ls0.dx / 2,
              ls0.ys[0] - ls0.dx / 2, ls0.ys[-1] + ls0.dx / 2]

    ax = axes[0, 0]
    ax.contourf(ls0.xs, ls0.ys, ls0.phi.T, levels=[-1e6, 0],
                colors=["#333333"], alpha=0.65)
    ax.contour(ls1.xs, ls1.ys, ls1.phi.T, levels=[0], colors=["#e4572e"], lw=1.4)
    ax.contour(ls0.xs, ls0.ys, ls0.phi.T, levels=[0], colors=["#2673b8"],
               linestyles="--", lw=1.0)
    ax.set_title("Initial (blue dashed) / one update (orange)")
    ax.set_xlabel("x (mm)"); ax.set_ylabel("y (mm)")

    for ax, z, title in [
        (axes[0, 1], result["extended"], "Extended velocity before mask"),
        (axes[1, 0], result["used"], "Velocity used by level-set update"),
    ]:
        im = ax.imshow(z.T, origin="lower", extent=extent, cmap="RdBu_r",
                       vmin=-1, vmax=1, interpolation="nearest", aspect="equal")
        ax.contour(ls0.xs, ls0.ys, ls0.phi.T, levels=[0], colors="black", lw=.65)
        ax.set_title(title); ax.set_xlabel("x (mm)"); ax.set_ylabel("y (mm)")
        fig.colorbar(im, ax=ax, fraction=.045, pad=.03, label="V")

    ax = axes[1, 1]
    im = ax.imshow(result["mask_actual"].T, origin="lower", extent=extent,
                   cmap="viridis", vmin=0, vmax=1, interpolation="nearest", aspect="equal")
    ax.contour(ls0.xs, ls0.ys, ls0.phi.T, levels=[0], colors="white", lw=.65)
    ax.set_title("Actual velocity mask (1 mm taper)")
    ax.set_xlabel("x (mm)"); ax.set_ylabel("y (mm)")
    fig.colorbar(im, ax=ax, fraction=.045, pad=.03, label="mask")

    for ax in axes.ravel():
        ax.set_xlim(box.x[0] - .3, box.x[1] + .3)
        ax.set_ylim(box.y[0] - .3, box.y[1] + .3)
        ax.set_aspect("equal")
    fig.suptitle(
        f"First-step replay | z={result['z_used']:.4g} mm | "
        f"extension={result['method']} | samples={len(result['dp'])}")
    fig.savefig(path, dpi=170, bbox_inches="tight")
    plt.close(fig)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", type=Path, default=Path("configs/coupler.yaml"))
    parser.add_argument("--results", type=Path, default=Path("results/coupler"),
                        help="Directory containing four field files and S parameter JSONs")
    parser.add_argument("--out", type=Path, default=None,
                        help="Output directory (default: <results>/first_iteration_diagnostic)")
    parser.add_argument("--offsets-mm", default="0,0.1,0.2,0.3",
                        help="Comma-separated xy sampling offsets for sensitivity comparison")
    args = parser.parse_args()

    cfg = CaseConfig.from_yaml(args.config)
    data_dir = args.results
    outdir = args.out or (data_dir / "first_iteration_diagnostic")
    outdir.mkdir(parents=True, exist_ok=True)

    expected = [data_dir / f"{d}_{f}field.txt"
                for d, f in (("fwd", "E"), ("fwd", "H"),
                             ("bwd", "E"), ("bwd", "H"))]
    for path in expected + [data_dir / "s_params_fwd.json",
                            data_dir / "s_params_bwd.json"]:
        if not path.is_file():
            raise FileNotFoundError(path)

    ls0 = make_level_set(cfg)
    contours = boundary_points(ls0, cfg)
    params_f = _read_complex_sparams(data_dir / "s_params_fwd.json")
    params_b = _read_complex_sparams(data_dir / "s_params_bwd.json")
    key = (cfg.objective.to_port, cfg.objective.from_port)
    reciprocal_key = (cfg.objective.from_port, cfg.objective.to_port)
    if key not in params_f:
        raise KeyError(f"Forward S parameter {key} missing in s_params_fwd.json")
    s31 = params_f[key]
    s13 = params_b.get(reciprocal_key)

    fields, field_meta = [], []
    for path in expected:
        field, meta = _read_field_plane(path, cfg.sampling.field_z_mm)
        fields.append(field)
        field_meta.append(meta)
    ref_axes = fields[0].axes()
    grid_alignment = {}
    for name, field in zip(("fwd_H", "bwd_E", "bwd_H"), fields[1:]):
        same = all(a.shape == b.shape and np.allclose(a, b, atol=1e-9)
                   for a, b in zip(ref_axes[:2], field.axes()[:2]))
        grid_alignment[name] = bool(same)
        if not same:
            raise ValueError(f"Field grid mismatch for {name}; cannot replay shared WLS stencil")

    offsets = [float(x.strip()) for x in args.offsets_mm.split(",") if x.strip()]
    if not offsets:
        raise ValueError("--offsets-mm must contain at least one offset")
    replay_cfg = copy.deepcopy(cfg)
    replay = [_offset_replay(replay_cfg, ls0, fields, contours, d) for d in offsets]
    current_offset = float(cfg.sampling.intersection_offset_mm)
    current_i = int(np.argmin([abs(r["offset_mm"] - current_offset) for r in replay]))
    cur = replay[current_i]

    # Compare offset sweeps to the configured-offset delta-p using signs and
    # correlation. These are stability diagnostics, not CST finite differences.
    offset_summary = []
    ref_dp = cur["dp"]
    ref_norm = np.linalg.norm(ref_dp)
    for r in replay:
        d = r["dp"]
        cosine = float(np.dot(ref_dp, d) / (ref_norm * np.linalg.norm(d))) \
            if ref_norm and np.linalg.norm(d) else None
        offset_summary.append({
            "offset_mm": r["offset_mm"],
            "dp_stats": _json_stats(d),
            "dp_sign_disagreement_fraction_vs_configured": float(np.mean(np.sign(d) != np.sign(ref_dp))),
            "dp_cosine_similarity_vs_configured": cosine,
            "wls_fallback_count": int(r["stencil"].n_fallback),
            "neighbor_count_min_median_max": [
                int(np.min(r["stencil"].n_neighbors)),
                float(np.median(r["stencil"].n_neighbors)),
                int(np.max(r["stencil"].n_neighbors)),
            ],
        })

    # Quantify edge taper on the actual interface and show alternative masks
    # without changing the production replay.
    mask_short = build_velocity_mask(ls0, cfg, taper_mm=0.2)
    no_taper_cfg = copy.deepcopy(cfg)
    no_taper_cfg.constraints.taper_edges = "none"
    mask_none = build_velocity_mask(ls0, no_taper_cfg)
    x = cur["boundary_xy"][:, 0]
    endpoint = (x < cfg.design_region.box.x[0] + 0.5) | \
               (x > cfg.design_region.box.x[1] - 0.5)
    center = (x >= cfg.design_region.box.x[0] + 2.0) & \
             (x <= cfg.design_region.box.x[1] - 2.0)
    endpoint_mask = interp_mask_at(ls0, cur["boundary_xy"], cur["mask_actual"])
    center_mask = endpoint_mask[center]
    edge_columns = (ls0.xs < cfg.design_region.box.x[0] + 0.5) | \
                   (ls0.xs > cfg.design_region.box.x[1] - 0.5)
    endpoint_extended = cur["extended"][edge_columns, :]
    endpoint_used = cur["used"][edge_columns, :]
    taper_summary = {
        "configured_taper_edges": cfg.constraints.taper_edges,
        "current_taper_width_mm_in_build_velocity_mask": 1.0,
        "endpoint_sample_count_first_last_0p5mm": int(endpoint.sum()),
        "endpoint_mask_mean_first_last_0p5mm": float(endpoint_mask[endpoint].mean()) if endpoint.any() else None,
        "center_mask_mean_x_2_to_10mm": float(center_mask.mean()) if center_mask.size else None,
        "mask_nonzero_nodes_actual_1mm": int(np.count_nonzero(cur["mask_actual"] > 1e-12)),
        "mask_nonzero_nodes_0p2mm_taper": int(np.count_nonzero(mask_short > 1e-12)),
        "mask_nonzero_nodes_no_taper": int(np.count_nonzero(mask_none > 1e-12)),
        "endpoint_extended_velocity_rms": float(np.sqrt(np.mean(endpoint_extended ** 2))) if endpoint.any() else None,
        "endpoint_used_velocity_rms": float(np.sqrt(np.mean(endpoint_used ** 2))) if endpoint.any() else None,
        "endpoint_used_velocity_nonzero_fraction": float(np.mean(np.abs(endpoint_used) > 1e-12)) if endpoint.any() else None,
    }

    ls1, steps, reinitialized = _update_once(cfg, ls0.phi, cur["used"])
    shape_summary = {
        "hj_substeps": int(steps),
        "reinitialized_after_first_update": bool(reinitialized),
        "phi_changed_nodes": int(np.count_nonzero(np.abs(ls1.phi - ls0.phi) > 1e-12)),
        "metal_nodes_before": int(np.count_nonzero(ls0.phi <= 0)),
        "metal_nodes_after": int(np.count_nonzero(ls1.phi <= 0)),
        "metal_area_proxy_before_mm2": float(np.count_nonzero(ls0.phi <= 0) * ls0.dx**2),
        "metal_area_proxy_after_mm2": float(np.count_nonzero(ls1.phi <= 0) * ls1.dx**2),
        "phi_delta_stats_mm": _json_stats(ls1.phi - ls0.phi),
    }

    reciprocity = None
    if s13 is not None:
        reciprocity = {
            "s13_bwd": [float(s13.real), float(s13.imag)],
            "relative_complex_error": float(abs(s31 - s13) / max(abs(s31), abs(s13), 1e-30)),
        }

    report = {
        "case": cfg.name,
        "source_files": {"results_dir": str(data_dir.resolve()),
                         "config": str(args.config.resolve())},
        "objective": {
            "s_parameter": f"S{key[0]}{key[1]}",
            "s_fwd": [float(s31.real), float(s31.imag)],
            "magnitude": float(abs(s31)),
            "magnitude_db": float(20 * np.log10(abs(s31))),
            "phase_deg": float(np.angle(s31, deg=True)),
            "fwd_bwd_reciprocity": reciprocity,
        },
        "field_files": field_meta,
        "xy_grid_aligned": grid_alignment,
        "sampling": {
            "requested_z_mm": float(cfg.sampling.field_z_mm),
            "used_z_mm": float(cur["z_used"]),
            "side": cfg.sampling.sample_side,
            "configured_offset_mm": current_offset,
            "configured_offset_replayed_mm": cur["offset_mm"],
            "boundary_samples": int(len(cur["dp"])),
            "active_boundary_samples": int(cur["active"].sum()),
            "wls_fallback_count": int(cur["stencil"].n_fallback),
            "wls_neighbors_min_median_max": [
                int(np.min(cur["stencil"].n_neighbors)),
                float(np.median(cur["stencil"].n_neighbors)),
                int(np.max(cur["stencil"].n_neighbors)),
            ],
            "sampled_delta_p": _json_stats(cur["dp"]),
        },
        "velocity": {
            "extension_method_used": cur["method"],
            "scikit_fmm_available": bool(skfmm_available()),
            "seed_nodes": int(np.count_nonzero(cur["counts"])),
            "seed_field": _json_stats(cur["seed"]),
            "extended_before_mask": _json_stats(cur["extended"]),
            "used_after_mask": _json_stats(cur["used"]),
            "active_velocity_sign": float(cfg.optimizer.velocity_sign),
            "sign_warning": "velocity_sign still requires a CST finite-difference FoM check; field replay cannot decide it.",
        },
        "endpoint_taper_diagnostic": taper_summary,
        "sampling_offset_sweep": offset_summary,
        "first_update": shape_summary,
        "cst_finite_difference": {
            "performed": False,
            "required": "Run forward CST solves for small outward and inward perturbations of the same boundary segment, then compare delta |S31|.",
        },
    }

    report_path = outdir / "diagnostic_report.json"
    report_path.write_text(json.dumps(report, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    csv_path = outdir / "boundary_samples.csv"
    with csv_path.open("w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(["x_mm", "y_mm", "nx", "ny", "delta_p_1_per_m",
                         "active", "boundary_velocity", "mask_at_boundary"])
        for i in range(len(cur["dp"])):
            writer.writerow([cur["boundary_xy"][i, 0], cur["boundary_xy"][i, 1],
                             cur["normals"][i, 0], cur["normals"][i, 1], cur["dp"][i],
                             int(cur["active"][i]), cur["boundary_velocity"][i],
                             endpoint_mask[i]])
    np.savez_compressed(
        outdir / "diagnostic_arrays.npz",
        xs=ls0.xs, ys=ls0.ys, phi_initial=ls0.phi, phi_after_first_update=ls1.phi,
        velocity_seed=cur["seed"], velocity_extended=cur["extended"],
        velocity_used=cur["used"], mask_actual=cur["mask_actual"],
        mask_taper_0p2mm=mask_short, mask_no_taper=mask_none,
    )
    _write_plot(outdir / "diagnostic_panels.png", cfg, ls0, ls1, cur)

    print(f"Diagnostic complete: {outdir.resolve()}")
    print(f"  |S{key[0]}{key[1]}| = {abs(s31):.6g} ({20*np.log10(abs(s31)):.3f} dB), phase={np.angle(s31,deg=True):+.2f} deg")
    print(f"  samples={len(cur['dp'])}, active={int(cur['active'].sum())}, WLS fallbacks={cur['stencil'].n_fallback}")
    print(f"  z={cur['z_used']:.6g} mm, extension={cur['method']}, |V_used| range=[{cur['used'].min():.4g}, {cur['used'].max():.4g}]")
    print(f"  endpoint used-V RMS={taper_summary['endpoint_used_velocity_rms']}, endpoint mask mean={taper_summary['endpoint_mask_mean_first_last_0p5mm']}")
    print(f"  offset sweep sign disagreement: " + ", ".join(
        f"{r['offset_mm']:g}mm={r['dp_sign_disagreement_fraction_vs_configured']:.1%}"
        for r in offset_summary))
    print(f"  first update: changed phi nodes={shape_summary['phi_changed_nodes']}, metal nodes {shape_summary['metal_nodes_before']} -> {shape_summary['metal_nodes_after']}")
    print("  CST finite-difference sign check: NOT performed (must use perturbed forward solves).")
    print(f"  report: {report_path.resolve()}")
    print(f"  plot:   {(outdir / 'diagnostic_panels.png').resolve()}")
    print(f"  samples:{csv_path.resolve()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
