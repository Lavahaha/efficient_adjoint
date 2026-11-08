"""Audit cached initial endpoint fields and velocity without opening CST."""
from pathlib import Path
import csv
import json
import sys

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
import numpy as np
from scipy.spatial import cKDTree
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as a
from eaopt.config import CaseConfig
from eaopt.geometry.design import compose_metal_regions, connection_boundary_mask
from eaopt.geometry.levelset import make_level_set
from eaopt.runner import _movement_constraints
from eaopt.solver.case import load_case


def main():
    source = ROOT / "results/fd_single_2026-11-02/coupler_planar_smoke"
    out = ROOT / "results/coupler_endpoint_fields_2026-11-02"
    out.mkdir(exist_ok=True)
    cfg = CaseConfig.from_dict(json.loads(
        (source / "run_config.json").read_text(encoding="utf-8"))["config"])
    setup, model = load_case(cfg)
    old = make_level_set(cfg)
    grid = a.Grid2D(old.nx, old.ny, old.dx, old.xs[0], old.ys[0])
    ls = a.LevelSet(grid, old.phi)
    mask, *_ = _movement_constraints(old, cfg)
    pinned = connection_boundary_mask(old, cfg, model.fixed_metal_polygons())
    mask[pinned] = 0
    b = a.extract_boundary(ls, mask)
    with np.load(source / "iter_000/boundary_fields.npz") as saved:
        d = {key: saved[key] for key in saved.files}
    ix = d["boundary_indices"]
    assert np.array_equal(b.pts, d["boundary"])
    assert np.array_equal(b.active, d["active"])
    assert np.array_equal(ix, np.flatnonzero(b.active))
    assert np.allclose(d["points_mm"][:, :2], b.pts[ix] + float(d["offset_mm"]) * b.normals[ix])
    assert np.all(d["points_mm"][:, 2] == float(d["z_mm"]))
    metal = unary_union([Polygon(r.outer, r.holes) for r in compose_metal_regions(cfg, old.phi)] +
                        [Polygon(p) for p in model.fixed_metal_polygons()])
    assert not any(metal.covers(Point(p[:2])) for p in d["points_mm"])
    selected = a.Boundary(b.pts[ix], b.normals[ix], b.weights[ix], b.active[ix])
    pref = -2j * 2 * np.pi * setup.frequency_ghz * 1e9 / setup.port_power_w
    ef, hf, eb, hb = [d[k] for k in ("E_fwd", "H_fwd", "E_bwd", "H_bwd")]
    ge = np.real(a.shape_derivative_from_fields(selected, ef, np.zeros_like(hf), eb,
                 np.zeros_like(hb), pref, float(d["eps_f_per_m"]), a.MU0))
    gh = np.real(a.shape_derivative_from_fields(selected, np.zeros_like(ef), hf,
                 np.zeros_like(eb), hb, pref, float(d["eps_f_per_m"]), a.MU0))
    g = np.zeros(len(b.pts))
    g[ix] = ge + gh
    assert np.allclose(g[ix], np.real(a.shape_derivative_from_fields(
        selected, ef, hf, eb, hb, pref, float(d["eps_f_per_m"]), a.MU0)))
    velocity = a.boundary_to_grid_velocity(ls, b, g, mask, 10)
    unsmoothed = a.boundary_to_grid_velocity(ls, b, g, mask, 10, smooth_sigma_cells=0)
    rows = []
    for j, i in enumerate(ix):
        rows.append(dict(index=int(i), x_mm=float(b.pts[i, 0]), y_mm=float(b.pts[i, 1]),
            nx=float(b.normals[i, 0]), ny=float(b.normals[i, 1]),
            sample_x_mm=float(d["points_mm"][j, 0]), sample_y_mm=float(d["points_mm"][j, 1]),
            sample_z_mm=float(d["points_mm"][j, 2]),
            boundary_distance_to_full_cad_mm=metal.boundary.distance(Point(b.pts[i])),
            abs_Ex_fwd=float(abs(ef[0, j])), abs_Ey_fwd=float(abs(ef[1, j])),
            abs_Ez_fwd=float(abs(ef[2, j])),
            g_electric=float(ge[j]), g_magnetic=float(gh[j]), g_total=float(g[i])))
    with (out / "active_boundary_fields.csv").open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(f, fieldnames=list(rows[0]))
        writer.writeheader()
        writer.writerows(rows)
    middle = int(round((-.8 - grid.y0) / grid.dx))
    seam_rows = []
    for x in (0., .1, .2, .4, 11.6, 11.8, 11.9, 12.):
        i = int(round(x / grid.dx))
        point = Point(x, grid.y[middle])
        seam_rows.append(dict(x_mm=x, y_mm=float(grid.y[middle]), phi_mm=float(ls.phi[i, middle]),
            full_cad_signed_distance_mm=-metal.boundary.distance(point),
            mask=float(mask[i, middle]), velocity=float(velocity[i, middle]),
            unsmoothed_velocity=float(unsmoothed[i, middle])))
    # Verify native export parsing/order independently of the production importer.
    native = []
    ref = ROOT / "results/fd_single_2026-11-02/coupler_td_fd_field_reference"
    for tag in ("td_fwd", "td_bwd", "fd_fwd"):
        with np.load(ref / tag / "boundary.npz") as saved:
            assert np.array_equal(saved["points_mm"], d["points_mm"])
            for kind, key in (("Efield", "E"), ("Hfield", "H")):
                table = np.atleast_2d(np.loadtxt(ref / tag / f"boundary_{kind}.txt", skiprows=2))
                dist, order = cKDTree(saved["points_mm"]).query(table[:, :3])
                assert len(np.unique(order)) == len(order) == len(ix)
                values = table[:, 3::2] + 1j * table[:, 4::2]
                error = float(np.max(abs(values - saved[key][order])))
                assert dist.max() < 1e-4 and error == 0
                native.append(dict(tag=tag, field=key, max_coordinate_error_mm=float(dist.max()),
                                   max_ascii_cache_field_error=error))
    with np.load(ref / "td_fwd/boundary.npz") as td_f, np.load(ref / "td_bwd/boundary.npz") as td_b:
        td_ef, td_hf, td_eb, td_hb = td_f["E"].T, td_f["H"].T, td_b["E"].T, td_b["H"].T
    td_ge = np.real(a.shape_derivative_from_fields(selected, td_ef, np.zeros_like(td_hf),
        td_eb, np.zeros_like(td_hb), pref, float(d["eps_f_per_m"]), a.MU0))
    td_gh = np.real(a.shape_derivative_from_fields(selected, np.zeros_like(td_ef), td_hf,
        np.zeros_like(td_eb), td_hb, pref, float(d["eps_f_per_m"]), a.MU0))
    td_g = np.zeros(len(b.pts))
    td_g[ix] = td_ge + td_gh
    td_v = a.boundary_to_grid_velocity(ls, b, td_g, mask, 10)
    td_end = []
    for j, i in enumerate(ix):
        if i in (3, 121, 123, 128, 246):
            td_end.append(dict(index=int(i), boundary_mm=b.pts[i].tolist(),
                abs_E_fwd=np.abs(td_ef[:, j]).tolist(), g_electric=float(td_ge[j]),
                g_magnetic=float(td_gh[j]), g_total=float(td_g[i])))
    for row in seam_rows:
        i = int(round(row["x_mm"] / grid.dx))
        row["td_reference_velocity"] = float(td_v[i, middle])
    edge = np.isclose(b.pts[:, 0], 0) | np.isclose(b.pts[:, 0], 12)
    report = dict(source=str(source.relative_to(ROOT)), new_cst_solves=0,
        boundary_count=len(b.pts), active_count=len(ix), pinned_nodes=int(pinned.sum()),
        extracted_end_points=b.pts[edge].tolist(), extracted_end_active=b.active[edge].tolist(),
        max_active_boundary_cad_distance_mm=max(r["boundary_distance_to_full_cad_mm"] for r in rows),
        native_export_checks=native, seam_interior_rows=seam_rows,
        td_reference_endpoint_fields=td_end,
        endpoint_active_fields=[r for r in rows if r["x_mm"] <= .4 or r["x_mm"] >= 11.6])
    (out / "audit.json").write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    fig, axes = plt.subplots(1, 2, figsize=(7, 6), constrained_layout=True)
    xx, yy = grid.mesh()
    for ax, limits, title in zip(axes, ((-.25, .65), (11.35, 12.25)), ("Left connection", "Right connection")):
        mesh = ax.pcolormesh(xx, yy, velocity * 1e-8, shading="auto", cmap="coolwarm", vmin=-2, vmax=2)
        polys = [metal] if metal.geom_type == "Polygon" else metal.geoms
        for polygon in polys:
            xy = np.asarray(polygon.exterior.coords)
            ax.plot(xy[:, 0], xy[:, 1], "k-", lw=1.5)
        show = (b.pts[:, 0] >= limits[0]) & (b.pts[:, 0] <= limits[1])
        ax.scatter(b.pts[show & ~b.active, 0], b.pts[show & ~b.active, 1], c="limegreen", marker="x", label="inactive contour")
        j = np.flatnonzero((selected.pts[:, 0] >= limits[0]) & (selected.pts[:, 0] <= limits[1]))
        ax.scatter(d["points_mm"][j, 0], d["points_mm"][j, 1], s=12, c="gold", edgecolors="k", linewidths=.3, label="CST air samples")
        ax.quiver(selected.pts[j, 0], selected.pts[j, 1], selected.normals[j, 0], selected.normals[j, 1],
                  angles="xy", scale_units="xy", scale=10, width=.005)
        ax.axvline(0 if title.startswith("Left") else 12, color="gray", ls="--", lw=1)
        ax.set(xlim=limits, ylim=(-2.3, .25), aspect="equal", title=title, xlabel="x / mm", ylabel="y / mm")
    axes[0].legend(loc="center", fontsize=8)
    fig.colorbar(mesh, ax=axes, label="Extended velocity x dt / mm (dt = 1e-8)")
    fig.savefig(out / "endpoints.png", dpi=180)
    plt.close(fig)
    print(json.dumps({k: v for k, v in report.items() if k not in ("endpoint_active_fields", "extracted_end_points")}, indent=2))


if __name__ == "__main__":
    main()
