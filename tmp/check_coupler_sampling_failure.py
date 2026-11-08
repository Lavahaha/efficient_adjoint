"""Replay current point-sampling checks against saved CAD; never starts CST."""
from pathlib import Path
import argparse
import json
import sys

import numpy as np
from shapely.geometry import Point, Polygon
from shapely.ops import unary_union

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
import adjoint_levelset as als
from eaopt.config import CaseConfig
from eaopt.geometry.contour import classify_metal_regions
from eaopt.geometry.design import connection_boundary_mask, design_box, polygon_union
from eaopt.geometry.levelset import make_level_set
from eaopt.runner import _movement_constraints, boundary_sample_points
from eaopt.solver.case import load_case


def contact_sample_mask(boundary, cfg, fixed):
    domain = design_box(cfg)
    frozen = polygon_union([p.vertices for p in cfg.initial_metal]).difference(domain).union(fixed)
    contact = domain.boundary.intersection(frozen)
    return np.array([not contact.is_empty and contact.distance(Point(p)) <= 1e-9
                     for p in boundary.pts], dtype=bool)


def check_run(root, offset, z):
    config = json.loads((root / "run_config.json").read_text(encoding="utf-8"))
    cfg = CaseConfig.from_dict(config["config"])
    setup, model = load_case(cfg)
    fixed = polygon_union(model.fixed_metal_polygons())
    substrate = unary_union([Polygon(p) for kind, p, _ in model.layout_view()["shapes"]
                             if kind == "substrate"])
    old = make_level_set(cfg)
    mask, *_ = _movement_constraints(old, cfg)
    mask[connection_boundary_mask(old, cfg, model.fixed_metal_polygons())] = 0
    grid = als.Grid2D(old.nx, old.ny, old.dx, old.xs[0], old.ys[0])
    rows = []
    for folder in sorted(root.glob("iter_[0-9][0-9][0-9]")):
        if not (folder / "shape.json").exists() or not (folder / "levelset.npz").exists():
            continue
        invalid_file = folder / "invalid_boundary_samples.npz"
        if invalid_file.exists():
            with np.load(invalid_file) as saved:
                boundary = als.Boundary(saved["boundary"], saved["normals"],
                    np.ones(len(saved["boundary"])), saved["active"].copy())
            origin = "saved_failure_boundary"
        else:
            with np.load(folder / "levelset.npz") as saved:
                boundary = als.extract_boundary(als.LevelSet(grid, saved["phi"]), mask)
            origin = "reconstructed_with_current_extraction_and_mask"
        shapes = json.loads((folder / "shape.json").read_text(encoding="utf-8"))["polygons"]
        regions = classify_metal_regions([np.asarray(p) for p in shapes])
        metal = unary_union([Polygon(r.outer, r.holes) for r in regions] + [fixed])
        excluded = boundary.active & contact_sample_mask(boundary, cfg, fixed)
        bad = []
        for i in np.flatnonzero(boundary.active):
            p = Point(boundary.pts[i])
            q = Point(boundary.pts[i] + offset * boundary.normals[i])
            outside = not substrate.contains(q)
            pec = 0 < z <= setup.metal_thickness_mm and metal.covers(q)
            if outside or pec:
                bad.append(dict(index=int(i), boundary_mm=boundary.pts[i].tolist(),
                    normal=boundary.normals[i].tolist(), sample_xy_mm=list(q.coords)[0],
                    outside_substrate=outside, in_pec=pec,
                    physical_boundary_distance_mm=metal.boundary.distance(p),
                    on_fixed_contact=bool(excluded[i]),
                    smaller_offset_in_pec=[bool(metal.covers(Point(
                        boundary.pts[i] + offset / divisor * boundary.normals[i])))
                        for divisor in (1, 2, 4, 8, 16)]))
        candidate = als.Boundary(boundary.pts, boundary.normals, boundary.weights,
                                 boundary.active & ~excluded)
        error = None
        if candidate.active.any():
            try:
                boundary_sample_points(candidate, offset, z, setup, metal, substrate, model.AIR_H)
            except ValueError as exc:
                error = str(exc)
        rows.append(dict(folder=str(folder.relative_to(ROOT)), boundary_origin=origin,
            active_before=int(boundary.active.sum()), excluded_contact_indices=np.flatnonzero(excluded).tolist(),
            invalid_before=bad, invalid_after=error))
    # A real interior collision away from the fixed contact must still fail.
    point = np.asarray(metal.representative_point().coords[0])
    fake = als.Boundary(point[None, :], np.array([[1., 0.]]), np.ones(1), np.ones(1, bool))
    assert not contact_sample_mask(fake, cfg, fixed).any()
    tiny_offset = min(.001, metal.boundary.distance(Point(point)) / 4)
    try:
        boundary_sample_points(fake, tiny_offset, .0175, setup, metal, substrate, model.AIR_H)
    except ValueError:
        pass
    else:
        raise AssertionError("Non-contact PEC collision was silently accepted")
    outside = np.array([[substrate.bounds[2] + 1., substrate.centroid.y]])
    fake = als.Boundary(outside, np.array([[1., 0.]]), np.ones(1), np.ones(1, bool))
    assert not contact_sample_mask(fake, cfg, fixed).any()
    try:
        boundary_sample_points(fake, .05, .0175, setup, metal, substrate, model.AIR_H)
    except ValueError:
        pass
    else:
        raise AssertionError("Substrate overflow was silently accepted")
    return rows


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("run_dirs", nargs="*", type=Path)
    args = parser.parse_args()
    roots = args.run_dirs or [ROOT / p for p in (
        "results/coupler_local_serial_smoke", "results/coupler_source_factor_ab_2026-11-02/unit",
        "results/coupler_source_factor_ab_2026-11-02/objective",
        "results/fd_single_2026-11-02/coupler_smoke",
        "results/fd_single_2026-11-02/coupler_planar_smoke")]
    rows = [row for root in roots for row in check_run(root.resolve(), .05, .0175)]
    assert rows
    report = dict(offset_mm=.05, z_mm=.0175, geometry_count=len(rows),
        note="Historical CAD replayed under the failing command's current sampling policy; not new EM solves",
        bad_points_before=sum(len(row["invalid_before"]) for row in rows),
        geometries_invalid_after=sum(row["invalid_after"] is not None for row in rows), rows=rows)
    if not args.run_dirs:
        assert report["geometries_invalid_after"] == 0
        assert all(not row["excluded_contact_indices"] for row in rows if row["folder"].endswith("iter_000"))
    out = ROOT / "results/coupler_sampling_failure_2026-11-03.json"
    out.write_text(json.dumps(report, indent=2) + "\n", encoding="utf-8")
    print(json.dumps(report, indent=2))


if __name__ == "__main__":
    main()
